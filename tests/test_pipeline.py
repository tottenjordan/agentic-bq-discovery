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

# BQ_CONTEXT_IMAGE, SECRET_ID and the config variables are pinned by
# `conftest.py` at *its* import, which precedes every test module. Setting them
# again here would be redundant, and worse: it would let this module pass in
# isolation while the ordering bug it guards against still existed under a full
# run. See the comment beside `_FAKE_ENV`.
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
# The API-key secret's name in finalize's environment
#
# `finalize` shells out to `bq-context figures`, which reads SECRET_ID to learn
# which Secret Manager secret holds the Gemini Developer API key. There is no
# `.env` in the runner image — `.dockerignore` excludes it, under "# Secrets" —
# so without this the variable is unset in the container, `secret_id()` raises,
# `api_key` catches it, and figure generation is skipped with a WARN on a green
# run.
#
# The value is baked at compile time rather than passed as a pipeline parameter
# because `set_env_variable` rejects a PipelineChannel: it fails the compile with
# `TypeError: bad argument type for built-in operation`. That is the opposite of
# `set_container_image`, which does take a runtime value — the two are not
# interchangeable, and the annotation on both is a bare `str`.
# ---------------------------------------------------------------------------
def _env(spec: dict[str, Any], executor: str) -> dict[str, str]:
    container = spec["deploymentSpec"]["executors"][executor]["container"]
    return {e["name"]: e.get("value", "") for e in container.get("env", [])}


def test_finalize_carries_the_secret_name_in_its_environment(spec: dict[str, Any]) -> None:
    # Non-empty first. Both sides are "" when SECRET_ID is unset at import, and
    # this assertion then compares nothing — which is what it did in CI until
    # conftest started pinning the value at import time.
    assert components.SECRET_ID, "unset at import; the comparison below would be vacuous"
    assert _env(spec, "exec-finalize").get("SECRET_ID") == components.SECRET_ID


def test_no_other_task_carries_it(spec: dict[str, Any]) -> None:
    """The 24 shards never generate figures. Scoping the variable to the one task
    that reads it keeps the blast radius of a rename to that task."""
    others = {
        name: _env(spec, name)
        for name in spec["deploymentSpec"]["executors"]
        if name != "exec-finalize"
    }
    assert not [n for n, env in others.items() if "SECRET_ID" in env]


def test_the_experiment_configuration_reaches_every_task(spec: dict[str, Any]) -> None:
    """Whatever the submitter set must appear on all six executors.

    Not just the shards: `ensure-infra` and `preflight` resolve the corpus from
    RESOURCE_PREFIX, and a run that provisions one corpus and measures another is
    the failure this closes.
    """
    assert components.CONFIG_ENV, "fixture env should have set at least one key"
    for name in spec["deploymentSpec"]["executors"]:
        env = _env(spec, name)
        for key, value in components.CONFIG_ENV.items():
            assert env.get(key) == value, f"{name} is missing {key}"


def test_no_derived_location_is_forwarded_into_the_spec(spec: dict[str, Any]) -> None:
    """The image pins GOOGLE_CLOUD_LOCATION=global because these models 404 in
    us-central1. A task-level value would override the image's, so a developer
    .env saying us-central1 must never reach the spec."""
    for name in spec["deploymentSpec"]["executors"]:
        env = _env(spec, name)
        assert "GOOGLE_CLOUD_LOCATION" not in env
        assert "GOOGLE_GENAI_USE_VERTEXAI" not in env


def test_the_compiled_spec_never_contains_the_key_itself(spec: dict[str, Any]) -> None:
    """The *name* of a secret is not sensitive; its value is.

    `set_env_variable` writes straight into the compiled spec and the PipelineJob
    resource, both readable by anyone with viewer access, so the key is fetched
    from Secret Manager at runtime and must never be baked. This asserts the
    distinction holds rather than trusting that nobody takes the shortcut.
    """
    rendered = yaml.safe_dump(spec)
    assert "AIza" not in rendered
    assert "GOOGLE_API_KEY" not in rendered


