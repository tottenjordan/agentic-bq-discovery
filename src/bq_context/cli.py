"""The single entrypoint every other surface wraps.

Each KFP pipeline component is a thin wrapper over one of these subcommands, so
the pipeline and a local shell run exactly the same code path. That is
deliberate: it keeps the debug loop in seconds rather than container builds, and
a pipeline failure can always be reproduced locally with one command.

    bq-context validate-config    # fail fast: identity, permissions, models
    bq-context ensure-infra       # create the 4-tier corpus (12-40 min)
    bq-context preflight          # assert enrichment is real before measuring
    bq-context run-shard          # execute one (tier, approach) shard
    bq-context merge              # collect shard JSONL into one deduped file
    bq-context score              # metrics + markdown report
    bq-context plot               # figures
"""

from __future__ import annotations

import itertools
import json
import logging
import subprocess
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from bq_context.config import TIERS, ExperimentConfig
from bq_context.runner.cells import APPROACHES
from bq_context.runner.models import ShardSpec
from bq_context.runner.store import store_for

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from bq_context.context_cache import TableCache

app = typer.Typer(
    name="bq-context",
    help="Evaluate six BigQuery table-discovery approaches.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_QUESTIONS = Path("experiments/questions.json")
DEFAULT_OUT = "gs://hybrid-vertex-bq-context"

# -- shared option types ----------------------------------------------------
ExperimentId = Annotated[
    str,
    typer.Option(
        "--experiment-id",
        "-e",
        help="Stable id; the GCS prefix derives from it and resume depends on it being reused.",
    ),
]
OutOpt = Annotated[str, typer.Option("--out", help="gs://bucket/prefix or a local directory.")]
# NB: an Annotated alias carries its flag name, so reusing TierOpt for a second
# parameter silently binds both to --tier. Any other tier-valued option needs
# its own alias; see BaselineOpt.
TierOpt = Annotated[int, typer.Option("--tier", "-t", min=0, max=3)]
BaselineOpt = Annotated[
    int,
    typer.Option("--baseline", min=0, max=3, help="Tier to compare enrichment against."),
]
QuestionsOpt = Annotated[Path, typer.Option("--questions", help="Path to questions.json.")]


@app.callback()
def _root(
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")] = False,
) -> None:
    """Keep this a multi-command app and configure logging once.

    Typer collapses a single-command app into a bare CLI; an explicit callback
    pins the subcommand form regardless of how many commands are registered.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _config() -> ExperimentConfig:
    try:
        return ExperimentConfig.from_env()
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from exc


def _load_questions(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        typer.secho(f"Questions file not found: {path}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    raw = json.loads(path.read_text())
    items = raw["questions"] if isinstance(raw, dict) else raw
    return {str(q["id"]): q for q in items}


def _code_version() -> str:
    """Short git SHA, or 'unknown' outside a checkout.

    Threaded into every shard because KFP's cache key includes component inputs:
    without it, editing a prompt and rerunning would silently return cached
    results produced by the old code.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return out.stdout.strip() or "unknown"


#: Below this, a tier's extra bytes are timestamps and ids rather than content.
_FLAT_RUNG_BYTES = 4096


def _tier_profile(tier: int, cache: TableCache) -> dict[str, Any]:
    """Summarise what enrichment actually reached one tier's capsules.

    Byte count alone is not enough to tell tiers apart: metadata timestamps and
    entry ids differ between datasets, so two functionally identical tiers can
    still differ by a kilobyte or two. The aspect keys and the profiled-table
    count are what actually distinguish them.
    """
    aspects: set[str] = set()
    profiled = 0
    for entry in cache.entries.values():
        capsule = json.loads(entry.detailed)
        if any("dataProfile" in column for column in capsule.get("schema", [])):
            profiled += 1
        for key in ("guidelines", "overview", "related_terms", "business_descriptions"):
            if key in capsule:
                aspects.add(key)
    return {
        "tier": tier,
        "tables": len(cache.entries),
        "bytes": len(cache.all_detailed()),
        "profiled": profiled,
        "aspects": sorted(aspects),
    }


def assess_ladder(ladder: list[dict[str, Any]], *, empty: bool) -> tuple[list[str], list[str]]:
    """Judge an enrichment ladder. Returns (fatal problems, warnings).

    Checks every rung, not just the endpoints. A ladder can gain a lot overall
    while one step contributes nothing — and that step is then a factor level
    silently measuring the level below it, which is worse than a missing tier
    because the factorial still reports the two as distinct.
    """
    problems: list[str] = []
    warnings: list[str] = []
    top, bottom = ladder[-1], ladder[0]

    if empty:
        problems.append(
            f"tier {top['tier']} context cache is EMPTY. lookupContext returns an "
            "empty response rather than 403 when permissions are missing, so check "
            "roles/dataplex.catalogViewer before assuming the tier is empty."
        )
    if top["bytes"] <= bottom["bytes"]:
        problems.append(
            f"tier {top['tier']} context ({top['bytes']:,} bytes) is not larger "
            f"than tier {bottom['tier']} ({bottom['bytes']:,} bytes). Enrichment is "
            "not reaching the capsule, so any tier comparison would measure nothing."
        )

    for lower, upper in itertools.pairwise(ladder):
        gained = upper["bytes"] - lower["bytes"]
        same_shape = upper["aspects"] == lower["aspects"] and upper["profiled"] == lower["profiled"]
        if same_shape and gained < _FLAT_RUNG_BYTES:
            warnings.append(
                f"tier {upper['tier']} adds nothing over tier {lower['tier']} "
                f"(+{gained:,} bytes, identical aspects). That tier is not a "
                "distinct factor level; treat its results as a duplicate of "
                f"tier {lower['tier']}."
            )
    return problems, warnings


#: The permissions this experiment actually exercises, grouped by what needs
#: them. Checked up front so a missing grant costs 30 seconds rather than
#: surfacing 40 minutes into ensure-infra, or — worse — as an empty
#: lookupContext response that looks like a genuine null result.
REQUIRED_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "create the tier datasets and views": (
        "bigquery.datasets.create",
        "bigquery.datasets.get",
        "bigquery.tables.create",
        "bigquery.tables.get",
        "bigquery.tables.list",
        "bigquery.tables.update",
    ),
    "run queries and profile scans": (
        "bigquery.jobs.create",
        "dataplex.datascans.create",
        "dataplex.datascans.run",
    ),
    "write catalog enrichment": (
        "dataplex.entries.update",
        "dataplex.entryLinks.create",
        "dataplex.glossaries.create",
    ),
    "read catalog context": (
        "dataplex.entries.get",
        "dataplex.entryGroups.get",
    ),
    "call Gemini": ("aiplatform.endpoints.predict",),
    "resolve the project": ("resourcemanager.projects.get",),
}


