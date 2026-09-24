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

import contextlib
import itertools
import json
import subprocess
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from bq_context.config import TIERS, ExperimentConfig
from bq_context.runner.cells import APPROACHES
from bq_context.runner.models import ShardSpec
from bq_context.runner.planner import order_shards
from bq_context.runner.store import store_for

if TYPE_CHECKING:
    from google.auth.credentials import Credentials

    from bq_context.context_cache import TableCache
    from bq_context.runner.store import ArtifactStore

app = typer.Typer(
    name="bq-context",
    help="Evaluate six BigQuery table-discovery approaches.",
    no_args_is_help=True,
    add_completion=False,
)

DEFAULT_QUESTIONS = Path("experiments/questions.json")

#: Empty means "work it out at parse time". These were literals naming the
#: project this was developed against, which is unreachable for anyone else —
#: and because they were *defaults*, a second user got no error, just a run
#: pointed at a bucket they cannot write and a service account that does not
#: exist, failing with a permission message that names neither.
#:
#: Resolved in a parameter callback rather than here, because a module-level
#: value is computed while Typer builds the command signature — before the app
#: callback runs `_load_dotenv()`, so `.env` would be invisible. A Typer
#: parameter callback runs *after* the group callback; verified, not assumed.
DEFAULT_OUT = ""
DEFAULT_SA = ""

#: How the bucket and service account are named when nothing says otherwise.
#: These reproduce this project's existing values exactly, so deriving them is a
#: no-op here rather than a migration.
_OUT_TEMPLATE = "gs://{project}-bq-context"
_SA_TEMPLATE = "bq-context-pipeline@{project}.iam.gserviceaccount.com"

#: Matches `make image-ref` and the paths cloudbuild.yaml builds.
_IMAGE_TEMPLATE = "{region}-docker.pkg.dev/{project}/bq-context/runner:{tag}"

#: Longer parameter values are summarised in the submission echo rather than
#: printed. Only `build_config` exceeds it today, at several KB of YAML.
_ECHO_MAX = 120


def _derive(template: str, override: str, flag: str) -> str:
    """Fill ``template`` from GOOGLE_CLOUD_PROJECT, unless ``override`` is set.

    The convention is a convenience, not a requirement: a bucket or account that
    does not follow it is configured with the override variable rather than by
    passing a flag to every command.
    """
    import os  # noqa: PLC0415

    explicit = os.environ.get(override, "").strip()
    if explicit:
        return explicit
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        message = (
            f"Cannot work out a default for {flag}: GOOGLE_CLOUD_PROJECT is unset. "
            f"Set it (in .env), pass {flag} explicitly, or set {override}."
        )
        raise typer.BadParameter(message)
    return template.format(project=project)


def default_out() -> str:
    """Where results go: ``gs://{project}-bq-context`` unless BQ_CONTEXT_OUT says otherwise."""
    return _derive(_OUT_TEMPLATE, "BQ_CONTEXT_OUT", "--out")


def default_service_account() -> str:
    """The pipeline's runtime identity, by the same convention."""
    return _derive(_SA_TEMPLATE, "BQ_CONTEXT_SERVICE_ACCOUNT", "--service-account")


def default_image(tag: str) -> str:
    """The runner image reference for a commit.

    Derived rather than demanded. `ensure_image` builds this exact tag if it is
    missing, so requiring the caller to supply a reference would reintroduce the
    friction the in-pipeline build exists to remove — you would still have to run
    `make image-ref`, and the first thing you would hit on a fresh checkout is an
    error telling you to.

    BQ_CONTEXT_IMAGE still wins, for pinning an image built elsewhere.
    """
    import os  # noqa: PLC0415

    explicit = os.environ.get("BQ_CONTEXT_IMAGE", "").strip()
    if explicit:
        return explicit
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    if not project:
        message = (
            "Cannot work out the runner image: GOOGLE_CLOUD_PROJECT is unset. "
            "Set it (in .env), or pass --image explicitly."
        )
        raise typer.BadParameter(message)
    region = os.environ.get("BQ_CONTEXT_BUILD_REGION", "us-central1").strip()
    return _IMAGE_TEMPLATE.format(region=region, project=project, tag=tag)


def _resolve_out(value: str) -> str:
    """Typer callback: leave an explicit value alone, otherwise derive one."""
    return value or default_out()


def _resolve_service_account(value: str) -> str:
    return value or default_service_account()


# -- shared option types ----------------------------------------------------
ExperimentId = Annotated[
    str,
    typer.Option(
        "--experiment-id",
        "-e",
        help="Stable id; the GCS prefix derives from it and resume depends on it being reused.",
    ),
]
OutOpt = Annotated[
    str,
    typer.Option(
        "--out",
        callback=_resolve_out,
        help="gs://bucket/prefix or a local directory. "
        "Defaults to gs://{GOOGLE_CLOUD_PROJECT}-bq-context; override with BQ_CONTEXT_OUT.",
    ),
]
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
    from bq_context.logging_setup import configure_logging  # noqa: PLC0415

    _load_dotenv()
    configure_logging(verbose=verbose)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _load_dotenv() -> None:
    """Load the repo-root `.env`, without overriding anything already set.

    Called from the CLI entrypoint rather than at import, deliberately. Importing
    this package must not read a developer's `.env` — `tests/conftest.py` pins a
    fake environment precisely so nothing can pass by inheriting real settings,
    and an import-time load would defeat it.

    `override=False` for the same reason: an explicitly exported variable, or the
    task environment in the pipeline container, always wins over the file. A
    missing `.env` is a silent no-op, which is the normal case in the image.
    """
    from dotenv import load_dotenv  # noqa: PLC0415

    # repo root: src/bq_context/cli.py -> src/bq_context -> src -> root
    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


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


