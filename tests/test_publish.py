"""Publishing what `finalize` produces.

These helpers were extracted from the component body specifically so they could
be tested. Inline in a KFP component they are unreachable without a pipeline
run — which is how the pipeline shipped for weeks rendering a report and three
figures into a container that was then destroyed.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bq_context.pipeline.publish import (
    EMPTY_REPORT,
    merge_args,
    merge_report,
    publish_figures,
    publish_report,
)
from bq_context.runner.store import LocalStore
from bq_context.scoring.merge import missing_path

if TYPE_CHECKING:
    from pathlib import Path

PNG = b"\x89PNG\r\n\x1a\n"


# ---------------------------------------------------------------------------
# Copying output to the stable prefix
# ---------------------------------------------------------------------------
def test_the_report_lands_under_the_experiment_prefix(tmp_path: Path) -> None:
    """Not a KFP artifact path: those embed the pipeline job id and change every
    run, so a human looking a month later would not find them."""
    store = LocalStore(tmp_path / "store")
    source = tmp_path / "report.md"
    source.write_text("# Results\n")

    assert publish_report(store, "exp", str(source)) == "experiments/exp/scoring/report.md"
    assert store.read_text("experiments/exp/scoring/report.md") == "# Results\n"


def test_a_missing_report_is_not_fatal(tmp_path: Path) -> None:
    """`score` exits 1 when there are no merged results. The exit task must still
    publish everything else rather than dying on the first absent file."""
    store = LocalStore(tmp_path / "store")
    assert publish_report(store, "exp", str(tmp_path / "nope.md")) is None


def test_every_figure_is_copied(tmp_path: Path) -> None:
    plots = tmp_path / "plots"
    plots.mkdir()
    for name in ("discovery_vs_final", "latency_cost"):
        (plots / f"{name}.png").write_bytes(PNG)
    store = LocalStore(tmp_path / "store")

    written = publish_figures(store, "exp", str(plots))
    assert written == [
        "experiments/exp/plots/discovery_vs_final.png",
        "experiments/exp/plots/latency_cost.png",
    ]
    assert store.exists(written[0])


def test_figures_keep_their_bytes(tmp_path: Path) -> None:
    """Through write_bytes, not write_text — the latter would tag a PNG as JSON."""
    plots = tmp_path / "plots"
    plots.mkdir()
    payload = PNG + bytes(range(256))
    (plots / "x.png").write_bytes(payload)
    store = LocalStore(tmp_path / "store")

    publish_figures(store, "exp", str(plots))
    assert (tmp_path / "store" / "experiments/exp/plots/x.png").read_bytes() == payload


def test_a_missing_plots_directory_is_not_fatal(tmp_path: Path) -> None:
    """`recall_vs_tier` is skipped below two tiers, and `plot` can fail entirely."""
    store = LocalStore(tmp_path / "store")
    assert publish_figures(store, "exp", str(tmp_path / "absent")) == []


# ---------------------------------------------------------------------------
# The completeness record
# ---------------------------------------------------------------------------
def test_the_merge_report_is_read_back(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.write_text(
        missing_path("exp"),
        json.dumps({"expected": 3000, "present": 3000, "missing_count": 0, "missing": []}),
    )
    assert merge_report(store, "exp")["present"] == 3000


def test_no_merge_report_yields_zeroes_rather_than_raising(tmp_path: Path) -> None:
    """The exit task runs when the sweep died — possibly before merge wrote
    anything. It must still publish its artifacts."""
    assert merge_report(LocalStore(tmp_path), "exp") == EMPTY_REPORT


def test_a_corrupt_merge_report_yields_zeroes(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.write_text(missing_path("exp"), "{not json")
    assert merge_report(store, "exp") == EMPTY_REPORT


def test_the_empty_report_is_not_shared_between_calls(tmp_path: Path) -> None:
    """A returned mutable default would let one caller corrupt the next."""
    first = merge_report(LocalStore(tmp_path), "exp")
    first["present"] = 99
    assert merge_report(LocalStore(tmp_path), "exp")["present"] == 0


# ---------------------------------------------------------------------------
# merge argv
# ---------------------------------------------------------------------------
def test_every_tier_and_approach_is_passed() -> None:
    args = merge_args("e", "gs://b", runs=5, tiers=[0, 3], approaches=["bq_tools", "kc_search"])
    assert args[:2] == ["bq-context", "merge"]
    assert args.count("--tier") == 2
    assert args.count("--approach") == 2


def test_the_question_limit_is_passed_when_set() -> None:
    """It must match what the shards ran, or merge reports phantom missing cells
    and the completeness gate fails a run that was actually fine."""
    args = merge_args("e", "gs://b", runs=1, tiers=[3], approaches=["a"], question_limit=5)
    assert args[args.index("--limit") + 1] == "5"


def test_no_limit_flag_when_unset() -> None:
    args = merge_args("e", "gs://b", runs=1, tiers=[3], approaches=["a"])
    assert "--limit" not in args


@pytest.mark.parametrize("flag", ["--experiment-id", "--out", "--runs"])
def test_the_required_flags_are_present(flag: str) -> None:
    assert flag in merge_args("e", "gs://b", runs=5, tiers=[0], approaches=["a"])
