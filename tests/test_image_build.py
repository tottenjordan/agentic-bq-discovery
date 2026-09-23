"""Building the runner image before the job is created.

`submit-pipeline` builds `runner:{sha}` when Artifact Registry does not already
have it, which removes the failure this project kept hitting: submitting against
a stale image because nobody ran `make image`.

**Why this is not a pipeline task, which is where it was first put.** Vertex
validates every statically-referenced image when the job is *created*:

    Failed to create pipeline job. Error: The image
    us-central1-docker.pkg.dev/hybrid-vertex/bq-context/runner:8ad9e76 does not exist.

The run was rejected outright, and the task that would have built the image never
started. A build step inside the pipeline could only ever confirm an image that
was already there.

A runtime `set_container_image` channel *is* exempt from that validation —
verified by submitting a job whose consumer image was a channel resolving to a
nonexistent tag, which was created successfully. So the five ordinary tasks could
have taken a built image. But `finalize` is the `ExitHandler` exit task, may not
depend on anything, and so needs a static reference that already resolves.
Something must exist before creation regardless; once that is true, doing the
whole job at submit time is simpler and needs no pipeline-side permissions.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any

import pytest
import typer

from bq_context import cli

if TYPE_CHECKING:
    from collections.abc import Callable

IMAGE = "us-central1-docker.pkg.dev/p/bq-context/runner:abc1234"


class _Result:
    def __init__(self, returncode: int = 0, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def gcloud(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Stub every subprocess call and record the argv of each."""
    calls: list[list[str]] = []

    def _install(
        *, exists: bool, exists_after_build: bool = True, build_ok: bool = True
    ) -> list[list[str]]:
        state = {"pushed": exists}

        def _run(argv: list[str], **_: Any) -> _Result:
            calls.append(argv)
            if argv[:2] == ["gcloud", "artifacts"]:
                return _Result(0 if state["pushed"] else 1)
            if argv[:2] == ["gcloud", "builds"]:
                if build_ok:
                    state["pushed"] = exists_after_build
                return _Result(0 if build_ok else 1)
            return _Result(0, "")

        monkeypatch.setattr(subprocess, "run", _run)
        return calls

    return _install


def _builds(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:2] == ["gcloud", "builds"]]


def test_an_existing_image_is_not_rebuilt(gcloud: Callable[..., list[list[str]]]) -> None:
    """The common path, and why this can run on every submit: resubmitting at an
    already-built commit costs one registry lookup, not a build."""
    calls = gcloud(exists=True)
    cli.ensure_image(IMAGE, "abc1234")
    assert not _builds(calls)


def test_a_missing_image_is_built(gcloud: Callable[..., list[list[str]]]) -> None:
    calls = gcloud(exists=False)
    cli.ensure_image(IMAGE, "abc1234")
    assert len(_builds(calls)) == 1
    assert "--substitutions=_TAG=abc1234" in _builds(calls)[0]


def test_the_build_is_given_the_tag_not_the_whole_reference(
    gcloud: Callable[..., list[list[str]]],
) -> None:
    """cloudbuild.yaml composes the full path from $PROJECT_ID itself, and Cloud
    Build does not expand substitutions recursively — a reference here fails to
    parse rather than building the wrong thing."""
    calls = gcloud(exists=False)
    cli.ensure_image(IMAGE, "abc1234")
    assert f"--substitutions=_TAG={IMAGE}" not in _builds(calls)[0]


def test_a_failed_build_stops_the_submission(gcloud: Callable[..., list[list[str]]]) -> None:
    """Submitting anyway would be rejected at creation with a bare "image does not
    exist", which says nothing about the build that actually failed."""
    gcloud(exists=False, build_ok=False)
    with pytest.raises(typer.Exit):
        cli.ensure_image(IMAGE, "abc1234")


def test_a_build_that_did_not_push_is_caught(gcloud: Callable[..., list[list[str]]]) -> None:
    """Cloud Build can report success before the push is visible. Vertex would
    then reject job creation, so the push is confirmed rather than assumed."""
    gcloud(exists=False, exists_after_build=False)
    with pytest.raises(typer.Exit):
        cli.ensure_image(IMAGE, "abc1234")


def test_an_existing_image_does_not_require_a_clean_tree(
    gcloud: Callable[..., list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is being built, so there is nothing for the tree to misdescribe.
    Demanding a commit here would block re-running against a known-good image."""
    gcloud(exists=True)
    monkeypatch.setattr(
        cli, "_require_clean_tree", lambda: pytest.fail("should not check the tree")
    )
    cli.ensure_image(IMAGE, "abc1234")


def test_building_does_require_a_clean_tree(
    gcloud: Callable[..., list[list[str]]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule `make image` has always enforced: a SHA tag must describe its
    contents, or the KFP cache treats two different images as the same one."""
    gcloud(exists=False)
    checked: list[bool] = []
    monkeypatch.setattr(cli, "_require_clean_tree", lambda: checked.append(True))
    cli.ensure_image(IMAGE, "abc1234")
    assert checked, "built without checking the working tree"


def test_a_dirty_tree_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: _Result(0, " M src/bq_context/cli.py"))
    with pytest.raises(typer.Exit):
        cli._require_clean_tree()


def test_a_clean_tree_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *_, **__: _Result(0, "\n"))
    cli._require_clean_tree()


def test_submit_pipeline_builds_before_it_compiles(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers the wiring, not just the function.

    Ordering is the whole point: Vertex rejects the job at creation when the image
    is absent, so this has to happen before the spec is compiled and submitted.
    """
    from typer.testing import CliRunner

    order: list[str] = []
    monkeypatch.setattr(cli, "ensure_image", lambda *_: order.append("build"))
    monkeypatch.setattr(
        "bq_context.pipeline.compilation.compile_pipeline",
        lambda dest: (order.append("compile"), dest)[1],
    )
    CliRunner().invoke(cli.app, ["submit-pipeline", "-e", "t", "--dry-run"])
    assert order[:2] == ["build", "compile"], order


def test_a_pinned_image_skips_the_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """--image means "use this one", so there is nothing to build."""
    from typer.testing import CliRunner

    called: list[bool] = []
    monkeypatch.setattr(cli, "ensure_image", lambda *_: called.append(True))
    CliRunner().invoke(
        cli.app, ["submit-pipeline", "-e", "t", "--image", "img:pinned", "--dry-run"]
    )
    assert not called
