"""The CLI surface.

Every pipeline component is a thin wrapper over one of these subcommands, so a
broken flag here breaks the pipeline in a way that only shows up after a
container build and a job submission. These tests keep that loop at zero
seconds. Commands that touch the network are exercised only for their argument
handling and their refusal paths.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from bq_context import cli
from bq_context.cli import (
    REQUIRED_PERMISSIONS,
    _credentials,
    _effective_identity,
    app,
    assess_ladder,
    assess_search_convergence,
)
from bq_context.runner.cells import APPROACHES
from bq_context.runner.models import Cell, ShardSpec
from bq_context.runner.resume import shard_prefix
from bq_context.runner.store import LocalStore

if TYPE_CHECKING:
    from collections.abc import Callable
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
def registered_commands() -> dict[str, object]:
    """The CLI's commands, by introspection rather than by scraping --help.

    Rendered help is a presentation concern: it carries ANSI codes, wraps at the
    terminal width, and can elide long option names. A test that greps it is
    asserting on formatting, not on the interface — which is how
    test_impersonate_is_offered_on_both_gates passed locally and failed on a CI
    runner. Introspection is deterministic everywhere.
    """
    import typer.main

    return dict(typer.main.get_command(app).commands)  # type: ignore[attr-defined]


def option_names(command: str) -> set[str]:
    """Every flag declared on a command, e.g. {"--tier", "-t"}."""
    params = registered_commands()[command].params  # type: ignore[attr-defined]
    return {opt for p in params for opt in getattr(p, "opts", [])}


def test_every_documented_subcommand_is_registered() -> None:
    commands = registered_commands()
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
        "compile-pipeline",
        "submit-pipeline",
    ):
        assert command in commands, command


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
def rung(
    tier: int,
    size: int,
    profiled: int = 209,
    terms: int = 0,
    aspects: list[str] | None = None,
) -> dict:
    return {
        "tier": tier,
        "tables": 15,
        "bytes": size,
        "profiled": profiled,
        "terms": terms,
        "aspects": aspects or [],
    }


def test_a_healthy_ladder_has_no_complaints() -> None:
    ladder = [
        rung(0, 49_000, profiled=0),
        rung(1, 118_000),
        rung(2, 119_882, terms=24),
        rung(3, 122_346, terms=24, aspects=["overview"]),
    ]
    problems, warnings = assess_ladder(ladder, empty=False)
    assert problems == []
    assert warnings == []


def test_a_flat_rung_is_warned_about_even_when_the_ladder_climbs_overall() -> None:
    """A rung that genuinely adds nothing must still be caught.

    Endpoint-only checks pass this happily: the ladder climbs 49KB -> 122KB
    overall while one rung in the middle contributes no enrichment at all.
    """
    ladder = [
        rung(0, 49_089, profiled=0),
        rung(1, 118_275),
        rung(2, 119_882),  # same features as tier 1 despite +1.6KB
        rung(3, 122_346, aspects=["overview"]),
    ]
    problems, warnings = assess_ladder(ladder, empty=False)

    assert problems == [], "the ladder does climb overall, so this is not fatal"
    assert len(warnings) == 1
    assert "tier 2 adds no enrichment over tier 1" in warnings[0]


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


def test_glossary_enrichment_is_detected_even_though_it_is_tiny() -> None:
    """The bug this gate actually had, inverted.

    Real glossary enrichment across 15 tables is ~1.6 KB — below any sensible
    byte threshold. An earlier version compared byte deltas and declared a
    fully-enriched tier 2 dead. Feature counts, not bytes.
    """
    _, warnings = assess_ladder([rung(1, 118_275), rung(2, 119_882, terms=24)], empty=False)
    assert warnings == [], "24 glossary-annotated columns is not 'nothing'"


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
        assert "--impersonate" in option_names(command), command


def test_validate_config_can_assert_identity_without_impersonating() -> None:
    """The pipeline path: a task already IS the SA and cannot impersonate itself."""
    assert "--expect-identity" in option_names("validate-config")


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


# ---------------------------------------------------------------------------
# Search index convergence
#
# The guard compares each tier against *itself over time*. It used to compare
# tiers against each other, on the premise that "the corpus is identical across
# tiers, so a converged index returns the same count everywhere". Only the
# *tables* are identical. The searchable metadata is what differs, and it is the
# experiment's independent variable — so unequal counts are the measurement, not
# a fault.
#
# Measured on the live corpus: tiers 0-2 return 3 hits for the probe and tier 3
# returns 4, stably across four consecutive samples. The extra hit is the NYC
# taxi table matching a bike-share question, because tier 3's overview aspect
# adds text that makes an irrelevant table match. Permanent and correct — and the
# old rule reported it as a broken index on every run.
# ---------------------------------------------------------------------------
def test_a_stable_index_produces_no_warning() -> None:
    stable = {"tier0": 3, "tier1": 3, "tier2": 3, "tier3": 4}
    assert assess_search_convergence(stable, dict(stable)) == []


def test_tier_differences_alone_are_never_a_warning() -> None:
    """THE regression. This shape occurs on the live corpus every run, and it is
    enrichment working rather than a fault."""
    observed = {"tier0": 3, "tier1": 3, "tier2": 3, "tier3": 4}
    assert assess_search_convergence(observed, dict(observed)) == []
    # Even a large spread, if it is not moving, is a measurement not drift.
    big = {"tier0": 2, "tier1": 4, "tier2": 6, "tier3": 9}
    assert assess_search_convergence(big, dict(big)) == []


def test_a_moving_index_is_caught() -> None:
    """The real failure, replayed as what it actually is: a tier whose own count
    changes between two observations.

    In full-01 the search approaches showed recall climbing 0.52 -> 0.68 -> 0.97
    across tiers, reading as an enrichment effect. It was not — shards run in plan
    order and the index was still warming, so tier was confounded with elapsed
    time. Re-running hours later gave an identical 0.967 everywhere.
    """
    warnings = assess_search_convergence({"tier0": 2, "tier1": 3}, {"tier0": 3, "tier1": 3})
    assert len(warnings) == 1
    assert "tier0: 2 -> 3" in warnings[0]
    assert "confounded with elapsed time" in warnings[0]


def test_drift_that_is_uniform_across_tiers_is_still_caught() -> None:
    """The old cross-tier rule was blind to this: every tier moving by the same
    amount left the spread unchanged and looked converged."""
    assert len(assess_search_convergence({"tier0": 3, "tier1": 4}, {"tier0": 5, "tier1": 6})) == 1


def test_one_observation_cannot_assess_anything() -> None:
    assert assess_search_convergence({"tier0": 3}) == []
    assert assess_search_convergence({"tier0": 3}, None) == []
    assert assess_search_convergence({}, {}) == []


def test_only_tiers_present_in_both_are_compared() -> None:
    """A tier missing from one pass is not evidence of drift."""
    assert assess_search_convergence({"tier0": 3, "tier1": 4}, {"tier0": 3}) == []


def test_preflight_probes_twice_and_reports_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers the wiring, which the unit tests above do not.

    Deleting the `assess_search_convergence(probe, again)` call from preflight
    leaves every test in this section green, because they exercise the function
    directly. Found by mutation.
    """
    import time

    from bq_context import cli
    from bq_context.context_cache import TableCache

    # One element per search, holding which probe pass it belongs to. The search
    # function is bound once and reused across both passes, so counting bindings
    # would report one.
    calls: list[int] = []
    seen: dict[tuple[int, str], int] = {}

    def _live(_config: object) -> Callable[[int, str], list]:
        def _search(tier: int, question: str) -> list:
            key = (tier, question)
            seen[key] = seen.get(key, 0) + 1
            calls.append(seen[key])
            # The second pass returns a different table: the index moved while
            # preflight was running.
            return [_Hit("p.d.hurricanes" if seen[key] == 1 else "p.d.zip_codes")]

        return _search

    monkeypatch.setattr(cli, "_live_search", _live)
    monkeypatch.setattr(cli, "_credentials", lambda _: None)
    # Without this the test is not hermetic: `_effective_identity(None)` resolves
    # Application Default Credentials, which pass on a developer box and raise
    # DefaultCredentialsError in CI — where it failed, having passed locally.
    monkeypatch.setattr(cli, "_effective_identity", lambda _: "sa@test-project.iam")
    monkeypatch.setattr(
        cli,
        "_tier_profile",
        lambda t, _c: {
            "tier": t,
            "tables": 15,
            "bytes": 10 * (t + 1),
            "profiled": 0,
            "terms": 0,
            "aspects": [],
        },
    )
    monkeypatch.setattr(cli, "assess_ladder", lambda *_, **__: ([], []))
    monkeypatch.setattr(time, "sleep", lambda _s: None)

    class _Ctx:
        @staticmethod
        def build(*_: object, **__: object) -> object:
            return object()

    monkeypatch.setattr("bq_context.runtime.TierContext", _Ctx)
    monkeypatch.setattr("bq_context.runtime.tier_scope", contextlib.nullcontext)
    monkeypatch.setattr("bq_context.runtime.get_datasets", lambda: ["d"])
    monkeypatch.setattr("bq_context.runtime.get_scoped_tables", lambda _d: ["t"])
    monkeypatch.setattr(
        TableCache, "build", classmethod(lambda _cls, *_a, **_k: TableCache.empty())
    )

    result = runner.invoke(app, ["preflight", "--tier", "1", "--baseline", "0", "--settle", "1"])
    assert set(calls) == {1, 2}, f"expected two probe passes, saw {sorted(set(calls))}"
    assert "second pass" in result.output
    assert "confounded with elapsed time" in result.output


