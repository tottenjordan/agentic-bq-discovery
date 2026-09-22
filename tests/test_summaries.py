"""Per-shard summaries are persisted, not just returned.

`ShardResult` carries the only record of several things that exist nowhere else
once the process exits: how long the context cache took to warm, whether the
circuit breaker tripped and why, and how the shard's planned/done/failed counts
split. Its docstring said "written alongside its attempt files". It wasn't — it
was returned in memory, printed to the console by `run-shard`, and dropped.

The cost was concrete. `cache_warm_s` was the number the design named as the one
that would flip the sharding topology decision, and answering that question
after the fact meant scraping Cloud Logging. Abort reasons are worse: a shard
that trips the circuit breaker records *why* in `abort_reason`, and that string
is the whole diagnosis.

One summary per attempt, not one per shard. A resumed run's first attempt is
usually the interesting one — it holds the failure that caused the resume — and
a single `summary.json` would be overwritten by the attempt that succeeded.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bq_context.runner.models import ShardResult, ShardSpec
from bq_context.runner.resume import shard_prefix, summary_path
from bq_context.runner.store import LocalStore
from bq_context.runner.summaries import load_summaries, write_summary


@pytest.fixture
def spec() -> ShardSpec:
    return ShardSpec(
        experiment_id="exp",
        tier=3,
        approach="kc_context",
        question_ids=["q1", "q2"],
        runs=1,
        code_version="abc1234",
    )


def _result(spec: ShardSpec, **over: object) -> ShardResult:
    base = {
        "shard_id": spec.shard_id,
        "experiment_id": spec.experiment_id,
        "tier": spec.tier,
        "approach": spec.approach,
        "code_version": spec.code_version,
        "planned": 2,
        "already_done": 0,
        "executed": 2,
        "succeeded": 2,
        "failed": 0,
        "cache_warm_s": 1.9,
        "elapsed_s": 12.5,
        "attempt_path": f"{shard_prefix(spec)}/attempt-0001.jsonl",
    }
    return ShardResult(**{**base, **over})


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
def test_the_summary_pairs_with_its_attempt() -> None:
    assert summary_path("x/shards/s/attempt-0007.jsonl") == "x/shards/s/summary-0007.json"


def test_an_unknown_attempt_path_still_yields_a_usable_path() -> None:
    """Never raise here: failing to name the file must not fail the shard."""
    assert summary_path("").endswith(".json")


def test_the_summary_is_not_mistaken_for_an_attempt_file(spec: ShardSpec) -> None:
    """Resume globs the shard directory; a summary must not be read as cells.

    `load_shard_records` filters on the attempt pattern, so this is really a
    guard on the filename staying outside it.
    """
    from bq_context.runner.resume import _ATTEMPT_RE

    assert not _ATTEMPT_RE.search(summary_path(f"{shard_prefix(spec)}/attempt-0001.jsonl"))


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------
def test_a_summary_round_trips(tmp_path: Path, spec: ShardSpec) -> None:
    store = LocalStore(tmp_path)
    write_summary(store, _result(spec))

    (loaded,) = load_summaries(store, "exp")
    assert loaded.shard_id == spec.shard_id
    assert loaded.cache_warm_s == 1.9
    assert loaded.elapsed_s == 12.5


def test_the_number_that_was_previously_lost_is_now_durable(
    tmp_path: Path, spec: ShardSpec
) -> None:
    """cache_warm_s was measured, logged, and then dropped on the floor.

    Recovering it for the first full run meant scraping Cloud Logging.
    """
    store = LocalStore(tmp_path)
    write_summary(store, _result(spec, cache_warm_s=2.4))
    assert load_summaries(store, "exp")[0].cache_warm_s == 2.4


def test_an_abort_reason_survives_the_process(tmp_path: Path, spec: ShardSpec) -> None:
    """The whole diagnosis for a tripped circuit breaker is this one string."""
    store = LocalStore(tmp_path)
    write_summary(store, _result(spec, aborted=True, abort_reason="20 consecutive failures"))

    loaded = load_summaries(store, "exp")[0]
    assert loaded.aborted
    assert loaded.abort_reason == "20 consecutive failures"
    assert not loaded.complete


def test_each_attempt_gets_its_own_summary(tmp_path: Path, spec: ShardSpec) -> None:
    """A resumed shard must not overwrite the record of why it was resumed."""
    store = LocalStore(tmp_path)
    prefix = shard_prefix(spec)
    write_summary(
        store,
        _result(spec, failed=1, succeeded=1, attempt_path=f"{prefix}/attempt-0001.jsonl"),
    )
    write_summary(
        store,
        _result(spec, already_done=1, executed=1, attempt_path=f"{prefix}/attempt-0002.jsonl"),
    )

    summaries = sorted(load_summaries(store, "exp"), key=lambda s: s.attempt_path)
    assert len(summaries) == 2
    assert summaries[0].failed == 1, "the first attempt's failure must survive the resume"
    assert summaries[1].failed == 0


def test_writing_a_summary_never_raises(
    tmp_path: Path, spec: ShardSpec, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A summary is diagnostics. Losing one must not fail a shard that worked."""
    store = LocalStore(tmp_path)

    def _boom(*_a: object, **_k: object) -> None:
        message = "disk went away"
        raise OSError(message)

    monkeypatch.setattr(store, "write_text", _boom)
    write_summary(store, _result(spec))  # must not raise


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
def test_no_summaries_is_not_an_error(tmp_path: Path) -> None:
    """Runs made before this existed have none, and must still merge."""
    assert load_summaries(LocalStore(tmp_path), "exp") == []


