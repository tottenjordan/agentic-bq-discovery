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
}


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
