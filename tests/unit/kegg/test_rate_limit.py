"""Tests for the process-wide no-burst KEGG rate limiter."""

from __future__ import annotations

import threading
from typing import cast

import pytest

from kegg_mcp.kegg.rate_limit import MAX_REQUESTS_PER_SECOND, ProcessWideRateLimiter


class FakeClock:
    """Deterministic monotonic clock whose sleeper advances time."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_rate_limiter_starts_one_request_immediately_then_spaces_requests() -> None:
    clock = FakeClock()
    limiter = ProcessWideRateLimiter(
        "rate-basic",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    starts: list[float] = []
    for _ in range(3):
        limiter.acquire()
        starts.append(clock.now)

    assert starts == pytest.approx([100.0, 100.5, 101.0])
    assert clock.sleeps == pytest.approx([0.5, 0.5])


def test_rate_limiter_does_not_accumulate_burst_capacity_while_idle() -> None:
    clock = FakeClock()
    limiter = ProcessWideRateLimiter(
        "rate-idle",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    limiter.acquire()
    clock.now += 20.0
    limiter.acquire()
    limiter.acquire()

    assert clock.sleeps == pytest.approx([0.5])


def test_rate_limiter_shares_schedule_across_instances_for_same_scope() -> None:
    clock = FakeClock()
    first = ProcessWideRateLimiter(
        "rate-shared",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    second = ProcessWideRateLimiter(
        "rate-shared",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    first.acquire()
    second.acquire()

    assert first.scope == second.scope == "rate-shared"
    assert clock.sleeps == pytest.approx([0.5])


def test_rate_limiter_keeps_independent_endpoint_scopes_independent() -> None:
    clock = FakeClock()
    first = ProcessWideRateLimiter(
        "rate-scope-a",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    second = ProcessWideRateLimiter(
        "rate-scope-b",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    first.acquire()
    second.acquire()

    assert clock.sleeps == []


def test_rate_limiter_retains_slowest_rate_for_shared_scope() -> None:
    clock = FakeClock()
    faster = ProcessWideRateLimiter(
        "rate-conservative",
        3.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    ProcessWideRateLimiter(
        "rate-conservative",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )

    faster.acquire()
    faster.acquire()

    assert clock.sleeps == pytest.approx([0.5])


def test_rate_limiter_serializes_concurrent_acquisitions() -> None:
    clock = FakeClock()
    limiter = ProcessWideRateLimiter(
        "rate-concurrent",
        2.0,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    barrier = threading.Barrier(6)
    failures: list[BaseException] = []

    def acquire() -> None:
        try:
            barrier.wait()
            limiter.acquire()
        except BaseException as error:  # pragma: no cover - assertion reports thread failures
            failures.append(error)

    threads = [threading.Thread(target=acquire) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert not failures
    assert all(not thread.is_alive() for thread in threads)
    assert clock.sleeps == pytest.approx([0.5] * 5)


@pytest.mark.parametrize(
    "rate",
    [
        0.0,
        -1.0,
        MAX_REQUESTS_PER_SECOND + 0.01,
        float("inf"),
        float("nan"),
        cast(float, True),
    ],
)
def test_rate_limiter_rejects_unsafe_rates(rate: float) -> None:
    with pytest.raises(ValueError, match="no greater than 3"):
        ProcessWideRateLimiter("rate-invalid", rate)


@pytest.mark.parametrize("scope", ["", " leading", "trailing ", "has space", "bad\nline"])
def test_rate_limiter_rejects_unsafe_scopes(scope: str) -> None:
    with pytest.raises(ValueError, match="safe non-empty"):
        ProcessWideRateLimiter(scope, 2.0)


def test_rate_limiter_rejects_invalid_clock_values() -> None:
    limiter = ProcessWideRateLimiter(
        "rate-invalid-clock",
        2.0,
        monotonic=lambda: float("nan"),
        sleeper=lambda _: None,
    )

    with pytest.raises(RuntimeError, match="monotonic clock"):
        limiter.acquire()
