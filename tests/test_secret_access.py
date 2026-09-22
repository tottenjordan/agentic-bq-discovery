"""`validate-config`'s Secret Manager check.

This check exists because the figures path fails *late*. `finalize` is the exit
task: it runs after all 24 shards, and `api_key` deliberately swallows a denied
or absent secret and returns None so a missing key cannot turn the run red. So
an ungranted service account produces a green pipeline with no diagrams, ninety
minutes in, and nothing in the logs that names IAM. Thirty seconds up front is
the whole point.

**The trap this file is really guarding.** `_check_permissions` asks
`resourcemanager` about `projects/{id}`, which only ever sees policy attached at
the *project*. `roles/secretmanager.secretAccessor` is routinely bound on the
individual secret instead — it is on this project's `bq-context-secret` right
now — and a resource-level binding is invisible to a project-level
`testIamPermissions`. Adding `secretmanager.versions.access` to
`REQUIRED_PERMISSIONS` would therefore report a missing grant that is in fact
present, and the fix for that false alarm is to over-grant at the project level.
The permission has to be tested against the secret resource itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from google.api_core import exceptions
from typer.testing import CliRunner

from bq_context import cli

if TYPE_CHECKING:
    from pathlib import Path

cli_runner = CliRunner()

#: The `client` fixture: installs a fake Secret Manager client and returns it.
Install = Callable[..., "_FakeClient"]

SECRET_PERMISSION = "secretmanager.versions.access"  # noqa: S105 - an IAM permission

_ABSENT = "no such secret"
_NO_GET = "no secrets.get"


class _FakeResponse:
    def __init__(self, permissions: list[str]) -> None:
        self.permissions = permissions


class _FakeClient:
    """Models the real API's contract, which is not the obvious one.

    Verified against live Secret Manager on 2026-09-22: for a secret that does
    not exist, `test_iam_permissions` returns an **empty permission list** rather
    than raising NotFound — identical to the response for a secret you exist but
    may not read. Only `get_secret` distinguishes them. An earlier version of
    this fake raised NotFound from `test_iam_permissions`, so the unit tests
    passed while the real command told anyone with a typo'd secret name to grant
    a role on a secret that was not there.
    """

    def __init__(
        self,
        granted: list[str] | None = None,
        raises: Exception | None = None,
        *,
        exists: bool = True,
    ) -> None:
        self._granted = granted or []
        self._raises = raises
        self._exists = exists
        self.calls: list[tuple[str, dict]] = []

    def test_iam_permissions(self, request: dict) -> _FakeResponse:
        self.calls.append(("test_iam_permissions", request))
        if self._raises is not None:
            raise self._raises
        return _FakeResponse(self._granted)

    def get_secret(self, request: dict) -> object:
        self.calls.append(("get_secret", request))
        if not self._exists:
            raise exceptions.NotFound(_ABSENT)
        return object()

    def access_secret_version(self, request: dict) -> None:
        self.calls.append(("access_secret_version", request))
        message = "the check must never read the secret value"
        raise AssertionError(message)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Install:
    """Install a fake SecretManagerServiceClient and hand the instance back."""

    def _install(**kwargs: object) -> _FakeClient:
        fake = _FakeClient(**kwargs)  # ty: ignore[invalid-argument-type]
        monkeypatch.setattr(
            "google.cloud.secretmanager.SecretManagerServiceClient",
            lambda **_: fake,
        )
        return fake

    return _install


def test_the_project_permission_table_does_not_claim_the_secret_permission() -> None:
    """THE test, and the reason this check is not three lines in REQUIRED_PERMISSIONS.

    A resource-level grant is invisible to the project-level testIamPermissions
    call, so listing it there reports a missing grant that is actually present.
    """
    every = {p for group in cli.REQUIRED_PERMISSIONS.values() for p in group}
    assert SECRET_PERMISSION not in every, (
        "secretmanager.versions.access is checked against the secret resource, "
        "not the project; a resource-level binding would read as missing here"
    )


def test_it_asks_about_the_secret_resource_not_the_project(client: Install) -> None:
    fake = client(granted=[SECRET_PERMISSION])
    cli._check_secret_access("proj", "my-secret", None)
    _, request = fake.calls[0]
    assert request["resource"] == "projects/proj/secrets/my-secret"
    assert request["permissions"] == [SECRET_PERMISSION]


def test_a_granted_permission_is_not_a_problem(client: Install) -> None:
    client(granted=[SECRET_PERMISSION])
    assert cli._check_secret_access("proj", "my-secret", None) is None


def test_a_missing_permission_names_the_grant_command(client: Install) -> None:
    """A diagnosis without the remedy just moves the search; the message must be
    something you can paste."""
    client(granted=[])
    problem = cli._check_secret_access("proj", "my-secret", None)
    assert problem is not None
    assert "gcloud secrets add-iam-policy-binding my-secret" in problem
    assert "roles/secretmanager.secretAccessor" in problem


def test_an_absent_secret_is_distinguished_from_a_denied_one(client: Install) -> None:
    """Different remedies: create the secret, versus bind a role on one that exists.

    Both arrive here as an empty permission list, so the only thing separating
    them is the `get_secret` probe.
    """
    client(granted=[], exists=False)
    problem = cli._check_secret_access("proj", "my-secret", None)
    assert problem is not None
    assert "does not exist" in problem
    assert "add-iam-policy-binding" not in problem


def test_an_existing_but_ungranted_secret_advises_the_binding(client: Install) -> None:
    """The mirror of the test above: same empty list, opposite remedy."""
    client(granted=[], exists=True)
    problem = cli._check_secret_access("proj", "my-secret", None)
    assert problem is not None
    assert "add-iam-policy-binding" in problem
    assert "does not exist" not in problem


def test_existence_is_not_guessed_when_the_probe_itself_is_denied(client: Install) -> None:
    """`get_secret` needs secretmanager.secrets.get, which a least-privilege
    principal may lack. A PermissionDenied there says nothing about existence, so
    the check must not tell someone to create a secret they already have."""
    fake = client(granted=[], exists=True)
    fake.get_secret = _denied  # ty: ignore[invalid-assignment]
    problem = cli._check_secret_access("proj", "my-secret", None)
    assert problem is not None
    assert "add-iam-policy-binding" in problem


def _denied(_: dict) -> object:
    raise exceptions.PermissionDenied(_NO_GET)


def test_an_unexpected_error_is_reported_rather_than_swallowed(client: Install) -> None:
    """`api_key` swallows everything by design. This check must not: a check that
    silently passes when it could not run is worse than no check."""
    client(raises=exceptions.ServiceUnavailable("down"))
    problem = cli._check_secret_access("proj", "my-secret", None)
    assert problem is not None
    assert "ServiceUnavailable" in problem


def test_the_check_never_reads_the_secret_value(client: Install) -> None:
    """Guard against a future 'improvement' that fetches the key to prove access.

    That would pull a live API key into the process and into anything capturing
    output, to learn what testIamPermissions already answers.
    """
    fake = client(granted=[SECRET_PERMISSION])
    cli._check_secret_access("proj", "my-secret", None)
    assert [name for name, _ in fake.calls] == ["test_iam_permissions"]


@pytest.fixture
def runnable(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Stub every network call in `validate-config` so the body actually runs.

    The unit tests above exercise `_check_secret_access` directly and never
    execute the command, which is precisely how the first version of this branch
    shipped an `os.getenv` call with no `os` in scope: green tests, `NameError`
    the moment anyone ran it. Something has to run the real body.
    """
    monkeypatch.setattr(cli, "_credentials", lambda _: None)
    monkeypatch.setattr(cli, "_effective_identity", lambda _: "sa@test-project.iam")
    monkeypatch.setattr(cli, "_check_permissions", lambda *_: [])

    class _FakeBigQuery:
        def __init__(self, **_: object) -> None: ...
        def query(self, *_: object, **__: object) -> None: ...

    monkeypatch.setattr("google.cloud.bigquery.Client", _FakeBigQuery)
    return monkeypatch


