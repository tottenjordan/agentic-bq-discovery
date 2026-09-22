"""A self-contained HTML summary: the numbers, the figures, and the caveats.

The caveats are the point. This project has produced one complete 3,000-cell run
whose approach comparison is sound and whose tier comparison is not, and the
difference is invisible in the numbers themselves — a flat tier response looks
identical whether enrichment genuinely does nothing, the corpus is too easy to
show it, or `lookupContext` silently returned nothing at all. A report that
prints the table without saying which of those applies is worse than no report,
because it reads as an answer.

So `findings()` is pure and separately tested: given the scores it decides which
caveats apply, and `render_html` only lays them out.

Self-contained by requirement, not preference — Vertex renders `system.HTML` in
a sandboxed iframe, so a `<img src="plots/x.png">` or a `gs://` reference
resolves to nothing. Figures are inlined as base64 data URIs.
"""

from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bq_context.scoring.metrics import (
    SEARCH_APPROACHES,
    summarize_by_approach,
    tier_response,
)
from bq_context.scoring.report import APPROACH_LABELS

if TYPE_CHECKING:
    from pathlib import Path

    from bq_context.runner.models import ShardResult
    from bq_context.scoring.metrics import ApproachSummary, CellScore

#: Floor above which the corpus is treated as saturated: if even the *worst*
#: approach scores this well, there is little room for enrichment to show up and
#: a flat tier response says more about the corpus than about catalog metadata.
#:
#: A judgement call, not a derived quantity, and it is close to the data. On
#: full-01 the floor is 0.888 at tier 3 and 0.917 pooled over tiers 2-3 — either
#: side of 0.90. So the finding reports the actual floor and spread rather than
#: only a verdict, and 0.85 is set low enough that the answer does not flip with
#: the slice.
SATURATION = 0.85

#: Approaches whose reported token cost excludes their own agent-side LLM calls.
UNMETERED_AGENT_LLM = ("bq_tools", "context_prefilter")


@dataclass(frozen=True)
class Finding:
    """One caveat, with the severity a reader should attach to it."""

    level: str  # "invalid" | "caution" | "note"
    title: str
    detail: str


def findings(
    scores: list[CellScore],
    *,
    convergence_warnings: list[str] | None = None,
    code_versions: set[str] | None = None,
    summaries: dict[str, ApproachSummary] | None = None,
) -> list[Finding]:
    """Decide which caveats this run carries. Pure; the renderer only formats.

    Ordered most to least severe, because a reader who stops after the first
    item should have read the one that would change their conclusion.
    """
    summaries = summaries if summaries is not None else summarize_by_approach(scores)
    found: list[Finding] = []

    if convergence_warnings:
        found.append(
            Finding(
                "invalid",
                "Tier response is not usable for the search approaches",
                "Semantic search returned different hit counts per tier on an identical "
                "corpus, which means the Dataplex index had not converged. Tier is then "
                "confounded with elapsed time rather than measuring enrichment. "
                f"{' '.join(convergence_warnings)} The three full-corpus approaches "
                "(bq_tools, kc_context, context_prefilter) perform no retrieval and are "
                "unaffected.",
            )
        )

    # Measured at the *highest* tier, not pooled. Pooling mixes in low tiers whose
    # recall may be depressed by the index-convergence confound above, which drags
    # the floor down and hides saturation that is really there.
    top = max((s.tier for s in scores), default=0)
    top_summaries = summarize_by_approach([s for s in scores if s.tier == top]) if scores else {}
    recalls = {name: s.final_recall for name, s in top_summaries.items() if s.n}
    if recalls and min(recalls.values()) >= SATURATION:
        floor, ceiling = min(recalls.values()), max(recalls.values())
        found.append(
            Finding(
                "caution",
                "Little headroom at the top tier, so a flat tier response means little",
                f"At tier {top} every approach scores between {floor:.3f} and {ceiling:.3f} "
                f"mean final recall — the weakest is {min(recalls, key=lambda k: recalls[k])}. "
                "With the floor that high there is limited room for enrichment to show up, "
                "so an absent tier effect is substantially a property of the corpus rather "
                "than a result about catalog metadata. A harder corpus is needed before the "
                "enrichment question can be answered either way.",
            )
        )

    metered = [a for a in UNMETERED_AGENT_LLM if a in summaries]
    if metered:
        found.append(
            Finding(
                "caution",
                "Token cost is understated for approaches that run their own LLM",
                f"Reported tokens cover the reranker only. {', '.join(metered)} also make "
                "agent-side LLM calls that are not metered, so their true cost is higher "
                "than shown and is not comparable with the others on that axis.",
            )
        )

    if code_versions and len(code_versions) > 1:
        found.append(
            Finding(
                "note",
                "Cells came from more than one code version",
                f"This dataset mixes {', '.join(sorted(code_versions))}. Comparable only "
                "if the differences between them do not touch measurement.",
            )
        )

    if not found:
        found.append(
            Finding("note", "No caveats detected", "Nothing in this run trips the known traps.")
        )
    return found