# ---------------------------------------------------------------------------
# run-shard's exit code
# ---------------------------------------------------------------------------
def test_run_shard_exits_nonzero_when_a_cell_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE fix. It used to exit 0 regardless of the outcome.

    The shard wrote `_FAILED: 124 ok, 1 failed`, Vertex recorded the task
    SUCCEEDED, KFP cached it, and the resubmit was a cache hit -- the shard never
    ran, resume never got a chance, and one transient 500 made a 3,000-cell sweep
    unrecoverable except with `--no-cache`. The marker was written and ignored.
    """
    from bq_context import cli
    from bq_context.runner.models import ShardResult

    incomplete = ShardResult(
        shard_id="tier1__search_direct",
        experiment_id="e",
        tier=1,
        approach="search_direct",
        code_version="v1",
        planned=125,
        already_done=0,
        executed=125,
        succeeded=124,
        failed=1,
    )
    monkeypatch.setattr("bq_context.runner.cells.execute_shard", lambda *_a, **_k: incomplete)
    # Also stub the store: `run-shard` builds one from ADC, which exists on a
    # developer box and not in CI. Leaving it real made these pass locally and
    # fail on the runner with DefaultCredentialsError -- the same hermeticity
    # trap as `_effective_identity` in the preflight test above.
    monkeypatch.setattr(cli, "store_for", lambda *_a, **_k: object())

    result = runner.invoke(
        app, ["run-shard", "-e", "e", "--tier", "1", "--approach", "search_direct", "--limit", "1"]
    )
    assert result.exit_code == 1, result.output
    assert "1 cell(s) failed" in result.output


def test_run_shard_exits_zero_when_the_shard_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common path must not start failing runs that are fine."""
    from bq_context import cli
    from bq_context.runner.models import ShardResult

    clean = ShardResult(
        shard_id="tier1__search_direct",
        experiment_id="e",
        tier=1,
        approach="search_direct",
        code_version="v1",
        planned=5,
        already_done=0,
        executed=5,
        succeeded=5,
        failed=0,
    )
    monkeypatch.setattr("bq_context.runner.cells.execute_shard", lambda *_a, **_k: clean)
    # Also stub the store: `run-shard` builds one from ADC, which exists on a
    # developer box and not in CI. Leaving it real made these pass locally and
    # fail on the runner with DefaultCredentialsError -- the same hermeticity
    # trap as `_effective_identity` in the preflight test above.
    monkeypatch.setattr(cli, "store_for", lambda *_a, **_k: object())

    result = runner.invoke(
        app, ["run-shard", "-e", "e", "--tier", "1", "--approach", "search_direct", "--limit", "1"]
    )
    assert result.exit_code == 0, result.output


