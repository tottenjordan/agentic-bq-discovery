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
