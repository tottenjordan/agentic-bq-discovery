"""Records the sweep reads and writes.

Field names deliberately match upstream's cell schema (see
``docs/upstream/run_questions.py``) so our results can be compared directly
against their published table. Additions — ``status``, ``written_at``,
``code_version``, ``error_type``, ``attempts`` — are new fields rather than
renames, so upstream's ``build_results.py`` scoring still reads our cells.

These live here rather than in ``schemas.py`` because that file is vendored
verbatim and carries a blanket lint exemption; our own models should not
inherit it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

__all__ = ["Cell", "CellStatus", "ShardResult", "ShardSpec", "cell_key", "shard_id"]

CellStatus = Literal["ok", "error"]


def cell_key(question_id: str, approach: str, tier: int, run_idx: int) -> str:
    """Globally unique id for one approach-run.

    Carries no shard identity on purpose: resume matches on this key, so a cell
    completed under one shard plan is still recognised as done under a different
    one. That is what lets us re-shard a partially-complete experiment.
    """
    return f"{question_id}|{approach}|tier{tier}|run{run_idx}"


def shard_id(tier: int, approach: str) -> str:
    """Directory name for one shard's artifacts."""
    return f"tier{tier}__{approach}"


class ShardSpec(BaseModel):
    """One unit of parallel work: all cells for a (tier, approach) pair."""

    experiment_id: str
    tier: int
    approach: str
    question_ids: list[str]
    runs: int
    code_version: str
    #: Enrichment shape this shard ran against. Provenance, and the KFP cache key
    #: input that stops a corpus change returning cells scored on the old corpus.
    #: Defaulted so older attempt files and summaries still parse.
    corpus_fingerprint: str = ""

    @property
    def shard_id(self) -> str:
        return shard_id(self.tier, self.approach)

    def planned_cells(self) -> list[str]:
        """Every cell key this shard is responsible for, in execution order."""
        return [
            cell_key(qid, self.approach, self.tier, run_idx)
            for qid in self.question_ids
            for run_idx in range(self.runs)
        ]


class Cell(BaseModel):
    """One approach-run: the atomic unit of the factorial."""

    # -- identity (always present, including on errors) ---------------------
    cell_key: str
    question_id: str
    approach: str
    tier: int
    run_idx: int
    status: CellStatus
    written_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    code_version: str = ""

    # -- question context ----------------------------------------------------
    category: str = ""
    question: str = ""
    relevance: dict[str, list[str]] = Field(default_factory=dict)

    # -- results -------------------------------------------------------------
    nominated: list[str] = Field(default_factory=list)
    nominated_count: int = 0
    ranked_tables: list[dict[str, Any]] = Field(default_factory=list)
    ranked_count: int = 0
    search_stats: dict[str, Any] | None = None

    # -- measurements --------------------------------------------------------
    latency_s: float = 0.0
    reranker_prompt_tokens: int = 0
    reranker_output_tokens: int = 0
    reranker_total_tokens: int = 0
    reranker_calls: int = 0
    adk_tool_calls: int = 0
    cache_warm_s: float = 0.0

    # -- failure -------------------------------------------------------------
    error_type: str = ""
    error_message: str = ""
    attempts: int = 1

    def to_jsonl(self) -> str:
        """One line of JSONL, newline included."""
        return self.model_dump_json() + "\n"


class ShardResult(BaseModel):
    """Outcome of one shard, written alongside its attempt files."""

    shard_id: str
    experiment_id: str
    tier: int
    approach: str
    code_version: str
    corpus_fingerprint: str = ""
    planned: int
    already_done: int
    executed: int
    succeeded: int
    failed: int
    aborted: bool = False
    abort_reason: str = ""
    cache_warm_s: float = 0.0
    elapsed_s: float = 0.0
    attempt_path: str = ""

    @property
    def complete(self) -> bool:
        """Whether every planned cell of this shard now has an ``ok`` record."""
        return not self.aborted and self.already_done + self.succeeded >= self.planned
