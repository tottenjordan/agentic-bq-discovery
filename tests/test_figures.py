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

import pytest

from bq_context.scoring.figures import DIAGRAMS, STYLE, api_key, generate, secret_id

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


# ---------------------------------------------------------------------------
# Which secret to read
# ---------------------------------------------------------------------------
def test_the_secret_name_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env` carries SECRET_ID locally; the pipeline gets it from the task env."""
    monkeypatch.setenv("SECRET_ID", "some-other-secret")
    assert secret_id() == "some-other-secret"


def test_an_unset_secret_id_is_an_error_not_a_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """No fallback, deliberately. A default would be a name invented in code that
    nobody configured, and silently reading the wrong secret is worse than saying
    so — especially in the pipeline, where there is no `.env`."""
    monkeypatch.delenv("SECRET_ID", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_ID"):
        secret_id()


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_a_blank_value_is_rejected_rather_than_building_an_empty_path(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """`SECRET_ID=` is easy to write in a .env, and would otherwise resolve to
    `projects/p/secrets//versions/latest`, which fails obscurely."""
    monkeypatch.setenv("SECRET_ID", value)
    with pytest.raises(RuntimeError, match="SECRET_ID"):
        secret_id()


def test_the_error_says_where_to_set_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config error is only useful if it names the fix."""
    monkeypatch.delenv("SECRET_ID", raising=False)
    with pytest.raises(RuntimeError) as err:
        secret_id()
    assert ".env" in str(err.value)
    assert "Secret Manager" in str(err.value)


def test_a_missing_secret_id_skips_figures_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """api_key runs inside the exit task, which may only turn a run red for
    missing cells. An unset SECRET_ID must degrade to "no figures", not a crash —
    and the handler must not re-call secret_id(), which would raise again."""
    import logging

    monkeypatch.delenv("SECRET_ID", raising=False)
    with caplog.at_level(logging.WARNING):
        assert api_key("hybrid-vertex") is None
    assert "SECRET_ID" in caplog.text


def test_it_is_read_at_call_time_not_import_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """A module-level constant would freeze whatever was set when the module was
    first imported — which for the CLI is before `.env` is loaded."""
    monkeypatch.setenv("SECRET_ID", "first")
    assert secret_id() == "first"
    monkeypatch.setenv("SECRET_ID", "second")
    assert secret_id() == "second"


def test_the_failure_message_names_the_secret_it_tried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Otherwise a missing-secret skip gives no clue which name was wrong — the
    exact confusion a configurable name introduces."""
    import logging

    monkeypatch.setenv("SECRET_ID", "definitely-not-there")
    with caplog.at_level(logging.WARNING):
        api_key("no-such-project-xyz")
    assert "definitely-not-there" in caplog.text
