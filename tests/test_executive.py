"""The executive report's interpretation.

A flat tier response looks identical whether enrichment does nothing, the corpus
is too easy to show it, or `lookupContext` silently returned nothing. The numbers
cannot distinguish those; the caveats can. So the caveats are the part worth
testing, and `findings()` is pure precisely so they can be.

A report that prints full-01's tables without saying the tier response is invalid
and the corpus is saturated would read as though the enrichment question had been
answered. That is the failure these tests exist to prevent.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import pytest

from bq_context.scoring.executive import (
    SATURATION,
    convergence_from_cells,
    findings,
    render_html,
    usable_conclusions,
)
from bq_context.scoring.metrics import CellScore

if TYPE_CHECKING:
    from pathlib import Path

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def cell(approach: str, tier: int, recall: float, **over: object) -> CellScore:
    base = {
        "cell_key": f"q1|{approach}|tier{tier}|run0",
        "approach": approach,
        "tier": tier,
        "question_id": "q1",
        "category": "single-table",
        "discovery_recall": recall,
        "final_recall": recall,
        "precision": 1.0,
        "ndcg": 1.0,
        "latency_s": 3.0,
        "reranker_total_tokens": 1000,
        "reranker_calls": 1,
    }
    return CellScore(**{**base, **over})  # type: ignore[arg-type]


def saturated(approaches: tuple[str, ...] = ("kc_search", "kc_context")) -> list[CellScore]:
    return [cell(a, t, 0.98) for a in approaches for t in (0, 3)]


def headroom(approaches: tuple[str, ...] = ("kc_search", "kc_context")) -> list[CellScore]:
    return [cell(a, t, 0.55) for a in approaches for t in (0, 3)]


# ---------------------------------------------------------------------------
# Saturation
# ---------------------------------------------------------------------------
def test_a_saturated_corpus_is_called_out() -> None:
    """Every approach near 1.0 means no headroom, so a flat tier response is a
    property of the corpus rather than a result about catalog metadata."""
    titles = [f.title for f in findings(saturated())]
    assert any("headroom" in t for t in titles), titles


def test_a_corpus_with_headroom_is_not_flagged() -> None:
    """The opposite must also hold, or the caveat is noise that gets ignored."""
    assert not any("headroom" in f.title for f in findings(headroom()))


def test_saturation_needs_every_approach_above_the_line() -> None:
    """One approach off the ceiling means the axis can still move.

    full-01's search_direct sat at 0.772 while the rest were near 1.0 — that run
    genuinely had headroom on one approach.
    """
    mixed = [*saturated(("kc_context",)), *[cell("search_direct", t, 0.40) for t in (0, 3)]]
    assert not any("headroom" in f.title for f in findings(mixed))


def test_the_threshold_is_where_it_is_documented() -> None:
    assert pytest.approx(0.85) == SATURATION


def test_saturation_is_measured_at_the_top_tier_only() -> None:
    """Pooling tiers lets a confound-depressed low tier hide real saturation.

    full-01 is exactly this: the floor is 0.745 pooled and 0.888 at tier 3,
    because the index warm-up dragged tiers 0 and 1 down.
    """
    mixed = [
        *[cell("kc_search", 0, 0.40) for _ in range(2)],  # depressed by the confound
        *[cell("kc_search", 3, 0.95) for _ in range(2)],
        *[cell("kc_context", t, 0.97) for t in (0, 3)],
    ]
    assert any("headroom" in f.title for f in findings(mixed))


# ---------------------------------------------------------------------------
# The index-convergence confound
# ---------------------------------------------------------------------------
def test_a_convergence_warning_invalidates_the_tier_section() -> None:
    """This is the finding that must outrank everything else in the report."""
    found = findings(headroom(), convergence_warnings=["tier0=3, tier3=6, spread 66%"])
    assert found[0].level == "invalid"
    assert "Tier response" in found[0].title


def test_the_warning_text_is_carried_through() -> None:
    """A reader needs the actual hit counts, not just that something was wrong."""
    found = findings(headroom(), convergence_warnings=["tier0=3, tier3=6"])
    assert "tier0=3, tier3=6" in found[0].detail


def test_the_full_corpus_approaches_are_exonerated() -> None:
    """They perform no retrieval, so the confound cannot touch them — and saying
    so is what stops a reader discarding the whole run."""
    found = findings(headroom(), convergence_warnings=["x"])
    assert "bq_tools" in found[0].detail


def test_no_warning_means_no_invalid_finding() -> None:
    assert not any(f.level == "invalid" for f in findings(headroom()))


# ---------------------------------------------------------------------------
# Unmetered agent-side tokens
# ---------------------------------------------------------------------------
def test_the_token_caveat_appears_whenever_bq_tools_is_present() -> None:
    """Reported tokens are the reranker's only; bq_tools runs its own LLM loop."""
    found = findings([cell("bq_tools", 3, 0.5)])
    assert any("understated" in f.title for f in found)


def test_the_token_caveat_is_absent_when_no_unmetered_approach_ran() -> None:
    assert not any("understated" in f.title for f in findings([cell("search_direct", 3, 0.5)]))


# ---------------------------------------------------------------------------
# Mixed provenance
# ---------------------------------------------------------------------------
def test_mixed_code_versions_are_declared() -> None:
    found = findings(headroom(), code_versions={"5de0dde", "28a7b2c"})
    assert any("code version" in f.title for f in found)


