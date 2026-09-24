"""The shard loop's durability guarantees.

These exercise the real ShardRunner against a real LocalStore with a fake cell
executor. The executor is the only thing stubbed, because everything worth
testing here — checkpointing, resume, partial failure — has nothing to do with
running an agent.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from bq_context.runner.backoff import CircuitBreaker
from bq_context.runner.models import Cell, ShardSpec
from bq_context.runner.resume import completed_keys, load_shard_records, shard_prefix
from bq_context.runner.shard import ShardRunner
from bq_context.runner.store import LocalStore

QUESTIONS: dict[str, dict[str, Any]] = {
    f"q{i}": {"id": f"q{i}", "category": "single-table", "question": f"question {i}?"}
    for i in range(1, 6)
}


def make_spec(runs: int = 1, n_questions: int = 3, **kw: Any) -> ShardSpec:
    return ShardSpec(
        experiment_id=kw.pop("experiment_id", "test-exp"),
        tier=kw.pop("tier", 3),
        approach=kw.pop("approach", "kc_search"),
        question_ids=[f"q{i}" for i in range(1, n_questions + 1)],
        runs=runs,
        code_version="v1",
        **kw,
    )


class FakeExecutor:
    """Returns an ok cell, unless the question id is in ``fail_on``."""

    def __init__(self, spec: ShardSpec, fail_on: set[str] | None = None) -> None:
        self.spec = spec
        self.fail_on = fail_on or set()
        self.calls: list[str] = []

    async def __call__(self, question: dict[str, Any], run_idx: int) -> Cell:
        qid = question["id"]
        self.calls.append(f"{qid}|run{run_idx}")
        failed = qid in self.fail_on
        return Cell(
            cell_key=f"{qid}|{self.spec.approach}|tier{self.spec.tier}|run{run_idx}",
            question_id=qid,
            approach=self.spec.approach,
            tier=self.spec.tier,
            run_idx=run_idx,
            status="error" if failed else "ok",
            code_version=self.spec.code_version,
            error_message="boom" if failed else "",
            latency_s=0.01,
            reranker_total_tokens=0 if failed else 1234,
        )


async def test_runs_every_planned_cell(tmp_path: Path) -> None:
    spec = make_spec(runs=2, n_questions=3)
    store = LocalStore(tmp_path)
    executor = FakeExecutor(spec)

    result = await ShardRunner(spec, store, executor, QUESTIONS, heartbeat_seconds=1e6).run()

    assert result.planned == 6
    assert result.executed == 6
    assert result.succeeded == 6
    assert result.failed == 0
    assert result.complete
    assert len(executor.calls) == 6
    assert store.exists(f"{shard_prefix(spec)}/_SUCCESS")


async def test_resume_skips_completed_and_reruns_errors(tmp_path: Path) -> None:
    """The self-healing property: a second pass retries only what failed."""
    spec = make_spec(runs=1, n_questions=3)
    store = LocalStore(tmp_path)

    first = FakeExecutor(spec, fail_on={"q2"})
    r1 = await ShardRunner(spec, store, first, QUESTIONS, heartbeat_seconds=1e6).run()
    assert r1.succeeded == 2
    assert r1.failed == 1
    assert not r1.complete
    assert store.exists(f"{shard_prefix(spec)}/_FAILED")

    second = FakeExecutor(spec)  # q2 now succeeds
    r2 = await ShardRunner(spec, store, second, QUESTIONS, heartbeat_seconds=1e6).run()

    assert second.calls == ["q2|run0"], "only the failed cell should re-run"
    assert r2.already_done == 2
    assert r2.executed == 1
    assert r2.complete
    assert store.exists(f"{shard_prefix(spec)}/_SUCCESS")

    final = completed_keys(load_shard_records(store, spec))
    assert final == set(spec.planned_cells())


async def test_a_completed_shard_does_no_work(tmp_path: Path) -> None:
    spec = make_spec(runs=1, n_questions=2)
    store = LocalStore(tmp_path)

    await ShardRunner(spec, store, FakeExecutor(spec), QUESTIONS, heartbeat_seconds=1e6).run()

    again = FakeExecutor(spec)
    result = await ShardRunner(spec, store, again, QUESTIONS, heartbeat_seconds=1e6).run()

    assert again.calls == []
    assert result.executed == 0
    assert result.complete


async def test_each_run_writes_a_new_attempt_file(tmp_path: Path) -> None:
    """Retries must never clobber a prior attempt's records."""
    spec = make_spec(runs=1, n_questions=2)
    store = LocalStore(tmp_path)

    await ShardRunner(
        spec, store, FakeExecutor(spec, fail_on={"q1", "q2"}), QUESTIONS, heartbeat_seconds=1e6
    ).run()
    await ShardRunner(spec, store, FakeExecutor(spec), QUESTIONS, heartbeat_seconds=1e6).run()

    attempts = [p for p in store.list_paths(shard_prefix(spec)) if "attempt-" in p]
    assert len(attempts) == 2
    # Both attempts' records survive; dedupe happens at read time, not write time.
    assert len(load_shard_records(store, spec)) == 4


