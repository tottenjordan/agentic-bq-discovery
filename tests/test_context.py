"""The three globals are gone and their replacements are concurrency-safe.

Upstream carried three pieces of module-global mutable state — `config.SCOPE` /
`ACTIVE_TIER`, `context_cache._CACHE`, and `reranker.util_rerank._USAGE_LOG` —
which is why its 3,000-cell factorial had to run serially for ~12 hours. These
tests pin the properties that make sharding safe, so a future refactor cannot
quietly reintroduce the coupling.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from bq_context.config import ExperimentConfig, Locations, tier_dataset
from bq_context.context_cache import TableCache
from bq_context.runtime import (
    NoTierContextError,
    TierContext,
    current_tier,
    get_datasets,
    is_table_in_scope,
    tier_scope,
)
from bq_context.usage import current_usage, record_usage_tokens, usage_scope


@pytest.fixture
def config() -> ExperimentConfig:
    return ExperimentConfig(
        project="test-project",
        locations=Locations(),
        agent_model="gemini-3.6-flash",
        tool_model="gemini-3.5-flash-lite",
        resource_prefix="bigquery_context",
        top_k=5,
    )


# ---------------------------------------------------------------------------
# Tier scope
# ---------------------------------------------------------------------------
def test_two_tier_contexts_are_independent(config: ExperimentConfig) -> None:
    a = TierContext.build(config, tier=0, cache=TableCache.empty())
    b = TierContext.build(config, tier=3, cache=TableCache.empty())

    assert a.scope == ("bigquery_context_tier0",)
    assert b.scope == ("bigquery_context_tier3",)
    assert a.scope != b.scope


def test_scope_is_exactly_one_dataset(config: ExperimentConfig) -> None:
    """Scoring matches on *short* table name and every tier dataset holds
    identically-named tables, so a run seeing two tiers would silently score
    against the wrong corpus. One dataset, always.
    """
    for tier in (0, 1, 2, 3):
        ctx = TierContext.build(config, tier=tier, cache=TableCache.empty())
        assert len(ctx.scope) == 1
        assert ctx.scope[0] == tier_dataset(config.resource_prefix, tier)


def test_reading_tier_outside_a_scope_raises() -> None:
    """Failing loudly beats defaulting to tier 3 and mislabelling the results."""
    with pytest.raises(NoTierContextError):
        current_tier()


def test_tier_scope_restores_the_previous_context(config: ExperimentConfig) -> None:
    outer = TierContext.build(config, tier=0, cache=TableCache.empty())
    inner = TierContext.build(config, tier=3, cache=TableCache.empty())

    with tier_scope(outer):
        assert current_tier().tier == 0
        with tier_scope(inner):
            assert current_tier().tier == 3
        assert current_tier().tier == 0

    with pytest.raises(NoTierContextError):
        current_tier()


def test_scope_helpers_read_the_ambient_context(config: ExperimentConfig) -> None:
    ctx = TierContext.build(config, tier=2, cache=TableCache.empty())
    with tier_scope(ctx):
        assert get_datasets() == ["bigquery_context_tier2"]
        assert is_table_in_scope("bigquery_context_tier2", "austin_crime")
        assert not is_table_in_scope("bigquery_context_tier3", "austin_crime")


async def test_concurrent_tiers_do_not_leak(config: ExperimentConfig) -> None:
    """The property that makes in-shard concurrency safe later.

    Two coroutines in different tier scopes must never observe each other's
    scope, even while interleaved at an await point.
    """

    async def observe(tier: int) -> list[str]:
        ctx = TierContext.build(config, tier=tier, cache=TableCache.empty())
        with tier_scope(ctx):
            await asyncio.sleep(0.01)  # force interleaving
            return get_datasets()

    got = await asyncio.gather(observe(0), observe(3), observe(1))
    assert got == [
        ["bigquery_context_tier0"],
        ["bigquery_context_tier3"],
        ["bigquery_context_tier1"],
    ]


# ---------------------------------------------------------------------------
# Usage accounting
# ---------------------------------------------------------------------------
async def test_usage_log_is_context_isolated() -> None:
    """Per-cell token accounting must stay exact under concurrency.

    Upstream's plain module list was only correct because runs were sequential.
    """

    async def one(n: int) -> int:
        with usage_scope() as log:
            await asyncio.sleep(0.01)
            record_usage_tokens(prompt=n, output=0, total=n)
            return log.total_tokens

    assert await asyncio.gather(one(100), one(200), one(300)) == [100, 200, 300]


def test_usage_accumulates_within_one_scope() -> None:
    with usage_scope() as log:
        record_usage_tokens(prompt=10, output=2, total=12)
        record_usage_tokens(prompt=20, output=3, total=23)

    assert log.calls == 2
    assert log.prompt_tokens == 30
    assert log.output_tokens == 5
    assert log.total_tokens == 35


def test_usage_outside_a_scope_is_a_noop() -> None:
    """Interactive `adk web` use has no benchmark scope; it must not crash."""
    record_usage_tokens(prompt=1, output=1, total=2)
    assert current_usage() is None


async def test_context_reaches_asyncio_to_thread_workers(config: ExperimentConfig) -> None:
    """Both ContextVars must survive the hop into a worker thread.

    The reranker is a blocking call, so every approach reaches it via
    ``asyncio.to_thread``, and it records tokens and reads config from inside
    that thread. ``to_thread`` copies the caller's Context, which is the only
    reason this works — a bare ``run_in_executor`` would not, and the failure
    would be silent: zero tokens recorded and a NoTierContextError.
    """

    def blocking_work() -> int:
        record_usage_tokens(prompt=7, output=1, total=8)
        return current_tier().tier

    ctx = TierContext.build(config, tier=2, cache=TableCache.empty())
    with tier_scope(ctx), usage_scope() as log:
        tier = await asyncio.to_thread(blocking_work)

    assert tier == 2
    assert log.total_tokens == 8
    assert log.calls == 1


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------
def test_gemini_location_is_global_not_regional() -> None:
    """Verified 2026-09-22: both models 404 in us-central1, 200 at global.

    Conflating pipeline compute location with the model endpoint is the most
    likely day-one failure, so it gets a test rather than a comment.
    """
    loc = Locations()
    assert loc.gemini == "global"
    assert loc.pipeline == "us-central1"
    assert loc.gemini != loc.pipeline


def test_catalog_location_tracks_bigquery_not_datascan() -> None:
    """Entry links must be co-located with the BigQuery entries they reference.

    Upstream's readme says these live in DATAPLEX_LOCATION; upstream's code puts
    them in BQ_LOCATION.lower(). The code is authoritative.
    """
    loc = Locations()
    assert loc.catalog == loc.bigquery.lower()
    assert loc.catalog != loc.datascan


# ---------------------------------------------------------------------------
# Regression guard
# ---------------------------------------------------------------------------
def test_no_mutable_module_globals_remain() -> None:
    """The three specific globals upstream used must not come back."""
    from bq_context import config as config_mod
    from bq_context.context_cache import cache as cache_mod
    from bq_context.reranker import util_rerank

    assert not hasattr(config_mod, "SCOPE")
    assert not hasattr(config_mod, "ACTIVE_TIER")
    assert not hasattr(config_mod, "set_active_tier")
    assert not hasattr(cache_mod, "_CACHE")
    assert not hasattr(util_rerank, "_USAGE_LOG")


# ---------------------------------------------------------------------------
# ADK environment
# ---------------------------------------------------------------------------
def test_configure_adk_env_sets_what_adk_builds_its_client_from(
    config: ExperimentConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard for a bug found only by running a live agent.

    Our reranker constructs genai.Client(vertexai=True, ...) explicitly, but ADK
    does not — for an LLM-driven agent it builds a client from these three
    environment variables. Task 3 removed upstream's import-time os.environ
    mutation without replacing it, and nothing caught it, because four of the
    six approaches short-circuit the agent LLM in a callback. The first live
    bq_tools cell failed with "No API key was provided".
    """
    for name in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"):
        monkeypatch.delenv(name, raising=False)

    config.configure_adk_env()

    assert os.environ["GOOGLE_GENAI_USE_VERTEXAI"] == "true"
    assert os.environ["GOOGLE_CLOUD_PROJECT"] == "test-project"
    # The model endpoint, not the compute region.
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"


