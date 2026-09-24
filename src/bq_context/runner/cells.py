"""Running one approach against one question, in isolation.

Each cell gets a fresh ADK session on a long-lived per-approach
``InMemoryRunner``. Following upstream, cells deliberately do **not** go through
the orchestrator's ``ParallelAgent``: isolation removes the parallel-contention
artifact that makes per-approach latency meaningless, and lets token usage be
attributed to exactly one approach.

Results are read out of ADK **session state**, not the text stream. Every
approach writes the same two keys regardless of how it works internally, and
that convention is the entire integration surface:

    state[f"nominated_tables_{method}"]  -> list[str]   candidates, pre-rank
    state[f"reranker_result_{method}"]   -> str         RerankerResponse JSON

The split between those two keys is what makes "discovery recall vs final
recall" measurable — whether the candidate set contained the right table, versus
whether the reranker kept it.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from google.adk.runners import InMemoryRunner
from google.genai import types

from bq_context.context_cache import TableCache
from bq_context.runner.backoff import retry_async
from bq_context.runner.models import Cell, cell_key
from bq_context.runner.shard import ShardRunner
from bq_context.runtime import TierContext, get_datasets, get_scoped_tables, tier_scope
from bq_context.schemas import RerankerResponse
from bq_context.usage import usage_scope

if TYPE_CHECKING:
    from collections.abc import Mapping

    from bq_context.config import ExperimentConfig
    from bq_context.runner.models import ShardResult, ShardSpec
    from bq_context.runner.store import ArtifactStore

logger = logging.getLogger(__name__)

__all__ = ["APPROACHES", "AdkCellExecutor", "execute_shard", "needs_cache"]

USER_ID = "benchmark"

#: Approach key -> module exposing ``root_agent``. Order matches upstream's
#: numbering (1..6) so labels line up with their published results.
APPROACHES: dict[str, str] = {
    "bq_tools": "bq_context.approaches.agent_bq_tools.agent",
    "kc_search": "bq_context.approaches.agent_kc_search.agent",
    "kc_context": "bq_context.approaches.agent_kc_context.agent",
    "context_prefilter": "bq_context.approaches.agent_context_prefilter.agent",
    "semantic_context": "bq_context.approaches.agent_semantic_context.agent",
    "search_direct": "bq_context.approaches.agent_search_direct.agent",
}

#: Approaches that read the Knowledge Catalog context cache. The other three
#: discover metadata per-request, so warming the cache for them is wasted time
#: and, at 24 shards, wasted money.
_CACHE_USERS = frozenset({"kc_context", "context_prefilter", "semantic_context"})


def needs_cache(approach: str) -> bool:
    """Whether this approach reads the per-tier context cache."""
    return approach in _CACHE_USERS


#: Backoff sleep, named so tests can replace it without patching asyncio for the
#: whole process. The delays themselves are RetryPolicy's and are tested there.
async def retry_sleep(seconds: float) -> None:
    """Await the backoff interval between cell attempts."""
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(seconds)


class AdkCellExecutor:
    """Runs cells for one approach on a shared ``InMemoryRunner``."""

    def __init__(self, spec: ShardSpec) -> None:
        module = importlib.import_module(APPROACHES[spec.approach])
        self.spec = spec
        self.app_name = f"bench_{spec.approach}"
        self.runner = InMemoryRunner(agent=module.root_agent, app_name=self.app_name)

    async def __call__(self, question: Mapping[str, Any], run_idx: int) -> Cell:
        cell = self._blank_cell(question, run_idx)
        attempts = 1

        def _count(_exc: BaseException) -> None:
            nonlocal attempts
            attempts += 1

        try:
            # Retried because a transient failure here used to be permanent.
            # hard-full-01 lost one cell of 3,000 to a single Google 500 and the
            # whole sweep went red on `require_complete`, after finalize had
            # already published every artifact. The shard's KFP retry cannot
            # help: the shard *succeeds*, since a per-cell error is recorded and
            # the loop moves on.
            #
            # `is_retryable` decides what counts -- 5xx and transport failures
            # yes, 403/404 no -- so a permanent error still fails fast.
            measured = await retry_async(
                lambda: self._invoke(str(question["question"])),
                sleep=retry_sleep,
                on_retry=_count,
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            logger.warning("cell %s failed: %s: %s", cell.cell_key, type(exc).__name__, exc)
            cell.status = "error"
            cell.error_type = type(exc).__name__
            cell.error_message = str(exc)
            cell.attempts = attempts
            return cell

        cell.attempts = attempts

        cell.status = "ok"
        for field, value in measured.items():
            setattr(cell, field, value)
        return cell

    def _blank_cell(self, question: Mapping[str, Any], run_idx: int) -> Cell:
        """Identity fields only. Filled in by the caller once the run resolves.

        Built up-front so an error cell carries the same identity and question
        context as a successful one — the scorer and the merge step both key on
        those fields regardless of outcome.
        """
        qid = str(question["id"])
        relevance = question.get("relevance") or {}
        return Cell(
            cell_key=cell_key(qid, self.spec.approach, self.spec.tier, run_idx),
            question_id=qid,
            approach=self.spec.approach,
            tier=self.spec.tier,
            run_idx=run_idx,
            status="error",
            code_version=self.spec.code_version,
            corpus_fingerprint=self.spec.corpus_fingerprint,
            category=str(question.get("category", "")),
            question=str(question.get("question", "")),
            relevance=dict(relevance),
        )

    async def _invoke(self, question_text: str) -> dict[str, Any]:
        """Run the agent once and pull the measurements out of session state."""
        session = await self.runner.session_service.create_session(
            app_name=self.app_name, user_id=USER_ID
        )
        message = types.Content(role="user", parts=[types.Part(text=question_text)])

        adk_tool_calls = 0
        # usage_scope must wrap the run: the reranker records into it from
        # inside an asyncio.to_thread worker, which inherits this context.
        with usage_scope() as usage:
            start = time.time()
            async for event in self.runner.run_async(
                user_id=USER_ID, session_id=session.id, new_message=message
            ):
                if event.content and event.content.parts:
                    adk_tool_calls += sum(1 for p in event.content.parts if p.function_call)
            latency_s = time.time() - start

        final = await self.runner.session_service.get_session(
            app_name=self.app_name, user_id=USER_ID, session_id=session.id
        )
        state = final.state if final else {}
        method = self.spec.approach

        nominated = state.get(f"nominated_tables_{method}", [])
        ranked = self._parse_ranked(state.get(f"reranker_result_{method}", ""), method)

        return {
            "nominated": nominated,
            "nominated_count": len(nominated),
            "ranked_tables": ranked,
            "ranked_count": len(ranked),
            "search_stats": state.get(f"search_stats_{method}"),
            "latency_s": round(latency_s, 3),
            "reranker_prompt_tokens": usage.prompt_tokens,
            "reranker_output_tokens": usage.output_tokens,
            "reranker_total_tokens": usage.total_tokens,
            "reranker_calls": usage.calls,
            "adk_tool_calls": adk_tool_calls,
        }

    @staticmethod
    def _parse_ranked(raw: str, method: str) -> list[dict[str, Any]]:
        if not raw:
            return []
        try:
            parsed = RerankerResponse.model_validate(json.loads(raw))
        except ValueError:
            logger.warning("Could not parse reranker result for %s", method)
            return []
        return [
            {"table_id": t.table_id, "rank": t.rank, "confidence": t.confidence}
            for t in parsed.ranked_tables
        ]


def execute_shard(
    spec: ShardSpec,
    config: ExperimentConfig,
    store: ArtifactStore,
    questions: Mapping[str, Mapping[str, Any]],
    **runner_kwargs: Any,
) -> ShardResult:
    """Warm this tier's cache, then run the shard inside its tier scope.

    The cache warm is timed and logged because sharding by ``(tier, approach)``
    means warming 24 times instead of 4. If it turns out to be expensive, that
    measurement is the argument for coarsening the shard key — so it is recorded
    on every shard result rather than left to guesswork.
    """
    import asyncio  # noqa: PLC0415 - local to keep the module import light

    # Must happen before any agent is constructed: ADK reads these to build its
    # own genai client, and without them an LLM-driven approach fails with
    # "No API key was provided".
    config.configure_adk_env()

    warm_start = time.monotonic()
    if needs_cache(spec.approach):
        # Scope must exist before listing tables, hence the throwaway context.
        bootstrap = TierContext.build(config, spec.tier, TableCache.empty())
        with tier_scope(bootstrap):
            datasets = get_datasets()
            scoped = {ds: get_scoped_tables(ds) for ds in datasets}
        cache = TableCache.build(config, datasets, scoped)
        logger.info(
            "[%s] cache warm: %d table(s) in %.1fs",
            spec.shard_id,
            len(cache),
            time.monotonic() - warm_start,
        )
        if len(cache) == 0:
            logger.error(
                "[%s] cache is EMPTY. lookupContext returns an empty response "
                "rather than 403 when permissions are missing, so this may be "
                "an IAM problem rather than an empty tier.",
                spec.shard_id,
            )
    else:
        cache = TableCache.empty()
    cache_warm_s = round(time.monotonic() - warm_start, 3)

    ctx = TierContext.build(config, spec.tier, cache)
    with tier_scope(ctx):
        executor = AdkCellExecutor(spec)
        shard_runner = ShardRunner(
            spec, store, executor, questions, cache_warm_s=cache_warm_s, **runner_kwargs
        )
        return asyncio.run(shard_runner.run())
