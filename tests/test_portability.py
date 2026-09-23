"""Nothing in the shipped code may name one particular GCP project.

This repo was built against `hybrid-vertex`, and the project id leaked into four
defaults: the results bucket and the pipeline service account in `cli.py`, and
`project` / `out` on the pipeline itself. None of them is reachable for anyone
else, and every one is a *default* — so a second user does not get an error, they
get a run pointed at a bucket they cannot write and a service account that does
not exist, with a permissions failure that names neither.

Defaults are derived from `GOOGLE_CLOUD_PROJECT` instead, which reproduces the
existing values exactly for this project (`hybrid-vertex` ->
`gs://hybrid-vertex-bq-context`) while working anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bq_context import cli

runner = CliRunner()
SRC = Path("src/bq_context")

#: The project this was developed against. Nothing under `src/` may name it.
DEVELOPMENT_PROJECT = "hybrid-vertex"


def test_no_shipped_module_names_the_development_project() -> None:
    """THE guard, and the cheapest possible one: grep the package for the id."""
    offenders = [
        f"{path}:{n}"
        for path in sorted(SRC.rglob("*.py"))
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if DEVELOPMENT_PROJECT in line
    ]
    assert not offenders, (
        f"{DEVELOPMENT_PROJECT} is hard-coded in {offenders}. Derive it from "
        "GOOGLE_CLOUD_PROJECT; a default nobody else can reach fails as a "
        "permission error that names neither the bucket nor the account."
    )


def test_the_notebook_does_not_default_to_one_project() -> None:
    """Code cells only. The recorded *outputs* legitimately show a real run
    against `hybrid-vertex`, and scrubbing them would misrepresent the evidence —
    but a `setdefault` in an executable cell silently points a second reader at a
    project they cannot read.
    """
    import json

    nb = json.loads(Path("notebooks/walkthrough.ipynb").read_text())
    offenders = [
        i
        for i, cell in enumerate(nb["cells"])
        if cell["cell_type"] == "code" and DEVELOPMENT_PROJECT in "".join(cell["source"])
    ]
    assert not offenders, f"notebook code cells {offenders} hard-code the project"


def test_neither_does_the_image_or_the_build() -> None:
    """The image would otherwise make one project the default for everyone."""
    for name in ("Dockerfile", "Makefile"):
        text = Path(name).read_text()
        assert DEVELOPMENT_PROJECT not in text, (
            f"{name} hard-codes {DEVELOPMENT_PROJECT}; cloudbuild.yaml already "
            "uses $PROJECT_ID and is the pattern to follow"
        )


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------
def test_the_bucket_is_derived_from_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "someone-else")
    monkeypatch.delenv("BQ_CONTEXT_OUT", raising=False)
    assert cli.default_out() == "gs://someone-else-bq-context"


def test_the_service_account_is_derived_from_the_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "someone-else")
    monkeypatch.delenv("BQ_CONTEXT_SERVICE_ACCOUNT", raising=False)
    assert cli.default_service_account() == (
        "bq-context-pipeline@someone-else.iam.gserviceaccount.com"
    )


def test_the_derived_values_reproduce_todays_behaviour(monkeypatch: pytest.MonkeyPatch) -> None:
    """The change must be a no-op for this project, or it is a migration."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", DEVELOPMENT_PROJECT)
    monkeypatch.delenv("BQ_CONTEXT_OUT", raising=False)
    monkeypatch.delenv("BQ_CONTEXT_SERVICE_ACCOUNT", raising=False)
    assert cli.default_out() == "gs://hybrid-vertex-bq-context"
    assert cli.default_service_account() == (
        "bq-context-pipeline@hybrid-vertex.iam.gserviceaccount.com"
    )


