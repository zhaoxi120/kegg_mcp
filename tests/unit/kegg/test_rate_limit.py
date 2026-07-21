"""Tests for the deployment-wide no-burst KEGG rate limiter."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import threading
from pathlib import Path
from typing import cast

import pytest

import kegg_mcp.kegg.rate_limit as rate_limit_module
from kegg_mcp.kegg.contracts import MIN_REQUESTS_PER_SECOND
from kegg_mcp.kegg.rate_limit import (
    MAX_REQUESTS_PER_SECOND,
    DeploymentRateLimiter,
)


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


def _scope(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _limiter(
    tmp_path: Path,
    label: str,
    rate: float,
    clock: FakeClock,
) -> DeploymentRateLimiter:
    return DeploymentRateLimiter(
        _scope(label),
        rate,
        state_root=tmp_path / "rate-limit",
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )


def test_rate_limiter_starts_one_request_immediately_then_spaces_requests(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, "rate-basic", 2.0, clock)

    starts: list[float] = []
    for _ in range(3):
        limiter.acquire()
        starts.append(clock.now)

    assert starts == pytest.approx([100.0, 100.5, 101.0])
    assert clock.sleeps == pytest.approx([0.5, 0.5])


def test_rate_limiter_does_not_accumulate_burst_capacity_while_idle(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, "rate-idle", 2.0, clock)

    limiter.acquire()
    clock.now += 20.0
    limiter.acquire()
    limiter.acquire()

    assert clock.sleeps == pytest.approx([0.5])


def test_state_sync_latency_cannot_reduce_request_spacing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeClock()
    sync_calls = 0

    def delayed_first_sync(_descriptor: int) -> None:
        nonlocal sync_calls
        sync_calls += 1
        if sync_calls == 1:
            clock.now += 0.2

    monkeypatch.setattr(rate_limit_module.os, "fsync", delayed_first_sync)
    limiter = _limiter(tmp_path, "rate-sync-latency", 1.0, clock)

    starts: list[float] = []
    for _ in range(2):
        limiter.acquire()
        starts.append(clock.now)

    assert starts == pytest.approx([100.0, 101.0])
    assert sync_calls == 1


def test_instances_share_schedule_for_the_same_endpoint_fingerprint(tmp_path: Path) -> None:
    clock = FakeClock()
    first = _limiter(tmp_path, "rate-shared", 2.0, clock)
    second = _limiter(tmp_path, "rate-shared", 2.0, clock)

    first.acquire()
    second.acquire()

    assert first.scope == second.scope == _scope("rate-shared")
    assert clock.sleeps == pytest.approx([0.5])


def test_distinct_endpoint_fingerprints_have_independent_schedules(tmp_path: Path) -> None:
    clock = FakeClock()
    first = _limiter(tmp_path, "rate-scope-a", 2.0, clock)
    second = _limiter(tmp_path, "rate-scope-b", 2.0, clock)

    first.acquire()
    second.acquire()

    assert clock.sleeps == []


def test_rate_limiter_retains_slowest_rate_for_shared_scope(tmp_path: Path) -> None:
    clock = FakeClock()
    faster = _limiter(tmp_path, "rate-conservative", 3.0, clock)
    _limiter(tmp_path, "rate-conservative", 2.0, clock)

    faster.acquire()
    faster.acquire()

    assert clock.sleeps == pytest.approx([0.5])


def test_rate_limiter_serializes_concurrent_acquisitions(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, "rate-concurrent", 2.0, clock)
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


def test_two_python_processes_share_one_endpoint_schedule(tmp_path: Path) -> None:
    state_root = tmp_path / "rate-limit"
    scope = _scope("cross-process")
    script = (
        "import sys,time; "
        "from kegg_mcp.kegg.rate_limit import DeploymentRateLimiter; "
        "limiter=DeploymentRateLimiter(sys.argv[1],3.0,state_root=sys.argv[2]); "
        "limiter.acquire(); print(time.monotonic(),flush=True)"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, scope, str(state_root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]

    starts: list[float] = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        starts.append(float(stdout.strip()))

    assert abs(starts[0] - starts[1]) >= 0.30


def test_state_root_and_files_are_owner_only(tmp_path: Path) -> None:
    clock = FakeClock()
    limiter = _limiter(tmp_path, "private-state", 2.0, clock)

    limiter.acquire()

    root = tmp_path / "rate-limit"
    state = next(root.iterdir())
    assert root.stat().st_mode & 0o077 == 0
    assert state.stat().st_mode & 0o077 == 0
    assert state.name == f"{_scope('private-state')}.state"


def test_state_file_open_remains_bound_to_the_validated_root_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "rate-limit"
    state_root.mkdir(mode=0o700)
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    displaced = tmp_path / "displaced"
    clock = FakeClock()
    limiter = DeploymentRateLimiter(
        _scope("pinned-root"),
        2.0,
        state_root=state_root,
        monotonic=clock.monotonic,
        sleeper=clock.sleep,
    )
    real_open_state_file = rate_limit_module._open_state_file  # pyright: ignore[reportPrivateUsage]

    def replace_named_root(root_descriptor: int, scope: str) -> int:
        state_root.rename(displaced)
        replacement.rename(state_root)
        return real_open_state_file(root_descriptor, scope)

    monkeypatch.setattr(rate_limit_module, "_open_state_file", replace_named_root)

    limiter.acquire()

    state_name = f"{_scope('pinned-root')}.state"
    assert (displaced / state_name).is_file()
    assert not (state_root / state_name).exists()


@pytest.mark.parametrize(
    "rate",
    [
        0.0,
        MIN_REQUESTS_PER_SECOND / 2.0,
        -1.0,
        MAX_REQUESTS_PER_SECOND + 0.01,
        float("inf"),
        float("nan"),
        cast(float, True),
    ],
)
def test_rate_limiter_rejects_unsafe_rates(tmp_path: Path, rate: float) -> None:
    with pytest.raises(ValueError, match="between 1/60 and 3"):
        DeploymentRateLimiter(_scope("invalid"), rate, state_root=tmp_path / "state")


@pytest.mark.parametrize("scope", ["", "not-a-fingerprint", "f" * 63, "G" * 64])
def test_rate_limiter_rejects_non_fingerprint_scopes(tmp_path: Path, scope: str) -> None:
    with pytest.raises(ValueError, match="endpoint fingerprint"):
        DeploymentRateLimiter(scope, 2.0, state_root=tmp_path / "state")


def test_rate_limiter_rejects_invalid_clock_values(tmp_path: Path) -> None:
    limiter = DeploymentRateLimiter(
        _scope("invalid-clock"),
        2.0,
        state_root=tmp_path / "state",
        monotonic=lambda: float("nan"),
        sleeper=lambda _: None,
    )

    with pytest.raises(RuntimeError, match="monotonic clock"):
        limiter.acquire()
