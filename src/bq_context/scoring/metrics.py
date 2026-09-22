"""Graded retrieval metrics for the factorial.

Ported from upstream's ``examples/build_results.py`` (vendored verbatim at
``scoring/upstream_build_results.py``) deliberately *without* changing any
formula, so our numbers are directly comparable to their published table. The
port is verified by a golden test that runs this scorer over upstream's own
3,000-cell results and reproduces their reported figures.

Scoring is graded, per ``experiments/GROUND_TRUTH.md``:

===============  ====  ==========================================
relevance class  gain  role
===============  ====  ==========================================
``must_have``       2  recall numerator; highest nDCG gain
``nice_to_have``    1  nDCG gain only; not counted in recall
``distractor``      0  dilutes precision when ranked
===============  ====  ==========================================

Aggregation policy, also matching upstream:

- **Means** for the discovery-vs-final headline. On an easy corpus most cells
  score 1.0, so a median saturates at 100% and hides the tail that matters.
- **Medians (+ IQR)** everywhere else, to resist a few pathological cells.

Operates on plain dicts rather than :class:`~bq_context.runner.models.Cell` so
the same code reads our JSONL and upstream's ``results.json``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

__all__ = [
    "APPROACH_ORDER",
    "CONTEXT_APPROACHES",
    "CONTROL_APPROACHES",
    "FULL_CORPUS_APPROACHES",
    "GAIN",
    "RERANK_K",
    "SEARCH_APPROACHES",
    "ApproachSummary",
    "CellScore",
    "score_cell",
    "summarize_by_approach",
    "summarize_by_tier",
    "tier_response",
]

#: nDCG graded gains by relevance class.
GAIN = {"must_have": 2.0, "nice_to_have": 1.0, "distractor": 0.0}

#: Rank cutoff for nDCG and precision.
RERANK_K = 5

#: A tier response needs at least a low and a high tier to be meaningful.
_MIN_TIERS_FOR_RESPONSE = 2

#: Canonical presentation order, matching upstream's 1..6 numbering. Reports are
#: meant to be read beside upstream's table, so alphabetical ordering (which puts
#: approach 4 second) actively hinders comparison.
APPROACH_ORDER = (
    "bq_tools",
    "kc_search",
    "kc_context",
    "context_prefilter",
    "semantic_context",
    "search_direct",
)


def _ordered(names: list[str]) -> list[str]:
    """Canonical order first, then anything unrecognised, alphabetically."""
    known = [n for n in APPROACH_ORDER if n in names]
    return known + sorted(n for n in names if n not in APPROACH_ORDER)


#: Approaches whose reranker candidates come from the Knowledge Catalog capsule,
#: so rerank quality should climb with enrichment tier.
CONTEXT_APPROACHES = frozenset({"kc_context", "context_prefilter", "semantic_context"})
#: Controls: bq_tools reads BigQuery schema, search_direct applies no reranker.
CONTROL_APPROACHES = frozenset({"bq_tools", "search_direct"})
#: Candidates from a single semantic ``search_entries`` call.
SEARCH_APPROACHES = frozenset({"kc_search", "semantic_context", "search_direct"})
#: Approaches that see the whole scoped corpus without a retrieval step.
FULL_CORPUS_APPROACHES = frozenset({"bq_tools", "kc_context", "context_prefilter"})


@dataclass(frozen=True, slots=True)
class CellScore:
    """Metrics for one approach-run."""

    cell_key: str
    approach: str
    tier: int
    question_id: str
    category: str
    discovery_recall: float
    final_recall: float
    precision: float
    ndcg: float
    latency_s: float
    reranker_total_tokens: int
    reranker_calls: int


def _short(full_id: str) -> str:
    """Last dotted segment: ``project.dataset.table`` -> ``table``.

    Scoring matches on the short name, which is exactly why a run must be scoped
    to one tier dataset — all four hold identically-named tables.
    """
    return full_id.rsplit(".", 1)[-1]


def _dcg(gains: list[float]) -> float:
    return sum(g / math.log2(i + 2) for i, g in enumerate(gains))


def _relevance_sets(relevance: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    return (
        set(relevance.get("must_have", [])),
        set(relevance.get("nice_to_have", [])),
        set(relevance.get("distractor", [])),
    )


def _is_error(cell: dict[str, Any]) -> bool:
    """Upstream marks errors with an ``error`` key; we use ``status``."""
    return "error" in cell or cell.get("status") == "error"


def score_cell(cell: dict[str, Any]) -> CellScore | None:
    """Metrics for one cell, or ``None`` if it errored.

    Error cells are excluded from every aggregate and counted separately.
    Cells that merely returned *nothing* are kept and score zero — they are a
    real result, and dropping them would flatter the means.
    """
    if _is_error(cell):
        return None

    must, nice, _distractor = _relevance_sets(cell.get("relevance", {}))
    ranked = [_short(t["table_id"]) for t in cell.get("ranked_tables", [])]
    nominated = [_short(t) for t in cell.get("nominated", [])]
    ranked_set, nominated_set = set(ranked), set(nominated)

    # Recall: fraction of must_have tables retrieved. Measured over the whole
    # ranked list, not truncated to RERANK_K — it is effectively recall@5 only
    # because the reranker is told to return at most top_k.
    final_recall = len(ranked_set & must) / len(must) if must else 1.0
    discovery_recall = len(nominated_set & must) / len(must) if must else 1.0

    # Precision over the deduped ranked set; distractors and unlabelled tables
    # both dilute it.
    relevant = must | nice
    precision = len(ranked_set & relevant) / len(ranked_set) if ranked_set else 0.0

    def gain_for(name: str) -> float:
        if name in must:
            return GAIN["must_have"]
        if name in nice:
            return GAIN["nice_to_have"]
        return 0.0

    gains = [gain_for(name) for name in ranked[:RERANK_K]]
    ideal = sorted(
        [GAIN["must_have"]] * len(must) + [GAIN["nice_to_have"]] * len(nice),
        reverse=True,
    )[:RERANK_K]
    idcg = _dcg(ideal)
    ndcg = (_dcg(gains) / idcg) if idcg else 1.0

    return CellScore(
        cell_key=cell.get("cell_key", ""),
        approach=cell.get("approach", ""),
        tier=int(cell.get("tier", -1)),
        question_id=cell.get("question_id", ""),
        category=cell.get("category", ""),
        discovery_recall=discovery_recall,
        final_recall=final_recall,
        precision=precision,
        ndcg=ndcg,
        latency_s=float(cell.get("latency_s", 0.0)),
        reranker_total_tokens=int(cell.get("reranker_total_tokens", 0)),
        reranker_calls=int(cell.get("reranker_calls", 0)),
    )


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _mean(values: list[float]) -> float:
    return float(np.mean(values)) if values else float("nan")


def _median(values: list[float]) -> float:
    return float(np.median(values)) if values else float("nan")


def _pctl(values: list[float], p: float) -> float:
    return float(np.percentile(values, p)) if values else float("nan")


def _iqr(values: list[float]) -> tuple[float, float]:
    if not values:
        return (float("nan"), float("nan"))
    return (float(np.percentile(values, 25)), float(np.percentile(values, 75)))


@dataclass(frozen=True, slots=True)
class ApproachSummary:
    """Aggregated metrics for one approach (optionally within one tier)."""

    approach: str
    n: int
    discovery_recall: float  # mean
    final_recall: float  # mean
    rerank_loss: float  # mean(discovery) - mean(final)
    precision: float  # median
    ndcg: float  # median
    ndcg_iqr: tuple[float, float]
    latency_p50: float
    latency_p95: float
    reranker_tokens: float  # median
    reranker_calls: float  # median


def _summarize(approach: str, scores: list[CellScore]) -> ApproachSummary:
    discovery = _mean([s.discovery_recall for s in scores])
    final = _mean([s.final_recall for s in scores])
    ndcgs = [s.ndcg for s in scores]
    latencies = [s.latency_s for s in scores]
    return ApproachSummary(
        approach=approach,
        n=len(scores),
        discovery_recall=discovery,
        final_recall=final,
        # The headline number: how much recall the reranker gave back.
        rerank_loss=discovery - final,
        precision=_median([s.precision for s in scores]),
        ndcg=_median(ndcgs),
        ndcg_iqr=_iqr(ndcgs),
        latency_p50=_pctl(latencies, 50),
        latency_p95=_pctl(latencies, 95),
        reranker_tokens=_median([float(s.reranker_total_tokens) for s in scores]),
        reranker_calls=_median([float(s.reranker_calls) for s in scores]),
    )


def summarize_by_approach(
    scores: list[CellScore], *, tier: int | None = None
) -> dict[str, ApproachSummary]:
    """Aggregate per approach, optionally restricted to one tier."""
    buckets: dict[str, list[CellScore]] = {}
    for score in scores:
        if tier is not None and score.tier != tier:
            continue
        buckets.setdefault(score.approach, []).append(score)
    return {name: _summarize(name, buckets[name]) for name in _ordered(list(buckets))}


def summarize_by_tier(scores: list[CellScore]) -> dict[int, dict[str, ApproachSummary]]:
    """Aggregate per tier, then per approach within it."""
    tiers = sorted({s.tier for s in scores})
    return {tier: summarize_by_approach(scores, tier=tier) for tier in tiers}


def tier_response(scores: list[CellScore], *, aggregate: str = "median") -> dict[str, float]:
    """Change in final recall from the lowest enrichment tier to the highest.

    This is the number the whole experiment turns on, so it gets its own
    function rather than living inside a report renderer.

    ``aggregate="median"`` reproduces upstream's ``tier_response_table``, which
    is what produced their headline "+0% for every approach". **That flatness is
    partly a measurement artifact**: on this corpus most cells score 1.0, so a
    median saturates and cannot move. Running the same cells with
    ``aggregate="mean"`` shows real per-approach movement in both directions.

    Use ``"median"`` when comparing against upstream's published table, and
    ``"mean"`` when asking whether enrichment actually changed anything.

    Either way, never read a flat response in isolation — it is also exactly
    what a silently empty ``lookupContext`` produces. Confirm enrichment is
    present before concluding it had no effect.
    """
    if aggregate not in {"median", "mean"}:
        msg = f"aggregate must be 'median' or 'mean', got {aggregate!r}"
        raise ValueError(msg)
    agg = _median if aggregate == "median" else _mean

    by_tier: dict[int, dict[str, list[float]]] = {}
    for score in scores:
        by_tier.setdefault(score.tier, {}).setdefault(score.approach, []).append(score.final_recall)
    if len(by_tier) < _MIN_TIERS_FOR_RESPONSE:
        return {}

    low, high = min(by_tier), max(by_tier)
    return {
        approach: agg(by_tier[high][approach]) - agg(by_tier[low][approach])
        for approach in _ordered(list(by_tier[high]))
        if approach in by_tier[low]
    }


def category_ndcg(
    scores: list[CellScore], *, tier: int | None = None
) -> dict[str, dict[str, float]]:
    """Median nDCG@5 per approach per question category."""
    buckets: dict[str, dict[str, list[float]]] = {}
    for score in scores:
        if tier is not None and score.tier != tier:
            continue
        buckets.setdefault(score.approach, {}).setdefault(score.category, []).append(score.ndcg)
    return {
        approach: {category: _median(vals) for category, vals in sorted(buckets[approach].items())}
        for approach in _ordered(list(buckets))
    }