def _credentials(impersonate: str) -> Credentials | None:
    """ADC, or impersonated credentials for ``impersonate``.

    Checking permissions as yourself proves nothing: a developer ADC account is
    typically near-Owner, so everything passes locally and the pipeline fails on
    a fresh principal. Impersonation is the only way to ask the question that
    matters.
    """
    if not impersonate:
        return None
    import google.auth  # noqa: PLC0415
    from google.auth import impersonated_credentials  # noqa: PLC0415

    source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=impersonate,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )


def _check_permissions(project: str, credentials: Credentials | None) -> list[str]:
    """Return the required permissions the caller is missing."""
    from google.cloud import resourcemanager_v3  # noqa: PLC0415

    client = resourcemanager_v3.ProjectsClient(credentials=credentials)
    wanted = sorted({p for group in REQUIRED_PERMISSIONS.values() for p in group})
    # testIamPermissions caps at 100 per call; we are well under.
    granted = set(
        client.test_iam_permissions(resource=f"projects/{project}", permissions=wanted).permissions
    )
    return [p for p in wanted if p not in granted]


def _selected(approaches: list[str] | None, tiers: list[int] | None) -> tuple[list[str], list[int]]:
    chosen = approaches or list(APPROACHES)
    unknown = [a for a in chosen if a not in APPROACHES]
    if unknown:
        typer.secho(
            f"Unknown approach(es): {', '.join(unknown)}. Valid: {', '.join(APPROACHES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return chosen, tiers or list(TIERS)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
@app.command()
def version() -> None:
    """Print the installed package version."""
    typer.echo(pkg_version("bq-context"))


@app.command("validate-config")
def validate_config(
    out: OutOpt = DEFAULT_OUT,
    impersonate: Annotated[
        str,
        typer.Option(
            "--impersonate",
            help="Service account to check as. Use this to test the pipeline SA "
            "rather than your own near-Owner ADC identity.",
        ),
    ] = "",
) -> None:
    """Fail fast on identity, permission, model, and storage problems.

    Thirty seconds here saves discovering a missing grant forty minutes into
    ensure-infra, which is the single most common way this pipeline wastes time.

    Run it with ``--impersonate`` before every pipeline submission. Checking as
    yourself proves nothing: a developer ADC account is typically near-Owner, so
    everything passes locally and the pipeline fails on a fresh principal.
    """
    config = _config()
    loc = config.locations
    credentials = _credentials(impersonate)
    typer.echo(f"identity           {impersonate or 'ADC (your own credentials)'}")
    typer.echo(f"project            {config.project}")
    typer.echo(f"agent model        {config.agent_model}")
    typer.echo(f"tool model         {config.tool_model}")
    typer.echo("locations")
    for name, value in [
        ("  pipeline", loc.pipeline),
        ("  gemini", loc.gemini),
        ("  bigquery", loc.bigquery),
        ("  datascan", loc.datascan),
        ("  catalog", loc.catalog),
        ("  gcs", loc.gcs),
    ]:
        typer.echo(f"{name:<19}{value}")

    problems: list[str] = []

    # The models are global-endpoint only; a regional client returns 404.
    if loc.gemini != "global":
        problems.append(
            f"gemini location is {loc.gemini!r}; gemini-3.x flash models resolve "
            "only at 'global' (verified: 404 in us-central1)."
        )

    try:
        missing = _check_permissions(config.project, credentials)
        if missing:
            by_purpose = [
                f"{purpose}: {', '.join(p for p in perms if p in missing)}"
                for purpose, perms in REQUIRED_PERMISSIONS.items()
                if any(p in missing for p in perms)
            ]
            problems.append(
                "missing project permissions — "
                + "; ".join(by_purpose)
                + ". Note dataplex.entries.get in particular: without it "
                "lookupContext returns an EMPTY response rather than 403, so "
                "every tier scores identically and the run looks like a "
                "genuine null result."
            )
        else:
            typer.echo(
                f"permissions        all {sum(map(len, REQUIRED_PERMISSIONS.values()))} present"
            )
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Could not check permissions: {type(exc).__name__}: {exc}")

    from google.cloud import bigquery  # noqa: PLC0415

    try:
        bigquery.Client(project=config.project, credentials=credentials).query(
            "SELECT 1", job_config=bigquery.QueryJobConfig(dry_run=True)
        )
        typer.echo("bigquery           reachable")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"BigQuery unreachable: {type(exc).__name__}: {exc}")

    try:
        store = store_for(out, credentials)
        store.write_text("_validate_config", "ok\n")
        typer.echo(f"storage            writable ({store.uri('')})")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"Storage not writable at {out}: {type(exc).__name__}: {exc}")

    if problems:
        typer.echo("")
        for problem in problems:
            typer.secho(f"FAIL  {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho("\nOK", fg=typer.colors.GREEN)


@app.command("ensure-infra")
def ensure_infra(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Create the four-tier corpus and its catalog enrichment. Idempotent.

    Creates 4 BigQuery datasets, 60 views over bigquery-public-data, 45 Dataplex
    profile scans, a glossary with 11 terms, 48 entry links, and 4 guidelines
    aspects. Takes 12-40 minutes, dominated by scan creation and polling.
    """
    config = _config()
    typer.echo(f"Creating bigquery_context_tier0..3 in {config.project}.")
    typer.echo("This creates ~160 cloud resources and takes 12-40 minutes.")
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Abort

    # setup.py reads its configuration from the environment at import time, so
    # the environment must be populated before it is imported. Vendored code is
    # kept close to upstream rather than refactored; see NOTICE.
    import os  # noqa: PLC0415

    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.project)
    os.environ.setdefault("BQ_LOCATION", config.locations.bigquery)
    os.environ.setdefault("DATAPLEX_LOCATION", config.locations.datascan)
    os.environ.setdefault("RESOURCE_PREFIX", config.resource_prefix)

    from bq_context.corpus import setup  # noqa: PLC0415

    setup.main()
    typer.secho(
        "\nInfrastructure ready. Run `bq-context preflight --tier 3` next.", fg=typer.colors.GREEN
    )


@app.command()
def cleanup(
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete everything ensure-infra created."""
    config = _config()
    typer.secho(
        f"This DELETES the 4 tier datasets, 45 scans, glossary and entry links "
        f"in {config.project}.",
        fg=typer.colors.YELLOW,
    )
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Abort

    import os  # noqa: PLC0415

    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.project)
    os.environ.setdefault("BQ_LOCATION", config.locations.bigquery)
    os.environ.setdefault("DATAPLEX_LOCATION", config.locations.datascan)
    os.environ.setdefault("RESOURCE_PREFIX", config.resource_prefix)

    from bq_context.corpus import cleanup as corpus_cleanup  # noqa: PLC0415

    corpus_cleanup.main()


@app.command()
def preflight(
    tier: TierOpt = 3,
    baseline: BaselineOpt = 0,
    impersonate: Annotated[
        str,
        typer.Option("--impersonate", help="Check as this service account instead of ADC."),
    ] = "",
) -> None:
    """Assert catalog enrichment is real before any measurement runs.

    This is the most important gate in the system. ``lookupContext`` returns an
    *empty response* rather than 403 when permissions are missing, so an
    under-permissioned run produces tiers that score identically, a green
    pipeline, and a plausible wrong result — indistinguishable from a genuine
    null finding. Never skip it.
    """
    config = _config()
    # Running this as the pipeline SA is the point. lookupContext returns an
    # empty response rather than 403 on missing permissions, so a developer ADC
    # account reads context fine while the SA silently reads nothing.
    credentials = _credentials(impersonate)
    typer.echo(f"identity: {impersonate or 'ADC (your own credentials)'}")

    from google.api_core.exceptions import NotFound  # noqa: PLC0415

    from bq_context.context_cache import TableCache  # noqa: PLC0415
    from bq_context.runtime import (  # noqa: PLC0415
        TierContext,
        get_datasets,
        get_scoped_tables,
        tier_scope,
    )

    caches: dict[int, TableCache] = {}
    for check_tier in sorted({baseline, *range(baseline, tier + 1), tier}):
        bootstrap = TierContext.build(config, check_tier, TableCache.empty())
        with tier_scope(bootstrap):
            datasets = get_datasets()
            try:
                scoped = {ds: get_scoped_tables(ds) for ds in datasets}
                caches[check_tier] = TableCache.build(
                    config, datasets, scoped, credentials=credentials
                )
            except NotFound:
                # The usual cause is simply that ensure-infra has not run. Say
                # so, rather than surfacing a BigQuery stack trace for what is
                # an ordering mistake.
                typer.secho(
                    f"FAIL  Dataset {config.tier_dataset(check_tier)} does not exist "
                    f"in {config.project}. Run `bq-context ensure-infra` first.",
                    fg=typer.colors.RED,
                    err=True,
                )
                raise typer.Exit(1) from None

    ladder = [_tier_profile(t, caches[t]) for t in sorted(caches)]
    typer.echo(f"{'tier':<6}{'tables':>7}{'bytes':>10}{'profiled':>10}  aspects")
    for rung in ladder:
        typer.echo(
            f"{rung['tier']:<6}{rung['tables']:>7}{rung['bytes']:>10,}"
            f"{rung['profiled']:>10}  {','.join(rung['aspects']) or '—'}"
        )

    problems, warnings = assess_ladder(ladder, empty=len(caches[tier]) == 0)

    for warning in warnings:
        typer.secho(f"WARN  {warning}", fg=typer.colors.YELLOW, err=True)
    if problems:
        for problem in problems:
            typer.secho(f"FAIL  {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    gained = ladder[-1]["bytes"] - ladder[0]["bytes"]
    typer.secho(
        f"\nOK — tier {tier} carries {gained:,} bytes more context than tier {baseline}.",
        fg=typer.colors.GREEN,
    )


@app.command("run-shard")
def run_shard(
    experiment_id: ExperimentId,
    tier: TierOpt,
    approach: Annotated[str, typer.Option("--approach", "-a")],
    out: OutOpt = DEFAULT_OUT,
    runs: Annotated[int, typer.Option("--runs", min=1)] = 5,
    questions_file: QuestionsOpt = DEFAULT_QUESTIONS,
    question_ids: Annotated[
        str, typer.Option("--questions-ids", help="Comma-separated subset.")
    ] = "",
    code_version: Annotated[str, typer.Option("--code-version")] = "",
) -> None:
    """Run every cell for one (tier, approach) pair. Resumable."""
    config = _config()
    if approach not in APPROACHES:
        typer.secho(
            f"Unknown approach {approach!r}. Valid: {', '.join(APPROACHES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    questions = _load_questions(questions_file)
    chosen = [q.strip() for q in question_ids.split(",") if q.strip()] or list(questions)
    unknown = [q for q in chosen if q not in questions]
    if unknown:
        typer.secho(f"Unknown question id(s): {', '.join(unknown)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)

    spec = ShardSpec(
        experiment_id=experiment_id,
        tier=tier,
        approach=approach,
        question_ids=chosen,
        runs=runs,
        code_version=code_version or _code_version(),
    )

    from bq_context.runner.cells import execute_shard  # noqa: PLC0415

    result = execute_shard(spec, config, store_for(out), questions)
    typer.echo(result.model_dump_json(indent=2))
    if result.aborted:
        typer.secho(f"\nShard aborted: {result.abort_reason}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def merge(
    experiment_id: ExperimentId,
    out: OutOpt = DEFAULT_OUT,
    runs: Annotated[int, typer.Option("--runs", min=1)] = 5,
    questions_file: QuestionsOpt = DEFAULT_QUESTIONS,
    approach: Annotated[list[str] | None, typer.Option("--approach", "-a")] = None,
    tier: Annotated[list[int] | None, typer.Option("--tier", "-t")] = None,
) -> None:
    """Collect shard output into one deduped results file.

    Records missing cells rather than failing on them, so a sweep with a few bad
    cells still produces a scoreable dataset plus an actionable list.
    """
    from bq_context.scoring.merge import merge_experiment  # noqa: PLC0415

    questions = _load_questions(questions_file)
    approaches, tiers = _selected(approach, tier)
    expected = [
        key
        for t in tiers
        for a in approaches
        for key in ShardSpec(
            experiment_id=experiment_id,
            tier=t,
            approach=a,
            question_ids=list(questions),
            runs=runs,
            code_version="",
        ).planned_cells()
    ]

    result = merge_experiment(store_for(out), experiment_id, expected)
    typer.echo(
        f"{result.ok_cells}/{result.expected} cells from {result.shards_seen} shard(s); "
        f"{result.error_cells} error, {len(result.missing)} missing"
    )
    if result.missing:
        preview = ", ".join(result.missing[:5])
        typer.secho(f"missing (first 5): {preview}", fg=typer.colors.YELLOW, err=True)


@app.command()
def score(
    experiment_id: ExperimentId,
    out: OutOpt = DEFAULT_OUT,
    report: Annotated[
        Path | None, typer.Option("--report", help="Also write markdown here.")
    ] = None,
) -> None:
    """Compute metrics over merged results and print the report."""
    from bq_context.scoring.merge import load_merged  # noqa: PLC0415
    from bq_context.scoring.metrics import score_cell  # noqa: PLC0415
    from bq_context.scoring.report import render_markdown  # noqa: PLC0415

    cells = load_merged(store_for(out), experiment_id)
    if not cells:
        typer.secho(
            f"No merged results for {experiment_id!r}. Run `bq-context merge` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    scores = [s for cell in cells if (s := score_cell(cell))]
    markdown = render_markdown(scores, experiment_id=experiment_id, errors=len(cells) - len(scores))
    typer.echo(markdown)
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(markdown)
        typer.secho(f"Wrote {report}", fg=typer.colors.GREEN, err=True)


@app.command()
def plot(
    experiment_id: ExperimentId,
    out: OutOpt = DEFAULT_OUT,
    plots_dir: Annotated[Path, typer.Option("--plots-dir")] = Path("plots"),
) -> None:
    """Render figures from merged results."""
    from bq_context.scoring.merge import load_merged  # noqa: PLC0415
    from bq_context.scoring.metrics import score_cell  # noqa: PLC0415
    from bq_context.scoring.report import write_plots  # noqa: PLC0415

    cells = load_merged(store_for(out), experiment_id)
    scores = [s for cell in cells if (s := score_cell(cell))]
    if not scores:
        typer.secho(f"No scored cells for {experiment_id!r}.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    for path in write_plots(scores, plots_dir):
        typer.echo(str(path))


@app.command("plan-shards")
def plan_shards(
    experiment_id: ExperimentId,
    runs: Annotated[int, typer.Option("--runs", min=1)] = 5,
    questions_file: QuestionsOpt = DEFAULT_QUESTIONS,
    approach: Annotated[list[str] | None, typer.Option("--approach", "-a")] = None,
    tier: Annotated[list[int] | None, typer.Option("--tier", "-t")] = None,
) -> None:
    """Print the shard plan as JSON. The pipeline fans out over this."""
    questions = _load_questions(questions_file)
    approaches, tiers = _selected(approach, tier)
    code_version = _code_version()
    specs = [
        ShardSpec(
            experiment_id=experiment_id,
            tier=t,
            approach=a,
            question_ids=list(questions),
            runs=runs,
            code_version=code_version,
        )
        for t in tiers
        for a in approaches
    ]
    typer.echo(json.dumps([s.model_dump() for s in specs], indent=2))
    total = sum(len(s.planned_cells()) for s in specs)
    typer.secho(f"{len(specs)} shard(s), {total} cell(s)", fg=typer.colors.GREEN, err=True)


if __name__ == "__main__":
    app()
