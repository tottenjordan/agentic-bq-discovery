"""Make the suite hermetic.

Tests must not depend on the developer's ambient shell. Two CLI tests passed
locally only because `GOOGLE_CLOUD_PROJECT` happened to be exported: without it,
`run-shard` fails on config resolution *before* it reaches the argument
validation those tests were asserting on, so they failed with the right exit
code for the wrong reason.

That is also the difference between a suite that runs in CI and one that does
not, which is how it was found — running pytest with every GCP variable
stripped.
"""

from __future__ import annotations

import os

import pytest

#: Deterministic stand-ins. Nothing here is reachable; any test that actually
#: touches GCP is doing something it should not.
_FAKE_ENV = {
    "GOOGLE_CLOUD_PROJECT": "test-project",
    "BQ_LOCATION": "US",
    "DATAPLEX_LOCATION": "us-central1",
    "AGENT_MODEL": "gemini-3.6-flash",
    "TOOL_MODEL": "gemini-3.5-flash-lite",
    "RESOURCE_PREFIX": "bigquery_context",
    "TOP_K": "5",
    "SECRET_ID": "test-secret-name",
    "BQ_CONTEXT_IMAGE": "us-central1-docker.pkg.dev/p/r/runner:testsha",
}

# Applied here, at *import* time, as well as by the fixture below — because a
# fixture is too late for anything resolved when a module is first imported.
# `components.py` computes RUNNER_IMAGE, SECRET_ID and CONFIG_ENV at import, and
# `@dsl.pipeline` runs the pipeline body at decoration time; both happen while
# pytest is importing test modules, before any fixture has run.
#
# The symptom of getting this wrong is not a failure, it is a *vacuous pass*.
# `test_finalize_carries_the_secret_name_in_its_environment` compares the
# compiled spec against `components.SECRET_ID`; with the variable unset both
# sides are `""` and the test asserts nothing. It did exactly that in CI, because
# `test_pipeline_cli_seam.py` imports components before `test_pipeline.py` sets
# anything, while passing locally on a shell that happened to export the values.
#
# Assignment, not `setdefault`: an exported RESOURCE_PREFIX in a developer's
# shell must not reach the suite. That leak has now hidden two real failures in
# this project, so the hermetic value wins outright.
os.environ.update(_FAKE_ENV)


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop the CLI reading the developer's real `.env`.

    The CLI loads `.env` on startup so `SECRET_ID` and friends work locally. That
    load uses `override=False`, which protects any variable the fixture below
    *sets* — but not one a test deliberately *deletes*. `test_missing_project_...`
    unsets GOOGLE_CLOUD_PROJECT to assert the error message, and a real `.env`
    filled it straight back in, so the test failed on a machine with a `.env` and
    passed in CI.

    Neutered here rather than given a skip-flag in `cli.py`: hermeticity is the
    harness's job, and production code should not know it is under test.
    """
    monkeypatch.setattr("bq_context.cli._load_dotenv", lambda: None)


@pytest.fixture(autouse=True)
def hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the environment for every test.

    Autouse so a new test cannot accidentally inherit a real project id. Tests
    that need a variable *absent* can still `monkeypatch.delenv` it, which is
    what the "missing project" test does.
    """
    for key, value in _FAKE_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def no_identity_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop `run-shard` asking Google who it is.

    It resolves its principal from ADC and then tokeninfo: a network call on a
    developer box, and a different answer in CI. Tests that care about the
    principal set their own.
    """
    monkeypatch.setattr("bq_context.cli._measuring_principal", lambda: "")
