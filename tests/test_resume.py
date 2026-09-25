"""Resume is the safety net the whole reliability design rests on.

A full sweep is ~12 hours of live Gemini calls. Nothing else in the system
matters if a crash at hour 11 costs 11 hours, so this logic gets real tests
against a real store rather than a mock.

The load-bearing property: ``cell_key`` carries no shard identity, so a cell
completed under one shard plan is still recognised under a different one.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bq_context.runner.models import Cell, ShardSpec, cell_key, shard_id
from bq_context.runner.resume import (
    completed_keys,
    latest_attempt_number,
    load_shard_records,
    new_run_id,
    next_attempt_path,
    note_experiment_identity,
    run_prefix,
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


# ---------------------------------------------------------------------------
# Run identity
#
# `experiment_prefix` is deliberately stable so resume works, which means every
# execution of one experiment id wrote its report, HTML and figures to the same
# paths. `hard-full-01` ran three times and kept one report: the two earlier
# ones — including the failed run whose report was the evidence for the shard
# exit-code bug — were overwritten. `run_id` gives each execution its own folder
# for derived output while leaving the shard and merged paths alone.
# ---------------------------------------------------------------------------
def test_two_runs_get_different_ids() -> None:
    assert new_run_id("abc1234") != new_run_id("abc1234", now=datetime(2026, 9, 24, tzinfo=UTC))


def test_a_run_id_sorts_chronologically() -> None:
    """Lexical order must equal time order, or `gcloud storage ls` lists a
    bucket's runs in an order that means nothing."""
    early = new_run_id("aaa", now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC))
    late = new_run_id("aaa", now=datetime(2026, 11, 2, 3, 4, 5, tzinfo=UTC))
    assert early < late


def test_a_run_id_names_the_commit_that_produced_it() -> None:
    """So the folder is readable without opening the manifest inside it."""
    assert new_run_id("14b273a", now=datetime(2026, 9, 24, 16, 46, 12, tzinfo=UTC)) == (
        "20260924T164612Z-14b273a"
    )


def test_the_run_prefix_nests_under_the_experiment() -> None:
    assert run_prefix("hard-full-01", "20260924T164612Z-14b273a") == (
        "experiments/hard-full-01/runs/20260924T164612Z-14b273a"
    )


@pytest.mark.parametrize("hostile", ["../../etc", "a/b", "", "   "])
def test_a_run_id_that_would_escape_its_folder_is_refused(hostile: str) -> None:
    """A run id reaches this from a pipeline parameter, so it is caller input.
    A slash would silently nest the run under a path nobody looks in; `..` would
    write over another experiment."""
    with pytest.raises(ValueError, match="run id"):
        run_prefix("exp", hostile)


@pytest.mark.parametrize("status", ["ok", "error"])
def test_cell_serializes_round_trip(status: str) -> None:
    original = make_cell("q1|kc_search|tier1|run2", status=status, latency_s=1.25)
    restored = Cell.model_validate_json(original.model_dump_json())
    assert restored.cell_key == original.cell_key
    assert restored.status == status
    assert restored.latency_s == 1.25


