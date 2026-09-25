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

import os

# Must precede the pipeline imports: components.py resolves base_image from the
# environment at import time, deliberately raising KeyError when it is unset.
os.environ.setdefault("BQ_CONTEXT_IMAGE", "us-central1-docker.pkg.dev/p/r/runner:testsha")

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


# ---------------------------------------------------------------------------
# What the submit CLI actually sends
#
# A pipeline parameter the DAG declares but the CLI never sends is not an error
# anywhere: KFP resolves the compiled default and the run proceeds, green and
# wrong. `refresh_figures` sat like that — declared on `bq_context_pipeline`,
# threaded into `finalize`, and unreachable, so every submit ran with False and
# `bq-context figures` was never invoked. Nothing failed; the feature was simply
# not connected.
#
# The reverse direction fails loudly at submit time (Vertex rejects an unknown
# parameter name), so it needs a test far less than this direction does.
# ---------------------------------------------------------------------------

#: DAG parameters the CLI deliberately leaves at their compiled default. Keep
#: this list short and justified: every entry is a knob no one can turn without
#: editing `dag.py`.
DELIBERATE_DEFAULTS = {
    # All six approaches, always. Running a subset is a local `run-shard`
    # concern; a partial sweep submitted to Vertex would produce a results file
    # that `merge --require-complete` then rejects.
    "approaches",
}


def _pipeline_parameters() -> set[str]:
    """Every parameter the compiled pipeline accepts.

    `component_spec.inputs`, not `inspect.signature`. `@dsl.pipeline` returns a
    GraphComponent, and introspecting it yields `{'args', 'kwargs'}` — the
    wrapper's signature, not the pipeline's. Checked rather than assumed: with
    `inspect.signature` the guard does not pass vacuously, it fails permanently,
    reporting `['args', 'kwargs']` as unwired parameters and saying nothing about
    the real one. A red test that names the wrong thing gets an entry added to
    DELIBERATE_DEFAULTS to silence it, and then the guard really is vacuous.

    `component_spec.inputs` is also what Vertex validates a submission against,
    so it is the right authority regardless.
    """
    from bq_context.pipeline.dag import bq_context_pipeline

    return set(bq_context_pipeline.component_spec.inputs or {})


def _capture_parameters(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], store: object | None = None
) -> dict[str, Any]:
    """Invoke `submit-pipeline` with the submit call stubbed, and return the
    `parameter_values` it would have sent to Vertex.

    The store is stubbed too. `submit-pipeline` snapshots the question set into
    the bucket before compiling, and conftest pins a fake project — so a real
    `store_for` here reaches GCS and 404s, which is exactly the hermeticity trap
    conftest exists to prevent.
    """
    import tempfile

    from typer.testing import CliRunner

    from bq_context import cli
    from bq_context.pipeline import submit as submit_module
    from bq_context.runner.store import LocalStore

    seen: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> _FakeJob:
        seen.update(kwargs)
        return _FakeJob()

    monkeypatch.setattr(submit_module, "submit_pipeline", _capture)
    monkeypatch.setattr(cli, "store_for", lambda *_a, **_k: store or LocalStore(tempfile.mkdtemp()))
    result = CliRunner().invoke(cli.app, argv)
    assert result.exit_code == 0, result.output
    return seen["parameter_values"]