def test_an_unset_secret_id_reports_but_does_not_fail(
    runnable: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Figures are opt-in and off by default, so an unconfigured secret is a
    normal state — not a reason to fail every run that never wanted one."""
    runnable.delenv("SECRET_ID", raising=False)
    result = cli_runner.invoke(cli.app, ["validate-config", "--out", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "not configured" in result.output


def test_require_secret_turns_an_unset_secret_id_into_a_failure(
    runnable: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runnable.delenv("SECRET_ID", raising=False)
    result = cli_runner.invoke(
        cli.app, ["validate-config", "--require-secret", "--out", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "SECRET_ID is unset" in result.output


def test_a_configured_secret_is_checked_and_can_fail_the_command(
    runnable: pytest.MonkeyPatch, tmp_path: Path, client: Install
) -> None:
    """The whole point: a missing grant stops the run now, not in the exit task."""
    runnable.setenv("SECRET_ID", "my-secret")
    client(granted=[])
    result = cli_runner.invoke(cli.app, ["validate-config", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert SECRET_PERMISSION in result.output


def test_validate_config_exposes_the_require_secret_flag() -> None:
    """Without this, an unset SECRET_ID can only ever warn — and the figures path
    is exactly the case where absence is fatal rather than merely unconfigured."""
    import typer.main

    # Introspection, not `--help` text: rendered help carries ANSI codes and
    # wraps at the terminal width, so grepping it asserts on formatting.
    command = dict(typer.main.get_command(cli.app).commands)["validate-config"]  # type: ignore[attr-defined]
    opts = {opt for p in command.params for opt in getattr(p, "opts", [])}
    assert "--require-secret" in opts
