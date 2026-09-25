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
    effective_env,
    merge_args,
    merge_report,
    publish_figures,
    publish_manifest,
    publish_report,
    run_manifest,
    run_preflight,
    run_questions,
    snapshot_questions_uri,
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
# The run manifest
#
# A run folder full of PNGs says nothing about what produced them. The manifest
# is what makes one self-describing: the commit, the corpus, the configuration
# and the completeness counts, in the same folder as the output they explain.
# ---------------------------------------------------------------------------
def _manifest(**overrides: object) -> dict:
    kwargs: dict = {
        "experiment_id": "hard-full-01",
        "run_id": RUN,
        "code_version": "abc1234",
        "pipeline_job": "projects/p/locations/l/pipelineJobs/j",
        "runs": 5,
        "tiers": [0, 1, 2, 3],
        "approaches": ["kc_context"],
        "question_limit": 0,
        "report": {"expected": 3000, "present": 3000, "missing_count": 0, "missing": []},
        "corpus": {"fingerprint": "13f9fcb4", "ladder": []},
        "questions": {"fingerprint": "a1b2c3d4", "count": 25},
        "environ": {"RESOURCE_PREFIX": "bigquery_context_hard", "CORPUS_PROFILE": "hard"},
    }
    kwargs.update(overrides)
    return run_manifest(**kwargs)  # ty: ignore[missing-argument]


def test_the_manifest_records_what_produced_the_run() -> None:
    manifest = _manifest()
    assert manifest["code_version"] == "abc1234"
    assert manifest["corpus_fingerprint"] == "13f9fcb4"
    assert manifest["resource_prefix"] == "bigquery_context_hard"
    assert manifest["corpus_profile"] == "hard"
    assert manifest["pipeline_job"].endswith("/pipelineJobs/j")


def test_the_manifest_records_completeness() -> None:
    """Straight from the merge report, not recounted — two counts that can
    disagree are worse than one."""
    manifest = _manifest()
    assert (manifest["expected"], manifest["present"], manifest["missing_count"]) == (3000, 3000, 0)


def test_the_manifest_never_carries_the_missing_cell_list() -> None:
    """`missing.json` already holds it, and on a badly broken run it is 3,000
    strings — enough to make the manifest the largest file in the folder."""
    assert "missing" not in _manifest()


def test_a_run_with_missing_cells_still_gets_a_manifest() -> None:
    """A failed run is exactly when someone needs to know what it was running."""
    manifest = _manifest(report={"expected": 3000, "present": 12, "missing_count": 2988})
    assert manifest["present"] == 12
    assert manifest["missing_count"] == 2988


def test_an_unconfigured_environment_leaves_the_fields_empty_not_absent() -> None:
    """A reader comparing two manifests must not have to distinguish "not set"
    from "this version did not record it"."""
    manifest = _manifest(environ={})
    assert manifest["resource_prefix"] == ""
    assert manifest["corpus_profile"] == ""


def test_the_manifest_records_the_corpus_actually_in_effect() -> None:
    """THE regression, caught by the first live run. `.env` carries
    RESOURCE_PREFIX but not CORPUS_PROFILE, so only the first is forwarded and
    the container falls back to setup.py's default. The manifest said
    `corpus_profile: ""` for a run that measured `base` — and "" reads as
    "unknown", which is worse than wrong in the one file whose job is provenance.
    """
    from bq_context.corpus import setup

    manifest = _manifest(environ=effective_env({}))
    assert manifest["corpus_profile"] == setup.CORPUS_PROFILE
    assert manifest["resource_prefix"] == setup.RESOURCE_PREFIX
    assert manifest["corpus_profile"], "an empty effective profile is not a value"


def test_an_explicit_setting_still_wins() -> None:
    """The resolution must not override a submitter who did set them."""
    resolved = effective_env({"RESOURCE_PREFIX": "custom_prefix", "AGENT_MODEL": "m"})
    assert resolved["RESOURCE_PREFIX"] == "custom_prefix"
    assert resolved["AGENT_MODEL"] == "m", "unrelated variables were dropped"


def test_the_manifest_lands_beside_the_report_it_explains(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "store")
    target = publish_manifest(store, "exp", RUN, {"run_id": RUN})
    assert target == f"experiments/exp/runs/{RUN}/manifest.json"
    assert json.loads(store.read_text(target))["run_id"] == RUN


def test_the_manifest_round_trips(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "store")
    original = _manifest()
    target = publish_manifest(store, "hard-full-01", RUN, original)
    assert json.loads(store.read_text(target)) == original