@pytest.mark.parametrize(
    ("var", "resolver", "override"),
    [
        ("BQ_CONTEXT_OUT", "default_out", "gs://somewhere/else"),
        (
            "BQ_CONTEXT_SERVICE_ACCOUNT",
            "default_service_account",
            "other@p.iam.gserviceaccount.com",
        ),
    ],
)
def test_an_explicit_override_wins_over_the_convention(
    monkeypatch: pytest.MonkeyPatch, var: str, resolver: str, override: str
) -> None:
    """The derived name is a convention, not a requirement. A bucket that does not
    follow it must still be usable without passing a flag to every command."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    monkeypatch.setenv(var, override)
    assert getattr(cli, resolver)() == override


def test_an_unresolvable_default_says_so_rather_than_guessing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Better an error naming the two ways to fix it than a silent `gs://-bq-context`."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.delenv("BQ_CONTEXT_OUT", raising=False)
    with pytest.raises(Exception, match=r"GOOGLE_CLOUD_PROJECT|--out"):
        cli.default_out()


class _AbortError(Exception):
    """Stop a command once it has told us which path it resolved."""


def _resolved_out(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> str:
    """Run a command with --out omitted and report the path it actually used."""
    seen: list[str] = []

    def _capture(out: str, *_: object, **__: object) -> None:
        seen.append(out)
        raise _AbortError

    monkeypatch.setattr(cli, "store_for", _capture)
    runner.invoke(cli.app, argv)
    assert seen, "the command never resolved a store path"
    return seen[0]


def test_omitting_the_flag_reaches_the_command_as_a_resolved_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the *wiring*, not just the resolver.

    Every --out default is `""`, so dropping `callback=_resolve_out` from OutOpt
    would hand all eight commands an empty path — and the resolver's own unit
    tests would still pass, because they call it directly.
    """
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "acme-analytics")
    monkeypatch.delenv("BQ_CONTEXT_OUT", raising=False)
    assert _resolved_out(monkeypatch, ["merge", "-e", "t"]) == "gs://acme-analytics-bq-context"


def test_an_explicit_out_is_passed_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "acme-analytics")
    got = _resolved_out(monkeypatch, ["merge", "-e", "t", "--out", "gs://typed/by/hand"])
    assert got == "gs://typed/by/hand"


def test_an_explicit_flag_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolution happens in a parameter callback, which must not clobber a value
    the user actually typed."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p")
    result = runner.invoke(cli.app, ["plan-shards", "-e", "t", "--limit", "1", "--tier", "0"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# The pipeline's own parameters
# ---------------------------------------------------------------------------
def test_the_pipeline_requires_a_project_and_a_bucket() -> None:
    """No defaults at all, rather than portable ones.

    `submit-pipeline` always sends both — a test in `test_submit.py` enforces
    that every declared parameter is sent — so a default here can only ever be
    wrong. Leaving one would mean a hand-built `PipelineJob` silently targeting
    whatever project the author happened to use.
    """
    source = (SRC / "pipeline" / "dag.py").read_text()
    signature = source.split("def bq_context_pipeline(")[1].split(") -> None:")[0]
    for required in ("project", "out"):
        declared = re.search(rf"\n\s+{required}: str(\s*=)?", signature)
        assert declared, f"{required} missing from the pipeline signature"
        assert not declared.group(1), f"{required} must not have a default"


# ---------------------------------------------------------------------------
# The runner image reference
# ---------------------------------------------------------------------------
def test_the_image_reference_is_derived_from_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ensure_image` builds exactly this tag when it is absent, so demanding the
    caller supply one would put `make image-ref` back in front of every submit —
    the friction the in-pipeline build exists to remove."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "acme-analytics")
    monkeypatch.delenv("BQ_CONTEXT_IMAGE", raising=False)
    monkeypatch.delenv("BQ_CONTEXT_BUILD_REGION", raising=False)
    assert cli.default_image("abc1234") == (
        "us-central1-docker.pkg.dev/acme-analytics/bq-context/runner:abc1234"
    )


def test_it_matches_what_make_image_ref_prints() -> None:
    """Two places compose this path; a divergence means the pipeline builds one
    tag and a human pushes another."""
    makefile = Path("Makefile").read_text()
    assert "$(REGION)-docker.pkg.dev/$(GOOGLE_CLOUD_PROJECT)/bq-context/runner" in makefile
    assert cli._IMAGE_TEMPLATE.startswith("{region}-docker.pkg.dev/{project}/bq-context/runner:")


def test_an_explicit_image_still_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "acme")
    monkeypatch.setenv("BQ_CONTEXT_IMAGE", "elsewhere/runner:pinned")
    assert cli.default_image("abc1234") == "elsewhere/runner:pinned"


def test_submitting_no_longer_demands_an_image(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression this closes: a fresh checkout could not submit at all
    without first running `make image-ref`."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "acme")
    monkeypatch.delenv("BQ_CONTEXT_IMAGE", raising=False)
    monkeypatch.setattr(cli, "ensure_image", lambda *_: None)
    result = runner.invoke(cli.app, ["submit-pipeline", "-e", "t", "--dry-run"])
    assert result.exit_code == 0, result.output
    # The exact line, not a substring of the output: `build_config` carries the
    # whole of cloudbuild.yaml, which mentions this path too, so a loose `in`
    # check passes even when nothing was derived. Found by mutation.
    expected = cli.default_image(cli._code_version())
    assert any(
        ln.startswith("image") and ln.split()[-1] == expected for ln in result.output.splitlines()
    ), result.output
