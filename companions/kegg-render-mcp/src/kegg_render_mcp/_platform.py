"""Renderer platform capability checks and portable POSIX advisory locking."""

from __future__ import annotations

import errno
import importlib
import os
from collections.abc import Callable
from types import ModuleType
from typing import Final, cast

UNSUPPORTED_PLATFORM_DIAGNOSTIC: Final = (
    "unsupported platform: kegg-render-mcp requires macOS or another POSIX host with "
    "descriptor-relative no-follow filesystem operations and advisory flock locking; "
    "on native Windows, run under WSL or another supported POSIX environment"
)

_REQUIRED_OS_ATTRIBUTES: Final = (
    "O_CLOEXEC",
    "O_DIRECTORY",
    "O_NOFOLLOW",
    "fchmod",
    "geteuid",
)
_REQUIRED_DIR_FD_FUNCTIONS: Final = (
    "link",
    "mkdir",
    "open",
    "rename",
    "rmdir",
    "stat",
    "unlink",
)
_REQUIRED_FOLLOW_SYMLINK_FUNCTIONS: Final = ("link", "stat")
_REQUIRED_LOCK_ATTRIBUTES: Final = ("LOCK_EX", "LOCK_NB", "LOCK_UN", "flock")


class UnsupportedRendererPlatformError(RuntimeError):
    """The host cannot preserve the renderer's local filesystem guarantees."""


def validate_renderer_platform() -> None:
    """Fail before any path access when required local guarantees are unavailable."""
    if not _renderer_platform_is_supported():
        raise UnsupportedRendererPlatformError(UNSUPPORTED_PLATFORM_DIAGNOSTIC)


def acquire_exclusive_lock(descriptor: int, *, nonblocking: bool = False) -> bool:
    """Acquire an advisory exclusive lock, reporting portable contention as false."""
    operation_names = ("LOCK_EX", "LOCK_NB") if nonblocking else ("LOCK_EX",)
    try:
        _invoke_flock(descriptor, operation_names)
    except OSError as error:
        if nonblocking and error.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def release_lock(descriptor: int) -> None:
    """Release one advisory lock acquired through this module."""
    _invoke_flock(descriptor, ("LOCK_UN",))


def _renderer_platform_is_supported() -> bool:
    if os.name != "posix" or any(
        not hasattr(os, attribute) for attribute in _REQUIRED_OS_ATTRIBUTES
    ):
        return False
    required_os_functions = (
        *_REQUIRED_DIR_FD_FUNCTIONS,
        *_REQUIRED_FOLLOW_SYMLINK_FUNCTIONS,
        "scandir",
    )
    if any(not hasattr(os, name) for name in required_os_functions):
        return False
    if _LOCK_MODULE is None or any(
        not hasattr(_LOCK_MODULE, attribute) for attribute in _REQUIRED_LOCK_ATTRIBUTES
    ):
        return False
    if any(
        cast(Callable[..., object], getattr(os, name)) not in os.supports_dir_fd
        for name in _REQUIRED_DIR_FD_FUNCTIONS
    ):
        return False
    if any(
        cast(Callable[..., object], getattr(os, name)) not in os.supports_follow_symlinks
        for name in _REQUIRED_FOLLOW_SYMLINK_FUNCTIONS
    ):
        return False
    return os.scandir in os.supports_fd


def _invoke_flock(descriptor: int, operation_names: tuple[str, ...]) -> None:
    module = _LOCK_MODULE
    if module is None:
        raise UnsupportedRendererPlatformError(UNSUPPORTED_PLATFORM_DIAGNOSTIC)
    operation = 0
    for name in operation_names:
        operation |= cast(int, getattr(module, name))
    flock = cast(Callable[[int, int], object], module.flock)
    flock(descriptor, operation)


def _load_lock_module() -> ModuleType | None:
    try:
        return importlib.import_module("fcntl")
    except ImportError:
        return None


_LOCK_MODULE: Final = _load_lock_module()

__all__ = [
    "UNSUPPORTED_PLATFORM_DIAGNOSTIC",
    "UnsupportedRendererPlatformError",
    "acquire_exclusive_lock",
    "release_lock",
    "validate_renderer_platform",
]
