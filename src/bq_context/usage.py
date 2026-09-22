"""Exact per-cell token accounting for the reranker's Gemini calls.

``call_reranker`` invokes Gemini directly rather than through ADK, so its
``usage_metadata`` never reaches the ADK event stream and has to be collected on
the side. Upstream used a plain module-level list, which was exact only because
runs were strictly sequential — the comment in its source says so.

A ``ContextVar`` keeps that exactness while allowing concurrent cells: each
``usage_scope()`` gets its own log, and coroutines inherit the context active
when they were created, so two cells in flight cannot pollute each other's
totals.

Two properties this module is responsible for:

- **Retries must not double-count.** Record usage only from the response you
  actually keep, never from an attempt you discarded. The backoff wrapper sits
  outside this call deliberately.
- **Recording outside a scope is a no-op**, so interactive ``adk web`` use does
  not crash on a missing benchmark context.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "UsageLog",
    "current_usage",
    "record_usage_response",
    "record_usage_tokens",
    "usage_scope",
]


@dataclass(slots=True)
class UsageLog:
    """Accumulated Gemini token usage for one scope (normally one cell)."""

    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "calls": self.calls,
        }


_USAGE: ContextVar[UsageLog | None] = ContextVar("bq_context_usage", default=None)


@contextmanager
def usage_scope() -> Iterator[UsageLog]:
    """Collect token usage recorded inside this block into a fresh log."""
    log = UsageLog()
    token = _USAGE.set(log)
    try:
        yield log
    finally:
        _USAGE.reset(token)


def current_usage() -> UsageLog | None:
    """Return the active usage log, or None when no scope is open."""
    return _USAGE.get()


def record_usage_tokens(*, prompt: int, output: int, total: int) -> None:
    """Add one call's token counts to the active log; no-op outside a scope."""
    log = _USAGE.get()
    if log is None:
        return
    log.prompt_tokens += prompt
    log.output_tokens += output
    log.total_tokens += total
    log.calls += 1


def record_usage_response(response: Any) -> None:
    """Record usage from a Gemini response.

    Call this only for a response you are keeping. Recording a retried-away
    attempt would inflate the per-cell token figure, which is the number the
    whole cost comparison rests on.
    """
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return
    record_usage_tokens(
        prompt=getattr(usage, "prompt_token_count", 0) or 0,
        output=getattr(usage, "candidates_token_count", 0) or 0,
        total=getattr(usage, "total_token_count", 0) or 0,
    )
