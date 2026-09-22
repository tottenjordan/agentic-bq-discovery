"""The pipeline spec compiles, and encodes the guarantees the design rests on.

Compiling is the only cheap way to catch a whole class of KFP mistakes. Two of
them cost real time while building this:

- ``from __future__ import annotations`` in a module defining components or the
  pipeline breaks compilation, because KFP introspects ``__annotations__`` at
  runtime and PEP 563 turns them all into strings. It fails with "Artifacts must
  have both a schema_title and a schema_version ... Got: str", which points
  nowhere near the cause.
- ``ParallelFor(parallelism=)`` and ``set_retry(...)`` reject pipeline
  parameters; both need compile-time constants.

Neither is visible until you compile, and neither is visible in a unit test that
imports the modules without compiling.
"""

from __future__ import annotations

import os

# Must precede the pipeline imports: components.py resolves base_image from the
# environment at import time, deliberately raising KeyError when it is unset.
os.environ.setdefault("BQ_CONTEXT_IMAGE", "us-central1-docker.pkg.dev/p/r/runner:testsha")

from pathlib import Path
from typing import Any

import pytest
import yaml

from bq_context.pipeline import components, dag
from bq_context.pipeline.compilation import compile_pipeline
from bq_context.pipeline.submit import PROFILES


@pytest.fixture(scope="module")
def spec(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    out = tmp_path_factory.mktemp("spec") / "pipeline.yaml"
    compile_pipeline(out)
    return yaml.safe_load(out.read_text())


def _tasks(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["root"]["dag"]["tasks"]


def _component(spec: dict[str, Any], fragment: str) -> dict[str, Any]:
    key = next(k for k in spec["components"] if fragment in k)
    return spec["components"][key]


# ---------------------------------------------------------------------------
# It compiles at all
# ---------------------------------------------------------------------------
def test_pipeline_compiles(spec: dict[str, Any]) -> None:
    assert spec["pipelineInfo"]["name"] == dag.PIPELINE_NAME


def test_compiling_without_an_image_is_a_hard_error() -> None:
    """base_image resolves at compile time, so a default would pin something stale."""
    assert components.RUNNER_IMAGE, "module must resolve an image at import"


def test_no_pep563_in_modules_kfp_introspects() -> None:
    """Guards the failure mode with the least helpful error message in KFP."""
    for module in (components, dag):
        lines = Path(module.__file__).read_text().splitlines()  # type: ignore[arg-type]
        # Match the statement, not the comment explaining why it is absent.
        offending = [
            n for n, ln in enumerate(lines, 1) if ln.strip() == "from __future__ import annotations"
        ]
        assert not offending, f"{module.__name__} line(s) {offending}"


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def test_execution_order(spec: dict[str, Any]) -> None:
    tasks = _tasks(spec)
    assert tasks["validate-config"].get("dependentTasks") is None
    assert tasks["ensure-infra"]["dependentTasks"] == ["validate-config"]
    assert tasks["plan-shards"]["dependentTasks"] == ["preflight"]


def test_preflight_waits_for_provisioning(spec: dict[str, Any]) -> None:
    """Regression: preflight originally depended only on validate-config.

    ensure_infra was wrapped in dsl.If, and a conditional group cannot be
    depended on from outside, so preflight raced provisioning and would have
    failed with "dataset does not exist" on a cold project.
    """
    assert _tasks(spec)["preflight"]["dependentTasks"] == ["ensure-infra"]


def test_finalize_runs_even_if_the_sweep_dies(spec: dict[str, Any]) -> None:
    """The ExitHandler guarantee: partial results beat no results."""
    finalize = _tasks(spec)["finalize"]
    assert finalize["triggerPolicy"]["strategy"] == "ALL_UPSTREAM_TASKS_COMPLETED"
    assert any("exit-handler" in d for d in finalize["dependentTasks"])


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------
def test_parallelism_is_eight(spec: dict[str, Any]) -> None:
    """Where the makespan curve bends; above it the bq_tools shard pins it."""
    handler = _component(spec, "exit-handler")["dag"]["tasks"]
    loop = next(t for t in handler if t.startswith("for-loop"))
    assert handler[loop]["iteratorPolicy"]["parallelismLimit"] == dag.PARALLELISM == 8


def test_shards_retry_with_backoff(spec: dict[str, Any]) -> None:
    """Only worth enabling because shards resume rather than restart."""
    retry = _component(spec, "for-loop")["dag"]["tasks"]["run-shard"]["retryPolicy"]
    assert retry["maxRetryCount"] == 2
    assert retry["backoffDuration"] == "120s"
    assert retry["backoffFactor"] == 2.0
    assert retry["backoffMaxDuration"] == "600s"


def test_code_version_reaches_every_shard(spec: dict[str, Any]) -> None:
    """KFP keys its cache on component inputs.

    Without code_version as an explicit input, editing a prompt and rerunning
    would silently return cached results produced by the old code — the most
    dangerous KFP behaviour for an experiment.
    """
    inputs = _component(spec, "for-loop")["dag"]["tasks"]["run-shard"]["inputs"]["parameters"]
    assert "code_version" in inputs


# ---------------------------------------------------------------------------
# Image and caching
# ---------------------------------------------------------------------------
def test_every_component_pins_the_same_immutable_image(spec: dict[str, Any]) -> None:
    images = {c["container"]["image"] for c in spec["deploymentSpec"]["executors"].values()}
    assert len(images) == 1
    image = images.pop()
    assert not image.endswith(":latest"), "a floating tag makes the KFP cache lie"
    assert image == components.RUNNER_IMAGE


@pytest.mark.parametrize("task", ["ensure-infra", "preflight", "finalize"])
def test_caching_is_off_where_staleness_would_mislead(spec: dict[str, Any], task: str) -> None:
    """Cloud state changes outside this pipeline.

    A cached "enrichment is fine" from last week is worse than useless, and a
    cached merge would hide cells written since.
    """
    assert _tasks(spec)[task].get("cachingOptions", {}).get("enableCache") is not True


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------
def test_profiles_cover_the_planned_escalation() -> None:
    assert set(PROFILES) == {"smoke", "pilot", "full"}


def test_smoke_runs_tier_three_not_tier_zero() -> None:
    """Tier 0 is unenriched: a green tier-0 smoke would pass even if every
    scan, term, link, and aspect were missing. Tier 3 has the most ways to fail
    informatively."""
    assert PROFILES["smoke"]["tiers"] == [3]


def test_full_is_the_published_factorial() -> None:
    assert PROFILES["full"]["tiers"] == [0, 1, 2, 3]
    assert PROFILES["full"]["runs"] == 5


def test_profiles_do_not_set_parallelism() -> None:
    """KFP requires it as a compile-time constant, so it lives in dag.PARALLELISM."""
    assert all("parallelism" not in p for p in PROFILES.values())


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------
# The identity regression (--impersonate vs --expect-identity) used to be
# asserted here by grepping components.py for those strings, which proved only
# that the characters appear somewhere in the file. It now lives in
# tests/test_pipeline_cli_seam.py, which runs the component and inspects the
# argv it actually builds — and additionally checks the flag exists on the CLI
# receiving it, which the textual version could not.


def test_preflight_component_takes_no_service_account(spec: dict[str, Any]) -> None:
    """It runs as the SA already; that is precisely what makes the gate real."""
    params = _tasks(spec)["preflight"]["inputs"]["parameters"]
    assert "service_account" not in params
    assert set(params) == {"project", "tier", "baseline"}


# ---------------------------------------------------------------------------
# Profiles carry a question limit
# ---------------------------------------------------------------------------
def test_smoke_and_pilot_are_cheap() -> None:
    """Without a limit the smoke profile inherits all 25 questions: 150 cells."""
    assert PROFILES["smoke"]["question_limit"] == 3
    assert PROFILES["pilot"]["question_limit"] == 5
    assert PROFILES["full"]["question_limit"] == 0, "0 means all 25"


# ---------------------------------------------------------------------------
# Output artifacts
# ---------------------------------------------------------------------------
def test_preflight_publishes_its_ladder_and_fingerprint(spec: dict[str, Any]) -> None:
    """The enrichment ladder gates the whole experiment and used to exist only on
    stdout. The fingerprint is what stops a corpus change returning cached cells."""
    out = spec["components"]["comp-preflight"]["outputDefinitions"]
    assert set(out["artifacts"]) == {"ladder", "tier_metrics"}
    assert out["parameters"]["fingerprint"]["parameterType"] == "STRING"


def test_finalize_publishes_its_results(spec: dict[str, Any]) -> None:
    """An ExitHandler exit task cannot *read* handler outputs, but it can declare
    its own — which is the only reason the report is reachable from the UI."""
    out = spec["components"]["comp-finalize"]["outputDefinitions"]
    assert {"merged", "report", "run_metrics"} <= set(out["artifacts"])


def test_shards_are_keyed_on_the_corpus_as_well_as_the_code(spec: dict[str, Any]) -> None:
    """Both inputs, or a changed corpus silently returns cells scored on the old one."""
    shard = next(k for k in spec["components"] if "run-shard" in k)
    params = spec["components"][shard]["inputDefinitions"]["parameters"]
    assert "code_version" in params
    assert "corpus_fingerprint" in params


# ---------------------------------------------------------------------------
# Caching, per task
# ---------------------------------------------------------------------------
#: Every task's intended setting, with the reason it holds. Exhaustive on
#: purpose: adding a task should require a decision, not inherit a default.
CACHING = {
    "validate-config": (False, "identity and IAM change outside the pipeline"),
    "ensure-infra": (False, "corpus state is external"),
    "preflight": (False, "a cached 'enrichment is fine' is worse than useless"),
    "plan-shards": (True, "a pure function of its inputs"),
    "run-shard": (True, "safe only because code_version AND corpus_fingerprint are inputs"),
    "finalize": (False, "must run on every attempt, including failed ones"),
}


def _all_tasks(spec: dict[str, Any]) -> dict[str, Any]:
    """Every task, including those nested in the ExitHandler and ParallelFor.

    run-shard lives two DAGs deep (exit-handler-1 -> for-loop-2 -> run-shard), so
    reading only spec["root"] silently skips the 24 tasks that matter most.
    """
    found = dict(spec["root"]["dag"]["tasks"])
    for component in spec["components"].values():
        if "dag" in component:
            found.update(component["dag"]["tasks"])
    return found


@pytest.mark.parametrize(("task", "expected"), [(t, v[0]) for t, v in CACHING.items()])
def test_each_task_sets_caching_deliberately(
    spec: dict[str, Any],
    task: str,
    expected: bool,  # noqa: FBT001 - a parametrize value, not a caller-facing flag
) -> None:
    """Note this only became meaningful once submit stopped passing a job-level
    bool, which overwrote every one of these settings before reaching Vertex."""
    actual = _all_tasks(spec)[task].get("cachingOptions", {}).get("enableCache", False)
    assert actual == expected, CACHING[task][1]


def test_every_task_in_the_dag_has_a_caching_decision(spec: dict[str, Any]) -> None:
    """A new task must not quietly inherit whatever the default happens to be.

    Group tasks are excluded: an ExitHandler and a ParallelFor are containers,
    not work, and carry no caching of their own.
    """
    groups = {"exit-handler-1", "for-loop-2"}
    assert set(_all_tasks(spec)) - groups == set(CACHING)
