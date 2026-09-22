"""KFP components, each a thin wrapper over a ``bq-context`` subcommand.

Every component shells out to the CLI rather than importing the library. That
is deliberate: the pipeline then runs the *same* code path as a local shell, so
any failure here reproduces with one command on a workstation instead of a
container build and a job submission.

Two structural constraints shape this module:

``base_image`` is resolved at **compile** time and cannot be parameterised at
runtime, so the image reference comes from the environment. ``os.environ[...]``
rather than ``.get()`` is the point — compiling without an explicit image should
fail loudly, not silently produce a spec pinned to something stale.

A KFP component body is extracted and executed standalone inside the container,
so it cannot reference module-level helpers and must import everything it needs
locally. That is why every function below repeats ``import os, subprocess, sys``
rather than sharing a helper, and why this module carries a lint exemption.

``install_kfp_package=False`` avoids a pip install in every task. It works only
because ``kfp`` is a runtime dependency in ``pyproject.toml`` and therefore
survives ``uv sync --no-dev`` in the image.
"""

# NB: deliberately NO `from __future__ import annotations` in this module.
# KFP introspects __annotations__ at runtime to build the component interface.
# With PEP 563 every annotation is a string, so KFP sees the literal "str" and
# tries to parse it as an artifact type, failing with the misleading
# "Artifacts must have both a schema_title and a schema_version ... Got: str".

import os
from typing import NamedTuple

from kfp import dsl

__all__ = [
    "FIGURES_EXTRA",
    "RUNNER_IMAGE",
    "ensure_infra",
    "finalize",
    "plan_shards",
    "preflight",
    "run_shard",
    "validate_config",
]

#: Immutable, SHA-tagged image reference. A KeyError here is intentional: the
#: KFP execution cache keys on the image, so a floating tag would let a cached
#: "success" come from code that no longer exists.
RUNNER_IMAGE = os.environ["BQ_CONTEXT_IMAGE"]

#: Installed at runtime by `finalize`, and only when figures are requested.
#:
#: A second image was tried first — the runner plus PaperBanana — and deleted.
#: A task's image is fixed at compile time (ContainerSpec.image rejects a
#: PipelineChannel), so it could not be built by an earlier step and handed over;
#: it had to be built and pushed out of band. Measured, a cold install of this
#: extra takes ~3s against minutes to build and push a multi-gigabyte image, so
#: the second image, its Dockerfile, its Cloud Build config and the job of
#: keeping it in step with the runner were all buying nothing.
#:
#: Must stay in step with the `figures` extra in pyproject.toml; a test asserts it.
FIGURES_EXTRA = "paperbanana>=0.1"


@dsl.component(base_image=RUNNER_IMAGE, install_kfp_package=False)
def validate_config(project: str, out: str, expect_identity: str = "") -> None:
    """Fail fast on identity, permissions, models, and storage.

    Runs first and retries zero times. Thirty seconds here beats discovering a
    missing grant forty minutes into ensure-infra — or never, because
    lookupContext returns empty rather than 403 and the run just looks null.
    """
    import os
    import subprocess
    import sys

    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    args = ["bq-context", "validate-config", "--out", out]
    if expect_identity:
        # Assert, do not impersonate. This task already runs as the service
        # account, so --impersonate would ask it to impersonate itself and fail
        # with "Permission 'iam.serviceAccounts.getAccessToken' denied".
        args += ["--expect-identity", expect_identity]
    print("+ " + " ".join(args), flush=True)
    sys.exit(subprocess.run(args, check=False).returncode)


@dsl.component(base_image=RUNNER_IMAGE, install_kfp_package=False)
def ensure_infra(project: str, out: str, skip: bool = False) -> None:
    """Create the four-tier corpus. Idempotent, 12-40 minutes.

    One component rather than five (datasets, views, scans, glossary, links):
    the phases are strictly ordered and cheap, and splitting them would buy five
    more VM startups and five more IAM surfaces to debug.

    ``skip`` is handled here rather than by wrapping the task in ``dsl.If``,
    because a conditional group cannot be depended on from outside it — preflight
    would then race provisioning instead of waiting for it.

    ``out`` only gets the bucket checked for existence: creating it needs
    storage.buckets.create, which this service account deliberately lacks, and
    the bucket must already exist anyway because pipeline_root lives in it.
    """
    import os
    import subprocess
    import sys

    if skip:
        print("skip=True; leaving existing infrastructure alone", flush=True)
        return

    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    args = ["bq-context", "ensure-infra", "--out", out, "--yes"]
    print("+ " + " ".join(args), flush=True)
    sys.exit(subprocess.run(args, check=False).returncode)