# ---------------------------------------------------------------------------
# What preflight saw, read back by the exit task
# ---------------------------------------------------------------------------
def test_the_preflight_record_is_read_back(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.write_text(
        f"experiments/exp/runs/{RUN}/preflight.json",
        json.dumps({"fingerprint": "13f9fcb4", "ladder": [{"tier": 3}]}),
    )
    assert run_preflight(store, "exp", RUN)["fingerprint"] == "13f9fcb4"


def test_no_preflight_record_is_not_fatal(tmp_path: Path) -> None:
    """Read from storage rather than taken as a task output, precisely so the
    exit task keeps no dependency on a task that may have died. The cost is that
    it can be absent, and an absent fingerprint must not lose the manifest."""
    assert run_preflight(LocalStore(tmp_path), "exp", RUN) == {"fingerprint": "", "ladder": []}


def test_a_corrupt_preflight_record_is_not_fatal(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.write_text(f"experiments/exp/runs/{RUN}/preflight.json", "{not json")
    assert run_preflight(store, "exp", RUN)["fingerprint"] == ""


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


# ---------------------------------------------------------------------------
# merge must score the questions the sweep actually ran
#
# `merge` computes expected cells from a question set. If the shards ran a
# custom set and the exit task uses the image's baked-in 25, every real cell is
# "unexpected" and every built-in question is "missing" -- so `require_complete`
# fails a run that is perfectly healthy. `merge_args` already carries this exact
# warning for `question_limit`.
# ---------------------------------------------------------------------------
def test_merge_reads_the_snapshot_when_the_sweep_had_one(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.write_text("experiments/byoq/questions.json", "{}")

    uri = snapshot_questions_uri(store, "byoq")
    assert uri.endswith("experiments/byoq/questions.json")
    assert "--questions" in merge_args(
        "byoq", "out", runs=1, tiers=[3], approaches=["a"], questions=uri
    )


def test_merge_falls_back_to_the_packaged_set_for_an_old_experiment(tmp_path: Path) -> None:
    """`full-01` and `hard-full-01` predate the snapshot. Nothing migrates them,
    so `merge` and `score` against them must keep working untouched."""
    assert snapshot_questions_uri(LocalStore(tmp_path), "full-01") == ""
    assert "--questions" not in merge_args("full-01", "out", runs=5, tiers=[0], approaches=["a"])


def test_the_questions_flag_carries_the_uri() -> None:
    args = merge_args(
        "e", "out", runs=1, tiers=[3], approaches=["a"], questions="gs://b/e/questions.json"
    )
    assert args[args.index("--questions") + 1] == "gs://b/e/questions.json"


def test_the_manifest_records_the_question_set() -> None:
    """A run folder should say what was *asked* as well as what was measured.
    Without it, two runs with identical corpus and commit are indistinguishable
    even when they answered different questions."""
    manifest = _manifest(questions={"fingerprint": "a1b2c3d4", "count": 25})
    assert manifest["questions_fingerprint"] == "a1b2c3d4"
    assert manifest["question_count"] == 25


def test_a_manifest_with_no_question_record_still_writes() -> None:
    """The exit task must publish on a run where the snapshot never landed."""
    manifest = _manifest(questions={})
    assert manifest["questions_fingerprint"] == ""
    assert manifest["question_count"] == 0


def test_the_question_record_is_read_back_from_the_snapshot(tmp_path: Path) -> None:
    from bq_context.runner.planner import questions_fingerprint

    store = LocalStore(tmp_path)
    questions = [{"id": "mine-q1", "category": "c", "question": "t", "relevance": {}}]
    store.write_text("experiments/e/questions.json", json.dumps({"questions": questions}))

    record = run_questions(store, "e")
    assert record["count"] == 1
    assert record["fingerprint"] == questions_fingerprint({"mine-q1": questions[0]})


def test_an_experiment_with_no_snapshot_reports_zero_questions(tmp_path: Path) -> None:
    """True of `full-01`, which predates snapshots. Unknown, not a crash — the
    exit task has to publish a manifest for it too."""
    assert run_questions(LocalStore(tmp_path), "full-01") == {"fingerprint": "", "count": 0}


def test_a_torn_snapshot_does_not_lose_the_manifest(tmp_path: Path) -> None:
    store = LocalStore(tmp_path)
    store.write_text("experiments/e/questions.json", "{not json")
    assert run_questions(store, "e")["count"] == 0