class _Hit:
    """Stand-in for discovery_common.SearchHit; only table_id is read here."""

    def __init__(self, table_id: str) -> None:
        self.table_id = table_id


# ---------------------------------------------------------------------------
# The convergence probe must use the questions that will actually be asked
#
# `enrich-probe-01` is why. Its 12 questions were novel, and tier-0 search
# recall measured 0.292 during the run and 0.625 forty minutes later -- same
# code, same corpus. The guard reported a settled index throughout, because it
# probed with one hard-coded question that happened to be `single-q1`: asked
# thousands of times across every previous run, and therefore maximally warm.
#
# A warm fixed probe says nothing about a cold novel query.
# ---------------------------------------------------------------------------
def test_the_probe_covers_every_question_in_every_tier() -> None:
    """One cold question is enough to invalidate a sweep, so sampling will not
    do — and probing everything doubles as the warm-up pass."""
    seen: list[tuple[int, str]] = []
    questions = {"q1": {"question": "a"}, "q2": {"question": "b"}}

    def _search(tier: int, question: str) -> list:
        seen.append((tier, question))
        return []

    labels = cli._probe_search_labels(questions, [0, 3], _search)
    assert {t for t, _ in seen} == {0, 3}
    assert {q for _, q in seen} == {"a", "b"}
    assert set(labels) == {"tier0/q1", "tier0/q2", "tier3/q1", "tier3/q2"}


