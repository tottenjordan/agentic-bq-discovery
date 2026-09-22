"""Submission-time behaviour the compiled spec cannot show.

`tests/test_pipeline.py` asserts that `ensure_infra`, `preflight` and `finalize`
compile with caching disabled, and they do. But the Vertex SDK rewrites that
*after* compilation: `PipelineJob.__init__` calls `_set_enable_caching_value`,
which blunt-overwrites every task in every DAG —

    for task in component["dag"]["tasks"].values():
        task["cachingOptions"] = {"enableCache": enable_caching}

— whenever `enable_caching is not None`. So a job-level `True` silently discards
every deliberate `set_caching_options(False)`, and the compiled-spec test goes on
passing. The enrichment gate the whole experiment depends on was eligible for
cache reuse on every submit.

These tests sit at the SDK boundary because that is the only place the overwrite
is visible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from bq_context.pipeline import submit as submit_mod


class _FakeJob:
    resource_name = "projects/p/locations/us-central1/pipelineJobs/123"

    def submit(self, **_: Any) -> None: ...

    def wait(self) -> None: ...

    def _dashboard_uri(self) -> str:
        return "https://console.example/job"


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the kwargs handed to aiplatform.PipelineJob."""
    seen: dict[str, Any] = {}

    class _FakeAiplatform:
        @staticmethod
        def init(**_: Any) -> None: ...

        @staticmethod
        def PipelineJob(**kwargs: Any) -> _FakeJob:  # noqa: N802 - mirrors the SDK name
            seen.update(kwargs)
            return _FakeJob()

    real_import = __import__

    def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "google.cloud":
            module = mock.MagicMock()
            module.aiplatform = _FakeAiplatform
            return module
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    return seen


def _submit(**over: Any) -> None:
    kwargs: dict[str, Any] = {
        "template_path": Path("/tmp/spec.yaml"),  # noqa: S108
        "project": "p",
        "location": "us-central1",
        "pipeline_root": "gs://b/pipeline_root",
        "service_account": "sa@p.iam.gserviceaccount.com",
        "experiment_id": "e",
        "parameter_values": {},
    }
    submit_mod.submit_pipeline(**{**kwargs, **over})


def test_submitting_defers_caching_to_the_per_task_settings(captured: dict[str, Any]) -> None:
    """The regression.

    `None` is the only value that leaves `set_caching_options` intact; the SDK
    skips the rewrite entirely when caching is None.
    """
    _submit()
    assert captured["enable_caching"] is None, (
        "a job-level bool overwrites cachingOptions on every task, discarding "
        "the deliberate per-task settings in dag.py"
    )


def test_caching_can_still_be_turned_off_globally(captured: dict[str, Any]) -> None:
    """A global off is a legitimate escape hatch; a global on is not.

    False is safe because it only ever disables — it cannot resurrect caching on
    a task that asked for none.
    """
    _submit(enable_caching=False)
    assert captured["enable_caching"] is False


def test_the_failure_policy_lets_sibling_shards_finish(captured: dict[str, Any]) -> None:
    """Unrelated to caching, but it rides on the same call and is worth pinning:
    one dead shard must not cancel the other 23."""
    _submit()
    assert captured["failure_policy"] == "slow"
