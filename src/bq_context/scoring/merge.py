"""Collect every shard's output into one deduped results file.

Merge globs GCS rather than consuming shard task outputs. Three reasons, all of
which bite in the pipeline:

- It runs under a KFP ``ExitHandler``, and an exit task cannot read the outputs
  of tasks inside the handler.
- It therefore still works when a shard died, producing partial results plus a
  precise list of what is missing.
- It has no artifact-count ceiling, unlike ``dsl.Collected`` (100 per task).

Merge never fails on missing cells; it records them. ``verify_completeness`` is
the only step allowed to turn a run red, so that a 12-hour sweep with three bad
cells still yields a scored dataset and an actionable list.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bq_context.runner.models import Cell
from bq_context.runner.resume import experiment_prefix

if TYPE_CHECKING:
    from bq_context.runner.store import ArtifactStore

logger = logging.getLogger(__name__)

__all__ = ["MergeResult", "load_merged", "merge_experiment", "merged_path", "missing_path"]


def merged_path(experiment_id: str) -> str:
    return f"{experiment_prefix(experiment_id)}/merged/results.jsonl"


def missing_path(experiment_id: str) -> str:
    return f"{experiment_prefix(experiment_id)}/merged/missing.json"


@dataclass(frozen=True, slots=True)
class MergeResult:
    experiment_id: str
    shards_seen: int
    records_read: int
    unique_cells: int
    ok_cells: int
    error_cells: int
    expected: int = 0
    missing: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing


def merge_experiment(
    store: ArtifactStore,
    experiment_id: str,
    expected_cells: list[str] | None = None,
) -> MergeResult:
    """Merge all shard attempt files into ``merged/results.jsonl``.

    Deduplicates on ``cell_key`` with last write winning, matching the per-shard
    resume rule so a cell's fate is decided the same way everywhere.
    """
    prefix = f"{experiment_prefix(experiment_id)}/shards"
    paths = [p for p in sorted(store.list_paths(prefix)) if p.endswith(".jsonl")]

    latest: dict[str, Cell] = {}
    records_read = 0
    for path in paths:
        for lineno, line in enumerate(store.read_text(path).splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                cell = Cell.model_validate_json(stripped)
            except ValueError:
                logger.warning("Skipping malformed record at %s:%d", path, lineno)
                continue
            records_read += 1
            latest[cell.cell_key] = cell

    ok = {key: cell for key, cell in latest.items() if cell.status == "ok"}
    shards = {p.split("/shards/", 1)[1].split("/", 1)[0] for p in paths}

    body = "".join(ok[key].to_jsonl() for key in sorted(ok))
    store.write_text(merged_path(experiment_id), body)

    missing = sorted(set(expected_cells) - set(ok)) if expected_cells else []
    store.write_text(
        missing_path(experiment_id),
        json.dumps(
            {
                "experiment_id": experiment_id,
                "expected": len(expected_cells or []),
                "present": len(ok),
                "missing_count": len(missing),
                "missing": missing,
            },
            indent=2,
        ),
    )

    result = MergeResult(
        experiment_id=experiment_id,
        shards_seen=len(shards),
        records_read=records_read,
        unique_cells=len(latest),
        ok_cells=len(ok),
        error_cells=len(latest) - len(ok),
        expected=len(expected_cells or []),
        missing=missing,
    )
    logger.info(
        "Merged %d shard(s), %d records -> %d unique (%d ok, %d error); %d missing",
        result.shards_seen,
        result.records_read,
        result.unique_cells,
        result.ok_cells,
        result.error_cells,
        len(result.missing),
    )
    return result


def load_merged(store: ArtifactStore, experiment_id: str) -> list[dict]:
    """Read ``merged/results.jsonl`` back as plain dicts for scoring."""
    try:
        text = store.read_text(merged_path(experiment_id))
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.splitlines() if line.strip()]
