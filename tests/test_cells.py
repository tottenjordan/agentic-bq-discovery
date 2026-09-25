"""The cell executor's pure logic.

`runner/cells.py` is the path that produced all 3,000 cells, and most of it
needs ADK and a live corpus. Two pieces do not, and both decide something the
recorded data cannot tell you afterwards:

- `_parse_ranked` returns `[]` when the reranker's output will not parse. In the
  results that is indistinguishable from a genuine miss: `ranked_count` 0 and
  recall 0, exactly like a search that found nothing. The full-01 audit reasoned
  about 15 such cells and attributed them to empty search results; that
  conclusion only holds if parse failures are loud, which is what the warning
  here is for.
- `needs_cache` decides whether a shard spends the cache-warm cost. Getting it
  wrong costs either 24 wasted warms or an approach reading an empty cache.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from bq_context.runner.cells import APPROACHES, AdkCellExecutor, needs_cache
from bq_context.scoring.metrics import APPROACH_ORDER

VALID = {
    "question": "q",
    "top_k": 5,
    "ranked_tables": [
        {
            "table_id": "p.d.weather_stations",
            "rank": 1,
            "confidence": 0.9,
            "reasoning": "r",
            "discovery_method": "kc_search",
        }
    ],
}


# ---------------------------------------------------------------------------
# _parse_ranked
# ---------------------------------------------------------------------------
def test_a_valid_response_keeps_only_the_scored_fields() -> None:
    assert AdkCellExecutor._parse_ranked(json.dumps(VALID), "kc_search") == [
        {"table_id": "p.d.weather_stations", "rank": 1, "confidence": 0.9}
    ]


def test_an_empty_response_is_not_an_error() -> None:
    """A search that found nothing legitimately produces no ranking."""
    assert AdkCellExecutor._parse_ranked("", "kc_search") == []


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        ("not json at all", "malformed"),
        ("{}", "missing required fields"),
        ('{"question": "q", "top_k": 5, "ranked_tables": [{"rank": 1}]}', "incomplete table"),
        ('["a", "list"]', "right JSON, wrong shape"),
    ],
)
def test_unparseable_output_yields_no_ranking(raw: str, why: str) -> None:
    assert AdkCellExecutor._parse_ranked(raw, "kc_search") == [], why


def test_a_parse_failure_is_logged_rather_than_silent(caplog: pytest.LogCaptureFixture) -> None:
    """The distinction the audit depends on.

    An unparseable response and an empty search both record `ranked_count` 0, so
    the only way to tell them apart after the fact is this warning. If it is
    ever dropped, a systematic reranker-output regression would read as poor
    retrieval and be attributed to the wrong cause.
    """
    with caplog.at_level(logging.WARNING):
        AdkCellExecutor._parse_ranked("{not json}", "kc_search")
    assert any(r.levelno >= logging.WARNING for r in caplog.records)
    assert "kc_search" in caplog.text


def test_an_empty_response_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    """The converse: a legitimate empty result must not look like a failure."""
    with caplog.at_level(logging.WARNING):
        AdkCellExecutor._parse_ranked("", "kc_search")
    assert not caplog.records


# ---------------------------------------------------------------------------
# needs_cache
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("approach", ["kc_context", "context_prefilter", "semantic_context"])
def test_cache_reading_approaches_warm_the_cache(approach: str) -> None:
    assert needs_cache(approach)


@pytest.mark.parametrize("approach", ["bq_tools", "kc_search", "search_direct"])
def test_per_request_approaches_do_not_warm_the_cache(approach: str) -> None:
    """Warming for these is pure cost: 24 shards paying for metadata never read."""
    assert not needs_cache(approach)


def test_every_approach_has_a_cache_decision() -> None:
    assert {a for a in APPROACHES if needs_cache(a)} | {
        a for a in APPROACHES if not needs_cache(a)
    } == set(APPROACHES)


# ---------------------------------------------------------------------------
# Cross-module consistency
# ---------------------------------------------------------------------------
def test_the_scorer_knows_every_runnable_approach() -> None:
    """Otherwise a new approach lands at the bottom of every report by accident.

    `APPROACH_ORDER` sorts unknown names to the end rather than dropping them,
    so this is a presentation bug rather than data loss — but a seventh approach
    should get a deliberate position, not an alphabetical one.
    """
    assert set(APPROACH_ORDER) == set(APPROACHES)


def test_there_are_exactly_six_approaches() -> None:
    """The experiment is a 6x4x25x5 factorial; a seventh changes every published count."""
    assert len(APPROACHES) == len(APPROACH_ORDER) == 6


# ---------------------------------------------------------------------------
# Transient failures must not become permanent holes
#
# hard-full-01 lost one cell of 3,000 to a single Google 500:
#
#   trap-q2|search_direct|tier1|run1   error   InternalServerError
#
# and that failed the whole 3-hour sweep, after finalize had already merged,
# scored, plotted and published every artifact -- `require_complete` counts only
# `status == "ok"` as present.
#
# The shard's own KFP retry could not help: the shard *succeeded*. A per-cell
# error is recorded and the loop moves on, so retry at shard granularity never
# fires for this failure. `retry_async` already existed and already knew a 500 is
# retryable; it was simply not wrapped around the agent invocation, only around
# the reranker call.
# ---------------------------------------------------------------------------
class _BoomError(Exception):
    """Carries an HTTP status the way google-genai surfaces one."""

    def __init__(self, code: int) -> None:
        super().__init__(f"{code} synthetic")
        self.code = code


def _executor() -> AdkCellExecutor:
    """An executor with no ADK behind it; only `_invoke` is exercised."""
    obj = AdkCellExecutor.__new__(AdkCellExecutor)
    obj.spec = SimpleNamespace(
        approach="kc_search",
        tier=1,
        code_version="test",
        corpus_fingerprint="test-corpus",
        principal="sa@test-project.iam",
    )
    obj.app_name = "bench_kc_search"
    return obj


async def _no_sleep(_seconds: float) -> None:
    """Skip the backoff wait; the delays are tested in test_backoff.py."""


QUESTION = {"id": "q1", "question": "text", "category": "single-table", "relevance": {}}


def _run(executor: AdkCellExecutor) -> object:
    import asyncio

    return asyncio.run(executor(QUESTION, run_idx=0))


def test_a_transient_failure_is_retried_and_the_cell_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression. One 500 used to cost the entire run."""
    calls = {"n": 0}

    async def _flaky() -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise _BoomError(500)
        return {"ranked_tables": []}

    ex = _executor()
    monkeypatch.setattr(ex, "_invoke", lambda _q: _flaky())
    monkeypatch.setattr("bq_context.runner.cells.retry_sleep", _no_sleep)
    cell = _run(ex)
    assert cell.status == "ok"
    assert cell.attempts == 2, "attempts must record the retry, or the cost caveat is invisible"
    assert calls["n"] == 2


