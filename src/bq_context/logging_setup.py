"""Structured logging, so Cloud Logging queries by field instead of by regex.

Cloud Logging parses a JSON object written to stdout and lifts `severity` and
`message` into real `LogEntry` fields, leaving everything else queryable under
`jsonPayload`. That turns

    gcloud logging read '"cache warm"' | grep -oP 'in \\K[0-9.]+'

— which is how `cache_warm_s` was actually recovered for the first full run —
into

    gcloud logging read 'jsonPayload.shard_id="tier3__bq_tools"'

Three things about this environment shape the implementation:

**stdout, not stderr.** ``cli.py`` logs to stderr, and Vertex tends to tag
container stderr as ``ERROR`` regardless of content, which fights the
``severity`` field. The JSON formatter therefore switches streams.

**``logging.basicConfig`` is a no-op once the root logger has handlers**, and
KFP's ``executor_main`` calls it before a component body runs. Configuring
logging *inside* a component body silently does nothing — which is why
``json_log`` writes to stdout directly rather than going through ``logging``.

**Honest scope.** ``executor_main`` also dumps the whole ``executor_input`` at
INFO as plain text, so "stdout is structured" is not literally true. Our lines
are structured; they are interleaved with the executor's, which arrive as
``textPayload``.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

#: Set to ``json`` in the runner image so pipeline tasks emit structured logs.
#: Unset locally, where a human is reading and the plain format is better.
FORMAT_ENV = "BQ_CONTEXT_LOG_FORMAT"

HUMAN_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Cloud Logging promotes "severity" and "message" out of the payload into the
# LogEntry itself; everything else stays in jsonPayload, which is what makes it
# queryable by field.

#: Never overwrite these when merging a record's own attributes.
_RESERVED = frozenset(
    {"args", "msg", "message", "levelname", "exc_info", "exc_text", "stack_info", "severity"}
)


class JsonFormatter(logging.Formatter):
    """One JSON object per record, with `severity` where Cloud Logging wants it."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        # Anything passed via `extra=` lands on the record; surface it as a
        # queryable field rather than losing it.
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _RESERVED
                and not key.startswith("_")
                and key not in payload
                # Skip the standard LogRecord furniture, which is noise in a payload.
                and key
                in {
                    "shard_id",
                    "experiment_id",
                    "tier",
                    "approach",
                    "code_version",
                    "corpus_fingerprint",
                    "cells_done",
                    "cells_total",
                    "cache_warm_s",
                    "abort_reason",
                }
            }
        )
        if record.exc_info:
            # Serialised rather than dropped: a traceback that only exists in a
            # formatter's default rendering is invisible to a structured query.
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(*, verbose: bool = False) -> None:
    """Install the human or JSON formatter, chosen by ``BQ_CONTEXT_LOG_FORMAT``.

    Call once, from the CLI entrypoint. ``force=True`` because a library or a
    harness may already have configured the root logger, and ``basicConfig``
    would otherwise return silently having done nothing.
    """
    import os  # noqa: PLC0415

    level = logging.DEBUG if verbose else logging.INFO
    if os.environ.get(FORMAT_ENV, "").lower() == "json":
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        logging.basicConfig(level=level, handlers=[handler], force=True)
    else:
        logging.basicConfig(level=level, format=HUMAN_FORMAT, stream=sys.stderr, force=True)


def json_log(severity: str, message: str, **fields: Any) -> None:
    """Write one structured line to stdout, bypassing ``logging`` entirely.

    For KFP component bodies. They cannot configure logging — ``executor_main``
    already owns the root logger, so ``basicConfig`` there is a silent no-op —
    but they *can* import this, because a component body may import the
    installed library even though it cannot reference module-level helpers in
    ``components.py``.
    """
    # print, not logging: stdout is the transport Cloud Logging parses, and the
    # root logger is already owned by KFP's executor by the time this runs.
    print(  # noqa: T201
        json.dumps({"severity": severity, "message": message, **fields}, default=str), flush=True
    )
