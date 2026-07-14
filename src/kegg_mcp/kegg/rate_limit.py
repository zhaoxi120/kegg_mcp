"""Process-wide, no-burst rate limiting for KEGG HTTP requests."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar

MAX_REQUESTS_PER_SECOND = 3.0


@dataclass(slots=True)
class _ScopeState:
    """Mutable scheduling state shared by all limiters for one endpoint scope."""

    interval_seconds: float
    next_allowed_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class ProcessWideRateLimiter:
    """Serialize request starts for one process-wide endpoint namespace.

    The scheduler retains no tokens. An idle period therefore permits one immediate
    request, not a burst of accumulated requests. If callers construct multiple
    limiters for the same scope with different rates, the slowest configured rate is
    retained for that scope for the life of the process.
    """

    _registry: ClassVar[dict[str, _ScopeState]] = {}
    _registry_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        scope: str,
        requests_per_second: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            not scope
            or len(scope) > 256
            or scope != scope.strip()
            or any(ord(character) < 33 or ord(character) == 127 for character in scope)
        ):
            raise ValueError("rate-limit scope must be a safe non-empty logical identifier")
        try:
            scope.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("rate-limit scope must be valid UTF-8 text") from error
        if (
            isinstance(requests_per_second, bool)
            or not math.isfinite(requests_per_second)
            or not 0.0 < requests_per_second <= MAX_REQUESTS_PER_SECOND
        ):
            raise ValueError("requests_per_second must be finite and no greater than 3")

        interval_seconds = 1.0 / float(requests_per_second)
        with self._registry_lock:
            state = self._registry.get(scope)
            if state is None:
                state = _ScopeState(interval_seconds=interval_seconds)
                self._registry[scope] = state

        # A later client must not silently weaken the process-wide policy chosen by
        # another client sharing the endpoint namespace.
        with state.lock:
            state.interval_seconds = max(state.interval_seconds, interval_seconds)

        self._scope = scope
        self._state = state
        self._monotonic = monotonic
        self._sleeper = sleeper

    @property
    def scope(self) -> str:
        """Return the logical endpoint namespace governed by this limiter."""
        return self._scope

    def acquire(self) -> None:
        """Wait until one request may start, reserving no future burst capacity."""
        with self._state.lock:
            now = self._read_clock()
            target = self._state.next_allowed_at
            if target is not None and now < target:
                self._sleeper(target - now)
                # Use the scheduled target as a floor. This prevents a test clock or
                # an imprecise sleeper from moving the schedule backwards.
                now = max(target, self._read_clock())

            self._state.next_allowed_at = now + self._state.interval_seconds

    def _read_clock(self) -> float:
        value = self._monotonic()
        if not math.isfinite(value):
            raise RuntimeError("the monotonic clock returned an invalid value")
        return float(value)
