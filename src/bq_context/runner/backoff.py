"""Surviving a shared-quota Gemini pool for twelve hours.

``gemini-3.6-flash`` and ``gemini-3.5-flash-lite`` are on Dynamic Shared Quota:
there is no per-project bucket to inspect or raise, so a 429 means transient
contention with other tenants and client-side backoff is the only lever. Three
layers, in increasing scope:

1. :func:`call_with_backoff` / :func:`retry_async` — truncated exponential
   backoff with full jitter around a single API call.
2. :class:`AdaptiveRateLimiter` — per-shard pacing that halves on a 429 and
   recovers slowly. Eight shards each running one converge on the pool's real
   capacity with no central coordinator.
3. :class:`CircuitBreaker` — gives up on a shard that is failing systematically,
   so a revoked credential costs seconds rather than the full retry budget.

**Why the retry lives here and not in the SDK.** ``google-genai`` has its own
retry, but it sits *inside* the call, so a retried request would record usage
per attempt. Every cost figure in this experiment is a reranker token count, so
the retry has to wrap our whole call boundary and usage has to be recorded once,
from the response we keep. Transport failures return no response and bill no
tokens, which is what makes "record only what we keep" both correct and exact.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "AdaptiveRateLimiter",
    "CircuitBreaker",
    "RetryPolicy",
    "call_with_backoff",
    "is_retryable",
    "retry_async",
]

#: Transport-level failures worth another attempt. Matched by class name so this
#: module does not hard-depend on google-api-core or google-genai being present,
#: and so it catches the equivalents from both SDKs.
_RETRYABLE_NAMES = frozenset(
    {
        "ResourceExhausted",  # 429
        "TooManyRequests",  # 429
        "ServiceUnavailable",  # 503
        "InternalServerError",  # 500
        "BadGateway",  # 502
        "GatewayTimeout",  # 504
        "DeadlineExceeded",
        "Aborted",
        "RetryError",
        "ConnectionError",
        "ConnectionResetError",
        "TimeoutError",
    }
)

_RETRYABLE_HTTP = frozenset({429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    Deliberately excluded: 400/403/404, and ``RerankerEmptyResponseError``. A
    safety block or truncation is not transient, and unlike a transport failure
    it already produced a billed response — retrying it is the one path that
    would double-count tokens.
    """
    if isinstance(exc, ConnectionError | TimeoutError):
        return True
    for klass in type(exc).__mro__:
        if klass.__name__ in _RETRYABLE_NAMES:
            return True
    # google-genai surfaces HTTP status on the exception rather than by type.
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    return isinstance(code, int) and code in _RETRYABLE_HTTP


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Truncated exponential backoff with full jitter."""

    max_attempts: int = 8
    base_delay: float = 1.0
    multiplier: float = 2.0
    max_delay: float = 64.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before attempt ``attempt + 1`` (1-indexed attempts).

        Full jitter — a uniform draw from ``[0, capped]`` rather than the capped
        value itself. With eight shards retrying against one shared pool,
        un-jittered backoff makes them resynchronise into a thundering herd at
        exactly the moment the pool is already under pressure.
        """
        capped = min(self.base_delay * (self.multiplier ** (attempt - 1)), self.max_delay)
        return random.uniform(0.0, capped) if self.jitter else capped  # noqa: S311


# The final attempt always re-raises, so the loops below never fall through.
_UNREACHABLE = "retry loop fell through; max_attempts must be >= 1"

DEFAULT_POLICY = RetryPolicy()


def call_with_backoff[T](
    fn: Callable[[], T],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[BaseException], None] | None = None,
) -> T:
    """Call ``fn``, retrying transient failures. Synchronous."""
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return fn()
        except BaseException as exc:
            if not is_retryable(exc) or attempt == policy.max_attempts:
                raise
            if on_retry is not None:
                on_retry(exc)
            delay = policy.delay_for(attempt)
            logger.warning(
                "Retryable %s on attempt %d/%d; sleeping %.1fs",
                type(exc).__name__,
                attempt,
                policy.max_attempts,
                delay,
            )
            sleep(delay)
    raise AssertionError(_UNREACHABLE)  # pragma: no cover


