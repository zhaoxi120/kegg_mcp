"""Private coordination for process-scoped renderer state directories."""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from kegg_render_mcp._filesystem import bounded_directory_names, open_absolute_directory
from kegg_render_mcp._platform import (
    acquire_exclusive_lock,
    release_lock,
)
from kegg_render_mcp.contracts import ARTIFACT_NAME_PATTERN, MAX_ARTIFACTS, RENDER_ID_PATTERN

_ARTIFACT_NAME = re.compile(rf"{ARTIFACT_NAME_PATTERN}\Z")
_RESULT_ID = re.compile(rf"{RENDER_ID_PATTERN}\Z")
_SCOPE_NAME = re.compile(r"scope_[A-Za-z0-9_-]{32}\Z")
_COORDINATION_LOCK_NAME: Final = ".renderer.lock"
_SCOPE_LOCK_NAME: Final = ".scope.lock"
MAX_SCOPES: Final = 32


@dataclass(frozen=True, slots=True)
class RendererStateScope:
    """Descriptors pinning one live renderer process scope."""

    state_fd: int
    scope_fd: int
    scope_name: str
    coordination_lock_fd: int
    scope_lock_fd: int


def open_state_scope(state_root: Path, max_results: int) -> RendererStateScope:
    """Open one isolated process scope under a shared renderer state root."""
    state_fd = _open_or_create_private_directory(state_root)
    coordination_lock_fd: int | None = None
    scope_fd: int | None = None
    scope_name: str | None = None
    scope_lock_fd: int | None = None
    try:
        coordination_lock_fd = _open_or_create_private_lock(
            state_fd,
            _COORDINATION_LOCK_NAME,
        )
        with _exclusive_lock(coordination_lock_fd):
            _validate_named_lock(
                state_fd,
                _COORDINATION_LOCK_NAME,
                coordination_lock_fd,
            )
            active_scopes = _cleanup_abandoned_scopes(state_fd, max_results)
            if active_scopes >= MAX_SCOPES:
                raise ValueError("renderer state root has reached its scope limit")
            scope_fd, scope_name, scope_lock_fd = _allocate_scope(state_fd)
        return RendererStateScope(
            state_fd=state_fd,
            scope_fd=scope_fd,
            scope_name=scope_name,
            coordination_lock_fd=coordination_lock_fd,
            scope_lock_fd=scope_lock_fd,
        )
    except Exception:
        _release_descriptors(
            state_fd=state_fd,
            scope_fd=scope_fd,
            coordination_lock_fd=coordination_lock_fd,
            scope_lock_fd=scope_lock_fd,
        )
        raise ValueError("renderer state root is already active or unsafe") from None


def cleanup_state_scope(scope: RendererStateScope, max_results: int) -> None:
    """Remove an empty owned scope while holding root coordination."""
    with _exclusive_lock(scope.coordination_lock_fd):
        _validate_named_lock(
            scope.state_fd,
            _COORDINATION_LOCK_NAME,
            scope.coordination_lock_fd,
        )
        _validate_named_directory(
            scope.state_fd,
            scope.scope_name,
            scope.scope_fd,
            "renderer scope",
        )
        _validate_named_lock(scope.scope_fd, _SCOPE_LOCK_NAME, scope.scope_lock_fd)
        if set(
            bounded_directory_names(
                scope.scope_fd,
                max_results + 1,
                "renderer scope",
            )
        ) != {_SCOPE_LOCK_NAME}:
            return
        _validate_named_directory(
            scope.state_fd,
            scope.scope_name,
            scope.scope_fd,
            "renderer scope",
        )
        _validate_named_lock(scope.scope_fd, _SCOPE_LOCK_NAME, scope.scope_lock_fd)
        os.unlink(_SCOPE_LOCK_NAME, dir_fd=scope.scope_fd)
        if bounded_directory_names(scope.scope_fd, 1, "renderer scope after lease removal"):
            raise ValueError("renderer scope changed during cleanup")
        _validate_named_directory(
            scope.state_fd,
            scope.scope_name,
            scope.scope_fd,
            "renderer scope",
        )
        os.rmdir(scope.scope_name, dir_fd=scope.state_fd)


