"""Resume is the safety net the whole reliability design rests on.

A full sweep is ~12 hours of live Gemini calls. Nothing else in the system
matters if a crash at hour 11 costs 11 hours, so this logic gets real tests
against a real store rather than a mock.

The load-bearing property: ``cell_key`` carries no shard identity, so a cell
completed under one shard plan is still recognised under a different one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bq_context.runner.models import Cell, ShardSpec, cell_key, shard_id
from bq_context.runner.resume import (
    completed_keys,
    latest_attempt_number,
    load_shard_records,
    next_attempt_path,
    shard_prefix,
)
from bq_context.runner.store import LocalStore, store_for


def make_cell(key: str, status: str = "ok", **kw: object) -> Cell:
    qid, approach, tier, run = key.split("|")
    return Cell(
        cell_key=key,
        question_id=qid,
        approach=approach,
        tier=int(tier.removeprefix("tier")),
        run_idx=int(run.removeprefix("run")),
        status=status,  # ty: ignore[invalid-argument-type]
        **kw,
    )


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------
def test_cell_key_format_matches_upstream() -> None:
    """Upstream's exact format, so their results.json can be read by our tools."""
    assert cell_key("multi-disp-q1", "bq_tools", 0, 0) == "multi-disp-q1|bq_tools|tier0|run0"


def test_planned_cells_covers_the_full_cross_product() -> None:
    spec = ShardSpec(
        experiment_id="e1",
        tier=3,
        approach="kc_search",
        question_ids=["q1", "q2"],
        runs=3,
        code_version="abc123",
    )
    cells = spec.planned_cells()
    assert len(cells) == 6
    assert cells[0] == "q1|kc_search|tier3|run0"
    assert cells[-1] == "q2|kc_search|tier3|run2"
    assert len(set(cells)) == 6


# ---------------------------------------------------------------------------
# Dedupe semantics
# ---------------------------------------------------------------------------
def test_resume_drops_errors_and_keeps_last_ok() -> None:
    """Error cells re-run, so a resume pass is self-healing."""
    records = [
        make_cell("q1|bq_tools|tier0|run0", status="error"),
        make_cell("q1|bq_tools|tier0|run0", status="ok"),
        make_cell("q2|bq_tools|tier0|run0", status="error"),
    ]
    assert completed_keys(records) == {"q1|bq_tools|tier0|run0"}


def test_last_write_wins_even_when_the_later_write_failed() -> None:
    """A cell that succeeded then failed on a retry is NOT complete.

    Ordering is what decides, not optimism: taking the best-ever status would
    hide a cell that has started failing reproducibly.
    """
    records = [
        make_cell("q1|bq_tools|tier0|run0", status="ok"),
        make_cell("q1|bq_tools|tier0|run0", status="error"),
    ]
    assert completed_keys(records) == set()


def test_resume_survives_a_shard_plan_change() -> None:
    """Cells finished as one big shard still count when re-sharded by run_idx.

    This is what lets a failed bq_tools shard be re-planned into five smaller
    ones without redoing completed work.
    """
    done = [make_cell(f"q1|bq_tools|tier0|run{i}") for i in range(5)]
    finished = completed_keys(done)

    narrow = ShardSpec(
        experiment_id="e1",
        tier=0,
        approach="bq_tools",
        question_ids=["q1"],
        runs=5,
        code_version="v1",
    )
    assert [c for c in narrow.planned_cells() if c not in finished] == []


# ---------------------------------------------------------------------------
# Round trip through a real store
# ---------------------------------------------------------------------------
def test_round_trip_through_the_store(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    spec = ShardSpec(
        experiment_id="exp-1",
        tier=2,
        approach="kc_context",
        question_ids=["q1", "q2"],
        runs=1,
        code_version="v1",
    )
    prefix = shard_prefix(spec)

    store.write_text(
        next_attempt_path(store, spec),
        make_cell("q1|kc_context|tier2|run0").to_jsonl(),
    )
    store.write_text(
        next_attempt_path(store, spec),
        make_cell("q2|kc_context|tier2|run0", status="error").to_jsonl(),
    )

    paths = store.list_paths(prefix)
    assert [p.rsplit("/", 1)[-1] for p in paths] == ["attempt-0001.jsonl", "attempt-0002.jsonl"]

    records = load_shard_records(store, spec)
    assert len(records) == 2
    assert completed_keys(records) == {"q1|kc_context|tier2|run0"}


def test_attempt_numbering_never_clobbers_a_prior_attempt(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    spec = ShardSpec(
        experiment_id="e",
        tier=0,
        approach="search_direct",
        question_ids=["q1"],
        runs=1,
        code_version="v1",
    )
    assert latest_attempt_number(store, spec) == 0
    for expected in (1, 2, 3):
        path = next_attempt_path(store, spec)
        assert path.endswith(f"attempt-{expected:04d}.jsonl")
        store.write_text(path, "")
        assert latest_attempt_number(store, spec) == expected


def test_malformed_lines_are_skipped_not_fatal(tmp_path: Path) -> None:
    """A torn final line must not make the whole shard unresumable."""
    store = LocalStore(tmp_path)
    spec = ShardSpec(
        experiment_id="e",
        tier=0,
        approach="kc_search",
        question_ids=["q1"],
        runs=1,
        code_version="v1",
    )
    good = make_cell("q1|kc_search|tier0|run0").to_jsonl()
    store.write_text(f"{shard_prefix(spec)}/attempt-0001.jsonl", good + '{"cell_key": "trunc')

    records = load_shard_records(store, spec)
    assert len(records) == 1
    assert completed_keys(records) == {"q1|kc_search|tier0|run0"}


def test_missing_shard_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    spec = ShardSpec(
        experiment_id="e",
        tier=1,
        approach="bq_tools",
        question_ids=["q1"],
        runs=1,
        code_version="v1",
    )
    assert load_shard_records(store, spec) == []
    assert completed_keys([]) == set()


# ---------------------------------------------------------------------------
# Store factory
# ---------------------------------------------------------------------------
def test_store_for_dispatches_on_scheme(tmp_path: Path) -> None:
    assert isinstance(store_for(str(tmp_path)), LocalStore)


def test_shard_prefix_is_stable_across_reruns() -> None:
    """Derived only from experiment_id + shard id — never a pipeline job id.

    KFP artifact URIs embed the job id, which changes every run; a resume built
    on them could not find the previous run's data.
    """
    spec = ShardSpec(
        experiment_id="pilot-01",
        tier=3,
        approach="bq_tools",
        question_ids=[],
        runs=1,
        code_version="v1",
    )
    assert shard_prefix(spec) == f"experiments/pilot-01/shards/{shard_id(3, 'bq_tools')}"


@pytest.mark.parametrize("status", ["ok", "error"])
def test_cell_serializes_round_trip(status: str) -> None:
    original = make_cell("q1|kc_search|tier1|run2", status=status, latency_s=1.25)
    restored = Cell.model_validate_json(original.model_dump_json())
    assert restored.cell_key == original.cell_key
    assert restored.status == status
    assert restored.latency_s == 1.25