async def retry_async[T](
    fn: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_POLICY,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    on_retry: Callable[[BaseException], None] | None = None,
) -> T:
    """Await ``fn()``, retrying transient failures.

    Preferred over the sync form where the call is already awaited, since
    sleeping here does not hold a worker thread for up to 64 seconds.
    """
    import asyncio  # noqa: PLC0415 - keeps this module importable without a loop

    do_sleep = sleep if sleep is not None else asyncio.sleep
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await fn()
        except BaseException as exc:
            if not is_retryable(exc) or attempt == policy.max_attempts:
                raise
            if on_retry is not None:
                on_retry(exc)
            delay = policy.delay_for(attempt)
            logger.warning(
                "Retryable %s on attempt %d/%d; sleeping %.1fs",
                type(exc).__name__,
                attempt,
                policy.max_attempts,
                delay,
            )
            await do_sleep(delay)
    raise AssertionError(_UNREACHABLE)  # pragma: no cover


@dataclass
class CircuitBreaker:
    """Abandons a shard that is failing systematically rather than sporadically.

    Without this, ``set_retry(num_retries=2)`` on a shard broken by bad IAM or a
    revoked credential burns the whole retry budget — hours of machine time — to
    learn nothing. The two conditions catch different shapes of failure: a burst
    of consecutive errors (something just broke) and a sustained elevated rate
    (something is intermittently wrong).
    """

    max_consecutive: int = 20
    error_rate: float = 0.10
    min_attempts: int = 30

    attempts: int = 0
    failures: int = 0
    consecutive: int = 0
    reason: str = ""

    def record(self, *, ok: bool) -> None:
        self.attempts += 1
        if ok:
            self.consecutive = 0
            return
        self.failures += 1
        self.consecutive += 1

        if self.consecutive >= self.max_consecutive:
            self.reason = f"{self.consecutive} consecutive failures (limit {self.max_consecutive})"
        elif self.attempts >= self.min_attempts:
            rate = self.failures / self.attempts
            if rate > self.error_rate:
                self.reason = (
                    f"error rate {rate:.0%} over {self.attempts} attempts "
                    f"(limit {self.error_rate:.0%})"
                )

    @property
    def tripped(self) -> bool:
        return bool(self.reason)


@dataclass
class AdaptiveRateLimiter:
    """Per-shard pacing that reacts to 429s without a central coordinator.

    Multiplicative decrease, additive-ish increase: halve on a 429, and creep
    back up by 10% only after a quiet minute. Run independently by each of eight
    shards, this settles near whatever the shared pool will actually bear —
    which is the only thing available when there is no quota number to read.
    """

    rate_per_sec: float = 1.0
    min_rate_per_sec: float = 0.05
    recovery_factor: float = 1.10
    recovery_after_s: float = 60.0
    sleep: Any = None
    clock: Callable[[], float] = time.monotonic

    _rate: float = field(init=False)
    _ceiling: float = field(init=False)
    _next_allowed: float = field(init=False, default=0.0)
    _last_penalty: float = field(init=False)

    def __post_init__(self) -> None:
        self._rate = self.rate_per_sec
        self._ceiling = self.rate_per_sec
        self._last_penalty = self.clock()
        self._next_allowed = 0.0

    @property
    def rate(self) -> float:
        """Current issue rate, in requests per second."""
        return self._rate

    async def acquire(self) -> None:
        """Wait until the next request is allowed under the current rate."""
        import asyncio  # noqa: PLC0415

        do_sleep = self.sleep if self.sleep is not None else asyncio.sleep
        now = self.clock()
        if self._next_allowed and now < self._next_allowed:
            await do_sleep(self._next_allowed - now)
            now = self.clock()
        self._next_allowed = now + (1.0 / self._rate)

    def penalize(self) -> None:
        """Halve the rate after a 429."""
        self._rate = max(self._rate / 2.0, self.min_rate_per_sec)
        self._last_penalty = self.clock()
        logger.warning("Rate limited; issue rate now %.2f/s", self._rate)

    def reward(self) -> None:
        """Creep the rate back up, but only after a quiet period."""
        if self.clock() - self._last_penalty < self.recovery_after_s:
            return
        self._rate = min(self._rate * self.recovery_factor, self._ceiling)
        self._last_penalty = self.clock()
