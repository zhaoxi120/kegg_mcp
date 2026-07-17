"""Deployment-wide, no-burst rate limiting for KEGG HTTP requests."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import ClassVar, Final, cast

from kegg_mcp.kegg.contracts import MIN_REQUESTS_PER_SECOND, default_rate_limit_root

MAX_REQUESTS_PER_SECOND = 3.0
_STATE_BYTES: Final = 1_024
_SCOPE_PATTERN: Final = re.compile(r"[a-f0-9]{64}\Z")


class DeploymentRateLimiter:
    """Serialize request starts across local processes for one endpoint fingerprint.

    The owner-only state directory and advisory file lock are shared by Core and
    Renderer. The scheduler stores only an opaque endpoint fingerprint, monotonic
    timing state, and the local boot identifier. It never accumulates burst tokens.
    """

    _intervals: ClassVar[dict[tuple[str, str], float]] = {}
    _intervals_lock: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        scope: str,
        requests_per_second: float,
        *,
        state_root: str | os.PathLike[str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        raw_scope = cast(object, scope)
        raw_rate = cast(object, requests_per_second)
        if not isinstance(raw_scope, str) or _SCOPE_PATTERN.fullmatch(raw_scope) is None:
            raise ValueError("rate-limit scope must be an endpoint fingerprint")
        if (
            isinstance(raw_rate, bool)
            or not isinstance(raw_rate, int | float)
            or not math.isfinite(raw_rate)
            or not MIN_REQUESTS_PER_SECOND <= raw_rate <= MAX_REQUESTS_PER_SECOND
        ):
            raise ValueError("requests_per_second must be finite and between 1/60 and 3")
        configured_root = Path(state_root or default_rate_limit_root()).expanduser()
        if (
            not configured_root.is_absolute()
            or ".." in configured_root.parts
            or "\x00" in str(configured_root)
        ):
            raise ValueError("rate-limit state root must be absolute and traversal-free")

        interval = 1.0 / float(raw_rate)
        registry_key = (str(configured_root), raw_scope)
        with self._intervals_lock:
            self._intervals[registry_key] = max(self._intervals.get(registry_key, 0.0), interval)

        self._scope = raw_scope
        self._state_root = configured_root
        self._registry_key = registry_key
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._boot_id = _boot_identifier()

    @property
    def scope(self) -> str:
        """Return the opaque endpoint fingerprint governed by this limiter."""
        return self._scope

    def acquire(self) -> None:
        """Reserve one globally spaced request start without future burst capacity."""
        root_descriptor = _open_state_root(self._state_root)
        try:
            descriptor = _open_state_file(root_descriptor, self._scope)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                interval = self._configured_interval()
                now = self._read_clock()
                state, malformed = _read_state(descriptor)
                target: float | None = None
                if state is not None and state[0] == self._boot_id:
                    stored_interval, stored_target = state[1], state[2]
                    interval = max(interval, stored_interval)
                    if stored_target <= now + interval * 2.0:
                        target = stored_target
                    else:
                        malformed = True
                if malformed:
                    target = now + interval
                if target is not None and now < target:
                    self._sleeper(target - now)
                    now = max(target, self._read_clock())
                _write_state(
                    descriptor,
                    boot_id=self._boot_id,
                    interval=interval,
                    next_allowed_at=now + interval,
                )
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
        finally:
            os.close(root_descriptor)

    def _configured_interval(self) -> float:
        with self._intervals_lock:
            return self._intervals[self._registry_key]

    def _read_clock(self) -> float:
        value = self._monotonic()
        if not math.isfinite(value):
            raise RuntimeError("the monotonic clock returned an invalid value")
        return float(value)


def _open_state_root(path: Path) -> int:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        missing.append(candidate)
        candidate = candidate.parent
    _reject_symlink_components(candidate)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in missing:
        with suppress(OSError):
            directory.chmod(0o700)
    _reject_symlink_components(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        named = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o077
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("rate-limit state root must be an owner-only directory")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_state_file(root_descriptor: int, scope: str) -> int:
    name = f"{scope}.state"
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=root_descriptor)
    try:
        opened = os.fstat(descriptor)
        named = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(named.st_mode)
            or opened.st_uid != os.geteuid()
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OSError("rate-limit state must be an owner-controlled regular file")
        os.fchmod(descriptor, 0o600)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_state(descriptor: int) -> tuple[tuple[str, float, float] | None, bool]:
    size = os.fstat(descriptor).st_size
    if size == 0:
        return None, False
    if size > _STATE_BYTES:
        return None, True
    try:
        raw = os.pread(descriptor, _STATE_BYTES + 1, 0)
        decoded = cast(object, json.loads(raw))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, True
    if not isinstance(decoded, dict):
        return None, True
    value = cast(dict[object, object], decoded)
    if set(value) != {"boot_id", "interval", "next_allowed_at"}:
        return None, True
    boot_id = value.get("boot_id")
    interval = value.get("interval")
    next_allowed_at = value.get("next_allowed_at")
    if (
        not isinstance(boot_id, str)
        or len(boot_id) > 128
        or isinstance(interval, bool)
        or not isinstance(interval, int | float)
        or isinstance(next_allowed_at, bool)
        or not isinstance(next_allowed_at, int | float)
        or not math.isfinite(interval)
        or not math.isfinite(next_allowed_at)
        or not 1.0 / MAX_REQUESTS_PER_SECOND <= interval <= 1.0 / MIN_REQUESTS_PER_SECOND
        or next_allowed_at < 0.0
    ):
        return None, True
    return (boot_id, float(interval), float(next_allowed_at)), False


def _write_state(
    descriptor: int,
    *,
    boot_id: str,
    interval: float,
    next_allowed_at: float,
) -> None:
    payload = json.dumps(
        {
            "boot_id": boot_id,
            "interval": interval,
            "next_allowed_at": next_allowed_at,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    if len(payload) > _STATE_BYTES:
        raise RuntimeError("rate-limit state exceeded its internal bound")
    os.ftruncate(descriptor, 0)
    written = os.pwrite(descriptor, payload, 0)
    if written != len(payload):
        raise OSError("rate-limit state write was incomplete")
    os.fsync(descriptor)


def _boot_identifier() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        value = f"proc1-{os.stat('/proc/1').st_ctime_ns}"
    if not value or len(value) > 128 or any(ord(character) < 33 for character in value):
        raise RuntimeError("the local boot identifier is unavailable")
    return value


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise OSError("rate-limit state root must not contain symlinks")


__all__ = ["MAX_REQUESTS_PER_SECOND", "DeploymentRateLimiter"]
