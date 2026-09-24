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
RUN = "20260924T164612Z-abc1234"


def test_the_report_lands_under_the_run_prefix(tmp_path: Path) -> None:
    """Not a KFP artifact path: those embed the pipeline job id and change every
    run, so a human looking a month later would not find them. And not the bare
    experiment prefix either — that is one path per *experiment*, so the second
    execution overwrote the first."""
    store = LocalStore(tmp_path / "store")
    source = tmp_path / "report.md"
    source.write_text("# Results\n")

    target = f"experiments/exp/runs/{RUN}/scoring/report.md"
    assert publish_report(store, "exp", RUN, str(source)) == target
    assert store.read_text(target) == "# Results\n"


def test_a_second_execution_does_not_overwrite_the_first(tmp_path: Path) -> None:
    """THE regression. `hard-full-01` ran three times and kept one report: the
    original and the failed run were both overwritten by the recovery run, and
    the failed run's report was the evidence for the shard exit-code bug."""
    store = LocalStore(tmp_path / "store")
    first = tmp_path / "first.md"
    first.write_text("# First\n")
    second = tmp_path / "second.md"
    second.write_text("# Second\n")

    a = publish_report(store, "exp", "20260924T100000Z-aaa", str(first))
    b = publish_report(store, "exp", "20260924T110000Z-bbb", str(second))

    assert a != b
    assert store.read_text(str(a)) == "# First\n"
    assert store.read_text(str(b)) == "# Second\n"


def test_a_missing_report_is_not_fatal(tmp_path: Path) -> None:
    """`score` exits 1 when there are no merged results. The exit task must still
    publish everything else rather than dying on the first absent file."""
    store = LocalStore(tmp_path / "store")
    assert publish_report(store, "exp", RUN, str(tmp_path / "nope.md")) is None


def test_every_figure_is_copied(tmp_path: Path) -> None:
    plots = tmp_path / "plots"
    plots.mkdir()
    for name in ("discovery_vs_final", "latency_cost"):
        (plots / f"{name}.png").write_bytes(PNG)
    store = LocalStore(tmp_path / "store")

    written = publish_figures(store, "exp", RUN, str(plots))
    assert written == [
        f"experiments/exp/runs/{RUN}/plots/discovery_vs_final.png",
        f"experiments/exp/runs/{RUN}/plots/latency_cost.png",
    ]
    assert store.exists(written[0])


def test_figures_keep_their_bytes(tmp_path: Path) -> None:
    """Through write_bytes, not write_text — the latter would tag a PNG as JSON."""
    plots = tmp_path / "plots"
    plots.mkdir()
    payload = PNG + bytes(range(256))
    (plots / "x.png").write_bytes(payload)
    store = LocalStore(tmp_path / "store")

    written = publish_figures(store, "exp", RUN, str(plots))
    assert (tmp_path / "store" / written[0]).read_bytes() == payload


def test_a_missing_plots_directory_is_not_fatal(tmp_path: Path) -> None:
    """`recall_vs_tier` is skipped below two tiers, and `plot` can fail entirely."""
    store = LocalStore(tmp_path / "store")
    assert publish_figures(store, "exp", RUN, str(tmp_path / "absent")) == []


def test_the_merged_results_stay_off_the_run_folder() -> None:
    """Resume and merge both build this path from `experiment_id` alone. Version
    it and a resumed run cannot find the previous one's work — which is the one
    thing this layout change promised not to break."""
    from bq_context.runner.resume import experiment_prefix, run_prefix

    assert not run_prefix("exp", RUN).startswith(f"{experiment_prefix('exp')}/merged")
    assert experiment_prefix("exp") == "experiments/exp"


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
