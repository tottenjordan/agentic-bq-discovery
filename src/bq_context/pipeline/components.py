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


class Preflight(NamedTuple):
    """`preflight`'s output parameters.

    A class rather than the functional ``NamedTuple("Preflight", [...])`` form:
    the functional form is a *call*, which is not a valid return annotation and
    which ty rejects outright. KFP reads ``_fields`` either way.

    Only the annotation can use this. The component *body* is extracted and run
    standalone in the container, where this class does not exist, so the body
    builds its own equivalent namedtuple.
    """

    fingerprint: str


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
def preflight(project: str, tier: int, baseline: int) -> Preflight:
    """Assert catalog enrichment is real, and publish the corpus fingerprint.

    The most important gate in the system. lookupContext returns an empty
    response rather than 403 on missing permissions, so a privileged developer
    account reads context fine while the SA silently reads nothing — and here
    the task *is* the SA, so this is the real test.

    Returns the enrichment fingerprint, which ``dag.py`` threads into every shard
    as a cache-key input. Without it, changing the corpus and resubmitting under
    the same commit returns cells scored against the old corpus.

    A ``NamedTuple`` rather than a bare ``-> str`` so the DAG reads
    ``check.outputs["fingerprint"]``; KFP names a bare return ``"Output"``, and
    ``task.output`` raises outright once a task has more than one output.
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
    if returncode != 0:
        # Fail the task. No outputs are produced, which is correct: a fingerprint
        # for a corpus that failed the gate would key shard cache to a corpus
        # nobody should be running on.
        raise SystemExit(returncode)

    with open(payload_path) as handle:  # noqa: PTH123
        payload = json.load(handle)
    print(f"corpus fingerprint {payload['fingerprint']}", flush=True)
    # Deliberately not the module-level Preflight: this body is extracted and run
    # standalone in the container, where that class does not exist. KFP matches on
    # _fields, so a structurally identical namedtuple is what it wants — but ty
    # sees two distinct types, and it is right to.
    return namedtuple("Preflight", ["fingerprint"])(  # noqa: PYI024  # ty: ignore[invalid-return-type]
        payload["fingerprint"]
    )


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
    print("+ " + " ".join(args), flush=True)
    sys.exit(subprocess.run(args, check=False).returncode)


@dsl.component(base_image=RUNNER_IMAGE, install_kfp_package=False)
def finalize(
    project: str,
    experiment_id: str,
    out: str,
    runs: int,
    tiers: list,
    approaches: list,
    require_complete: bool = True,
    question_limit: int = 0,
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

    merge = ["bq-context", "merge", *base, "--runs", str(runs)]
    if question_limit:
        # Must match what the shards ran, or merge reports phantom missing cells.
        merge += ["--limit", str(question_limit)]
    for tier in tiers:
        merge += ["--tier", str(tier)]
    for approach in approaches:
        merge += ["--approach", str(approach)]

    # Give score and plot real destinations. Both flags already existed and were
    # simply never passed: score wrote markdown only with --report, and plot's
    # --plots-dir defaulted to a *relative* path, so the figures landed in
    # /app/plots inside this container and died with it. Every run produced them
    # and threw them away.
    import tempfile

    workdir = tempfile.mkdtemp(prefix="bq-context-finalize-")
    report_path = f"{workdir}/report.md"
    plots_dir = f"{workdir}/plots"

    steps = (
        merge,
        ["bq-context", "score", *base, "--report", report_path],
        ["bq-context", "plot", *base, "--plots-dir", plots_dir],
    )
    for args in steps:
        print("+ " + " ".join(args), flush=True)
        completed = subprocess.run(args, check=False)
        if completed.returncode != 0:
            print(f"WARN  {args[1]} exited {completed.returncode}", flush=True)

    # Upload to the stable experiment prefix rather than a KFP artifact path.
    # Artifact URIs embed the pipeline job id and change every run; these are the
    # copies a human goes looking for weeks later.
    import pathlib

    from bq_context.runner.resume import experiment_prefix
    from bq_context.runner.store import store_for

    store = store_for(out)
    prefix = experiment_prefix(experiment_id)
    if pathlib.Path(report_path).exists():
        store.write_text(f"{prefix}/scoring/report.md", pathlib.Path(report_path).read_text())
        print(f"report    {store.uri(f'{prefix}/scoring/report.md')}", flush=True)
    for png in sorted(pathlib.Path(plots_dir).glob("*.png")):
        store.write_bytes(f"{prefix}/plots/{png.name}", png.read_bytes(), "image/png")
        print(f"figure    {store.uri(f'{prefix}/plots/{png.name}')}", flush=True)

    if not require_complete:
        return

    # Read the merge report rather than re-deriving: it is the single record of
    # what the sweep actually produced.
    import json

    from bq_context.runner.store import store_for
    from bq_context.scoring.merge import missing_path

    preview_limit = 20
    report = json.loads(store_for(out).read_text(missing_path(experiment_id)))
    missing = report["missing"]
    print(f"{report['present']}/{report['expected']} cells present", flush=True)
    if missing:
        preview = "\n  ".join(missing[:preview_limit])
        suffix = (
            f"\n  ... and {len(missing) - preview_limit} more"
            if len(missing) > preview_limit
            else ""
        )
        print(f"FAIL  {len(missing)} missing cell(s):\n  {preview}{suffix}", flush=True)
        sys.exit(1)