#: Top-level capsule keys that indicate a table-level enrichment aspect.
#: Max acceptable spread in search hits across tiers, as a fraction of the mean.
#: Above this the semantic index has not converged and tier is confounded with
#: shard execution order.
_SEARCH_SPREAD_TOLERANCE = 0.15

#: Fewer tiers than this and there is nothing to compare.
_MIN_TIERS_TO_COMPARE = 2

#: ...but only when the absolute gap is at least this many hits. Counts here are
#: small (3-6), so a single-hit difference is 25-30% and would cry wolf on a
#: converged index. The real failure was a gap of 2+ (2.88 vs 4.96 mean hits).
_SEARCH_SPREAD_MIN_GAP = 2

#: How long preflight waits between the two probes that decide whether the index
#: is still moving. Long enough to catch the churn that follows `ensure-infra`
#: rewriting catalog entries, negligible against a sweep measured in hours.
_SETTLE_SECONDS = 45

#: A question whose answer table exists identically in every tier.
_CONVERGENCE_PROBE = "What are the busiest bike share stations in Austin by month?"


def _search_hits_by_tier(config: ExperimentConfig, tiers: list[int]) -> dict[int, int]:
    """Raw semantic-search hit count per tier for one fixed question.

    The corpus is identical across tiers, so a converged index returns the same
    count everywhere. A rising count is the signature of an index still warming
    after provisioning.
    """
    from bq_context.context_cache import TableCache  # noqa: PLC0415
    from bq_context.discovery_common import search_entries_scoped  # noqa: PLC0415
    from bq_context.runtime import TierContext, tier_scope  # noqa: PLC0415

    hits: dict[int, int] = {}
    for tier in tiers:
        ctx = TierContext.build(config, tier, TableCache.empty())
        with tier_scope(ctx):
            _, stats = search_entries_scoped(_CONVERGENCE_PROBE)
        hits[tier] = int(stats["raw_search_count"])
    return hits


def assess_search_convergence(
    first: dict[int, int], second: dict[int, int] | None = None
) -> list[str]:
    """Warn when the Dataplex semantic index is still changing.

    This exists because of a real failure. In the first full 3,000-cell run the
    three search-based approaches showed discovery recall climbing
    0.52 -> 0.68 -> 0.97 -> 0.92 across tiers 0-3, which reads as a large
    enrichment effect. It was not: shards run in plan order, tier 0 first, and the
    index was still warming. Re-running every tier later gave an identical 0.967.
    Tier was confounded with elapsed time.

    **It compares each tier against itself, never against other tiers**, and the
    first version got that wrong. It asserted that "the corpus is identical across
    tiers, so a converged index returns the same count everywhere" — but only the
    *tables* are identical. The searchable metadata is exactly what differs, and
    it is the experiment's independent variable, so unequal counts are the thing
    being measured rather than evidence of a fault.

    Measured on the live corpus: tiers 0-2 return 3 hits for the probe and tier 3
    returns 4, stably across repeated samples. The extra hit is the NYC *taxi*
    table matching a *bike share* question — tier 3's overview aspect adds text
    that makes an irrelevant table match. A permanent, correct consequence of
    enrichment, which the old rule reported as a broken index on every run.

    Two observations of the same tiers, separated in time, are the only thing that
    distinguishes drift from enrichment. With one observation there is nothing to
    compare and this returns no warnings.

    Returns a list of warnings; empty means nothing moved.
    """
    if not second:
        return []
    moved = {
        tier: (first[tier], second[tier])
        for tier in sorted(set(first) & set(second))
        if first[tier] != second[tier]
    }
    if not moved:
        return []
    detail = ", ".join(f"tier{t}: {a} -> {b}" for t, (a, b) in moved.items())
    return [
        (
            f"semantic search hit counts changed while preflight was running "
            f"({detail}). The Dataplex index is still settling, so shards started "
            "now would see different index states as the sweep progresses, and "
            "tier would be confounded with elapsed time exactly as it was in "
            "full-01. Wait for it to settle and re-check before scoring."
        )
    ]


_ASPECT_KEYS = ("guidelines", "overview", "business_descriptions")

#: Per-column capsule keys. Glossary definitions arrive here, as ``terms``, on
#: the individual column — NOT as a top-level ``related_terms`` object. Looking
#: only at top-level keys is what made an earlier version of this gate report a
#: fully-enriched tier 2 as dead.
_COLUMN_ENRICHMENT_KEYS = ("terms", "dataProfile")