@dsl.component(base_image=RUNNER_IMAGE, install_kfp_package=False)
def preflight(
    project: str,
    tier: int,
    baseline: int,
    ladder: dsl.Output[dsl.Markdown],
    tier_metrics: dsl.Output[dsl.Metrics],
) -> NamedTuple("Preflight", [("fingerprint", str)]):  # ty: ignore[invalid-type-form]
    """Assert catalog enrichment is real, and publish the corpus fingerprint.

    The most important gate in the system. lookupContext returns an empty
    response rather than 403 on missing permissions, so a privileged developer
    account reads context fine while the SA silently reads nothing — and here
    the task *is* the SA, so this is the real test.

    Returns the enrichment fingerprint, which ``dag.py`` threads into every shard
    as a cache-key input. Without it, changing the corpus and resubmitting under
    the same commit returns cells scored against the old corpus.

    The **functional** ``NamedTuple(...)`` form is load-bearing, not a style
    choice. KFP extracts this function into a standalone ``ephemeral_component.py``
    and re-evaluates the ``def`` — annotations included — with only
    ``from kfp.dsl import *`` and ``from typing import *`` in scope. A
    module-level ``class Preflight(NamedTuple)`` is in neither, so the annotation
    raises ``NameError: name 'Preflight' is not defined`` at task startup. The
    class form was tried, compiled cleanly, and failed in Vertex; ``NamedTuple``
    itself comes from ``typing`` and survives.

    ty rejects a call in a return annotation, hence the ignore. That is the price
    of the only form KFP can execute.
    """
    import json
    import os
    import subprocess
    from collections import namedtuple

    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    payload_path = "/tmp/preflight.json"  # noqa: S108
    # No --impersonate: the task already runs as the pipeline service account,
    # which is exactly the identity this gate needs to exercise.
    args = [
        "bq-context",
        "preflight",
        "--tier",
        str(tier),
        "--baseline",
        str(baseline),
        "--json",
        payload_path,
    ]
    print("+ " + " ".join(args), flush=True)
    returncode = subprocess.run(args, check=False).returncode

    # Write the artifact even on the failure path. KFP creates only the *parent*
    # directory of an artifact path, so leaving the file unwritten registers a
    # system.Markdown pointing at nothing and the UI shows a dead link — on
    # exactly the runs someone needs to read.
    if not os.path.exists(payload_path):  # noqa: PTH110
        with open(ladder.path, "w") as handle:  # noqa: PTH123
            handle.write(f"# preflight\n\nExited {returncode}; no ladder produced.\n")
        raise SystemExit(returncode or 1)

    with open(payload_path) as handle:  # noqa: PTH123
        payload = json.load(handle)

    rows = payload["ladder"]
    lines = [
        f"# Enrichment ladder — tier {baseline} to {tier}",
        "",
        f"Corpus fingerprint `{payload['fingerprint']}`.",
        "",
        "| tier | tables | bytes | profiled cols | glossary cols | aspects |",
        "|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['tier']} | {r['tables']} | {r['bytes']:,} | {r['profiled']} | "
        f"{r['terms']} | {', '.join(r['aspects']) or '—'} |"
        for r in rows
    ]
    with open(ladder.path, "w") as handle:  # noqa: PTH123
        handle.write("\n".join(lines) + "\n")

    # log_metric takes floats only, so `aspects` (a list) stays in the markdown.
    for row in rows:
        for field in ("tables", "bytes", "profiled", "terms"):
            tier_metrics.log_metric(f"tier{row['tier']}_{field}", float(row[field]))

    if returncode != 0:
        raise SystemExit(returncode)

    print(f"corpus fingerprint {payload['fingerprint']}", flush=True)
    # Built here rather than referencing anything module-level, for the same
    # reason the annotation uses the functional form: this body is extracted and
    # run standalone, where nothing from this module exists. KFP matches on
    # _fields, so a structurally identical namedtuple is what it wants.
    return namedtuple("Preflight", ["fingerprint"])(payload["fingerprint"])  # noqa: PYI024


