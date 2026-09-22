"""The CLI surface.

Every pipeline component is a thin wrapper over one of these subcommands, so a
broken flag here breaks the pipeline in a way that only shows up after a
container build and a job submission. These tests keep that loop at zero
seconds. Commands that touch the network are exercised only for their argument
handling and their refusal paths.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bq_context.cli import (
    REQUIRED_PERMISSIONS,
    _credentials,
    _effective_identity,
    app,
    assess_ladder,
)
from bq_context.runner.cells import APPROACHES
from bq_context.runner.models import Cell, ShardSpec
from bq_context.runner.resume import shard_prefix
from bq_context.runner.store import LocalStore

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

QUESTIONS = [
    {
        "id": "q1",
        "category": "single-table",
        "question": "how many trips?",
        "relevance": {"must_have": ["trips"]},
    },
    {
        "id": "q2",
        "category": "trap",
        "question": "how many docks?",
        "relevance": {"must_have": ["stations"], "distractor": ["citibike_stations"]},
    },
]


@pytest.fixture
def questions_file(tmp_path: Path) -> Path:
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(QUESTIONS))
    return path


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    """A store holding one complete shard, as run-shard would have left it."""
    store = LocalStore(tmp_path / "store")
    spec = ShardSpec(
        experiment_id="e1",
        tier=0,
        approach="kc_search",
        question_ids=["q1", "q2"],
        runs=1,
        code_version="v1",
    )
    body = "".join(
        Cell(
            cell_key=f"{qid}|kc_search|tier0|run0",
            question_id=qid,
            approach="kc_search",
            tier=0,
            run_idx=0,
            status="ok",
            category="single-table",
            relevance={"must_have": [want]},
            nominated=[f"p.d.{want}"],
            nominated_count=1,
            ranked_tables=[{"table_id": f"p.d.{want}", "rank": 1, "confidence": 0.9}],
            ranked_count=1,
            latency_s=1.0,
            reranker_total_tokens=100,
            reranker_calls=1,
        ).to_jsonl()
        for qid, want in (("q1", "trips"), ("q2", "stations"))
    )
    store.write_text(f"{shard_prefix(spec)}/attempt-0001.jsonl", body)
    return tmp_path / "store"


# ---------------------------------------------------------------------------
# Surface
# ---------------------------------------------------------------------------
def test_help_lists_every_documented_subcommand() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "validate-config",
        "ensure-infra",
        "preflight",
        "run-shard",
        "merge",
        "score",
        "plot",
        "plan-shards",
        "cleanup",
    ):
        assert command in result.stdout, command


def test_version_is_a_subcommand_not_swallowed_as_an_argument() -> None:
    """Typer collapses single-command apps; the root callback prevents that."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip()


def test_missing_project_fails_with_a_useful_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    result = runner.invoke(app, ["validate-config"])
    assert result.exit_code == 2
    assert "GOOGLE_CLOUD_PROJECT" in result.output


# ---------------------------------------------------------------------------
# plan-shards
# ---------------------------------------------------------------------------
def test_plan_shards_emits_the_full_factorial(questions_file: Path) -> None:
    result = runner.invoke(app, ["plan-shards", "-e", "e1", "--questions", str(questions_file)])
    assert result.exit_code == 0, result.output
    specs = json.loads(result.stdout)
    assert len(specs) == 4 * len(APPROACHES)
    assert {s["tier"] for s in specs} == {0, 1, 2, 3}


def test_plan_shards_honours_filters(questions_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "plan-shards",
            "-e",
            "e1",
            "-t",
            "3",
            "-a",
            "search_direct",
            "--questions",
            str(questions_file),
        ],
    )
    assert result.exit_code == 0, result.output
    specs = json.loads(result.stdout)
    assert len(specs) == 1
    assert specs[0]["tier"] == 3
    assert specs[0]["approach"] == "search_direct"


def test_plan_shards_stamps_a_code_version(questions_file: Path) -> None:
    """KFP caches on component inputs; without this a prompt edit reruns stale."""
    result = runner.invoke(app, ["plan-shards", "-e", "e1", "--questions", str(questions_file)])
    specs = json.loads(result.stdout)
    assert all(s["code_version"] for s in specs)


def test_unknown_approach_is_rejected_before_any_work(questions_file: Path) -> None:
    result = runner.invoke(
        app, ["plan-shards", "-e", "e1", "-a", "nope", "--questions", str(questions_file)]
    )
    assert result.exit_code == 2
    assert "Unknown approach" in result.output