def _tier_profile(tier: int, cache: TableCache) -> dict[str, Any]:
    """Summarise what enrichment actually reached one tier's capsules.

    Counts enrichment *features*, not bytes. Byte deltas are a trap in both
    directions: dataset timestamps and entry ids differ between tiers even when
    nothing else does, and — the failure that actually happened — real glossary
    enrichment across 15 tables amounted to only ~1.6 KB and was dismissed as
    noise by a byte threshold.
    """
    aspects: set[str] = set()
    counts = dict.fromkeys(_COLUMN_ENRICHMENT_KEYS, 0)
    for entry in cache.entries.values():
        capsule = json.loads(entry.detailed)
        for column in capsule.get("schema", []):
            for key in _COLUMN_ENRICHMENT_KEYS:
                if column.get(key):
                    counts[key] += 1
        aspects.update(key for key in _ASPECT_KEYS if key in capsule)
    return {
        "tier": tier,
        "tables": len(cache.entries),
        "bytes": len(cache.all_detailed()),
        "profiled": counts["dataProfile"],
        "terms": counts["terms"],
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
        if _signature(upper) == _signature(lower):
            warnings.append(
                f"tier {upper['tier']} adds no enrichment over tier "
                f"{lower['tier']} (same aspects, {upper['profiled']} profiled "
                f"columns, {upper['terms']} glossary-annotated columns). That "
                "tier is not a distinct factor level; treat its results as a "
                f"duplicate of tier {lower['tier']}."
            )
    return problems, warnings


def _signature(rung: dict[str, Any]) -> tuple[Any, ...]:
    """What makes a tier distinct. Deliberately excludes byte count."""
    return (tuple(rung["aspects"]), rung["profiled"], rung["terms"])


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


#: What GCE-family credentials report instead of an email address.
_METADATA_ALIAS = "default"

_METADATA_EMAIL_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"
)


def _is_vm_identity(credentials: object) -> bool:
    """Whether these credentials are the VM's own service account."""
    from google.auth import compute_engine  # noqa: PLC0415

    return isinstance(credentials, compute_engine.Credentials)


def _metadata_service_account() -> str:
    """The VM's service account email, from the metadata server."""
    import urllib.request  # noqa: PLC0415

    req = urllib.request.Request(_METADATA_EMAIL_URL, headers={"Metadata-Flavor": "Google"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return resp.read().decode().strip()


def _effective_identity(credentials: Credentials | None) -> str:
    """Best-effort principal for the credentials in use.

    Inside a pipeline task there is nothing to impersonate — the task already
    *is* the service account — so the useful question is "who am I?" rather than
    "can I become someone else?".
    """
    import google.auth  # noqa: PLC0415
    import google.auth.transport.requests  # noqa: PLC0415

    creds = credentials
    if creds is None:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])

    email = getattr(creds, "service_account_email", "") or ""
    if email in ("", _METADATA_ALIAS):
        with contextlib.suppress(Exception):
            creds.refresh(google.auth.transport.requests.Request())  # type: ignore[union-attr]
            email = getattr(creds, "service_account_email", "") or ""

    # On a GCE-family VM — which is what a Vertex pipeline task is — the
    # credentials object reports the literal alias "default" rather than an
    # address, and it stays "default" after a refresh. Because that is truthy,
    # treating it as an identity made --expect-identity reject a pipeline Vertex
    # had configured correctly. The metadata server has the real answer.
    #
    # Gated on the credentials actually being the VM's: under a *user* ADC the
    # metadata server still answers, but with the VM's service account, which is
    # not who the calls are made as. Reporting it would be a confident lie.
    if email in ("", _METADATA_ALIAS) and _is_vm_identity(creds):
        with contextlib.suppress(Exception):
            email = _metadata_service_account()

    return "" if email == _METADATA_ALIAS else email


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


#: Tested against the secret itself, never against the project — see
#: `_check_secret_access`. Deliberately absent from REQUIRED_PERMISSIONS, and a
#: test asserts it stays absent.
SECRET_PERMISSION = "secretmanager.versions.access"  # noqa: S105 - an IAM permission, not a secret