def release_state_scope(scope: RendererStateScope) -> None:
    """Release all descriptors associated with a process scope."""
    _release_descriptors(
        state_fd=scope.state_fd,
        scope_fd=scope.scope_fd,
        coordination_lock_fd=scope.coordination_lock_fd,
        scope_lock_fd=scope.scope_lock_fd,
    )


def validate_named_directory(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    label: str,
) -> None:
    """Require a pinned directory descriptor to match its current name."""
    _validate_named_directory(parent_descriptor, name, descriptor, label)


def validate_owner_only_directory(descriptor: int) -> None:
    """Require an owner-only directory owned by the effective user."""
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("renderer state directories must be owner-only direct directories")


def _allocate_scope(state_fd: int) -> tuple[int, str, int]:
    for _ in range(8):
        scope_name = f"scope_{secrets.token_urlsafe(24)}"
        try:
            os.mkdir(scope_name, mode=0o700, dir_fd=state_fd)
        except FileExistsError:
            continue
        scope_fd: int | None = None
        scope_lock_fd: int | None = None
        try:
            scope_fd = os.open(
                scope_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=state_fd,
            )
            validate_owner_only_directory(scope_fd)
            _validate_named_directory(state_fd, scope_name, scope_fd, "renderer scope")
            scope_lock_fd = _create_private_lock(scope_fd, _SCOPE_LOCK_NAME)
            if not acquire_exclusive_lock(scope_lock_fd, nonblocking=True):
                raise OSError("new renderer scope lease could not be locked")
            _validate_named_lock(scope_fd, _SCOPE_LOCK_NAME, scope_lock_fd)
        except Exception:
            if scope_lock_fd is not None:
                assert scope_fd is not None
                with contextlib.suppress(OSError, ValueError):
                    _validate_named_lock(scope_fd, _SCOPE_LOCK_NAME, scope_lock_fd)
                    os.unlink(_SCOPE_LOCK_NAME, dir_fd=scope_fd)
                with contextlib.suppress(OSError):
                    os.close(scope_lock_fd)
            if scope_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(scope_fd)
            with contextlib.suppress(OSError):
                os.rmdir(scope_name, dir_fd=state_fd)
            raise
        return scope_fd, scope_name, scope_lock_fd
    raise OSError("could not allocate renderer process scope")


def _cleanup_abandoned_scopes(state_fd: int, max_results: int) -> int:
    state_names = bounded_directory_names(
        state_fd,
        MAX_SCOPES + 1,
        "renderer state root",
    )
    active_scopes = 0
    for scope_name in state_names:
        if scope_name == _COORDINATION_LOCK_NAME:
            continue
        if _SCOPE_NAME.fullmatch(scope_name) is None:
            raise ValueError("renderer state root contains an unsafe entry")
        scope_fd = os.open(
            scope_name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=state_fd,
        )
        scope_lock_fd: int | None = None
        scope_is_locked = False
        try:
            validate_owner_only_directory(scope_fd)
            _validate_named_directory(state_fd, scope_name, scope_fd, "renderer scope")
            scope_lock_fd = _open_private_lock(scope_fd, _SCOPE_LOCK_NAME)
            if not acquire_exclusive_lock(scope_lock_fd, nonblocking=True):
                active_scopes += 1
                continue
            scope_is_locked = True
            _validate_named_lock(scope_fd, _SCOPE_LOCK_NAME, scope_lock_fd)
            scope_names = bounded_directory_names(
                scope_fd,
                max_results + 1,
                "abandoned renderer scope",
            )
            if _SCOPE_LOCK_NAME not in scope_names:
                raise ValueError("a renderer scope is missing its lease")
            result_names = tuple(name for name in scope_names if name != _SCOPE_LOCK_NAME)
            for result_name in result_names:
                if _RESULT_ID.fullmatch(result_name) is None:
                    raise ValueError("an abandoned renderer scope contains an unsafe entry")
                _remove_abandoned_result(scope_fd, result_name)
            _validate_named_directory(state_fd, scope_name, scope_fd, "renderer scope")
            _validate_named_lock(scope_fd, _SCOPE_LOCK_NAME, scope_lock_fd)
            os.unlink(_SCOPE_LOCK_NAME, dir_fd=scope_fd)
            if bounded_directory_names(scope_fd, 1, "abandoned renderer scope after cleanup"):
                raise ValueError("an abandoned renderer scope changed during cleanup")
            _validate_named_directory(state_fd, scope_name, scope_fd, "renderer scope")
            os.rmdir(scope_name, dir_fd=state_fd)
        finally:
            if scope_lock_fd is not None:
                if scope_is_locked:
                    with contextlib.suppress(OSError):
                        release_lock(scope_lock_fd)
                os.close(scope_lock_fd)
            os.close(scope_fd)
    return active_scopes


