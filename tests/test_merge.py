"""Merge collects shards without depending on any of them having succeeded.

The pipeline runs merge under a KFP ExitHandler, which fires whether or not the
shards inside it worked. So merge must glob storage rather than consume task
outputs, must produce partial results when shards are missing, and must record
exactly what is absent instead of failing.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from bq_context.runner.models import Cell, ShardSpec
from bq_context.runner.resume import shard_prefix
from bq_context.runner.store import LocalStore
from bq_context.scoring.merge import load_merged, merge_experiment, merged_path, missing_path

if TYPE_CHECKING:
    from pathlib import Path

EXPERIMENT = "test-exp"


def spec_for(tier: int, approach: str, questions: list[str], runs: int = 1) -> ShardSpec:
    return ShardSpec(
        experiment_id=EXPERIMENT,
        tier=tier,
        approach=approach,
        question_ids=questions,
        runs=runs,
        code_version="v1",
    )


def cell(
    qid: str, approach: str, tier: int, run: int = 0, status: str = "ok", **kw: object
) -> Cell:
    return Cell(
        cell_key=f"{qid}|{approach}|tier{tier}|run{run}",
        question_id=qid,
        approach=approach,
        tier=tier,
        run_idx=run,
        status=status,  # ty: ignore[invalid-argument-type]
        **kw,
    )


def write_shard(store: LocalStore, spec: ShardSpec, cells: list[Cell], attempt: int = 1) -> None:
    store.write_text(
        f"{shard_prefix(spec)}/attempt-{attempt:04d}.jsonl",
        "".join(c.to_jsonl() for c in cells),
    )


def test_merges_across_shards(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    a = spec_for(0, "kc_search", ["q1", "q2"])
    b = spec_for(0, "search_direct", ["q1", "q2"])
    write_shard(store, a, [cell("q1", "kc_search", 0), cell("q2", "kc_search", 0)])
    write_shard(store, b, [cell("q1", "search_direct", 0), cell("q2", "search_direct", 0)])

    result = merge_experiment(store, EXPERIMENT, [*a.planned_cells(), *b.planned_cells()])

    assert result.shards_seen == 2
    assert result.ok_cells == 4
    assert result.complete
    assert len(load_merged(store, EXPERIMENT)) == 4


def test_later_attempt_supersedes_earlier(tmp_path: Path) -> None:
    """Same dedupe rule as per-shard resume: last write decides."""
    store = LocalStore(tmp_path)
    spec = spec_for(1, "kc_context", ["q1"])
    write_shard(store, spec, [cell("q1", "kc_context", 1, status="error")], attempt=1)
    write_shard(store, spec, [cell("q1", "kc_context", 1, latency_s=2.5)], attempt=2)

    result = merge_experiment(store, EXPERIMENT, spec.planned_cells())

    assert result.ok_cells == 1
    assert result.error_cells == 0
    merged = load_merged(store, EXPERIMENT)
    assert merged[0]["latency_s"] == 2.5


def test_a_still_failing_cell_is_excluded_and_reported_missing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    spec = spec_for(1, "kc_context", ["q1", "q2"])
    write_shard(
        store, spec, [cell("q1", "kc_context", 1), cell("q2", "kc_context", 1, status="error")]
    )

    result = merge_experiment(store, EXPERIMENT, spec.planned_cells())

    assert result.ok_cells == 1
    assert result.error_cells == 1
    assert result.missing == ["q2|kc_context|tier1|run0"]
    assert not result.complete


def test_a_dead_shard_yields_partial_results_not_a_failure(tmp_path: Path) -> None:
    """The property the ExitHandler design depends on."""
    store = LocalStore(tmp_path)
    alive = spec_for(0, "kc_search", ["q1", "q2"])
    dead = spec_for(0, "bq_tools", ["q1", "q2"])
    write_shard(store, alive, [cell("q1", "kc_search", 0), cell("q2", "kc_search", 0)])
    # `dead` never wrote anything.

    result = merge_experiment(store, EXPERIMENT, [*alive.planned_cells(), *dead.planned_cells()])

    assert result.ok_cells == 2
    assert sorted(result.missing) == ["q1|bq_tools|tier0|run0", "q2|bq_tools|tier0|run0"]
    assert len(load_merged(store, EXPERIMENT)) == 2, "surviving shard still merged"


def test_missing_report_is_written_even_when_complete(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    spec = spec_for(0, "kc_search", ["q1"])
    write_shard(store, spec, [cell("q1", "kc_search", 0)])

    merge_experiment(store, EXPERIMENT, spec.planned_cells())

    report = json.loads(store.read_text(missing_path(EXPERIMENT)))
    assert report["expected"] == 1
    assert report["present"] == 1
    assert report["missing"] == []


def test_merge_is_idempotent(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    spec = spec_for(0, "kc_search", ["q1", "q2"])
    write_shard(store, spec, [cell("q1", "kc_search", 0), cell("q2", "kc_search", 0)])

    first = merge_experiment(store, EXPERIMENT, spec.planned_cells())
    body = store.read_text(merged_path(EXPERIMENT))
    second = merge_experiment(store, EXPERIMENT, spec.planned_cells())

    assert first.ok_cells == second.ok_cells
    assert store.read_text(merged_path(EXPERIMENT)) == body


def test_empty_experiment_merges_to_nothing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    result = merge_experiment(store, EXPERIMENT, ["q1|kc_search|tier0|run0"])

    assert result.ok_cells == 0
    assert result.missing == ["q1|kc_search|tier0|run0"]
    assert load_merged(store, EXPERIMENT) == []


def test_malformed_line_does_not_abort_the_merge(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    spec = spec_for(0, "kc_search", ["q1"])
    store.write_text(
        f"{shard_prefix(spec)}/attempt-0001.jsonl",
        cell("q1", "kc_search", 0).to_jsonl() + '{"cell_key": "trunc',
    )

    result = merge_experiment(store, EXPERIMENT, spec.planned_cells())

    assert result.ok_cells == 1
    assert result.complete
