"""The metrics port must reproduce upstream's published numbers exactly.

Our whole "reproduce first, then extend" plan depends on our scorer agreeing
with theirs. If a formula drifts, we would not discover it as a bug — we would
discover it as an unexplained difference from their results, months later, and
have no way to tell which side was wrong.

So the golden test runs our scorer over upstream's own 3,000-cell results file
(vendored gzipped in ``fixtures/``) and asserts we land on their published
table. The hand-computed unit tests below pin the individual formulas.
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from bq_context.scoring.metrics import (
    GAIN,
    RERANK_K,
    score_cell,
    summarize_by_approach,
    tier_response,
)

FIXTURE = Path(__file__).parent / "fixtures" / "upstream_results.json.gz"

#: Upstream's published headline table (examples/results.md), to 3 decimals:
#: approach -> (mean discovery recall, mean final recall, rerank loss)
PUBLISHED = {
    "bq_tools": (1.000, 0.993, 0.007),
    "kc_search": (0.967, 0.949, 0.018),
    "kc_context": (1.000, 0.940, 0.060),
    "context_prefilter": (1.000, 0.976, 0.024),
    "semantic_context": (0.967, 0.945, 0.022),
    "search_direct": (0.967, 0.967, 0.000),
}

#: Upstream's published cost/latency table: approach -> (p50, p95, median tokens)
PUBLISHED_COST = {
    "bq_tools": (39.48, 64.24, 3470),
    "kc_search": (8.57, 11.74, 8388),
    "kc_context": (3.99, 6.90, 44020),
    "context_prefilter": (14.60, 27.66, 10749),
    "semantic_context": (6.06, 9.84, 14909),
    "search_direct": (2.09, 2.40, 0),
}


@lru_cache(maxsize=1)
def upstream_cells() -> tuple[dict[str, Any], ...]:
    with gzip.open(FIXTURE, "rt") as fh:
        return tuple(json.load(fh)["cells"])


@lru_cache(maxsize=1)
def upstream_scores() -> tuple[Any, ...]:
    return tuple(s for cell in upstream_cells() if (s := score_cell(cell)))


# ---------------------------------------------------------------------------
# Golden: reproduce upstream's published tables
# ---------------------------------------------------------------------------
def test_fixture_is_the_full_factorial() -> None:
    cells = upstream_cells()
    assert len(cells) == 3000
    assert len(upstream_scores()) == 3000, "upstream reported 0 error cells"


@pytest.mark.parametrize("approach", sorted(PUBLISHED))
def test_reproduces_published_recall_table(approach: str) -> None:
    expected_discovery, expected_final, expected_loss = PUBLISHED[approach]
    got = summarize_by_approach(list(upstream_scores()))[approach]

    assert got.discovery_recall == pytest.approx(expected_discovery, abs=0.001)
    assert got.final_recall == pytest.approx(expected_final, abs=0.001)
    assert got.rerank_loss == pytest.approx(expected_loss, abs=0.001)


@pytest.mark.parametrize("approach", sorted(PUBLISHED_COST))
def test_reproduces_published_cost_table(approach: str) -> None:
    expected_p50, expected_p95, expected_tokens = PUBLISHED_COST[approach]
    got = summarize_by_approach(list(upstream_scores()))[approach]

    assert got.latency_p50 == pytest.approx(expected_p50, abs=0.01)
    assert got.latency_p95 == pytest.approx(expected_p95, abs=0.01)
    assert got.reranker_tokens == pytest.approx(expected_tokens, abs=1)


def test_reproduces_the_flat_tier_response_upstream_published() -> None:
    """Median aggregation, which is what upstream's tier table used."""
    deltas = tier_response(list(upstream_scores()), aggregate="median")
    assert len(deltas) == 6
    assert all(delta == pytest.approx(0.0, abs=0.001) for delta in deltas.values())


def test_the_flat_response_is_partly_a_median_artifact() -> None:
    """The mean moves where the median cannot.

    Worth pinning: upstream's headline "+0% enrichment response" is reported on
    a metric that saturates at 1.0, so the median is structurally unable to show
    movement. The same cells scored by mean do move — which is an argument about
    measurement, not about enrichment.
    """
    medians = tier_response(list(upstream_scores()), aggregate="median")
    means = tier_response(list(upstream_scores()), aggregate="mean")

    assert all(v == pytest.approx(0.0, abs=1e-9) for v in medians.values())
    assert any(abs(v) > 0.001 for v in means.values()), means


def test_search_direct_has_exactly_zero_rerank_loss() -> None:
    """It applies no reranker, so discovery and final recall must be identical.

    A structural invariant, not an empirical finding: if these ever diverge,
    something is writing ranked tables for an approach that does not rank.
    """
    got = summarize_by_approach(list(upstream_scores()))["search_direct"]
    assert got.discovery_recall == pytest.approx(got.final_recall)
    assert got.rerank_loss == pytest.approx(0.0, abs=1e-9)
    assert got.reranker_calls == 0.0


