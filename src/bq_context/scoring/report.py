"""Render scored cells as a markdown report and a small set of figures.

Mirrors upstream's report structure — headline recall decomposition, enrichment
response, cost and latency, nDCG by category — so the two can be read side by
side. Rendering is deliberately separate from :mod:`bq_context.scoring.metrics`
so the numbers can be recomputed and inspected without producing files.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from bq_context.scoring.metrics import (
    CONTEXT_APPROACHES,
    CONTROL_APPROACHES,
    category_ndcg,
    summarize_by_approach,
    summarize_by_tier,
    tier_response,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bq_context.scoring.metrics import ApproachSummary, CellScore

logger = logging.getLogger(__name__)

__all__ = ["render_markdown", "write_plots"]

APPROACH_LABELS = {
    "bq_tools": "1: BQ Tools",
    "kc_search": "2: KC Search",
    "kc_context": "3: KC Context",
    "context_prefilter": "4: Pre-Filter",
    "semantic_context": "5: Semantic",
    "search_direct": "6: Search Direct",
}
CATEGORY_ORDER = ["single-table", "multi-table-related", "multi-table-disparate", "trap"]

_MIN_TIERS_FOR_RESPONSE = 2


def _label(approach: str) -> str:
    role = ""
    if approach in CONTROL_APPROACHES:
        role = " *(control)*"
    elif approach in CONTEXT_APPROACHES:
        role = " *(context)*"
    return APPROACH_LABELS.get(approach, approach) + role


def _pct(value: float) -> str:
    return "—" if math.isnan(value) else f"{value:.1%}"


def _num(value: float, digits: int = 2) -> str:
    return "—" if math.isnan(value) else f"{value:.{digits}f}"


# ---------------------------------------------------------------------------
# Markdown sections
# ---------------------------------------------------------------------------


def _recall_section(summaries: dict[str, ApproachSummary]) -> list[str]:
    lines = [
        "## Discovery vs rerank",
        "",
        (
            "Means, not medians: on this corpus most cells score 1.0, so a "
            "median saturates and hides the tail."
        ),
        "",
        "| Approach | Discovery recall | Final recall | Rerank loss |",
        "|---|---|---|---|",
    ]
    lines += [
        (
            f"| {_label(name)} | {_pct(s.discovery_recall)} "
            f"| {_pct(s.final_recall)} | {s.rerank_loss:+.3f} |"
        )
        for name, s in summaries.items()
    ]
    return lines


def _enrichment_section(scores: list[CellScore], names: list[str], tiers: list[int]) -> list[str]:
    lines = ["## Enrichment response", ""]
    if len(tiers) < _MIN_TIERS_FOR_RESPONSE:
        lines.append("_Single tier in this run; no enrichment response to report._")
        return lines

    medians = tier_response(scores, aggregate="median")
    means = tier_response(scores, aggregate="mean")
    lines += [
        (
            f"Change in final recall, tier {min(tiers)} → tier {max(tiers)}. "
            "Median is upstream's measure; it saturates on this corpus, so the "
            "mean is shown alongside it."
        ),
        "",
        "| Approach | Δ median | Δ mean |",
        "|---|---|---|",
    ]
    nan = float("nan")
    lines += [
        f"| {_label(name)} | {medians.get(name, nan):+.3f} | {means.get(name, nan):+.3f} |"
        for name in names
    ]
    return lines


def _cost_section(summaries: dict[str, ApproachSummary]) -> list[str]:
    lines = [
        "## Cost and latency",
        "",
        "| Approach | p50 (s) | p95 (s) | Reranker tokens | Calls | Precision | nDCG@5 |",
        "|---|---|---|---|---|---|---|",
    ]
    lines += [
        (
            f"| {_label(name)} | {_num(s.latency_p50)} | {_num(s.latency_p95)} "
            f"| {s.reranker_tokens:,.0f} | {_num(s.reranker_calls, 1)} "
            f"| {_pct(s.precision)} | {_num(s.ndcg, 3)} |"
        )
        for name, s in summaries.items()
    ]
    return lines


def _category_section(scores: list[CellScore], max_tier: int) -> list[str]:
    by_category = category_ndcg(scores, tier=max_tier)
    categories = [c for c in CATEGORY_ORDER if any(c in v for v in by_category.values())]
    if not categories:
        return []

    header = " | ".join(categories)
    lines = [
        f"## nDCG@5 by question category (tier {max_tier})",
        "",
        f"| Approach | {header} |",
        ("|---" * (len(categories) + 1)) + "|",
    ]
    nan = float("nan")
    for name, cats in by_category.items():
        row = " | ".join(_num(cats.get(c, nan), 2) for c in categories)
        lines.append(f"| {_label(name)} | {row} |")
    return lines


def render_markdown(scores: list[CellScore], *, experiment_id: str, errors: int = 0) -> str:
    """Full report: recall decomposition, tier response, cost, category nDCG."""
    if not scores:
        return f"# Results — {experiment_id}\n\nNo scored cells.\n"

    summaries = summarize_by_approach(scores)
    names = list(summaries)
    tiers = sorted({s.tier for s in scores})

    sections: list[list[str]] = [
        [
            f"# Results — {experiment_id}",
            "",
            (
                f"{len(scores)} scored approach-runs across {len(tiers)} tier(s); "
                f"{errors} error cell(s) excluded."
            ),
        ],
        _recall_section(summaries),
        _enrichment_section(scores, names, tiers),
        _cost_section(summaries),
        _category_section(scores, max(tiers)),
        [
            "---",
            "",
            (
                "Graded relevance: `must_have` gain 2 (recall numerator), "
                "`nice_to_have` gain 1, `distractor` gain 0. Cells returning "
                "nothing are scored zero rather than dropped."
            ),
        ],
    ]
    return "\n".join("\n".join(section) + "\n" for section in sections if section)


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def _plot_discovery_vs_final(plt, summaries: dict[str, ApproachSummary], out: Path) -> Path:  # noqa: ANN001
    """The headline decomposition: did retrieval find it, did rerank keep it."""
    names = list(summaries)
    fig, ax = plt.subplots(figsize=(9, 4.5))
    x = range(len(names))
    ax.bar(
        [i - 0.2 for i in x],
        [summaries[n].discovery_recall for n in names],
        0.4,
        label="discovery",
    )
    ax.bar([i + 0.2 for i in x], [summaries[n].final_recall for n in names], 0.4, label="final")
    ax.set_xticks(list(x), [APPROACH_LABELS.get(n, n) for n in names], rotation=20, ha="right")
    ax.set_ylabel("recall (mean)")
    ax.set_ylim(0, 1.05)
    ax.set_title("Discovery vs final recall")
    ax.legend()
    fig.tight_layout()
    path = out / "discovery_vs_final.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def _plot_recall_vs_tier(plt, scores: list[CellScore], out: Path) -> Path | None:  # noqa: ANN001
    by_tier = summarize_by_tier(scores)
    if len(by_tier) < _MIN_TIERS_FOR_RESPONSE:
        return None
    fig, ax = plt.subplots(figsize=(9, 4.5))
    for name in summarize_by_approach(scores):
        tiers = [t for t in sorted(by_tier) if name in by_tier[t]]
        ax.plot(
            tiers,
            [by_tier[t][name].final_recall for t in tiers],
            marker="o",
            label=APPROACH_LABELS.get(name, name),
        )
    ax.set_xlabel("enrichment tier")
    ax.set_ylabel("final recall (mean)")
    ax.set_xticks(sorted(by_tier))
    ax.set_title("Final recall vs enrichment tier")
    ax.legend(fontsize=8)
    fig.tight_layout()
    path = out / "recall_vs_tier.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def _plot_latency_cost(plt, summaries: dict[str, ApproachSummary], out: Path) -> Path:  # noqa: ANN001
    """The two axes quality trades against."""
    names = list(summaries)
    labels = [APPROACH_LABELS.get(n, n) for n in names]
    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 4.5))
    left.bar(labels, [summaries[n].latency_p50 for n in names])
    left.set_ylabel("latency p50 (s)")
    left.set_title("Latency")
    left.tick_params(axis="x", rotation=25)
    right.bar(labels, [summaries[n].reranker_tokens for n in names], color="tab:orange")
    right.set_ylabel("reranker tokens (median)")
    right.set_title("Reranker token cost")
    right.tick_params(axis="x", rotation=25)
    fig.tight_layout()
    path = out / "latency_cost.png"
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def write_plots(scores: list[CellScore], out_dir: Path) -> list[Path]:
    """Write the figure set. Returns the paths written."""
    import matplotlib as mpl  # noqa: PLC0415 - heavy import, only needed when plotting

    mpl.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    out_dir.mkdir(parents=True, exist_ok=True)
    summaries = summarize_by_approach(scores)
    candidates = [
        _plot_discovery_vs_final(plt, summaries, out_dir),
        _plot_recall_vs_tier(plt, scores, out_dir),
        _plot_latency_cost(plt, summaries, out_dir),
    ]
    return [p for p in candidates if p is not None]