def usable_conclusions(scores: list[CellScore], *, invalidated: bool) -> list[str]:
    """What a reader may take from this run, stated plainly."""
    tiers = {s.tier for s in scores}
    lines = [
        "Approach comparison — usable.",
        "Reranker value (precision, nDCG@5) — usable.",
        "Latency and token cost per approach — usable, subject to the metering caveat.",
    ]
    if len(tiers) < 2:  # noqa: PLR2004 - one tier is not a comparison
        lines.append("Tier response — not measured; this run covers a single tier.")
    elif invalidated:
        lines.append("Tier response for the search approaches — NOT usable.")
    else:
        lines.append("Tier response — usable.")
    return lines


def _img(path: Path) -> str:
    """A figure as an inline data URI. Required: the iframe cannot fetch files."""
    encoded = base64.b64encode(path.read_bytes()).decode()
    return (
        f'<figure><img alt="{html.escape(path.stem)}" '
        f'src="data:image/png;base64,{encoded}"/>'
        f"<figcaption>{html.escape(path.stem.replace('_', ' '))}</figcaption></figure>"
    )


_CSS = """
body{font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
max-width:60rem;margin:2rem auto;padding:0 1rem;color:#202124}
h1,h2{font-weight:600;color:#202124} h2{margin-top:2rem;border-bottom:1px solid #dadce0;
padding-bottom:.3rem}
table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:14px}
th,td{border:1px solid #dadce0;padding:.4rem .6rem;text-align:right}
th:first-child,td:first-child{text-align:left}
th{background:#f8f9fa;font-weight:600}
figure{margin:1rem 0}img{max-width:100%;border:1px solid #dadce0;border-radius:4px}
figcaption{font-size:13px;color:#5f6368;margin-top:.3rem}
.finding{border-left:4px solid;padding:.6rem 1rem;margin:.8rem 0;background:#f8f9fa}
.invalid{border-color:#EA4335}.caution{border-color:#FBBC05}.note{border-color:#4285F4}
.finding b{display:block;margin-bottom:.2rem}
"""