# ---------------------------------------------------------------------------
# Image and caching
# ---------------------------------------------------------------------------
def test_every_component_pins_the_same_immutable_image(spec: dict[str, Any]) -> None:
    """One image, still.

    A second image — the runner plus PaperBanana — was built and then deleted, on
    cost rather than feasibility: a cold install of the extra measures ~3s, and a
    dynamic image would still have to be built and pushed. `finalize` installs it
    at runtime when figures are requested. `set_container_image` remains available
    if that trade ever changes; see docs/notes/kfp-pipeline.md.
    """
    images = {c["container"]["image"] for c in spec["deploymentSpec"]["executors"].values()}
    assert len(images) == 1
    image = images.pop()
    assert not image.endswith(":latest"), "a floating tag makes the KFP cache lie"
    assert image == components.RUNNER_IMAGE


def test_the_figures_extra_matches_the_declared_optional_dependency() -> None:
    """finalize installs FIGURES_EXTRA by string; pyproject declares the same
    extra for local use. Drift means the pipeline installs a version nobody
    tested against."""
    import tomllib

    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    declared = pyproject["project"]["optional-dependencies"]["figures"]
    assert declared == [components.FIGURES_EXTRA]


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
    assert set(PROFILES) == {"smoke", "pilot", "survey", "full"}


def test_smoke_runs_tier_three_not_tier_zero() -> None:
    """Tier 0 is unenriched: a green tier-0 smoke would pass even if every
    scan, term, link, and aspect were missing. Tier 3 has the most ways to fail
    informatively."""
    assert PROFILES["smoke"]["tiers"] == [3]


def test_full_is_the_published_factorial() -> None:
    assert PROFILES["full"]["tiers"] == [0, 1, 2, 3]
    assert PROFILES["full"]["runs"] == 5


def test_survey_runs_every_question_once() -> None:
    """The gap `pilot` left, found by running it.

    `pilot` takes the first five questions and all five happen to be
    `single-table`, so a hard-corpus pilot exercised none of the four traps, none
    of the multi-table questions, and none of the twelve that name no place --
    which are exactly the ones a near-neighbour corpus is meant to make hard. Its
    flat tier response measured almost nothing.

    `full` would cover them, at 3,000 cells. This is the missing rung: every
    question, every tier, once.
    """
    survey = PROFILES["survey"]
    assert survey["question_limit"] == 0, "0 means all 25 questions"
    assert survey["runs"] == 1
    assert survey["tiers"] == [0, 1, 2, 3]


def test_survey_is_a_quarter_of_full() -> None:
    """The point of it: breadth without repetition. 600 cells against 3,000."""
    assert PROFILES["survey"]["runs"] * 5 == PROFILES["full"]["runs"]
    assert PROFILES["survey"]["question_limit"] == PROFILES["full"]["question_limit"]
    assert PROFILES["survey"]["tiers"] == PROFILES["full"]["tiers"]


def test_every_profile_requires_a_complete_sweep() -> None:
    """A partial sweep silently scores against missing cells."""
    assert all(p["require_complete"] for p in PROFILES.values())


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
    """It runs as the SA already; that is precisely what makes the gate real.

    The exhaustive comparison is the point: it is what stops an identity
    parameter arriving under another name. `out`, `experiment_id` and `run_id`
    are here only so preflight can leave its fingerprint where the exit task
    reads it — none of them says who the task runs as.
    """
    params = _tasks(spec)["preflight"]["inputs"]["parameters"]
    assert "service_account" not in params
    assert set(params) == {"project", "tier", "baseline", "out", "experiment_id", "run_id"}


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


# ---------------------------------------------------------------------------
# The extracted module must be self-sufficient
# ---------------------------------------------------------------------------
def test_every_component_body_resolves_in_the_namespace_kfp_gives_it(
    spec: dict[str, Any],
) -> None:
    """Regression from a real pipeline failure, and the compile could not see it.

    KFP does not ship the module. It extracts each function's source into a
    standalone `ephemeral_component.py` and re-evaluates the `def` — annotations
    included — with only `from kfp.dsl import *` and `from typing import *` in
    scope.

    `preflight` was annotated `-> Preflight`, a module-level
    `class Preflight(NamedTuple)`. It compiled cleanly, because compilation
    introspects the *original* module where that class exists, and then died at
    task startup with `NameError: name 'Preflight' is not defined`. The
    functional `NamedTuple("Preflight", [...])` form works because `NamedTuple`
    comes from `typing`.

    Executing the embedded source in the same namespace is the only offline check
    for this: it exercises the def statement KFP will actually run.
    """
    import typing

    import kfp
    from kfp import dsl as kfp_dsl

    executors = spec["deploymentSpec"]["executors"]
    for name, executor in executors.items():
        source = executor["container"]["command"][-1]
        # Exactly what KFP's generated preamble provides, by name.
        namespace: dict[str, Any] = {"kfp": kfp, "dsl": kfp_dsl}
        namespace.update({k: getattr(kfp_dsl, k) for k in dir(kfp_dsl) if not k.startswith("_")})
        namespace.update({k: getattr(typing, k) for k in dir(typing) if not k.startswith("_")})
        # Only the def statements matter; the executor preamble needs argv.
        body = source.split("\ndef ", 1)
        assert len(body) == 2, f"{name}: no function found in the embedded source"
        definition = "def " + body[1].split("\n\ndef main(")[0]
        try:
            # dont_inherit=True is load-bearing. compile() otherwise inherits the
            # __future__ flags of *this* module, and this file opens with
            # `from __future__ import annotations` — so the def would get PEP 563
            # lazy annotations, never evaluate them, and this test would pass
            # whatever the annotation referenced. It did exactly that at first.
            exec(compile(definition, f"<{name}>", "exec", dont_inherit=True), namespace)  # noqa: S102
        except NameError as exc:
            pytest.fail(f"{name} references {exc}, which KFP does not provide at runtime")