def test_a_corrupt_summary_is_skipped_rather_than_fatal(tmp_path: Path, spec: ShardSpec) -> None:
    """One torn file must not make a whole experiment unreadable."""
    store = LocalStore(tmp_path)
    write_summary(store, _result(spec))
    store.write_text(f"{shard_prefix(spec)}/summary-0002.json", "{not json")

    assert len(load_summaries(store, "exp")) == 1


def test_summaries_are_scoped_to_their_experiment(tmp_path: Path, spec: ShardSpec) -> None:
    store = LocalStore(tmp_path)
    write_summary(store, _result(spec))
    other = spec.model_copy(update={"experiment_id": "other"})
    write_summary(store, _result(other, attempt_path=f"{shard_prefix(other)}/attempt-0001.jsonl"))

    assert [s.experiment_id for s in load_summaries(store, "exp")] == ["exp"]


def test_the_summary_is_valid_json_on_disk(tmp_path: Path, spec: ShardSpec) -> None:
    """Readable with `gcloud storage cat` and jq, not only through our loader."""
    store = LocalStore(tmp_path)
    write_summary(store, _result(spec))
    raw = store.read_text(summary_path(f"{shard_prefix(spec)}/attempt-0001.jsonl"))
    assert json.loads(raw)["approach"] == "kc_context"


def test_only_summary_files_are_read(tmp_path: Path, spec: ShardSpec) -> None:
    """Regression: the filter used to be a substring test on the whole path.

    The experiment id is part of that path, so an experiment named
    `summary-smoke` made every `_SUCCESS` marker and attempt file match, and the
    loader tried to parse them as JSON. Found by naming a smoke run exactly
    that, entirely by accident.
    """
    store = LocalStore(tmp_path)
    smoke = spec.model_copy(update={"experiment_id": "summary-smoke"})
    prefix = shard_prefix(smoke)
    write_summary(store, _result(smoke, attempt_path=f"{prefix}/attempt-0001.jsonl"))
    store.write_text(f"{prefix}/_SUCCESS", "")
    store.write_text(f"{prefix}/attempt-0001.jsonl", '{"cell_key": "x"}\n')

    assert len(load_summaries(store, "summary-smoke")) == 1