@dsl.component(base_image=RUNNER_IMAGE, install_kfp_package=False)
def plan_shards(tiers: list, approaches: list) -> list:
    """Return the shard plan the sweep fans out over.

    Each item is just ``{tier, approach}``. The question set is baked into the
    image, so it does not travel through the plan — which keeps this well under
    KFP's 131,072-byte cap on a task's output-parameter payload, and keeps the
    cap irrelevant even if the plan grows.

    **Order matters here, not just contents.** ParallelFor dispatches in list
    order, so a tier-major plan puts all of tier 0 in the first wave and tier 3
    hours later — which is how the first full run confounded tier with search
    index warm-up. order_shards groups each approach's tiers into one wave.
    """
    from bq_context.runner.planner import order_shards

    return [
        {"tier": int(t), "approach": str(a)}
        for t, a in order_shards([int(x) for x in tiers], [str(x) for x in approaches])
    ]


@dsl.component(base_image=RUNNER_IMAGE, install_kfp_package=False)
def run_shard(
    project: str,
    experiment_id: str,
    tier: int,
    approach: str,
    runs: int,
    out: str,
    code_version: str,
    corpus_fingerprint: str = "",
    question_limit: int = 0,
) -> None:
    """Execute one (tier, approach) shard. Resumable, and never fails the run.

    ``code_version`` is an explicit input rather than something read inside the
    container, because KFP's cache key is built from component inputs. Without
    it, editing a prompt and rerunning would silently return cached results
    produced by the old code — the most dangerous KFP behaviour for an
    experiment.

    Exits 0 even when cells failed. A failed cell is data; only
    ``verify_completeness`` decides whether the run as a whole is acceptable.
    An aborted shard (circuit breaker) does exit non-zero so the retry fires.
    """
    import os
    import subprocess
    import sys

    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    args = [
        "bq-context",
        "run-shard",
        "--experiment-id",
        experiment_id,
        "--tier",
        str(tier),
        "--approach",
        approach,
        "--runs",
        str(runs),
        "--out",
        out,
        "--code-version",
        code_version,
        "--corpus-fingerprint",
        corpus_fingerprint,
    ]
    if question_limit:
        args += ["--limit", str(question_limit)]

    from bq_context.logging_setup import json_log

    json_log(
        "INFO",
        "shard starting",
        shard_id=f"tier{tier}__{approach}",
        experiment_id=experiment_id,
        tier=tier,
        approach=approach,
        code_version=code_version,
        corpus_fingerprint=corpus_fingerprint,
    )
    print("+ " + " ".join(args), flush=True)
    returncode = subprocess.run(args, check=False).returncode
    json_log(
        "INFO" if returncode == 0 else "ERROR",
        "shard finished",
        shard_id=f"tier{tier}__{approach}",
        experiment_id=experiment_id,
        returncode=returncode,
    )
    sys.exit(returncode)