def test_the_probe_records_which_tables_came_back_not_just_how_many() -> None:
    """A count can hold still while the identities change underneath it. Recall
    depends on identity, so that is what has to be compared."""

    def _search(_tier: int, _question: str) -> list:
        return [_Hit("p.d.hurricanes"), _Hit("p.d.zip_codes")]

    labels = cli._probe_search_labels({"q1": {"question": "a"}}, [0], _search)
    assert labels["tier0/q1"] == ("hurricanes", "zip_codes")


def test_swapped_results_with_an_identical_count_are_caught() -> None:
    """THE gap this closes. `cat-landfall` at tier 0 returned two wrong tables
    during the run and the right one afterwards. A count-only guard can miss
    that entirely; comparing identities cannot."""
    first = {"tier0/cat-landfall": ("air_quality_annual_summary", "zip_codes")}
    second = {"tier0/cat-landfall": ("hurricanes", "us_counties")}
    warnings = assess_search_convergence(first, second)

    assert len(warnings) == 1
    assert "tier0/cat-landfall" in warnings[0]


def test_a_settled_question_set_produces_no_warning() -> None:
    settled = {"tier0/q1": ("hurricanes",), "tier3/q1": ("hurricanes", "us_counties")}
    assert assess_search_convergence(settled, dict(settled)) == []


def test_the_warning_names_the_question_so_it_can_be_acted_on() -> None:
    """ "The index moved" is not actionable. "cat-landfall at tier 0 moved" tells
    you which cell to distrust."""
    warnings = assess_search_convergence(
        {"tier0/cat-kiosk": (), "tier0/cat-knots": ("hurricanes",)},
        {"tier0/cat-kiosk": ("austin_bikeshare_stations",), "tier0/cat-knots": ("hurricanes",)},
    )
    assert len(warnings) == 1
    assert "cat-kiosk" in warnings[0]
    assert "cat-knots" not in warnings[0], "a settled question must not be named"