# ---------------------------------------------------------------------------
# Experiment identity
#
# `experiment_prefix` is derived from `experiment_id` alone, so resuming
# `hard-full-01` after switching CORPUS_PROFILE would append cells measured
# against a second corpus to the first one's shards, merge them into one
# results.jsonl, and say nothing. Nothing else in the system would notice: the
# KFP shard cache keys on the fingerprint, so the new cells are legitimately
# new work.
# ---------------------------------------------------------------------------
def test_a_first_run_records_its_identity_and_says_nothing(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    assert note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1") == []
    assert json.loads(store.read_text("experiments/exp/experiment.json"))["corpus_fingerprint"] == (
        "aaa"
    )


def test_the_same_corpus_stays_silent(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1")
    assert note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1") == []


def test_a_changed_corpus_warns_and_names_both(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1")
    warnings = note_experiment_identity(store, "exp", corpus_fingerprint="bbb", code_version="v1")

    assert len(warnings) == 1
    # Both, so the reader can tell which is the surprise without going digging.
    assert "aaa" in warnings[0]
    assert "bbb" in warnings[0]


def test_a_changed_code_version_alone_does_not_warn(tmp_path: Path) -> None:
    """It is *expected*: a new commit is what invalidates the shard cache, and
    every resubmit after an edit has one. Warning here would fire on the normal
    path 24 times a run and teach everyone to ignore the channel."""
    store = LocalStore(tmp_path)
    note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1")
    assert note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v9") == []


def test_the_first_corpus_is_not_overwritten_by_a_later_one(tmp_path: Path) -> None:
    """The record is what the experiment *was*. Letting the second run replace it
    means the third run compares against the second and the collision goes
    quiet — the bug hides itself after one resume."""
    store = LocalStore(tmp_path)
    note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1")
    note_experiment_identity(store, "exp", corpus_fingerprint="bbb", code_version="v1")
    third = note_experiment_identity(store, "exp", corpus_fingerprint="bbb", code_version="v1")

    assert third, "the collision went silent on the third run"
    assert json.loads(store.read_text("experiments/exp/experiment.json"))["corpus_fingerprint"] == (
        "aaa"
    )


def test_an_unknown_fingerprint_is_not_worth_warning_about(tmp_path: Path) -> None:
    """A local `run-shard` with no --corpus-fingerprint passes "". Comparing it
    against a recorded one would warn on every ad-hoc run, and comparing two
    empties says nothing anyway."""
    store = LocalStore(tmp_path)
    note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1")
    assert note_experiment_identity(store, "exp", corpus_fingerprint="", code_version="v1") == []


def test_a_torn_record_does_not_stop_the_shard(tmp_path: Path) -> None:
    """This runs at the head of a 90-minute shard. A diagnostics file that
    cannot be parsed is not a reason to refuse to do the work."""
    store = LocalStore(tmp_path)
    store.write_text("experiments/exp/experiment.json", "{not json")
    assert note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1") == []


def test_a_changed_question_set_warns_too(tmp_path: Path) -> None:
    """Same hazard as a changed corpus, arriving by a different door: resume
    appends cells for different questions to one experiment's shards."""
    store = LocalStore(tmp_path)
    note_experiment_identity(
        store, "exp", corpus_fingerprint="aaa", code_version="v1", questions_fingerprint="q1"
    )
    warnings = note_experiment_identity(
        store, "exp", corpus_fingerprint="aaa", code_version="v1", questions_fingerprint="q2"
    )

    assert len(warnings) == 1
    assert "q1" in warnings[0]
    assert "q2" in warnings[0]


def test_both_identities_can_change_at_once(tmp_path: Path) -> None:
    """Two warnings, not one merged sentence. They have different causes and
    different fixes, and a reader needs to know it is both."""
    store = LocalStore(tmp_path)
    note_experiment_identity(
        store, "exp", corpus_fingerprint="aaa", code_version="v1", questions_fingerprint="q1"
    )
    warnings = note_experiment_identity(
        store, "exp", corpus_fingerprint="bbb", code_version="v1", questions_fingerprint="q2"
    )
    assert len(warnings) == 2


def test_an_experiment_recorded_before_questions_were_tracked_still_reads(tmp_path: Path) -> None:
    """`full-01`'s record predates the field. An absent value means unknown, so
    it must stay quiet rather than warn on every shard of a resumed sweep."""
    store = LocalStore(tmp_path)
    store.write_text(
        "experiments/exp/experiment.json",
        json.dumps({"experiment_id": "exp", "corpus_fingerprint": "aaa", "code_version": "v1"}),
    )
    assert (
        note_experiment_identity(
            store, "exp", corpus_fingerprint="aaa", code_version="v1", questions_fingerprint="q9"
        )
        == []
    )


def test_the_question_fingerprint_is_recorded_on_a_first_run(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    note_experiment_identity(
        store, "exp", corpus_fingerprint="aaa", code_version="v1", questions_fingerprint="q1"
    )
    record = json.loads(store.read_text("experiments/exp/experiment.json"))
    assert record["questions_fingerprint"] == "q1"


def test_the_first_principal_is_recorded(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    note_experiment_identity(
        store, "exp", corpus_fingerprint="aaa", code_version="v1", principal="sa@p.iam"
    )
    record = json.loads(store.read_text("experiments/exp/experiment.json"))
    assert record["principal"] == "sa@p.iam"


def test_a_changed_principal_warns(tmp_path: Path) -> None:
    """`full-01`'s misreading: the pipeline SA's cells compared against a
    developer's re-run, two searches that return different tables."""
    store = LocalStore(tmp_path)
    note_experiment_identity(
        store, "exp", corpus_fingerprint="aaa", code_version="v1", principal="sa@p.iam"
    )
    warnings = note_experiment_identity(
        store, "exp", corpus_fingerprint="aaa", code_version="v1", principal="dev@example.com"
    )
    assert len(warnings) == 1
    assert "sa@p.iam" in warnings[0]
    assert "dev@example.com" in warnings[0]


def test_an_unknown_principal_is_not_a_change(tmp_path: Path) -> None:
    """A record written before the field existed, or a run that could not tell
    who it was, must not warn on every shard."""
    store = LocalStore(tmp_path)
    note_experiment_identity(store, "exp", corpus_fingerprint="aaa", code_version="v1")
    assert (
        note_experiment_identity(
            store, "exp", corpus_fingerprint="aaa", code_version="v1", principal="sa@p.iam"
        )
        == []
    )