def _remove_abandoned_result(scope_descriptor: int, result_name: str) -> None:
    result_fd = os.open(
        result_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=scope_descriptor,
    )
    try:
        validate_owner_only_directory(result_fd)
        _validate_named_directory(
            scope_descriptor,
            result_name,
            result_fd,
            "abandoned renderer result",
        )
        artifact_names = bounded_directory_names(
            result_fd,
            MAX_ARTIFACTS,
            "abandoned renderer result",
        )
        for artifact_name in artifact_names:
            if _ARTIFACT_NAME.fullmatch(artifact_name) is None:
                raise ValueError("an abandoned renderer result contains an unsafe entry")
            metadata = os.stat(artifact_name, dir_fd=result_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise ValueError("abandoned renderer artifacts must be owner-only regular files")
        for artifact_name in artifact_names:
            os.unlink(artifact_name, dir_fd=result_fd)
        _validate_named_directory(
            scope_descriptor,
            result_name,
            result_fd,
            "abandoned renderer result",
        )
        os.rmdir(result_name, dir_fd=scope_descriptor)
    finally:
        os.close(result_fd)


@contextmanager
def _exclusive_lock(descriptor: int) -> Generator[None]:
    acquire_exclusive_lock(descriptor)
    try:
        yield
    finally:
        release_lock(descriptor)


def _open_or_create_private_lock(directory_descriptor: int, name: str) -> int:
    created = False
    try:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_descriptor,
        )
        created = True
    except FileExistsError:
        descriptor = os.open(
            name,
            os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=directory_descriptor,
        )
    try:
        if created:
            os.fchmod(descriptor, 0o600)
        _validate_named_lock(directory_descriptor, name, descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _create_private_lock(directory_descriptor: int, name: str) -> int:
    descriptor = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        os.fchmod(descriptor, 0o600)
        _validate_named_lock(directory_descriptor, name, descriptor)
        return descriptor
    except Exception:
        with contextlib.suppress(OSError, ValueError):
            _validate_named_lock(directory_descriptor, name, descriptor)
            os.unlink(name, dir_fd=directory_descriptor)
        os.close(descriptor)
        raise


def _open_private_lock(directory_descriptor: int, name: str) -> int:
    descriptor = os.open(
        name,
        os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=directory_descriptor,
    )
    try:
        _validate_named_lock(directory_descriptor, name, descriptor)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_named_lock(directory_descriptor: int, name: str, descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    named_metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or not stat.S_ISREG(named_metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or named_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or stat.S_IMODE(named_metadata.st_mode) != 0o600
        or (metadata.st_dev, metadata.st_ino) != (named_metadata.st_dev, named_metadata.st_ino)
    ):
        raise ValueError("renderer locks must be owner-only direct regular files")


def _validate_named_directory(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    label: str,
) -> None:
    metadata = os.fstat(descriptor)
    named_metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if not stat.S_ISDIR(named_metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (
        named_metadata.st_dev,
        named_metadata.st_ino,
    ):
        raise ValueError(f"{label} was replaced while it was open")


def _release_descriptors(
    *,
    state_fd: int | None,
    scope_fd: int | None,
    coordination_lock_fd: int | None,
    scope_lock_fd: int | None,
) -> None:
    if scope_lock_fd is not None:
        with contextlib.suppress(OSError):
            release_lock(scope_lock_fd)
        os.close(scope_lock_fd)
    if scope_fd is not None:
        os.close(scope_fd)
    if coordination_lock_fd is not None:
        os.close(coordination_lock_fd)
    if state_fd is not None:
        os.close(state_fd)


def _open_or_create_private_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ValueError("state root must be an absolute non-root path")
    parent_fd = open_absolute_directory(path.parent)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            with contextlib.suppress(FileExistsError):
                os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        validate_owner_only_directory(descriptor)
        return descriptor
    finally:
        os.close(parent_fd)