@dsl.component(base_image=RUNNER_IMAGE, install_kfp_package=False)
def finalize(
    project: str,
    experiment_id: str,
    out: str,
    runs: int,
    tiers: list,
    approaches: list,
    merged: dsl.Output[dsl.Dataset],
    report: dsl.Output[dsl.Markdown],
    summary: dsl.Output[dsl.HTML],
    run_metrics: dsl.Output[dsl.Metrics],
    require_complete: bool = True,
    question_limit: int = 0,
    refresh_figures: bool = False,
) -> None:
    """Merge, score, plot — then fail if cells are missing.

    Runs as the ``ExitHandler`` exit task, so it executes whether or not the
    sweep succeeded. That is why it globs storage instead of consuming shard
    outputs: an exit task cannot read the outputs of tasks inside its handler,
    and it must still produce something useful when a shard died.

    Collapsing merge/score/plot/verify into one component departs from the
    four-component sketch in the plan. An ``ExitHandler`` accepts exactly one
    exit task, and splitting them would mean either a task that cannot depend on
    the merge or a race on ``missing.json``. Nothing is lost: iterating on
    scoring happens through the CLI against a frozen ``merged/results.jsonl``,
    not by rerunning the pipeline.

    This is **the only task allowed to turn the run red**, and it does so only
    for missing cells — never for a merge or scoring hiccup.
    """
    import os
    import subprocess
    import sys

    os.environ["GOOGLE_CLOUD_PROJECT"] = project
    base = ["--experiment-id", experiment_id, "--out", out]

    from bq_context.pipeline.publish import merge_args

    merge = merge_args(
        experiment_id,
        out,
        runs=runs,
        tiers=tiers,
        approaches=approaches,
        question_limit=question_limit,
    )

    # Give score and plot real destinations. Both flags already existed and were
    # simply never passed: score wrote markdown only with --report, and plot's
    # --plots-dir defaulted to a *relative* path, so the figures landed in
    # /app/plots inside this container and died with it. Every run produced them
    # and threw them away.
    import tempfile

    workdir = tempfile.mkdtemp(prefix="bq-context-finalize-")
    report_path = report.path
    plots_dir = f"{workdir}/plots"

    steps = (
        merge,
        ["bq-context", "score", *base, "--report", report_path],
        ["bq-context", "plot", *base, "--plots-dir", plots_dir],
        # Last: it inlines the figures plot just produced.
        ["bq-context", "report", *base, "--html", summary.path, "--figures", plots_dir],
    )
    if refresh_figures:
        # Install the extra here rather than shipping it in the image. It is ~51
        # packages needed by one task on the rare run that asks for figures, and a
        # cold install measures ~3s — far cheaper than a second image nobody can
        # hand to a component anyway, since base_image is compile-time only.
        install = ["uv", "pip", "install", "--python", sys.executable, "-q", FIGURES_EXTRA]
        print("+ " + " ".join(install), flush=True)
        if subprocess.run(install, check=False).returncode != 0:
            print("WARN  could not install the figures extra; skipping diagrams", flush=True)
        else:
            # Architecture diagrams only: generation is slow, paid and
            # non-deterministic, and they do not change between runs.
            steps = (*steps[:-1], ["bq-context", "figures", "--dir", plots_dir], steps[-1])
    for args in steps:
        print("+ " + " ".join(args), flush=True)
        completed = subprocess.run(args, check=False)
        if completed.returncode != 0:
            print(f"WARN  {args[1]} exited {completed.returncode}", flush=True)

    # Upload to the stable experiment prefix rather than a KFP artifact path.
    # Artifact URIs embed the pipeline job id and change every run; these are the
    # copies a human goes looking for weeks later.
    from bq_context.pipeline.publish import (
        ensure_placeholder,
        merge_report,
        publish_figures,
        publish_report,
        publish_summary,
    )
    from bq_context.runner.resume import experiment_prefix
    from bq_context.runner.store import store_for

    store = store_for(out)
    if published := publish_report(store, experiment_id, report.path):
        print(f"report    {store.uri(published)}", flush=True)
    for figure in publish_figures(store, experiment_id, plots_dir):
        print(f"figure    {store.uri(figure)}", flush=True)

    # Populate every artifact *before* anything can raise. SystemExit propagates
    # past KFP's executor before write_executor_output runs, so a raise here would
    # discard every .uri and .metadata mutation — on precisely the red runs a
    # human most wants to inspect.
    ensure_placeholder(report.path, f"# Results — {experiment_id}\n\nNo report produced.\n")
    ensure_placeholder(
        summary.path, f"<html><body><h1>{experiment_id}</h1><p>No report.</p></body></html>"
    )
    publish_summary(store, experiment_id, summary.path)

    report_json = merge_report(store, experiment_id)
    merged.uri = store.uri(f"{experiment_prefix(experiment_id)}/merged/results.jsonl")
    merged.metadata.update(
        {
            "experiment_id": experiment_id,
            "expected": report_json["expected"],
            "present": report_json["present"],
            "missing_count": report_json["missing_count"],
        }
    )
    for key in ("expected", "present", "missing_count"):
        run_metrics.log_metric(key, float(report_json[key]))

    if not require_complete:
        return

    preview_limit = 20
    missing = report_json["missing"]
    print(f"{report_json['present']}/{report_json['expected']} cells present", flush=True)
    if missing:
        preview = "\n  ".join(missing[:preview_limit])
        suffix = (
            f"\n  ... and {len(missing) - preview_limit} more"
            if len(missing) > preview_limit
            else ""
        )
        print(f"FAIL  {len(missing)} missing cell(s):\n  {preview}{suffix}", flush=True)
        sys.exit(1)
