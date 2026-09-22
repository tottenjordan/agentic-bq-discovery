"""Every pipeline component shells out to the CLI. Test that seam for real.

`components.py` and `cli.py` are each well covered on their own, and that is
exactly the problem: the only thing joining them is an argv list built inside a
component and interpreted by a Typer app that never sees it in a test. Rename a
CLI option and both sides stay green while the pipeline fails at runtime — 40
minutes and a VM per shard to find out.

That is not hypothetical here. Two pipeline runs failed on this seam: one
passing `--impersonate` to a task already running *as* the service account, and
one where `--expect-identity` met a metadata-server alias it did not handle.
The existing guard greps `components.py` for the string `"--expect-identity"`,
which proves the characters are present in the file and nothing more.

These tests run each component's undecorated function with `subprocess.run`
stubbed, capture the argv it actually builds, and check it against the real
Click command objects Typer produces. A component is allowed to fail *after*
building its argv — `finalize` goes on to read GCS — because the argv is the
only thing under test.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

# Must precede the pipeline import: components.py resolves base_image from the
# environment at import time and deliberately raises KeyError when it is unset.
os.environ.setdefault("BQ_CONTEXT_IMAGE", "us-central1-docker.pkg.dev/p/r/runner:testsha")

import pytest
import typer
from kfp import dsl

from bq_context.cli import app
from bq_context.pipeline import components


def _artifact(kind: type, name: str) -> Any:
    """A real KFP artifact backed by a scratch path.

    Components write to `.path` and mutate `.metadata`, so a MagicMock would let
    a broken write pass. These are the genuine classes pointed at a tempdir.
    """
    return kind(name=name, uri=str(Path(tempfile.mkdtemp()) / f"{name}.out"))


#: Representative arguments for every component that shells out. Values only
#: need to be well-formed; nothing here reaches GCP.
#:
#: The artifact entries are load-bearing. A component gaining a required
#: `Output[...]` parameter without one here raises TypeError inside
#: `contextlib.suppress(BaseException)` below, so nothing is captured and only
#: `test_every_component_shells_out_at_least_once` fails — pointing at the
#: harness rather than the cause.
COMPONENT_ARGS: dict[str, dict[str, Any]] = {
    "validate_config": {
        "project": "p",
        "out": "gs://b/e",
        "expect_identity": "sa@p.iam.gserviceaccount.com",
    },
    "ensure_infra": {"project": "p", "out": "gs://b/e"},
    "preflight": {
        "project": "p",
        "tier": 3,
        "baseline": 0,
        "ladder": _artifact(dsl.Markdown, "ladder"),
        "tier_metrics": _artifact(dsl.Metrics, "tier_metrics"),
    },
    "run_shard": {
        "project": "p",
        "experiment_id": "e",
        "tier": 3,
        "approach": "bq_tools",
        "runs": 5,
        "out": "gs://b/e",
        "code_version": "abc1234",
    },
    "finalize": {
        "project": "p",
        "experiment_id": "e",
        "out": "gs://b/e",
        "runs": 5,
        "tiers": [0, 3],
        "approaches": ["bq_tools"],
        "merged": _artifact(dsl.Dataset, "merged"),
        "report": _artifact(dsl.Markdown, "report"),
        "summary": _artifact(dsl.HTML, "summary"),
        "run_metrics": _artifact(dsl.Metrics, "run_metrics"),
    },
}


def _invocations(component: str) -> list[list[str]]:
    """Run one component with subprocess stubbed; return the bq-context argv it built.

    Filtered to our own CLI: resolving application-default credentials shells
    out to `gcloud config get project`, which the stub would otherwise capture.
    """
    captured: list[list[str]] = []

    class _Completed:
        returncode = 0

    def _record(args: Any, **_: Any) -> _Completed:
        captured.append(list(args))
        return _Completed()

    # Suppress everything: a component may legitimately sys.exit() on the stubbed
    # return code, or reach GCS after building its argv, and argv is all we test.
    with mock.patch.object(subprocess, "run", _record), contextlib.suppress(BaseException):
        getattr(components, component).python_func(**COMPONENT_ARGS[component])
    return [a for a in captured if a and a[0] == "bq-context"]


def _cli_options(subcommand: str) -> set[str]:
    command = typer.main.get_command(app).commands[subcommand]  # type: ignore[attr-defined]
    return {opt for param in command.params for opt in param.opts}


ALL_INVOCATIONS = [
    pytest.param(component, argv, id=f"{component}->{argv[1]}")
    for component in COMPONENT_ARGS
    for argv in _invocations(component)
]


def test_every_component_shells_out_at_least_once() -> None:
    """Guards the harness itself: a silent stub would make every test below vacuous."""
    assert ALL_INVOCATIONS
    invoked = {component for component, _ in (p.values for p in ALL_INVOCATIONS)}
    assert invoked == set(COMPONENT_ARGS), f"built no argv: {set(COMPONENT_ARGS) - invoked}"


@pytest.mark.parametrize(("component", "argv"), ALL_INVOCATIONS)
def test_component_invokes_a_registered_subcommand(component: str, argv: list[str]) -> None:
    registered = typer.main.get_command(app).commands  # type: ignore[attr-defined]
    assert argv[1] in registered, f"{component} calls unknown subcommand {argv[1]!r}"


@pytest.mark.parametrize(("component", "argv"), ALL_INVOCATIONS)
def test_every_flag_a_component_passes_exists_on_the_cli(component: str, argv: list[str]) -> None:
    """The test that fires when a CLI option is renamed.

    Both sides of the seam stay green without it, because nothing else compares
    the argv a component builds to the options the CLI declares.
    """
    known = _cli_options(argv[1])
    unknown = [a for a in argv if a.startswith("--") and a not in known]
    assert not unknown, f"{component} passes {unknown} which `{argv[1]}` does not accept"


def test_preflight_does_not_impersonate_itself() -> None:
    """Regression, behavioural rather than textual.

    A pipeline task already runs *as* the service account, so --impersonate
    <that same SA> asks it to impersonate itself and fails 403 on
    iam.serviceAccounts.getAccessToken. Asserted on the argv the component
    builds, so a docstring mentioning the flag cannot make this pass or fail.
    """
    (argv,) = _invocations("preflight")
    assert "--impersonate" not in argv
    assert "--impersonate" in _cli_options("preflight"), (
        "the flag must still exist for local use; this test is about the pipeline"
    )


def test_validate_config_asserts_identity_instead() -> None:
    """The useful in-pipeline check is the opposite of impersonation.

    It catches Vertex silently falling back to the Compute Engine default
    service account when service_account= is omitted.
    """
    (argv,) = _invocations("validate_config")
    assert "--expect-identity" in argv
    assert argv[argv.index("--expect-identity") + 1] == "sa@p.iam.gserviceaccount.com"


def test_run_shard_passes_the_code_version() -> None:
    """KFP caching keys on inputs, so an un-threaded code_version serves stale cells."""
    (argv,) = _invocations("run_shard")
    assert "--code-version" in argv
    assert argv[argv.index("--code-version") + 1] == "abc1234"


def test_ensure_infra_is_non_interactive() -> None:
    """A provisioning prompt in a pipeline task hangs until the 7-day task timeout."""
    (argv,) = _invocations("ensure_infra")
    assert "--yes" in argv


def test_finalize_merges_scores_and_plots_in_that_order() -> None:
    """Scoring reads merged results, and plotting reads scores."""
    assert [argv[1] for argv in _invocations("finalize")] == ["merge", "score", "plot", "report"]


# ---------------------------------------------------------------------------
# finalize must keep what it produces
# ---------------------------------------------------------------------------
def test_finalize_persists_the_report_and_figures() -> None:
    """Regression: every run rendered these and destroyed the container holding them.

    `score` writes markdown only when `--report PATH` is given, and `plot`
    defaults `--plots-dir` to the relative `Path("plots")` — which resolved to
    `/app/plots/` inside the task container. So the report existed on stdout
    only and the three figures were deleted with the pod, on every run.
    """
    argv = {a[1]: a for a in _invocations("finalize")}
    assert "--report" in argv["score"], "the markdown report was stdout-only"
    assert "--plots-dir" in argv["plot"], "figures went to a relative path inside the container"


def _all_invocations(component: str, **over: Any) -> list[list[str]]:
    """Every argv the component builds, including non-bq-context ones."""
    captured: list[list[str]] = []

    class _Completed:
        returncode = 0

    with (
        mock.patch.object(
            subprocess, "run", lambda a, **_k: (captured.append(list(a)), _Completed())[1]
        ),
        contextlib.suppress(BaseException),
    ):
        getattr(components, component).python_func(**{**COMPONENT_ARGS[component], **over})
    return captured


def test_figures_are_off_by_default() -> None:
    """Generation is slow, paid and non-deterministic, and architecture diagrams
    do not change between runs. Nothing should install or render unless asked."""
    argv = _all_invocations("finalize")
    assert not any("paperbanana" in " ".join(a) for a in argv)
    assert not any(a[:2] == ["bq-context", "figures"] for a in argv)


def test_requesting_figures_installs_the_extra_then_renders() -> None:
    """The extra is installed at runtime rather than shipped in the image.

    A second image was tried and deleted: a task's image is fixed at compile time
    so it cannot be handed over by an earlier step, and a cold install measures
    ~3s against minutes to build and push one.
    """
    argv = _all_invocations("finalize", refresh_figures=True)
    install = next(a for a in argv if a[:3] == ["uv", "pip", "install"])
    render = next(i for i, a in enumerate(argv) if a[:2] == ["bq-context", "figures"])
    assert components.FIGURES_EXTRA in install
    assert argv.index(install) < render, "the install must precede the render"