def _params_the_cli_sends(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """The parameter *names* `submit-pipeline` sends."""
    argv = ["submit-pipeline", "-e", "t", "--image", "img:test", "--profile", "smoke"]
    return set(_capture_parameters(monkeypatch, argv))


def test_every_pipeline_parameter_is_sent_or_deliberately_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE guard. Adding a parameter to `bq_context_pipeline` without wiring it
    through the CLI must fail here rather than silently run on its default."""
    declared = _pipeline_parameters()
    sent = _params_the_cli_sends(monkeypatch)

    unwired = declared - sent - DELIBERATE_DEFAULTS
    assert not unwired, (
        f"{sorted(unwired)} declared on the pipeline but never sent by the CLI; "
        "they would silently run on the compiled default. Wire them, or add them "
        "to DELIBERATE_DEFAULTS with a reason."
    )


def test_the_cli_sends_no_parameter_the_pipeline_does_not_declare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vertex rejects an unknown parameter name at submit time, which costs a
    round trip to discover. A typo should fail in the suite instead."""
    assert _params_the_cli_sends(monkeypatch) <= _pipeline_parameters()


def test_the_submission_carries_a_usable_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sending the empty default would put every execution back in one folder —
    the overwriting this parameter exists to stop. It must also survive
    `run_prefix`, which is what the exit task calls with it."""
    from bq_context.runner.resume import run_prefix

    sent = _capture_parameters(monkeypatch, ["submit-pipeline", "-e", "t", "--image", "img:test"])
    run_id = sent["run_id"]
    assert run_id, "submitted the compiled default; every run would share one folder"
    assert run_prefix("t", run_id).endswith(f"/runs/{run_id}")


def test_refresh_figures_is_reachable_from_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The specific regression: the flag exists and its value reaches the job."""
    argv = ["submit-pipeline", "-e", "t", "--image", "img:test", "--refresh-figures"]
    assert _capture_parameters(monkeypatch, argv)["refresh_figures"] is True


def test_figures_are_off_unless_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generation is slow, paid and non-deterministic; the default must be off."""
    argv = ["submit-pipeline", "-e", "t", "--image", "img:test"]
    assert _capture_parameters(monkeypatch, argv)["refresh_figures"] is False


# ---------------------------------------------------------------------------
# The question set travels as a snapshot, not a reference
#
# Shards read a copy in the experiment prefix rather than the submitter's
# filesystem. That is what makes the set immutable for the life of the run --
# it cannot be edited out from under 24 shards -- and what archives it beside
# the results it produced.
# ---------------------------------------------------------------------------
def test_the_submission_carries_a_question_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sent even for the default set. It costs nothing and gives a second,
    independent guard beside `code_version`."""
    argv = ["submit-pipeline", "-e", "t", "--image", "img:test"]
    assert _capture_parameters(monkeypatch, argv)["questions_fingerprint"]


def test_the_snapshot_lands_in_the_experiment_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    from bq_context.runner.store import LocalStore

    store = LocalStore(tmp_path)
    _capture_parameters(
        monkeypatch, ["submit-pipeline", "-e", "byoq", "--image", "img:test"], store=store
    )

    snapshot = json.loads(store.read_text("experiments/byoq/questions.json"))
    assert len(snapshot["questions"]) == 25, "the shipped set should round-trip whole"
    assert all("relevance" in q for q in snapshot["questions"])


def test_a_custom_question_file_changes_the_fingerprint_and_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole feature in one test: point --questions somewhere else and both
    the submitted fingerprint and the archived copy follow."""
    import json

    from bq_context.runner.store import LocalStore

    custom = tmp_path / "mine.json"
    custom.write_text(
        json.dumps(
            {
                "questions": [
                    {
                        "id": "mine-q1",
                        "category": "single-table",
                        "question": "t",
                        "relevance": {"must_have": ["austin_bikeshare_trips"]},
                    }
                ]
            }
        )
    )
    store = LocalStore(tmp_path / "store")
    default_fp = _capture_parameters(
        monkeypatch, ["submit-pipeline", "-e", "t", "--image", "img:test"]
    )["questions_fingerprint"]
    custom_fp = _capture_parameters(
        monkeypatch,
        ["submit-pipeline", "-e", "byoq", "--image", "img:test", "--questions", str(custom)],
        store=store,
    )["questions_fingerprint"]

    assert custom_fp != default_fp
    snapshot = json.loads(store.read_text("experiments/byoq/questions.json"))
    assert [q["id"] for q in snapshot["questions"]] == ["mine-q1"]


def test_the_snapshot_preserves_question_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--limit` takes a deterministic prefix, so a snapshot that reordered the
    set would make a smoke run measure different questions than the submitter
    chose — and the fingerprint would no longer describe what ran."""
    import json

    from bq_context.runner.store import LocalStore

    store = LocalStore(tmp_path)
    _capture_parameters(
        monkeypatch, ["submit-pipeline", "-e", "byoq", "--image", "img:test"], store=store
    )

    raw = json.loads(Path("experiments/questions.json").read_text())
    original = raw["questions"] if isinstance(raw, dict) else raw
    snapshot = json.loads(store.read_text("experiments/byoq/questions.json"))["questions"]
    assert [q["id"] for q in snapshot] == [q["id"] for q in original]