def test_no_component_body_references_a_module_level_name(spec: dict[str, Any]) -> None:
    """The bug the *other* body test could not see.

    `test_every_component_body_resolves_in_the_namespace_kfp_gives_it` execs the
    `def` statement, which proves the annotations resolve. It says nothing about
    names used *inside* the body, and those fail only when that line runs.

    `finalize` referenced `FIGURES_EXTRA`, a module-level constant in
    components.py. KFP extracts the function into a standalone file, so the name
    is simply absent — but only the `refresh_figures=True` branch touches it, so
    it survived every compile, every test, and one live smoke run before firing:

        NameError: name 'FIGURES_EXTRA' is not defined

    This walks each body's symbol table instead, and flags any free name the
    extracted module will not have.
    """
    import ast
    import builtins
    import symtable
    import typing

    import kfp
    from kfp import dsl as kfp_dsl

    provided = (
        set(dir(builtins)) | set(dir(kfp_dsl)) | set(dir(typing)) | {"kfp", "dsl", "NamedTuple"}
    )
    del kfp

    offenders: dict[str, list[str]] = {}
    for executor in spec["deploymentSpec"]["executors"].values():
        source = executor["container"]["command"][-1]
        module = ast.parse(source)
        funcs = [n for n in module.body if isinstance(n, ast.FunctionDef)]
        for fn in funcs:
            # Re-render just this def so symtable scopes it on its own.
            text = ast.unparse(fn)
            table = symtable.symtable(text, "<component>", "exec")
            inner = table.get_children()[0]
            free = {
                s.get_name()
                for s in inner.get_symbols()
                if s.is_global() and not s.is_assigned() and s.get_name() not in provided
            }
            if free:
                offenders[fn.name] = sorted(free)

    assert not offenders, (
        f"component bodies reference names KFP will not provide: {offenders}. "
        "A component body is extracted standalone; import what it needs inside "
        "the function, or inline the value."
    )


def test_the_shard_cache_key_carries_all_three_identities(spec: dict[str, Any]) -> None:
    """The shards are the only cacheable task that does real work, so every
    input that changes what a cell *means* has to be declared here or a rerun
    returns the wrong cells, green.

    Three of them now, each added after a near miss or a real one:
    `code_version` for edited code, `corpus_fingerprint` for re-provisioned
    enrichment, `questions_fingerprint` for a swapped question set.
    """
    # Nested inside the ParallelFor group, not at the root dag.
    shard = _component(spec, "for-loop-2")["dag"]["tasks"]["run-shard"]
    params = shard["inputs"]["parameters"]
    for identity in ("code_version", "corpus_fingerprint", "questions_fingerprint"):
        assert identity in params, identity


def test_the_shards_read_the_snapshot_not_the_image(spec: dict[str, Any]) -> None:
    """The image's baked-in `experiments/questions.json` is the default for a
    local run. A pipeline shard must read the copy `submit-pipeline` wrote, or
    `--questions` would be accepted at submission and silently ignored."""
    import json as _json

    # The component body is embedded as source in the executor spec, so the
    # flag and the path it points at are both readable there.
    executor = _json.dumps(spec["deploymentSpec"]["executors"])
    assert "--questions" in executor
    assert "experiments/{experiment_id}/questions.json" in executor
    assert "--questions-fingerprint" in executor
