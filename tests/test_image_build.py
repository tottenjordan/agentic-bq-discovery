"""The in-pipeline image build.

`ensure_image` is the first task: it guarantees `runner:{sha}` exists before any
other task tries to pull it. That removes the failure this project kept hitting —
submitting against a stale image because nobody remembered `make image` — and is
what makes an unattended, scheduled run possible at all.

Two things here are worth testing rather than trusting.

**The body is shell.** Every other component shells out to `bq-context`, which is
Python and covered. This one is a `bash -c` script on a stock Google image,
because making it a Python component would mean `pip install kfp` into the
builder before the pipeline can build anything. Shell that only ever runs in
Cloud Build is shell nobody reads until it fails at 3am, so it is exercised here
against a fake `gcloud`.

**The source must be `git archive HEAD`, not the working directory.** The image
is tagged with the HEAD SHA. Uploading the directory would let uncommitted edits
into an image whose tag says otherwise, and the KFP cache would then treat two
different images as the same one — the exact "cache lie" the SHA tag exists to
prevent.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from bq_context.pipeline import components

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

IMAGE = "us-central1-docker.pkg.dev/p/bq-context/runner:abc1234"

#: A stand-in `gcloud`. Records every invocation, and fails `describe` unless the
#: image has been "pushed" — which the fake build does by touching a file.
_FAKE_GCLOUD = """#!/usr/bin/env bash
echo "$@" >> "$GCLOUD_LOG"
case "$1 $2" in
  "artifacts docker")
    [ -f "$PUSHED" ] && exit 0
    exit 1
    ;;
  "builds submit")
    [ "$BUILD_FAILS" = "1" ] && exit 1
    touch "$PUSHED"
    exit 0
    ;;
esac
exit 0
"""


@pytest.fixture
def run_script(tmp_path: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run the real `ensure_image` script with `gcloud` stubbed out."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(_FAKE_GCLOUD)
    gcloud.chmod(0o755)
    log = tmp_path / "calls.log"
    pushed = tmp_path / "pushed"

    def _run(
        *, exists: bool = False, source: str = "gs://b/src.tar.gz", build_fails: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if exists:
            pushed.touch()
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GCLOUD_LOG": str(log),
            "PUSHED": str(pushed),
            "BUILD_FAILS": "1" if build_fails else "0",
        }
        proc = subprocess.run(  # noqa: S603
            ["bash", "-c", components._ENSURE_IMAGE, "ensure-image", IMAGE, source, "cfg", "us-c1"],  # noqa: S607
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )
        proc.calls = log.read_text() if log.exists() else ""  # type: ignore[attr-defined]
        return proc

    return _run


def test_an_existing_image_is_not_rebuilt(run_script) -> None:  # noqa: ANN001
    """The common path, and the reason this can run on every submit.

    Resubmitting at a commit whose image is already pushed must cost one registry
    lookup, not a build — otherwise every smoke test pays several minutes.
    """
    proc = run_script(exists=True)
    assert proc.returncode == 0, proc.stderr
    assert "builds submit" not in proc.calls  # type: ignore[attr-defined]
    assert "already present" in proc.stdout


def test_a_missing_image_is_built_from_the_uploaded_source(run_script) -> None:  # noqa: ANN001
    proc = run_script(exists=False)
    assert proc.returncode == 0, proc.stderr
    assert "builds submit gs://b/src.tar.gz" in proc.calls  # type: ignore[attr-defined]


def test_the_build_is_given_the_tag_not_the_whole_reference(run_script) -> None:  # noqa: ANN001
    """cloudbuild.yaml composes the full path from $PROJECT_ID itself. Passing a
    reference would nest one substitution inside another, which Cloud Build does
    not expand — it fails to parse rather than building the wrong thing."""
    proc = run_script(exists=False)
    assert "_TAG=abc1234" in proc.calls  # type: ignore[attr-defined]
    assert f"_TAG={IMAGE}" not in proc.calls  # type: ignore[attr-defined]


def test_a_failed_build_fails_the_task(run_script) -> None:  # noqa: ANN001
    """`set -e`, so this is really a test that the script does not swallow it.
    A build failure must stop the pipeline here, not surface as an
    ImagePullBackOff on the next task."""
    proc = run_script(exists=False, build_fails=True)
    assert proc.returncode != 0


def test_a_missing_image_with_no_source_says_what_to_do(run_script) -> None:  # noqa: ANN001
    """Submitting with --image against an image that does not exist. The message
    has to name both fixes, because the obvious reading — "the build failed" — is
    wrong; no build was ever attempted."""
    proc = run_script(exists=False, source="")
    assert proc.returncode != 0
    assert "no build source" in proc.stderr
    assert "make image" in proc.stderr


def test_the_push_is_verified_before_the_task_succeeds(run_script) -> None:  # noqa: ANN001
    """Cloud Build can report success before a subsequent describe sees the tag.
    The next task pulls this exact reference, and a miss there is an opaque
    ImagePullBackOff, so the script confirms rather than assuming."""
    proc = run_script(exists=False)
    # describe before the build, and again after it
    describes = [ln for ln in proc.calls.splitlines() if ln.startswith("artifacts docker")]  # type: ignore[attr-defined]
    assert len(describes) == 2, proc.calls  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# What gets uploaded
# ---------------------------------------------------------------------------
def test_the_source_is_the_commit_not_the_working_directory() -> None:
    """THE correctness property. `git archive HEAD` contains only tracked files
    at HEAD, so the tarball always describes the commit the tag names — and an
    untracked `.env` cannot be swept into an image."""
    import inspect

    from bq_context import cli

    body = inspect.getsource(cli._build_source)
    assert "git" in body
    assert "archive" in body
    assert "HEAD" in body


def test_a_dirty_tree_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule `make image` has always enforced: a SHA tag must describe its
    contents."""
    import typer

    from bq_context import cli

    class _Dirty:
        stdout = " M src/bq_context/cli.py\n"

    monkeypatch.setattr(subprocess, "run", lambda *_, **__: _Dirty())
    with pytest.raises(typer.Exit):
        cli._require_clean_tree()


def test_submit_pipeline_actually_calls_the_clean_tree_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the *wiring*, which the unit tests above do not.

    Deleting the `_require_clean_tree()` call from `submit-pipeline` leaves every
    test here green, because they exercise the function directly. Found by
    mutation rather than by reading the code.
    """
    from typer.testing import CliRunner

    from bq_context import cli

    called: list[bool] = []

    def _boom() -> None:
        called.append(True)
        raise typer.Exit(2)

    import typer

    monkeypatch.setattr(cli, "_require_clean_tree", _boom)
    result = CliRunner().invoke(cli.app, ["submit-pipeline", "-e", "t", "--dry-run"])
    assert called, "submit-pipeline never checked the working tree"
    assert result.exit_code == 2


def test_the_check_is_skipped_when_an_image_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """--image means "use this, do not build", so there is nothing to build from
    a clean tree and no reason to demand one."""
    from typer.testing import CliRunner

    from bq_context import cli

    called: list[bool] = []
    monkeypatch.setattr(cli, "_require_clean_tree", lambda: called.append(True))
    CliRunner().invoke(
        cli.app, ["submit-pipeline", "-e", "t", "--image", "img:pinned", "--dry-run"]
    )
    assert not called


def test_a_clean_tree_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Clean:
        stdout = "\n"

    monkeypatch.setattr(subprocess, "run", lambda *_, **__: _Clean())
    from bq_context import cli

    cli._require_clean_tree()
