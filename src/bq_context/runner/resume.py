"""Work out what a shard has already done, so a rerun only does the rest.

Resume is per-shard and needs no coordination: each shard lists its own attempt
files, reconstructs the set of cells that finished successfully, and runs the
difference. No lock, no state service, no leader.

Two independent mechanisms stack here:

1. **Cell-level** (this module) — recovers a partially complete shard.
2. **Shard-level** — KFP execution caching skips finished shards without even
   starting a VM. That is only safe because ``code_version`` is an explicit
   shard input; without it, editing a prompt and rerunning would silently return
   cached results from the old code.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from bq_context.runner.models import Cell

if TYPE_CHECKING:
    from bq_context.runner.models import ShardSpec
    from bq_context.runner.store import ArtifactStore

logger = logging.getLogger(__name__)

__all__ = [
    "completed_keys",
    "experiment_prefix",
    "latest_attempt_number",
    "load_shard_records",
    "new_run_id",
    "next_attempt_path",
    "run_prefix",
    "shard_prefix",
]

_ATTEMPT_RE = re.compile(r"attempt-(\d{4})\.jsonl$")

#: A run id is a path segment, so it must not contain one. Timestamp-plus-SHA
#: fits comfortably; anything with a separator in it is a caller error.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def experiment_prefix(experiment_id: str) -> str:
    """Root for one experiment's artifacts.

    Derived from ``experiment_id`` alone, which is a pipeline *parameter*. It is
    therefore stable across reruns — unlike a KFP artifact URI, which embeds the
    pipeline job id and changes every time.
    """
    return f"experiments/{experiment_id}"


def shard_prefix(spec: ShardSpec) -> str:
    """Directory holding one shard's attempt files and markers."""
    return f"{experiment_prefix(spec.experiment_id)}/shards/{spec.shard_id}"


def new_run_id(code_version: str, now: datetime | None = None) -> str:
    """Identity for one *execution* of an experiment.

    ``experiment_id`` is stable so resume can find prior work, which means every
    execution of the same experiment previously wrote its report, HTML and
    figures over the last one's. ``hard-full-01`` ran three times and kept one
    report. This gives each execution somewhere of its own.

    Timestamp first so lexical order is time order — a bucket listing is then
    chronological — and the commit after it so the folder says what produced it
    without opening the manifest inside. Not the Vertex job id: a local run has
    none, and the job id goes in the manifest instead.

    ``now`` exists for the tests; nothing in the codebase passes it.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{code_version}"


def run_prefix(experiment_id: str, run_id: str) -> str:
    """Root for one execution's derived artifacts — report, HTML, figures.

    Under the experiment, not beside it: everything one experiment produced
    stays in one listing, and the shard and merged prefixes are untouched so
    resume keeps working.
    """
    if not _RUN_ID_RE.match(run_id):
        msg = f"not a usable run id: {run_id!r}"
        raise ValueError(msg)
    return f"{experiment_prefix(experiment_id)}/runs/{run_id}"


def latest_attempt_number(store: ArtifactStore, spec: ShardSpec) -> int:
    """Highest attempt number already written, or 0 if none."""
    numbers = [
        int(m.group(1))
        for path in store.list_paths(shard_prefix(spec))
        if (m := _ATTEMPT_RE.search(path))
    ]
    return max(numbers, default=0)


def summary_path(attempt_path: str) -> str:
    """Path of the ShardResult summary paired with ``attempt_path``.

    One summary per *attempt*, not per shard. A resumed shard's first attempt is
    usually the interesting one — it holds the failure that caused the resume —
    and a single ``summary.json`` would be overwritten by the attempt that
    succeeded.

    Deliberately outside ``_ATTEMPT_RE`` so ``load_shard_records`` keeps
    skipping it; a summary read as cells would be a parse error per resume.
    """
    if attempt_path.endswith(".jsonl"):
        return attempt_path.replace("attempt-", "summary-").removesuffix(".jsonl") + ".json"
    # Never raise: failing to name a diagnostics file must not fail the shard.
    return f"{attempt_path or 'summary'}.json"


def next_attempt_path(store: ArtifactStore, spec: ShardSpec) -> str:
    """Path for a fresh attempt file, never colliding with a previous one."""
    return f"{shard_prefix(spec)}/attempt-{latest_attempt_number(store, spec) + 1:04d}.jsonl"


# ---------------------------------------------------------------------------
# Reading prior work
# ---------------------------------------------------------------------------


def load_shard_records(store: ArtifactStore, spec: ShardSpec) -> list[Cell]:
    """Every cell this shard has written, in attempt order then line order.

    Order matters: ``completed_keys`` resolves duplicates by last write, so the
    caller must not reorder the result.

    A malformed line is skipped with a warning rather than raising. The last
    line of an attempt file can be torn if the process died mid-write, and one
    truncated line must not make an otherwise good shard unresumable.
    """
    records: list[Cell] = []
    for path in sorted(store.list_paths(shard_prefix(spec))):
        if not _ATTEMPT_RE.search(path):
            continue
        for lineno, line in enumerate(store.read_text(path).splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                records.append(Cell.model_validate_json(stripped))
            except ValueError:
                logger.warning("Skipping malformed record at %s:%d", path, lineno)
    return records


def completed_keys(records: list[Cell]) -> set[str]:
    """Cell keys that finished successfully, by last write per key.

    Last-write-wins rather than best-status-wins: a cell that succeeded once and
    then failed on a later attempt is *not* complete. Taking the optimistic view
    would hide a cell that has started failing reproducibly.
    """
    final: dict[str, str] = {}
    for record in records:
        final[record.cell_key] = record.status
    return {key for key, status in final.items() if status == "ok"}