async def test_an_executor_exception_becomes_an_error_cell(tmp_path: Path) -> None:
    """A bug in one cell costs one cell, not the shard."""
    spec = make_spec(runs=1, n_questions=3)
    store = LocalStore(tmp_path)

    class Exploding(FakeExecutor):
        async def __call__(self, question: dict[str, Any], run_idx: int) -> Cell:
            if question["id"] == "q2":
                msg = "unexpected"
                raise RuntimeError(msg)
            return await super().__call__(question, run_idx)

    result = await ShardRunner(spec, store, Exploding(spec), QUESTIONS, heartbeat_seconds=1e6).run()

    assert result.executed == 3, "the shard kept going past the exception"
    assert result.failed == 1
    records = {c.cell_key: c for c in load_shard_records(store, spec)}
    bad = records["q2|kc_search|tier3|run0"]
    assert bad.status == "error"
    assert bad.error_type == "RuntimeError"


async def test_checkpoints_during_the_run_not_just_at_the_end(tmp_path: Path) -> None:
    """Bounds worst-case loss. Without this, a crash costs the whole shard."""
    spec = make_spec(runs=1, n_questions=5)
    store = LocalStore(tmp_path)
    seen_midway: list[int] = []

    class Observing(FakeExecutor):
        async def __call__(self, question: dict[str, Any], run_idx: int) -> Cell:
            # Count what is durable remotely *before* this cell runs.
            seen_midway.append(len(load_shard_records(store, spec)))
            return await super().__call__(question, run_idx)

    await ShardRunner(
        spec,
        store,
        Observing(spec),
        QUESTIONS,
        upload_every_cells=2,
        heartbeat_seconds=1e6,
    ).run()

    assert max(seen_midway) >= 2, f"nothing was uploaded mid-run: {seen_midway}"


async def test_circuit_breaker_abandons_a_systematically_broken_shard(
    tmp_path: Path,
) -> None:
    """Bad IAM should cost seconds, not the whole retry budget."""
    spec = make_spec(runs=5, n_questions=5)  # 25 cells planned
    store = LocalStore(tmp_path)
    executor = FakeExecutor(spec, fail_on=set(QUESTIONS))  # everything fails

    result = await ShardRunner(
        spec,
        store,
        executor,
        QUESTIONS,
        breaker=CircuitBreaker(max_consecutive=3),
        heartbeat_seconds=1e6,
    ).run()

    assert result.aborted
    assert "consecutive" in result.abort_reason
    assert result.executed == 3, "stopped at the breaker, not after all 25"
    assert not result.complete
    assert store.exists(f"{shard_prefix(spec)}/_FAILED")
    # Work done before the trip is still durable and still resumable.
    assert len(load_shard_records(store, spec)) == 3


async def test_breaker_does_not_fire_on_an_occasional_failure(tmp_path: Path) -> None:
    spec = make_spec(runs=2, n_questions=5)  # 10 cells, 2 will fail
    store = LocalStore(tmp_path)

    result = await ShardRunner(
        spec, store, FakeExecutor(spec, fail_on={"q3"}), QUESTIONS, heartbeat_seconds=1e6
    ).run()

    assert not result.aborted
    assert result.executed == 10
    assert result.failed == 2


@pytest.mark.parametrize("upload_every", [1, 3, 100])
async def test_all_records_land_regardless_of_upload_cadence(
    tmp_path: Path, upload_every: int
) -> None:
    spec = make_spec(runs=1, n_questions=5)
    store = LocalStore(tmp_path)

    await ShardRunner(
        spec,
        store,
        FakeExecutor(spec),
        QUESTIONS,
        upload_every_cells=upload_every,
        heartbeat_seconds=1e6,
    ).run()

    assert completed_keys(load_shard_records(store, spec)) == set(spec.planned_cells())


