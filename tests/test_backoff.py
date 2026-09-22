"""Retry, rate limiting, and the circuit breaker.

These models are on Dynamic Shared Quota: there is no per-project bucket to
raise, so a 429 means transient contention in a shared pool and client-side
backoff is the only mitigation available. That makes this module the load-
bearing defence for a ~12-hour sweep against a shared sandbox project.

The most important test here is the token-accounting one. Every cost comparison
in the experiment rests on reranker token counts, and a retry loop in the wrong
place silently inflates them.
"""

from __future__ import annotations

import asyncio

import pytest
from google.api_core import exceptions as gexc

from bq_context.runner.backoff import (
    AdaptiveRateLimiter,
    CircuitBreaker,
    RetryPolicy,
    call_with_backoff,
    is_retryable,
    retry_async,
)
from bq_context.usage import record_usage_response, usage_scope

FAST = RetryPolicy(max_attempts=8, base_delay=0.0, max_delay=0.0, jitter=False)


class FakeUsage:
    def __init__(self, tokens: int) -> None:
        self.prompt_token_count = tokens
        self.candidates_token_count = 0
        self.total_token_count = tokens


class FakeResponse:
    def __init__(self, tokens: int) -> None:
        self.usage_metadata = FakeUsage(tokens)


class FlakyClient:
    """Raises 429 ``fail_times`` times, then returns a response."""

    def __init__(self, fail_times: int, tokens: int = 100) -> None:
        self.fail_times = fail_times
        self.tokens = tokens
        self.attempts = 0

    def generate(self) -> FakeResponse:
        self.attempts += 1
        if self.attempts <= self.fail_times:
            msg = "429 quota exceeded"
            raise gexc.ResourceExhausted(msg)
        response = FakeResponse(self.tokens)
        record_usage_response(response)
        return response


# ---------------------------------------------------------------------------
# Token accounting under retry — the one that protects the cost metric
# ---------------------------------------------------------------------------
def test_retried_call_counts_tokens_once() -> None:
    """A retried call must count once, not once per attempt.

    Transport failures (429/503/timeout) return no response and bill no tokens,
    so recording only on the response we keep is both correct and exact.
    """
    client = FlakyClient(fail_times=2, tokens=100)

    with usage_scope() as log:
        call_with_backoff(client.generate, policy=FAST)

    assert client.attempts == 3
    assert log.total_tokens == 100  # not 300
    assert log.calls == 1


def test_repeated_distinct_calls_do_accumulate() -> None:
    """Guard against 'fixing' the above by suppressing all but the first call."""
    with usage_scope() as log:
        for _ in range(3):
            call_with_backoff(FlakyClient(fail_times=1, tokens=50).generate, policy=FAST)

    assert log.calls == 3
    assert log.total_tokens == 150


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
def test_gives_up_after_max_attempts_and_reraises() -> None:
    client = FlakyClient(fail_times=99)
    policy = RetryPolicy(max_attempts=4, base_delay=0.0, max_delay=0.0, jitter=False)

    with pytest.raises(gexc.ResourceExhausted):
        call_with_backoff(client.generate, policy=policy)

    assert client.attempts == 4


def test_non_retryable_errors_fail_immediately() -> None:
    """A bad request will fail identically forever; retrying just wastes quota."""
    calls = 0

    def bad() -> None:
        nonlocal calls
        calls += 1
        msg = "400 malformed request"
        raise gexc.InvalidArgument(msg)

    with pytest.raises(gexc.InvalidArgument):
        call_with_backoff(bad, policy=FAST)

    assert calls == 1


@pytest.mark.parametrize(
    "exc",
    [
        gexc.ResourceExhausted("429"),
        gexc.ServiceUnavailable("503"),
        gexc.DeadlineExceeded("timeout"),
        gexc.InternalServerError("500"),
        ConnectionError("reset by peer"),
        TimeoutError(),
    ],
)
def test_transient_failures_are_retryable(exc: BaseException) -> None:
    assert is_retryable(exc)


@pytest.mark.parametrize(
    "exc",
    [
        gexc.InvalidArgument("400"),
        gexc.PermissionDenied("403"),
        gexc.NotFound("404"),
        ValueError("nonsense"),
    ],
)
def test_permanent_failures_are_not_retryable(exc: BaseException) -> None:
    assert not is_retryable(exc)


def test_empty_reranker_response_is_not_retried() -> None:
    """A safety block or truncation is not transient.

    It also already recorded its usage, since the API did return a response, so
    retrying it would be the one path that double-counts tokens.
    """
    from bq_context.reranker.util_rerank import RerankerEmptyResponseError

    assert not is_retryable(RerankerEmptyResponseError("blocked"))