def _check_secret_access(project: str, secret: str, credentials: Credentials | None) -> str | None:
    """Return why the API-key secret is unusable, or None if it is fine.

    Why this is not just another entry in ``REQUIRED_PERMISSIONS``: that table is
    checked with one ``testIamPermissions`` against ``projects/{id}``, which sees
    only policy bound at the *project*. ``roles/secretmanager.secretAccessor`` is
    normally bound on the individual secret — it is, on this project's
    ``bq-context-secret`` — and a resource-level binding is invisible to a
    project-level query. Listing the permission there would report a missing
    grant that is present, and the natural "fix" for that false alarm is to
    over-grant across the whole project.

    Permission is tested rather than the value fetched. ``testIamPermissions``
    answers the question without pulling a live API key into this process and
    into whatever is capturing its output.

    This reports rather than raises so the caller can collect it alongside the
    other problems and print them together.
    """
    from google.cloud import secretmanager  # noqa: PLC0415

    resource = f"projects/{project}/secrets/{secret}"
    try:
        client = secretmanager.SecretManagerServiceClient(credentials=credentials)
        granted = set(
            client.test_iam_permissions(
                request={"resource": resource, "permissions": [SECRET_PERMISSION]}
            ).permissions
        )
    except Exception as exc:  # noqa: BLE001 - a check that cannot run must say so, not pass
        return f"Could not check access to secret {secret!r}: {type(exc).__name__}: {exc}"

    if SECRET_PERMISSION in granted:
        return None

    # A secret that does not exist returns an empty permission list rather than
    # raising NotFound — verified against the live API, where a typo'd name and a
    # real missing grant are indistinguishable here. The remedies are opposite
    # (create it, versus bind a role on one that exists), so probe before
    # advising. Same shape as the `lookupContext` trap in CLAUDE.md: the API
    # answers "nothing" where it could have answered "denied".
    if not _secret_exists(client, resource):
        return (
            f"secret {secret!r} does not exist in {project}. Create it with "
            f"`gcloud secrets create {secret} --project={project} --replication-policy=automatic` "
            "and add a version holding a Gemini Developer API key, or unset "
            "SECRET_ID to run without figures."
        )
    return (
        f"missing {SECRET_PERMISSION} on secret {secret!r}. Figure generation "
        "would be skipped silently, ninety minutes into the exit task, with "
        "nothing in the logs naming IAM. Grant it with `gcloud secrets "
        f"add-iam-policy-binding {secret} --project={project} "
        "--member=serviceAccount:<pipeline-sa> "
        "--role=roles/secretmanager.secretAccessor`."
    )


def _secret_exists(client: Any, resource: str) -> bool:
    """Whether the secret is there, used only to pick the right remedy.

    Defaults to True on any error other than NotFound. ``get_secret`` needs
    ``secretmanager.secrets.get``, which a least-privilege principal may not
    hold; a PermissionDenied there says nothing about existence, and guessing
    "absent" would tell someone to create a secret they already have.
    """
    from google.api_core import exceptions  # noqa: PLC0415

    try:
        client.get_secret(request={"name": resource})
    except exceptions.NotFound:
        return False
    except Exception:  # noqa: BLE001 - cannot tell; the grant message is the safer default
        return True
    return True