# ---------------------------------------------------------------------------
# Unit: the individual formulas, hand-computed
# ---------------------------------------------------------------------------
def make_cell(ranked: list[str], nominated: list[str], **relevance: list[str]) -> dict[str, Any]:
    return {
        "cell_key": "q1|kc_search|tier0|run0",
        "question_id": "q1",
        "approach": "kc_search",
        "tier": 0,
        "category": "single-table",
        "status": "ok",
        "relevance": relevance,
        "ranked_tables": [{"table_id": f"p.d.{t}", "rank": i + 1} for i, t in enumerate(ranked)],
        "nominated": [f"p.d.{t}" for t in nominated],
    }


def test_error_cells_score_none() -> None:
    assert score_cell({"status": "error", "error_message": "boom"}) is None
    assert score_cell({"error": "upstream style"}) is None


def test_perfect_retrieval() -> None:
    score = score_cell(make_cell(["a", "b"], ["a", "b"], must_have=["a", "b"]))
    assert score is not None
    assert score.discovery_recall == 1.0
    assert score.final_recall == 1.0
    assert score.precision == 1.0
    assert score.ndcg == pytest.approx(1.0)


def test_recall_counts_only_must_have() -> None:
    score = score_cell(make_cell(["a"], ["a"], must_have=["a", "b"], nice_to_have=["c"]))
    assert score is not None
    assert score.final_recall == pytest.approx(0.5)


def test_distractors_dilute_precision() -> None:
    """Ranking a distractor alongside the answer halves precision."""
    score = score_cell(make_cell(["a", "bad"], ["a", "bad"], must_have=["a"], distractor=["bad"]))
    assert score is not None
    assert score.final_recall == 1.0
    assert score.precision == pytest.approx(0.5)


def test_precision_uses_the_deduped_set() -> None:
    """A reranker repeating a table must not be able to inflate precision."""
    score = score_cell(make_cell(["a", "a", "a"], ["a"], must_have=["a"]))
    assert score is not None
    assert score.precision == pytest.approx(1.0)


def test_discovery_and_final_recall_diverge_when_rerank_drops_a_table() -> None:
    """The decomposition the whole experiment rests on."""
    score = score_cell(make_cell(["a"], ["a", "b"], must_have=["a", "b"]))
    assert score is not None
    assert score.discovery_recall == 1.0
    assert score.final_recall == pytest.approx(0.5)


def test_ndcg_rewards_putting_the_must_have_first() -> None:
    good = score_cell(make_cell(["a", "c"], [], must_have=["a"], nice_to_have=["c"]))
    bad = score_cell(make_cell(["c", "a"], [], must_have=["a"], nice_to_have=["c"]))
    assert good is not None
    assert bad is not None
    assert good.ndcg == pytest.approx(1.0)
    assert bad.ndcg < good.ndcg


def test_ndcg_only_considers_the_top_k() -> None:
    """A must-have ranked below the cutoff earns no gain."""
    filler = [f"x{i}" for i in range(RERANK_K)]
    score = score_cell(make_cell([*filler, "a"], ["a"], must_have=["a"]))
    assert score is not None
    assert score.ndcg == pytest.approx(0.0)
    # ...but recall is measured over the whole list, so it still counts there.
    assert score.final_recall == 1.0


def test_empty_result_scores_zero_rather_than_being_dropped() -> None:
    """An approach that returned nothing is a real result, not a missing one."""
    score = score_cell(make_cell([], [], must_have=["a"]))
    assert score is not None
    assert score.final_recall == 0.0
    assert score.precision == 0.0
    assert score.ndcg == 0.0


def test_question_with_no_must_have_is_vacuously_satisfied() -> None:
    score = score_cell(make_cell([], [], nice_to_have=["c"]))
    assert score is not None
    assert score.final_recall == 1.0
    assert score.discovery_recall == 1.0


def test_short_name_matching_ignores_the_tier_dataset() -> None:
    """Scoring keys on the short table name across all four tier datasets."""
    for tier in range(4):
        cell = make_cell(["a"], ["a"], must_have=["a"])
        cell["ranked_tables"] = [{"table_id": f"proj.bigquery_context_tier{tier}.a", "rank": 1}]
        score = score_cell(cell)
        assert score is not None
        assert score.final_recall == 1.0


def test_gain_constants_match_upstream() -> None:
    assert GAIN == {"must_have": 2.0, "nice_to_have": 1.0, "distractor": 0.0}
    assert RERANK_K == 5