def test_a_permanent_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 is not transient. Retrying it burns the budget and, unlike a transport
    failure, it already produced a billed response."""
    calls = {"n": 0}

    async def _denied() -> dict[str, object]:
        calls["n"] += 1
        raise _BoomError(403)

    ex = _executor()
    monkeypatch.setattr(ex, "_invoke", lambda _q: _denied())
    monkeypatch.setattr("bq_context.runner.cells.retry_sleep", _no_sleep)
    cell = _run(ex)
    assert cell.status == "error"
    assert calls["n"] == 1
    assert cell.attempts == 1


def test_an_exhausted_retry_budget_still_records_the_cell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cell that never succeeds must still be written, with its identity intact,
    or the merge cannot tell a failure from a cell that was never attempted."""

    async def _always() -> dict[str, object]:
        raise _BoomError(503)

    ex = _executor()
    monkeypatch.setattr(ex, "_invoke", lambda _q: _always())
    monkeypatch.setattr("bq_context.runner.cells.retry_sleep", _no_sleep)
    cell = _run(ex)
    assert cell.status == "error"
    assert cell.error_type == "_BoomError"
    assert cell.cell_key == "q1|kc_search|tier1|run0"
    assert cell.attempts > 1


def test_a_first_time_success_records_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common path must not claim a retry that did not happen."""

    async def _fine() -> dict[str, object]:
        return {"ranked_tables": []}

    ex = _executor()
    monkeypatch.setattr(ex, "_invoke", lambda _q: _fine())
    cell = _run(ex)
    assert cell.status == "ok"
    assert cell.attempts == 1


def test_a_cell_records_the_corpus_it_came_from() -> None:
    """Identity, like code_version: set on the blank cell so an *error* cell
    carries it too. A run's failures are part of its record, and a failure with
    no corpus attached cannot be compared against anything."""
    from types import SimpleNamespace

    executor = AdkCellExecutor.__new__(AdkCellExecutor)
    executor.spec = SimpleNamespace(
        approach="kc_search",
        tier=1,
        code_version="abc1234",
        corpus_fingerprint="13f9fcb47deb5c32",
        principal="sa@test-project.iam",
    )
    cell = executor._blank_cell(
        {"id": "q1", "question": "text", "category": "single-table", "relevance": {}},
        run_idx=0,
    )
    assert cell.corpus_fingerprint == "13f9fcb47deb5c32"
    assert cell.code_version == "abc1234"
    assert cell.principal == "sa@test-project.iam"
    assert cell.status == "error", "blank cells start as error until the run resolves"