def _report_secret(project: str, credentials: Credentials | None, *, require: bool) -> list[str]:
    """Echo the API-key secret's status; return any problem as a one-item list.

    A list rather than ``str | None`` so the caller can ``+=`` it without a branch
    — ``validate_config`` is one branch under the complexity limit already.

    Split out of ``validate_config`` rather than inlined: three branches pushed
    that function past the branch and statement limits, and the reporting rule
    here is worth stating in one place.

    An unset ``SECRET_ID`` is normal, not broken. Figures are opt-in and off by
    default, so demanding a secret from every run would fail the 99% of runs that
    never wanted one. ``require=True`` is for the figures path, where absence is
    fatal rather than merely unconfigured.
    """
    import os  # noqa: PLC0415

    secret = os.getenv("SECRET_ID", "").strip()
    if secret:
        problem = _check_secret_access(project, secret, credentials)
        if problem:
            return [problem]
        typer.echo(f"secret             {secret} readable")
        return []
    if require:
        return [
            (
                "SECRET_ID is unset, and --require-secret was passed. It names the "
                "Secret Manager secret holding a Gemini Developer API key; set it in "
                ".env locally, or on the task environment in the pipeline."
            )
        ]
    typer.echo("secret             not configured (SECRET_ID unset; figures skipped)")
    return []


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
            help="Service account to check as. For LOCAL use: inside a pipeline "
            "the task already is the SA, so use --expect-identity instead.",
        ),
    ] = "",
    expect_identity: Annotated[
        str,
        typer.Option(
            "--expect-identity",
            help="Fail unless the effective principal matches. Use this in the "
            "pipeline, where impersonating yourself is a 403.",
        ),
    ] = "",
    require_secret: Annotated[
        bool,
        typer.Option(
            "--require-secret",
            help="Treat an unset SECRET_ID as a failure. Use it when the run will "
            "generate figures, where a missing key is fatal rather than merely "
            "unconfigured.",
        ),
    ] = False,
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
    identity = _effective_identity(credentials)
    typer.echo(f"identity           {identity or '(ADC, principal not resolvable)'}")
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

    if expect_identity and identity != expect_identity:
        problems.append(
            f"running as {identity or '<unknown>'}, expected {expect_identity}. "
            "Vertex silently falls back to the Compute Engine default service "
            "account when service_account= is omitted on the PipelineJob."
        )

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

    problems += _report_secret(config.project, credentials, require=require_secret)

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
    out: OutOpt = DEFAULT_OUT,
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Create the four-tier corpus and its catalog enrichment. Idempotent.

    Creates the results bucket, 4 BigQuery datasets, 60 views over
    bigquery-public-data, 45 Dataplex profile scans, a glossary with 11 terms,
    48 entry links, and 4 guidelines aspects. Takes 12-40 minutes, dominated by
    scan creation and polling.
    """
    config = _config()
    typer.echo(f"Creating bigquery_context_tier0..3 in {config.project}.")
    typer.echo("This creates ~160 cloud resources and takes 12-40 minutes.")
    if not yes and not typer.confirm("Proceed?"):
        raise typer.Abort

    # First, because everything downstream writes here and a missing bucket
    # otherwise surfaces 40 minutes later as a failed shard rather than now.
    # Almost always a no-op: in a pipeline run the bucket must already exist,
    # since pipeline_root lives in it.
    from bq_context.corpus.bucket import ensure_bucket  # noqa: PLC0415

    try:
        state = ensure_bucket(config, out)
    except RuntimeError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    if state != "skipped":
        typer.echo(f"bucket             {out} ({state})")

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
        typer.Option(
            "--impersonate",
            help="Check as this service account. LOCAL use only; inside a "
            "pipeline the task already is the SA.",
        ),
    ] = "",
    json_out: Annotated[
        Path | None,
        typer.Option("--json", help="Also write the ladder and corpus fingerprint here."),
    ] = None,
    settle: Annotated[
        int,
        typer.Option(
            "--settle",
            min=0,
            help="Seconds between the two search probes that check the index is "
            "not still moving. 0 skips the second probe.",
        ),
    ] = _SETTLE_SECONDS,
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
    typer.echo(f"identity: {_effective_identity(credentials) or '(ADC)'}")
    # What this run measured, in the saved log. The ladder below already shows the
    # table count, but a corpus profile and a TOP_K that were only ever set in a
    # shell are invisible afterwards -- and both change the numbers.
    from bq_context.corpus.setup import CORPUS_PROFILE  # noqa: PLC0415

    typer.echo(
        f"corpus:   profile={CORPUS_PROFILE} prefix={config.resource_prefix} top_k={config.top_k}"
    )

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
    typer.echo(f"{'tier':<6}{'tables':>7}{'bytes':>10}{'profiled':>10}{'terms':>7}  aspects")
    for rung in ladder:
        typer.echo(
            f"{rung['tier']:<6}{rung['tables']:>7}{rung['bytes']:>10,}"
            f"{rung['profiled']:>10}{rung['terms']:>7}  {','.join(rung['aspects']) or '—'}"
        )

    problems, warnings = assess_ladder(ladder, empty=len(caches[tier]) == 0)

    if len(caches) > 1:
        probe = _search_hits_by_tier(config, sorted(caches))
        typer.echo(
            "search hits/tier  " + "  ".join(f"tier{t}={n}" for t, n in sorted(probe.items()))
        )
        # Twice, separated in time. One observation cannot distinguish a moving
        # index from enrichment doing its job — see assess_search_convergence.
        if settle:
            import time  # noqa: PLC0415

            typer.echo(f"re-probing in {settle}s to check the index is not still moving")
            time.sleep(settle)
            again = _search_hits_by_tier(config, sorted(caches))
            typer.echo(
                "search hits/tier  "
                + "  ".join(f"tier{t}={n}" for t, n in sorted(again.items()))
                + "  (second pass)"
            )
            warnings.extend(assess_search_convergence(probe, again))

    for warning in warnings:
        typer.secho(f"WARN  {warning}", fg=typer.colors.YELLOW, err=True)
    if problems:
        for problem in problems:
            typer.secho(f"FAIL  {problem}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    from bq_context.runner.planner import corpus_fingerprint  # noqa: PLC0415

    fingerprint = corpus_fingerprint(ladder)
    typer.echo(f"corpus fingerprint  {fingerprint}")

    if json_out is not None:
        # Written only once the gate has passed: a fingerprint for a corpus that
        # failed preflight would key shard cache to a corpus nobody should run on.
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps({"ladder": ladder, "fingerprint": fingerprint}, indent=2) + "\n"
        )
        typer.echo(f"wrote {json_out}", err=True)

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
    limit: Annotated[
        int,
        typer.Option("--limit", min=0, help="Use only the first N questions. 0 means all."),
    ] = 0,
    code_version: Annotated[str, typer.Option("--code-version")] = "",
    corpus_fingerprint: Annotated[
        str,
        typer.Option(
            "--corpus-fingerprint",
            help="Enrichment shape this shard ran against, from `preflight --json`. "
            "Recorded as provenance; the pipeline also passes it so a corpus "
            "change invalidates the KFP shard cache.",
        ),
    ] = "",
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
    if limit:
        # Deterministic prefix, not a sample: the smoke and pilot profiles must
        # hit the same cells every run or resume cannot recognise prior work.
        chosen = chosen[:limit]
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
        corpus_fingerprint=corpus_fingerprint,
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
    limit: Annotated[int, typer.Option("--limit", min=0)] = 0,
    bigquery: Annotated[
        bool,
        typer.Option(
            "--bigquery/--no-bigquery",
            help="Also load results into the BigQuery query sink.",
        ),
    ] = True,
) -> None:
    """Collect shard output into one deduped results file.

    Records missing cells rather than failing on them, so a sweep with a few bad
    cells still produces a scoreable dataset plus an actionable list.
    """
    from bq_context.scoring.merge import merge_experiment  # noqa: PLC0415

    questions = _load_questions(questions_file)
    question_ids = list(questions)[:limit] if limit else list(questions)
    approaches, tiers = _selected(approach, tier)
    expected = [
        key
        for t in tiers
        for a in approaches
        for key in ShardSpec(
            experiment_id=experiment_id,
            tier=t,
            approach=a,
            question_ids=question_ids,
            runs=runs,
            code_version="",
        ).planned_cells()
    ]

    store = store_for(out)
    result = merge_experiment(store, experiment_id, expected)
    typer.echo(
        f"{result.ok_cells}/{result.expected} cells from {result.shards_seen} shard(s); "
        f"{result.error_cells} error, {len(result.missing)} missing"
    )
    if result.missing:
        preview = ", ".join(result.missing[:5])
        typer.secho(f"missing (first 5): {preview}", fg=typer.colors.YELLOW, err=True)

    _report_shard_health(experiment_id, store)

    if bigquery and out.startswith("gs://"):
        _publish_to_bigquery(experiment_id, store)


def _report_shard_health(experiment_id: str, store: ArtifactStore) -> None:
    """Summarise the per-shard records, if the run wrote any.

    Surfaces the two things that otherwise only exist in a log nobody tails: a
    shard the circuit breaker aborted, and how long cache warm actually took —
    the number that decides whether per-shard warming is affordable.
    """
    from bq_context.runner.summaries import load_summaries  # noqa: PLC0415

    summaries = load_summaries(store, experiment_id)
    if not summaries:
        return  # a run made before summaries existed; not a problem

    warms = sorted(s.cache_warm_s for s in summaries if s.cache_warm_s)
    if warms:
        typer.echo(
            f"cache warm        {len(warms)} shard(s), "
            f"median {warms[len(warms) // 2]:.1f}s, max {warms[-1]:.1f}s"
        )
    for summary in summaries:
        if summary.aborted:
            typer.secho(
                f"ABORTED {summary.shard_id}: {summary.abort_reason}",
                fg=typer.colors.RED,
                err=True,
            )


def _publish_to_bigquery(experiment_id: str, store: ArtifactStore) -> None:
    """Load merged results into the query sink.

    Never fatal. The merged JSONL is the system of record and is already
    written by the time we get here, so a BigQuery outage or a missing grant
    must not turn a good sweep into a failed one — re-running `merge` reloads.
    """
    from bq_context.scoring import sink  # noqa: PLC0415
    from bq_context.scoring.merge import load_merged  # noqa: PLC0415

    try:
        records = load_merged(store, experiment_id)
        loaded = sink.load_experiment(_config(), experiment_id, records)
    except Exception as exc:  # noqa: BLE001 - the sink is a convenience, not the record
        typer.secho(
            f"BigQuery sink skipped: {type(exc).__name__}: {exc}", fg=typer.colors.YELLOW, err=True
        )
        return
    typer.echo(f"BigQuery          {loaded:,} rows -> {sink.table_id(_config())}")


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
def figures(
    out_dir: Annotated[Path, typer.Option("--dir", help="Where to write the PNGs.")] = Path(
        "figures"
    ),
) -> None:
    """Generate architecture diagrams with PaperBanana. Off the default path.

    Diagrams only — never the data charts. `discovery_vs_final`, `recall_vs_tier`
    and `latency_cost` plot measured numbers, and a generative image model
    producing bars whose heights are not derived from the data would be a
    correctness hazard. Those stay in matplotlib.

    Skips with a message rather than failing when the Gemini Developer API key is
    absent: this runs inside the exit task, which must only turn a run red for
    missing cells.
    """
    from bq_context.scoring.figures import generate  # noqa: PLC0415

    written = generate(_config().project, out_dir)
    if not written:
        typer.secho(
            "No figures generated (no API key, or PaperBanana absent).", fg=typer.colors.YELLOW
        )
        return
    for path in written:
        typer.echo(str(path))


@app.command()
def report(
    experiment_id: ExperimentId,
    out: OutOpt = DEFAULT_OUT,
    html_out: Annotated[Path, typer.Option("--html", help="Write the report here.")] = Path(
        "executive.html"
    ),
    figures_dir: Annotated[
        Path | None, typer.Option("--figures", help="PNGs to inline, e.g. from `plot`.")
    ] = None,
) -> None:
    """Render the executive report: numbers, figures, and the caveats that apply.

    Separate from `score` because it carries interpretation, not just metrics. The
    caveats are the point — a flat tier response looks the same whether enrichment
    does nothing, the corpus is too easy to show it, or lookupContext silently
    returned nothing, and only the caveats distinguish those.
    """
    from bq_context.runner.summaries import load_summaries  # noqa: PLC0415
    from bq_context.scoring.executive import (  # noqa: PLC0415
        convergence_from_cells,
        render_html,
    )
    from bq_context.scoring.merge import load_merged  # noqa: PLC0415
    from bq_context.scoring.metrics import score_cell  # noqa: PLC0415

    store = store_for(out)
    cells = load_merged(store, experiment_id)
    if not cells:
        typer.secho(
            f"No merged results for {experiment_id}. Run `bq-context merge` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    scores = [s for c in cells if (s := score_cell(c))]
    figures = sorted(figures_dir.glob("*.png")) if figures_dir and figures_dir.exists() else []
    document = render_html(
        scores,
        experiment_id=experiment_id,
        figures=figures,
        convergence_warnings=convergence_from_cells(cells),
        code_versions={str(c.get("code_version", "")) for c in cells if c.get("code_version")},
        shard_summaries=load_summaries(store, experiment_id),
        errors=len(cells) - len(scores),
    )
    html_out.parent.mkdir(parents=True, exist_ok=True)
    html_out.write_text(document)
    typer.echo(f"Wrote {html_out} ({len(document):,} bytes, {len(figures)} figure(s))")


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


@app.command("compile-pipeline")
def compile_pipeline_cmd(
    dest: Annotated[Path | None, typer.Option("--dest", help="Output path.")] = None,
    image: Annotated[str, typer.Option("--image", help="Overrides $BQ_CONTEXT_IMAGE.")] = "",
) -> None:
    """Compile the pipeline spec. Never commit the result.

    A compiled YAML is a build artifact. A stale one still submits successfully
    and runs obsolete code that returns plausible results, which is why this
    exists as a command rather than a checked-in file.
    """
    import os  # noqa: PLC0415

    # Derived from the commit when not given, because `ensure_image` builds
    # exactly this tag if it is absent. Demanding a reference here would put the
    # `make image-ref` step back in front of every submit.
    os.environ["BQ_CONTEXT_IMAGE"] = image or default_image(_code_version())
    if not os.environ["BQ_CONTEXT_IMAGE"]:
        typer.secho(
            "Could not determine a runner image reference. base_image resolves at "
            "compile time, so a spec cannot be produced without one. "
            "Try: --image $(make -s image-ref)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    from bq_context.pipeline.compilation import compile_pipeline  # noqa: PLC0415

    path = compile_pipeline(dest)
    typer.echo(str(path))


def _image_exists(image: str) -> bool:
    """Whether the tag is already in Artifact Registry."""
    return (
        subprocess.run(  # noqa: S603
            ["gcloud", "artifacts", "docker", "images", "describe", image],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=300,
        ).returncode
        == 0
    )


def _require_clean_tree() -> None:
    """Refuse to build from a dirty tree.

    `make image` has always refused this, for the reason the SHA tag exists: an
    image tagged with the HEAD SHA must contain the HEAD commit, or the KFP cache
    treats two different images as the same one.
    """
    dirty = subprocess.run(
        ["git", "status", "--porcelain"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    ).stdout.strip()
    if dirty:
        typer.secho(
            "Working tree is dirty. The runner image is tagged with the HEAD SHA, "
            "so building from uncommitted changes would produce a tag that does "
            "not describe its contents. Commit, stash, or pass --image.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)


def ensure_image(image: str, tag: str, region: str = "") -> None:
    """Build and push ``image`` if Artifact Registry does not already have it.

    **Before the job is created, not as a pipeline task.** The first attempt made
    this the pipeline's own first step, which cannot work: Vertex validates every
    statically-referenced image when the job is *created*, so a run whose image
    did not yet exist was rejected outright —

        Failed to create pipeline job. Error: The image ... does not exist.

    — and the task that would have built it never ran. A task can only ever have
    confirmed an image that was already there, which is no use to anyone.

    A runtime `set_container_image` channel *is* exempt from that validation
    (verified), so the five ordinary tasks could have taken a built image. But
    `finalize` is the `ExitHandler` exit task, may not depend on anything, and so
    needs a static reference that already resolves. Something therefore has to
    exist before creation no matter what, and once that is true, doing the whole
    job here is simpler and needs no new pipeline permissions.

    Skip-if-exists, so resubmitting at an already-built commit costs one registry
    lookup rather than a build.
    """
    import os  # noqa: PLC0415

    if _image_exists(image):
        typer.echo(f"image     {image} (already built)")
        return

    _require_clean_tree()
    typer.echo(f"image     {image} not found; building")
    region = region or os.environ.get("BQ_CONTEXT_BUILD_REGION", "us-central1")
    build = subprocess.run(  # noqa: S603
        [  # noqa: S607 - gcloud from PATH, as it is for `make image`
            "gcloud",
            "builds",
            "submit",
            "--region",
            region,
            "--config",
            "cloudbuild.yaml",
            f"--substitutions=_TAG={tag}",
            ".",
        ],
        check=False,
        timeout=3600,
    )
    if build.returncode != 0:
        typer.secho("Cloud Build failed; not submitting.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    # Cloud Build can report success before the push is visible. The job would
    # otherwise be rejected at creation with a bare "image does not exist".
    if not _image_exists(image):
        typer.secho(
            f"Build reported success but {image} is still not in the registry.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)


@app.command("submit-pipeline")
def submit_pipeline_cmd(
    experiment_id: ExperimentId,
    profile: Annotated[
        str, typer.Option("--profile", help="smoke | pilot | survey | full")
    ] = "smoke",
    out: OutOpt = DEFAULT_OUT,
    image: Annotated[str, typer.Option("--image", help="Overrides $BQ_CONTEXT_IMAGE.")] = "",
    service_account: Annotated[
        str,
        typer.Option(
            "--service-account",
            callback=_resolve_service_account,
            help="Defaults to bq-context-pipeline@{GOOGLE_CLOUD_PROJECT}.iam.gserviceaccount.com; "
            "override with BQ_CONTEXT_SERVICE_ACCOUNT.",
        ),
    ] = DEFAULT_SA,
    skip_infra: Annotated[bool, typer.Option("--skip-infra")] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Compile and print, do not submit.")
    ] = False,
    no_cache: Annotated[
        bool,
        typer.Option(
            "--no-cache",
            help="Disable execution caching for the whole job, overriding every per-task setting.",
        ),
    ] = False,
    refresh_figures: Annotated[
        bool,
        typer.Option(
            "--refresh-figures",
            help="Regenerate the PaperBanana architecture diagrams in finalize. "
            "Slow, paid and non-deterministic; off by default, so a normal run "
            "reuses the figures already in the experiment prefix.",
        ),
    ] = False,
) -> None:
    """Compile and submit the pipeline to Vertex AI.

    Without ``--no-cache`` the job defers to the per-task settings in ``dag.py``,
    which is what lets finished shards be skipped while the enrichment gate still
    runs every time.

    ``--refresh-figures`` only redraws the *diagrams*. The three data charts are
    matplotlib and are rendered on every run regardless: a generative model
    drawing bars whose heights are not derived from the numbers is a correctness
    hazard in a benchmark report.
    """
    import os  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    config = _config()
    # Derived from the commit when not given: `ensure_image` builds exactly this
    # tag if it is absent, so demanding a reference here would put `make
    # image-ref` back in front of every submit.
    sha = _code_version()
    os.environ["BQ_CONTEXT_IMAGE"] = image or default_image(sha)

    from bq_context.pipeline.compilation import compile_pipeline  # noqa: PLC0415
    from bq_context.pipeline.submit import PROFILES, submit_pipeline  # noqa: PLC0415

    if profile not in PROFILES:
        typer.secho(
            f"Unknown profile {profile!r}. Valid: {', '.join(PROFILES)}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    # Before compiling: Vertex rejects job creation outright when a statically
    # referenced image is absent, so this has to happen here rather than as a
    # pipeline step.
    if not image:
        ensure_image(os.environ["BQ_CONTEXT_IMAGE"], sha)

    params = {
        "project": config.project,
        "experiment_id": experiment_id,
        "out": out,
        "code_version": sha,
        "service_account": service_account,
        "skip_infra": skip_infra,
        "refresh_figures": refresh_figures,
        **PROFILES[profile],
    }
    typer.echo(f"profile   {profile}")
    typer.echo(f"image     {os.environ['BQ_CONTEXT_IMAGE']}")
    for key, value in sorted(params.items()):
        # build_config is the whole of cloudbuild.yaml; printing it buries every
        # other parameter in a screen of YAML.
        shown = (
            f"<{len(value)} bytes>" if isinstance(value, str) and len(value) > _ECHO_MAX else value
        )
        typer.echo(f"  {key:<18}{shown}")

    with tempfile.TemporaryDirectory() as tmp:
        spec = compile_pipeline(Path(tmp) / "pipeline.yaml")
        typer.echo(f"compiled  {spec.stat().st_size:,} bytes")
        if dry_run:
            typer.secho("\ndry run; not submitted", fg=typer.colors.YELLOW)
            return
        job = submit_pipeline(
            template_path=spec,
            project=config.project,
            location=config.locations.pipeline,
            pipeline_root=f"{out}/pipeline_root",
            service_account=service_account,
            experiment_id=experiment_id,
            parameter_values=params,
            # None defers to per-task settings; False is a blunt global off.
            enable_caching=False if no_cache else None,
        )
        typer.secho(f"\nsubmitted {job.resource_name}", fg=typer.colors.GREEN)


@app.command("plan-shards")
def plan_shards(
    experiment_id: ExperimentId,
    runs: Annotated[int, typer.Option("--runs", min=1)] = 5,
    questions_file: QuestionsOpt = DEFAULT_QUESTIONS,
    approach: Annotated[list[str] | None, typer.Option("--approach", "-a")] = None,
    tier: Annotated[list[int] | None, typer.Option("--tier", "-t")] = None,
    limit: Annotated[int, typer.Option("--limit", min=0)] = 0,
) -> None:
    """Print the shard plan as JSON. The pipeline fans out over this."""
    questions = _load_questions(questions_file)
    question_ids = list(questions)[:limit] if limit else list(questions)
    approaches, tiers = _selected(approach, tier)
    code_version = _code_version()
    # order_shards, not a nested loop: tier-major ordering confounded tier with
    # elapsed time in the first full run. See runner/planner.py.
    specs = [
        ShardSpec(
            experiment_id=experiment_id,
            tier=t,
            approach=a,
            question_ids=question_ids,
            runs=runs,
            code_version=code_version,
        )
        for t, a in order_shards(tiers, approaches)
    ]
    typer.echo(json.dumps([s.model_dump() for s in specs], indent=2))
    total = sum(len(s.planned_cells()) for s in specs)
    typer.secho(f"{len(specs)} shard(s), {total} cell(s)", fg=typer.colors.GREEN, err=True)


if __name__ == "__main__":
    app()