def render_html(  # noqa: PLR0913 - each argument is a distinct source the report
    # draws on; bundling them would hide what the report actually depends on.
    scores: list[CellScore],
    *,
    experiment_id: str,
    figures: list[Path] | None = None,
    convergence_warnings: list[str] | None = None,
    code_versions: set[str] | None = None,
    shard_summaries: list[ShardResult] | None = None,
    errors: int = 0,
) -> str:
    """The executive summary as one self-contained HTML document."""
    summaries = summarize_by_approach(scores)
    caveats = findings(
        scores,
        convergence_warnings=convergence_warnings,
        code_versions=code_versions,
        summaries=summaries,
    )
    invalidated = any(f.level == "invalid" for f in caveats)
    tiers = sorted({s.tier for s in scores})

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(experiment_id)}</title><style>{_CSS}</style></head><body>",
        f"<h1>{html.escape(experiment_id)}</h1>",
        (
            f"<p>{len(scores):,} scored cells across {len(tiers)} tier(s) and "
            f"{len(summaries)} approach(es); {errors} error cell(s) excluded.</p>"
        ),
        "<h2>What this run supports</h2><ul>",
        *(
            f"<li>{html.escape(line)}</li>"
            for line in usable_conclusions(scores, invalidated=invalidated)
        ),
        "</ul>",
        "<h2>Read this first</h2>",
    ]
    parts += [
        f'<div class="finding {f.level}"><b>{html.escape(f.title)}</b>{html.escape(f.detail)}</div>'
        for f in caveats
    ]

    parts += [
        (
            "<h2>Approaches</h2><table><tr><th>Approach</th><th>Discovery recall</th>"
            "<th>Final recall</th><th>Precision</th><th>nDCG@5</th><th>nDCG IQR</th>"
            "<th>p50 (s)</th><th>Reranker tokens</th></tr>"
        )
    ]
    parts += [
        "<tr><td>{}</td><td>{:.1%}</td><td>{:.1%}</td><td>{:.0%}</td><td>{:.3f}</td>"
        "<td>{:.2f}&ndash;{:.2f}</td><td>{:.2f}</td><td>{:,.0f}</td></tr>".format(
            html.escape(APPROACH_LABELS.get(name, name)),
            s.discovery_recall,
            s.final_recall,
            s.precision,
            s.ndcg,
            *s.ndcg_iqr,
            s.latency_p50,
            s.reranker_tokens,
        )
        for name, s in summaries.items()
    ]
    parts.append("</table>")

    if len(tiers) > 1:
        response = tier_response(scores, aggregate="mean")
        note = " These numbers are not usable — see the finding above." if invalidated else ""
        parts += [
            f"<h2>Enrichment response, tier {tiers[0]} to {tiers[-1]}</h2>",
            f"<p>Change in mean final recall.{note}</p>",
            "<table><tr><th>Approach</th><th>Δ final recall</th><th>Retrieval?</th></tr>",
            *(
                "<tr><td>{}</td><td>{:+.3f}</td><td>{}</td></tr>".format(
                    html.escape(APPROACH_LABELS.get(name, name)),
                    delta,
                    "yes" if name in SEARCH_APPROACHES else "no",
                )
                for name, delta in response.items()
            ),
            "</table>",
        ]

    if figures:
        parts.append("<h2>Figures</h2>")
        parts += [_img(path) for path in figures if path.exists()]

    if shard_summaries:
        warms = sorted(s.cache_warm_s for s in shard_summaries if s.cache_warm_s)
        aborted = [s for s in shard_summaries if s.aborted]
        parts += ["<h2>Reliability</h2><ul>"]
        if warms:
            parts.append(
                f"<li>Cache warm across {len(warms)} shard(s): median "
                f"{warms[len(warms) // 2]:.1f}s, max {warms[-1]:.1f}s.</li>"
            )
        parts += [
            f"<li><b>Aborted:</b> {html.escape(s.shard_id)} — {html.escape(s.abort_reason)}</li>"
            for s in aborted
        ]
        if not aborted:
            parts.append("<li>No shard was aborted.</li>")
        parts.append("</ul>")

    parts.append("</body></html>")
    return "\n".join(parts)


def convergence_from_cells(cells: list[dict]) -> list[str]:
    """Detect the index warm-up confound from the run's own cells.

    `assess_search_convergence` normally probes one fixed question live. Here the
    evidence is already in the data: `search_stats.raw_search_count` per tier, on
    a corpus that is identical across tiers. A converged index returns the same
    count everywhere; a rising count is the warm-up signature.

    Deriving it from the cells rather than re-probing means a report rendered
    months later still carries the caveat, instead of silently dropping it
    because the index has converged since.
    """
    from bq_context.cli import assess_search_convergence  # noqa: PLC0415 - avoids a cycle

    totals: dict[int, list[int]] = {}
    for cell in cells:
        stats = cell.get("search_stats") or {}
        if "raw_search_count" in stats:
            totals.setdefault(int(cell["tier"]), []).append(int(stats["raw_search_count"]))
    if len(totals) < 2:  # noqa: PLR2004 - one tier is not a comparison
        return []
    means = {tier: round(sum(v) / len(v)) for tier, v in totals.items() if v}
    return assess_search_convergence(means)