async def test_a_run_persists_its_summary(tmp_path: Path) -> None:
    """The shard's own record must outlive the process.

    cache_warm_s and abort_reason exist nowhere else once it exits: the first
    had to be recovered from Cloud Logging after the fact, and the second is the
    entire diagnosis for a shard the circuit breaker stopped.
    """
    from bq_context.runner.summaries import load_summaries

    spec = make_spec()
    store = LocalStore(tmp_path)
    await ShardRunner(
        spec, store, FakeExecutor(spec), QUESTIONS, heartbeat_seconds=1e6, cache_warm_s=2.5
    ).run()

    (summary,) = load_summaries(store, spec.experiment_id)
    assert summary.shard_id == spec.shard_id
    assert summary.cache_warm_s == 2.5, "the number that was previously dropped"
    assert summary.succeeded == summary.planned


async def test_a_summary_is_written_even_when_there_was_nothing_to_do(tmp_path: Path) -> None:
    """The early-return branch is the one a second write would be forgotten on."""
    from bq_context.runner.summaries import load_summaries

    spec = make_spec()
    store = LocalStore(tmp_path)
    await ShardRunner(spec, store, FakeExecutor(spec), QUESTIONS, heartbeat_seconds=1e6).run()
    await ShardRunner(spec, store, FakeExecutor(spec), QUESTIONS, heartbeat_seconds=1e6).run()

    summaries = load_summaries(store, spec.experiment_id)
    assert len(summaries) == 2, "the no-op resume must record a summary too"
    assert summaries[-1].executed == 0


# ---------------------------------------------------------------------------
# A shard that failed must say so in its exit code
#
# hard-full-01 lost one cell of 3,000 to a transient 500. The shard noticed --
# it wrote `_FAILED: 124 ok, 1 failed` -- and then exited 0. Vertex recorded the
# task SUCCEEDED, KFP cached it, and the resubmit was a cache hit: the shard
# never ran, resume never got a chance, and the cell was unrecoverable except by
# `--no-cache`.
#
# The marker was written and then ignored. Exiting non-zero lets the existing
# `set_retry(num_retries=2)` do its job: resume skips the 124 good cells and only
# the failed one is re-attempted.
# ---------------------------------------------------------------------------
async def test_an_incomplete_shard_is_not_complete(tmp_path: Path) -> None:
    """The property already existed and nothing acted on it."""
    spec = make_spec(runs=1, n_questions=3)
    store = LocalStore(tmp_path)
    executor = FakeExecutor(spec, fail_on={"q2"})
    result = await ShardRunner(spec, store, executor, QUESTIONS, heartbeat_seconds=1e6).run()
    assert result.failed == 1
    assert result.complete is False


async def test_a_clean_shard_is_complete(tmp_path: Path) -> None:
    spec = make_spec(runs=1, n_questions=3)
    store = LocalStore(tmp_path)
    result = await ShardRunner(
        spec, store, FakeExecutor(spec), QUESTIONS, heartbeat_seconds=1e6
    ).run()
    assert result.complete is True


async def test_the_fingerprint_reaches_the_summary(tmp_path: Path) -> None:
    """THE comparability gap.

    `_result` copied `code_version` from the spec and silently dropped
    `corpus_fingerprint`, so every shard summary recorded `""`. The value is
    computed in preflight and threaded into each shard as a cache-key input, then
    thrown away -- which leaves nothing in the stored results saying *which
    corpus* produced them. Two corpora in one sink are then distinguishable only
    by an experiment-id naming convention.
    """
    spec = make_spec(runs=1, n_questions=2, corpus_fingerprint="13f9fcb47deb5c32")
    store = LocalStore(tmp_path)
    result = await ShardRunner(
        spec, store, FakeExecutor(spec), QUESTIONS, heartbeat_seconds=1e6
    ).run()
    assert result.corpus_fingerprint == "13f9fcb47deb5c32"

    written = json.loads(store.read_text(f"{shard_prefix(spec)}/summary-0001.json"))
    assert written["corpus_fingerprint"] == "13f9fcb47deb5c32"
