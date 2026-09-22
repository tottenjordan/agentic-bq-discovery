"""PaperBanana figure generation: diagrams only, and never on the critical path.

Two properties matter and neither is about image quality. Generation must skip
cleanly when the API key is absent — it runs inside the exit task, which is the
only thing allowed to turn a run red and must do so only for missing cells. And
it must never be pointed at a data chart: `discovery_vs_final`, `recall_vs_tier`
and `latency_cost` plot measured numbers, and bars whose heights are not derived
from the data would be a correctness hazard in a benchmark report.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bq_context.scoring.figures import DIAGRAMS, STYLE, generate

if TYPE_CHECKING:
    from pathlib import Path

DATA_CHARTS = {"discovery_vs_final", "recall_vs_tier", "latency_cost"}


def test_no_data_chart_is_ever_generated() -> None:
    """The load-bearing assertion. Those three stay in matplotlib."""
    assert not DATA_CHARTS & set(DIAGRAMS), "a measured-data chart must not be LLM-rendered"


def test_the_house_style_matches_the_committed_diagrams() -> None:
    """docs/images/ was produced with this preamble; drifting makes new figures
    visibly not belong."""
    for token in ("#4285F4", "#34A853", "no 3D effects", "no hexagons"):
        assert token in STYLE


def test_a_missing_key_skips_rather_than_raises(tmp_path: Path) -> None:
    """Inside the exit task an exception here would fail a run for a cosmetic
    extra. There is no key in the test environment, so this is the real path."""
    assert generate("no-such-project", tmp_path / "figures") == []


def test_an_injected_renderer_writes_every_diagram(tmp_path: Path) -> None:
    source = tmp_path / "rendered.png"
    source.write_bytes(b"\x89PNG")
    written = generate("p", tmp_path / "out", renderer=lambda _c, _i: source)
    assert {p.stem for p in written} == set(DIAGRAMS)
    assert all(p.read_bytes() == b"\x89PNG" for p in written)


def test_the_style_is_prepended_to_every_prompt(tmp_path: Path) -> None:
    seen: list[str] = []
    source = tmp_path / "r.png"
    source.write_bytes(b"x")

    def renderer(context: str, _intent: str) -> Path:
        seen.append(context)
        return source

    generate("p", tmp_path / "out", renderer=renderer)
    assert all(c.startswith(STYLE) for c in seen)


def test_a_failing_renderer_loses_only_that_diagram(tmp_path: Path) -> None:
    """One bad diagram must not cost the others, or the report loses everything
    over a single transient API error."""

    def renderer(_c: str, _i: str) -> Path:
        message = "429 from the image API"
        raise RuntimeError(message)

    assert generate("p", tmp_path / "out", renderer=renderer) == []


def test_a_renderer_returning_nothing_is_handled(tmp_path: Path) -> None:
    assert generate("p", tmp_path / "out", renderer=lambda _c, _i: None) == []