def test_configure_adk_env_overrides_a_stale_regional_location(
    config: ExperimentConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leftover regional value must not survive; it 404s for these models."""
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    config.configure_adk_env()
    assert os.environ["GOOGLE_CLOUD_LOCATION"] == "global"


# ---------------------------------------------------------------------------
# ADK's own Gemini calls
# ---------------------------------------------------------------------------
def test_agent_model_carries_retry_options() -> None:
    """Regression from the first full 3,000-cell run, which lost 7 cells.

    Our jittered backoff wraps call_reranker — the Gemini calls we make. ADK
    builds and drives its own client for an LLM-driven agent, and a bare model
    string leaves those calls with no retry at all. Every one of the 7 lost
    cells was 429 RESOURCE_EXHAUSTED on bq_tools or context_prefilter, the only
    two approaches that reach the agent LLM.
    """
    from bq_context.runtime import agent_model

    retry = agent_model().retry_options  # type: ignore[attr-defined]
    assert retry is not None, "a bare model string gets no retry"
    assert retry.attempts == 8
    assert 429 in retry.http_status_codes
    assert retry.jitter, "eight shards retrying in lockstep re-collide"


def test_agent_retry_matches_our_own_policy() -> None:
    """Both Gemini paths should behave the same under contention."""
    from bq_context.runner.backoff import DEFAULT_POLICY
    from bq_context.runtime import agent_model

    retry = agent_model().retry_options  # type: ignore[attr-defined]
    assert retry.attempts == DEFAULT_POLICY.max_attempts
    assert retry.initial_delay == DEFAULT_POLICY.base_delay
    assert retry.max_delay == DEFAULT_POLICY.max_delay
    assert retry.exp_base == DEFAULT_POLICY.multiplier


def test_every_approach_agent_has_a_retrying_model() -> None:
    """A new approach added with model=<string> would silently lose retry."""
    import importlib

    from bq_context.runner.cells import APPROACHES

    for approach, module_path in APPROACHES.items():
        model = importlib.import_module(module_path).root_agent.model
        assert not isinstance(model, str), f"{approach} uses a bare model string"
        assert model.retry_options is not None, approach
