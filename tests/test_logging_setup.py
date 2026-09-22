"""Structured logging: the fields must survive, and the default must not change.

The motivation is concrete. Recovering `cache_warm_s` for the first full run
meant `gcloud logging read '"cache warm"'` piped through a regex, because the
value existed only inside a formatted sentence. A field survives that; a
sentence does not.
"""

from __future__ import annotations

import json
import logging

import pytest

from bq_context.logging_setup import (
    FORMAT_ENV,
    HUMAN_FORMAT,
    JsonFormatter,
    configure_logging,
    json_log,
)


def _record(level: int = logging.INFO, msg: str = "hello", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("bq_context.test", level, __file__, 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _formatted(**kwargs: object) -> dict:
    return json.loads(JsonFormatter().format(_record(**kwargs)))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The JSON shape Cloud Logging expects
# ---------------------------------------------------------------------------
def test_severity_and_message_are_where_cloud_logging_looks() -> None:
    """These two keys are promoted out of jsonPayload into the LogEntry itself.

    Spelling either differently leaves the entry at default severity with the
    text buried in the payload.
    """
    entry = _formatted(level=logging.WARNING, msg="cache warm")
    assert entry["severity"] == "WARNING"
    assert entry["message"] == "cache warm"


def test_it_is_one_json_object_per_line() -> None:
    """Cloud Logging parses per line; a multi-line record becomes several
    unparseable entries."""
    out = JsonFormatter().format(_record(msg="a\nb"))
    assert "\n" not in out
    assert json.loads(out)["message"] == "a\nb"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("shard_id", "tier3__bq_tools"),
        ("experiment_id", "full-01"),
        ("cache_warm_s", 1.9),
        ("abort_reason", "20 consecutive failures"),
        ("corpus_fingerprint", "861648cc513c3862"),
    ],
)
def test_the_fields_worth_querying_survive(field: str, value: object) -> None:
    """Each of these was painful to recover from formatted text at least once."""
    assert _formatted(**{field: value})[field] == value


def test_logrecord_furniture_is_not_dumped_into_the_payload() -> None:
    """A LogRecord carries ~20 attributes. Emitting all of them makes every entry
    noisy and the useful fields hard to find."""
    entry = _formatted()
    assert set(entry) == {"severity", "message", "logger"}


def test_an_unknown_extra_is_dropped_rather_than_guessed_at() -> None:
    """The allowlist is deliberate: a payload that grows by accident stops being
    a schema anyone can query against."""
    assert "some_new_thing" not in _formatted(some_new_thing="x")


def test_an_exception_is_serialised_rather_than_dropped() -> None:
    """A traceback that exists only in the default rendering is invisible to a
    structured query."""
    try:
        raise ValueError("boom")  # noqa: EM101, TRY301
    except ValueError:
        import sys

        record = _record(level=logging.ERROR, msg="failed")
        record.exc_info = sys.exc_info()
    entry = json.loads(JsonFormatter().format(record))
    assert "ValueError: boom" in entry["exception"]


def test_a_non_serialisable_value_does_not_lose_the_record() -> None:
    """`default=str` rather than raising: one odd value must not drop the line."""
    assert _formatted(shard_id=object())["severity"] == "INFO"


# ---------------------------------------------------------------------------
# Choosing the formatter
# ---------------------------------------------------------------------------
def test_the_human_format_is_still_the_local_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON in a terminal is worse for the person reading it."""
    monkeypatch.delenv(FORMAT_ENV, raising=False)
    configure_logging()
    (handler,) = logging.getLogger().handlers
    assert handler.formatter is not None
    assert handler.formatter._fmt == HUMAN_FORMAT


def test_json_goes_to_stdout_not_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vertex tends to tag container stderr as ERROR regardless of content, which
    would override the severity field on every line."""
    import sys

    monkeypatch.setenv(FORMAT_ENV, "json")
    configure_logging()
    (handler,) = logging.getLogger().handlers
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stdout
    assert isinstance(handler.formatter, JsonFormatter)


def test_configuring_twice_replaces_rather_than_stacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """`basicConfig` is a no-op when handlers exist, so force=True is required —
    without it the second call silently does nothing."""
    monkeypatch.setenv(FORMAT_ENV, "json")
    configure_logging()
    monkeypatch.delenv(FORMAT_ENV)
    configure_logging()
    assert len(logging.getLogger().handlers) == 1


# ---------------------------------------------------------------------------
# The component-body helper
# ---------------------------------------------------------------------------
def test_json_log_writes_one_parseable_line(capsys: pytest.CaptureFixture[str]) -> None:
    """Component bodies cannot configure logging: KFP's executor_main already owns
    the root logger, so basicConfig there is a silent no-op."""
    json_log("INFO", "shard done", shard_id="tier3__kc_search", cells_done=125)
    out = capsys.readouterr().out.strip()
    assert "\n" not in out
    assert json.loads(out) == {
        "severity": "INFO",
        "message": "shard done",
        "shard_id": "tier3__kc_search",
        "cells_done": 125,
    }


def test_json_log_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    json_log("ERROR", "x")
    captured = capsys.readouterr()
    assert captured.out
    assert not captured.err