def test_missing_questions_file_is_rejected() -> None:
    result = runner.invoke(app, ["plan-shards", "-e", "e1", "--questions", "/nope/questions.json"])
    assert result.exit_code == 2
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# merge / score / plot
# ---------------------------------------------------------------------------
def test_merge_reports_completeness(seeded: Path, questions_file: Path) -> None:
    result = runner.invoke(
        app,
        [
            "merge",
            "-e",
            "e1",
            "--out",
            str(seeded),
            "--runs",
            "1",
            "-t",
            "0",
            "-a",
            "kc_search",
            "--questions",
            str(questions_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "2/2 cells" in result.output


def test_merge_surfaces_missing_cells(seeded: Path, questions_file: Path) -> None:
    """Expecting all six approaches when only one ran."""
    result = runner.invoke(
        app,
        [
            "merge",
            "-e",
            "e1",
            "--out",
            str(seeded),
            "--runs",
            "1",
            "-t",
            "0",
            "--questions",
            str(questions_file),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "10 missing" in result.output
    assert "missing (first 5)" in result.output


def test_score_renders_a_report(seeded: Path, questions_file: Path, tmp_path: Path) -> None:
    runner.invoke(
        app,
        [
            "merge",
            "-e",
            "e1",
            "--out",
            str(seeded),
            "--runs",
            "1",
            "-t",
            "0",
            "-a",
            "kc_search",
            "--questions",
            str(questions_file),
        ],
    )
    report = tmp_path / "report.md"
    result = runner.invoke(
        app, ["score", "-e", "e1", "--out", str(seeded), "--report", str(report)]
    )

    assert result.exit_code == 0, result.output
    assert "Discovery vs rerank" in result.stdout
    assert "2: KC Search" in result.stdout
    assert report.exists()


def test_score_without_merged_results_fails_with_guidance(tmp_path: Path) -> None:
    result = runner.invoke(app, ["score", "-e", "nothing", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert "merge" in result.output


def test_plot_writes_figures(seeded: Path, questions_file: Path, tmp_path: Path) -> None:
    runner.invoke(
        app,
        [
            "merge",
            "-e",
            "e1",
            "--out",
            str(seeded),
            "--runs",
            "1",
            "-t",
            "0",
            "-a",
            "kc_search",
            "--questions",
            str(questions_file),
        ],
    )
    plots = tmp_path / "plots"
    result = runner.invoke(
        app, ["plot", "-e", "e1", "--out", str(seeded), "--plots-dir", str(plots)]
    )

    assert result.exit_code == 0, result.output
    assert (plots / "discovery_vs_final.png").exists()
    assert (plots / "latency_cost.png").exists()
    # Only one tier present, so the tier curve is correctly skipped.
    assert not (plots / "recall_vs_tier.png").exists()


# ---------------------------------------------------------------------------
# run-shard argument handling (no network)
# ---------------------------------------------------------------------------
def test_run_shard_rejects_an_unknown_approach(questions_file: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run-shard",
            "-e",
            "e1",
            "--tier",
            "0",
            "--approach",
            "bogus",
            "--out",
            str(tmp_path),
            "--questions",
            str(questions_file),
        ],
    )
    assert result.exit_code == 2
    assert "Unknown approach" in result.output


def test_run_shard_rejects_unknown_question_ids(questions_file: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run-shard",
            "-e",
            "e1",
            "--tier",
            "0",
            "--approach",
            "search_direct",
            "--out",
            str(tmp_path),
            "--questions",
            str(questions_file),
            "--questions-ids",
            "q1,nope",
        ],
    )
    assert result.exit_code == 2
    assert "nope" in result.output


def test_run_shard_rejects_an_out_of_range_tier(questions_file: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "run-shard",
            "-e",
            "e1",
            "--tier",
            "9",
            "--approach",
            "search_direct",
            "--out",
            str(tmp_path),
            "--questions",
            str(questions_file),
        ],
    )
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# preflight ladder assessment
# ---------------------------------------------------------------------------
def rung(tier: int, size: int, profiled: int = 15, aspects: list[str] | None = None) -> dict:
    return {
        "tier": tier,
        "tables": 15,
        "bytes": size,
        "profiled": profiled,
        "aspects": aspects or [],
    }


def test_a_healthy_ladder_has_no_complaints() -> None:
    ladder = [
        rung(0, 49_000, profiled=0),
        rung(1, 118_000),
        rung(2, 130_000, aspects=["related_terms"]),
        rung(3, 140_000, aspects=["guidelines", "related_terms"]),
    ]
    problems, warnings = assess_ladder(ladder, empty=False)
    assert problems == []
    assert warnings == []


def test_a_flat_rung_is_warned_about_even_when_the_ladder_climbs_overall() -> None:
    """The failure this gate exists for, and the one it originally missed.

    Observed live on 2026-09-22: glossary entry links are created successfully
    but never surface in the context capsule, so tier 2 is byte-for-byte
    equivalent to tier 1 while the factorial still treats them as distinct
    levels. Endpoint-only checks pass this happily.
    """
    ladder = [
        rung(0, 49_089, profiled=0),
        rung(1, 118_275),
        rung(2, 119_882),  # glossary never lands
        rung(3, 122_346, aspects=["overview"]),
    ]
    problems, warnings = assess_ladder(ladder, empty=False)

    assert problems == [], "the ladder does climb overall, so this is not fatal"
    assert len(warnings) == 1
    assert "tier 2 adds nothing over tier 1" in warnings[0]


def test_an_empty_cache_is_fatal_and_names_the_likely_cause() -> None:
    problems, _ = assess_ladder([rung(0, 0, profiled=0), rung(3, 0, profiled=0)], empty=True)
    assert any("EMPTY" in p and "catalogViewer" in p for p in problems)


def test_a_non_climbing_ladder_is_fatal() -> None:
    problems, _ = assess_ladder([rung(0, 50_000), rung(3, 50_000)], empty=False)
    assert any("not larger" in p for p in problems)


def test_a_rung_that_only_gains_metadata_bytes_still_warns() -> None:
    """Timestamps and entry ids differ between datasets; that is not enrichment."""
    _, warnings = assess_ladder([rung(1, 118_000), rung(2, 118_900)], empty=False)
    assert len(warnings) == 1


def test_a_rung_gaining_a_new_aspect_is_not_flat() -> None:
    _, warnings = assess_ladder(
        [rung(2, 118_000), rung(3, 118_500, aspects=["overview"])], empty=False
    )
    assert warnings == []


# ---------------------------------------------------------------------------
# IAM preflight
# ---------------------------------------------------------------------------
def test_required_permissions_cover_every_api_the_experiment_uses() -> None:
    """A missing entry here is a grant nobody discovers until it fails live."""
    flat = {p for group in REQUIRED_PERMISSIONS.values() for p in group}

    # The four services the experiment actually calls.
    assert any(p.startswith("bigquery.") for p in flat)
    assert any(p.startswith("dataplex.") for p in flat)
    assert any(p.startswith("aiplatform.") for p in flat)
    assert "resourcemanager.projects.get" in flat


def test_dataplex_entries_get_is_checked() -> None:
    """The single most important permission in the list.

    Without it lookupContext returns an EMPTY response rather than 403, so every
    tier scores identically, the pipeline goes green, and the run is
    indistinguishable from a genuine null result.
    """
    flat = {p for group in REQUIRED_PERMISSIONS.values() for p in group}
    assert "dataplex.entries.get" in flat


def test_permissions_are_grouped_by_purpose_not_by_service() -> None:
    """Groups become the failure message, so they must read as actions."""
    for purpose in REQUIRED_PERMISSIONS:
        assert " " in purpose, f"{purpose!r} should describe an action"
    assert all(REQUIRED_PERMISSIONS.values()), "no empty groups"


def test_impersonate_is_offered_on_both_gates() -> None:
    """Checking as yourself proves nothing; both gates must support the SA."""
    for command in ("validate-config", "preflight"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0
        assert "--impersonate" in result.stdout, command


def test_no_impersonation_means_no_credentials_object() -> None:
    assert _credentials("") is None


def test_metadata_alias_is_not_mistaken_for_an_identity() -> None:
    """Second pipeline failure: a correct run was rejected by its own gate.

    GCE-family credentials report service_account_email as the literal string
    "default", and it stays "default" after a refresh. Because that is truthy,
    --expect-identity compared "default" against the real SA and failed a
    pipeline that Vertex had in fact configured correctly.
    """

    class FakeGceCreds:
        service_account_email = "default"

    assert _effective_identity(FakeGceCreds()) == ""  # type: ignore[arg-type]


def test_a_real_service_account_email_passes_through() -> None:
    class FakeSaCreds:
        service_account_email = "svc@p.iam.gserviceaccount.com"

    assert _effective_identity(FakeSaCreds()) == "svc@p.iam.gserviceaccount.com"  # type: ignore[arg-type]


def test_user_credentials_resolve_to_nothing_rather_than_the_vm_sa() -> None:
    """The metadata server answers under user ADC too, with the VM's account.

    Reporting it would be a confident lie: the calls are made as the user.
    """

    class FakeUserCreds:
        pass

    assert _effective_identity(FakeUserCreds()) == ""  # type: ignore[arg-type]