def test_backoff_delays_grow_and_are_capped() -> None:
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=64.0, jitter=False)
    delays = [policy.delay_for(attempt) for attempt in range(1, 9)]

    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]
    assert max(delays) <= 64.0
    assert delays == sorted(delays), "delays must be non-decreasing"


def test_full_jitter_stays_within_the_cap() -> None:
    """Full jitter spreads retries so eight shards don't resynchronise."""
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=64.0, jitter=True)
    samples = [policy.delay_for(5) for _ in range(200)]

    assert all(0.0 <= d <= 16.0 for d in samples)
    assert len(set(samples)) > 1, "jitter must actually vary"


async def test_retry_async_awaits_between_attempts() -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    client = FlakyClient(fail_times=2)
    policy = RetryPolicy(base_delay=1.0, multiplier=2.0, max_delay=64.0, jitter=False)

    await retry_async(lambda: asyncio.to_thread(client.generate), policy=policy, sleep=fake_sleep)

    assert client.attempts == 3
    assert slept == [1.0, 2.0]


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------
def test_circuit_breaker_trips_on_consecutive_failures() -> None:
    """One poisoned question costs a cell; a revoked credential must not cost 90
    minutes of retrying."""
    breaker = CircuitBreaker(max_consecutive=20)

    for _ in range(19):
        breaker.record(ok=False)
    assert not breaker.tripped

    breaker.record(ok=False)
    assert breaker.tripped
    assert "consecutive" in breaker.reason


def test_a_success_resets_the_consecutive_counter() -> None:
    breaker = CircuitBreaker(max_consecutive=3)
    breaker.record(ok=False)
    breaker.record(ok=False)
    breaker.record(ok=True)
    breaker.record(ok=False)
    breaker.record(ok=False)

    assert not breaker.tripped


def test_circuit_breaker_trips_on_sustained_error_rate() -> None:
    breaker = CircuitBreaker(max_consecutive=100, error_rate=0.10, min_attempts=30)

    # 29 attempts at ~14% error: under the minimum sample, so no trip yet.
    for i in range(29):
        breaker.record(ok=i % 7 != 0)
    assert not breaker.tripped

    breaker.record(ok=False)
    assert breaker.tripped
    assert "error rate" in breaker.reason


def test_healthy_runs_never_trip() -> None:
    breaker = CircuitBreaker()
    for i in range(500):
        breaker.record(ok=i % 100 != 0)  # 1% errors
    assert not breaker.tripped


# ---------------------------------------------------------------------------
# Adaptive rate limiter
# ---------------------------------------------------------------------------
async def test_limiter_paces_calls_to_the_configured_rate() -> None:
    slept: list[float] = []
    now = [0.0]

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = AdaptiveRateLimiter(rate_per_sec=2.0, sleep=fake_sleep, clock=lambda: now[0])

    for _ in range(3):
        await limiter.acquire()

    assert slept == [pytest.approx(0.5), pytest.approx(0.5)], slept


async def test_limiter_halves_its_rate_on_a_429() -> None:
    """Eight shards each doing this converge on the shared pool's real capacity
    with no central coordinator."""
    limiter = AdaptiveRateLimiter(rate_per_sec=4.0)
    assert limiter.rate == pytest.approx(4.0)

    limiter.penalize()
    assert limiter.rate == pytest.approx(2.0)
    limiter.penalize()
    assert limiter.rate == pytest.approx(1.0)


async def test_limiter_recovers_slowly_after_a_quiet_period() -> None:
    now = [0.0]
    limiter = AdaptiveRateLimiter(rate_per_sec=4.0, clock=lambda: now[0])

    limiter.penalize()
    assert limiter.rate == pytest.approx(2.0)

    limiter.reward()  # too soon after the penalty
    assert limiter.rate == pytest.approx(2.0)

    now[0] += 61.0
    limiter.reward()
    assert limiter.rate == pytest.approx(2.2)


def test_limiter_never_drops_to_zero() -> None:
    limiter = AdaptiveRateLimiter(rate_per_sec=1.0, min_rate_per_sec=0.05)
    for _ in range(20):
        limiter.penalize()
    assert limiter.rate >= 0.05


def test_limiter_recovery_is_capped_at_the_initial_rate() -> None:
    now = [0.0]
    limiter = AdaptiveRateLimiter(rate_per_sec=2.0, clock=lambda: now[0])
    for _ in range(50):
        now[0] += 61.0
        limiter.reward()
    assert limiter.rate == pytest.approx(2.0)