def test_a_single_code_version_is_not_flagged() -> None:
    assert not any("code version" in f.title for f in findings(headroom(), code_versions={"abc"}))


def test_findings_are_ordered_most_severe_first() -> None:
    """A reader who stops after the first item must have read the worst one."""
    found = findings(
        saturated(("bq_tools", "kc_context")),
        convergence_warnings=["x"],
        code_versions={"a", "b"},
    )
    assert [f.level for f in found] == ["invalid", "caution", "caution", "note"]


def test_a_clean_run_still_says_something() -> None:
    assert findings(headroom(("search_direct",)))[0].level == "note"


# ---------------------------------------------------------------------------
# What the run supports
# ---------------------------------------------------------------------------
def test_an_invalidated_run_says_the_tier_axis_is_unusable() -> None:
    lines = usable_conclusions(headroom(), invalidated=True)
    assert any("NOT usable" in line for line in lines)


def test_a_single_tier_run_says_tier_was_not_measured() -> None:
    """Not the same as invalid, and conflating them would misrepresent a smoke run."""
    lines = usable_conclusions([cell("kc_search", 3, 0.5)], invalidated=False)
    assert any("single tier" in line for line in lines)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------
def test_the_html_is_self_contained(tmp_path: Path) -> None:
    """Vertex renders system.HTML in a sandboxed iframe: a file or gs:// reference
    resolves to nothing, so figures must be inlined."""
    figure = tmp_path / "discovery_vs_final.png"
    figure.write_bytes(PNG)

    doc = render_html(saturated(), experiment_id="e", figures=[figure])
    assert 'src="data:image/png;base64,' in doc
    assert base64.b64encode(PNG).decode() in doc
    assert "file://" not in doc
    assert 'src="/' not in doc
    assert "gs://" not in doc


def test_a_missing_figure_is_skipped_rather_than_breaking_the_document(tmp_path: Path) -> None:
    doc = render_html(saturated(), experiment_id="e", figures=[tmp_path / "absent.png"])
    assert doc.startswith("<!doctype html>")


def test_the_caveats_appear_in_the_document() -> None:
    doc = render_html(saturated(), experiment_id="e", convergence_warnings=["tier0=3"])
    assert "invalid" in doc
    assert "headroom" in doc


def test_the_experiment_id_is_escaped() -> None:
    """It comes from user input and lands in a title and a heading."""
    doc = render_html(headroom(), experiment_id="<script>x</script>")
    assert "<script>x</script>" not in doc
    assert "&lt;script&gt;" in doc


def test_a_single_tier_run_omits_the_enrichment_table() -> None:
    doc = render_html([cell("kc_search", 3, 0.9)], experiment_id="e")
    assert "Enrichment response" not in doc


# ---------------------------------------------------------------------------
# Detecting the warm-up confound from a run's own cells
#
# The previous version of `convergence_from_cells` could not fire. It built one
# mean per tier and passed it as `first` with no `second`, and the guard returns
# [] unless it has two observations to compare. It reported a clean bill on
# every report ever rendered -- including full-01, whose tier comparison was
# invalid for exactly this reason. It had no tests, which is how that survived.
# ---------------------------------------------------------------------------
def _cell(tier: int, when: str, hits: int) -> dict:
    return {"tier": tier, "written_at": when, "search_stats": {"raw_search_count": hits}}


def _steady(tier: int, hits: int, n: int = 12) -> list[dict]:
    return [_cell(tier, f"2026-09-25T00:{i:02d}:00", hits) for i in range(n)]


def test_a_settled_run_reports_nothing() -> None:
    cells = _steady(0, 3) + _steady(3, 4)
    assert convergence_from_cells(cells) == []


def test_a_tier_that_moved_during_the_run_is_caught() -> None:
    """THE case. Search results climbing while the sweep progresses is tier
    confounded with elapsed time, which is what invalidated full-01."""
    early = [_cell(0, f"2026-09-25T00:{i:02d}:00", 1) for i in range(6)]
    late = [_cell(0, f"2026-09-25T01:{i:02d}:00", 5) for i in range(6)]
    warnings = convergence_from_cells(early + late + _steady(3, 4))

    assert len(warnings) == 1
    assert "tier0" in warnings[0]
    assert "confounded with elapsed time" in warnings[0]


def test_tier_differences_are_not_drift() -> None:
    """Tiers legitimately differ — that is the independent variable. Only a tier
    moving against *itself* is evidence of a warming index."""
    assert convergence_from_cells(_steady(0, 2) + _steady(1, 5) + _steady(3, 9)) == []


def test_ordering_is_by_write_time_not_file_order() -> None:
    """Cells arrive merged from 24 shards, so list order is not time order."""
    early = [_cell(0, f"2026-09-25T00:{i:02d}:00", 1) for i in range(6)]
    late = [_cell(0, f"2026-09-25T01:{i:02d}:00", 5) for i in range(6)]
    shuffled = [c for pair in zip(late, early, strict=True) for c in pair]
    assert convergence_from_cells(shuffled)


def test_too_few_cells_to_split_is_not_evidence() -> None:
    """Three cells a side is noise, and a guard that cries wolf gets ignored."""
    assert convergence_from_cells(_steady(0, 1, n=4) + _steady(0, 9, n=2)) == []


def test_cells_without_search_stats_are_ignored() -> None:
    """Only the three search approaches record `raw_search_count`; bq_tools and
    the capsule approaches never search."""
    assert convergence_from_cells([{"tier": 0, "written_at": "x"}] * 20) == []
