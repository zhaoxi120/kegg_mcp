"""Symlink-safe local state and output operations for DeepKOALA jobs."""

from __future__ import annotations

import fcntl
import os
import re
import stat
from pathlib import Path

from deepkoala_mcp.contracts import MAX_OUTPUT_BYTES, MAX_QUEUE_SIZE

_JOB = re.compile(r"^job_[a-f0-9]{32}$")
_SESSION = re.compile(r"^session_[a-f0-9]{32}$")
_LOCK_NAME = ".deepkoala.lock"
_MAX_JOB_FILES = 16


def validate_output(path: Path) -> int:
    try:
        named = path.lstat()
    except OSError as error:
        raise RuntimeError("DeepKOALA output is unavailable") from error
    if stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(named.st_mode):
        raise RuntimeError("DeepKOALA output is not a direct regular file")
    if named.st_size < 1:
        raise RuntimeError("DeepKOALA output is empty")
    if named.st_size > MAX_OUTPUT_BYTES:
        raise RuntimeError("DeepKOALA output exceeds the handoff limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
            or opened.st_size != named.st_size
        ):
            raise RuntimeError("DeepKOALA output changed during validation")
    finally:
        os.close(descriptor)
    return named.st_size


def _prepare_state_root(path: Path) -> Path:
    existed = os.path.lexists(path)
    if existed:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("state root must be a direct directory")
    else:
        path.mkdir(parents=True, mode=0o700)
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValueError("state root must not contain symlinks")
    metadata = resolved.lstat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        if not existed:
            os.chmod(resolved, 0o700)
        else:
            raise ValueError("existing state root must be owner-only and owned by this user")
    return resolved


def acquire_state_root(path: Path) -> tuple[Path, int, int]:
    """Open one owner-only state root and hold its deployment-wide exclusive lock."""
    root = _prepare_state_root(path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_fd = os.open(root, directory_flags)
    lock_fd: int | None = None
    try:
        _validate_owner_only_directory(directory_fd)
        lock_fd = os.open(
            _LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != os.geteuid():
            raise OSError("state lock must be a user-owned regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return root, directory_fd, lock_fd
    except (OSError, ValueError):
        if lock_fd is not None:
            os.close(lock_fd)
        os.close(directory_fd)
        raise ValueError("state root is already active or unsafe") from None


def release_state_root(directory_fd: int, lock_fd: int) -> None:
    """Release one state-root lease without unlinking its stable lock inode."""
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    os.close(directory_fd)


def cleanup_abandoned_sessions(directory_fd: int) -> None:
    """Remove only strict abandoned session/job directories after the lock is held."""
    for name in os.listdir(directory_fd):
        if _SESSION.fullmatch(name) is None:
            continue
        try:
            _remove_session_name(directory_fd, name, reject_file_symlinks=True)
        except OSError as error:
            raise ValueError("abandoned session state is unsafe") from error


def remove_job_directory(directory: Path, session: Path) -> None:
    if directory.parent != session or _JOB.fullmatch(directory.name) is None:
        raise ValueError("invalid controlled job directory")
    session_fd = _open_owner_only_directory(session)
    try:
        if not _entry_exists(session_fd, directory.name):
            return
        _remove_job_name(session_fd, directory.name, reject_file_symlinks=False)
    finally:
        os.close(session_fd)


def remove_session_directory(session: Path) -> None:
    if _SESSION.fullmatch(session.name) is None:
        raise ValueError("invalid controlled session directory")
    root_fd = _open_owner_only_directory(session.parent)
    try:
        if not _entry_exists(root_fd, session.name):
            return
        _remove_session_name(root_fd, session.name, reject_file_symlinks=False)
    finally:
        os.close(root_fd)


def _remove_session_name(root_fd: int, session_name: str, *, reject_file_symlinks: bool) -> None:
    if _SESSION.fullmatch(session_name) is None:
        raise ValueError("invalid controlled session directory")
    session_fd = os.open(
        session_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    try:
        _validate_owner_only_directory(session_fd)
        job_names = os.listdir(session_fd)
        if len(job_names) > MAX_QUEUE_SIZE + 1:
            raise ValueError("controlled session exceeds the bounded job count")
        if any(_JOB.fullmatch(name) is None for name in job_names):
            raise ValueError("controlled session contains an unexpected entry")
        for job_name in job_names:
            _remove_job_name(
                session_fd,
                job_name,
                reject_file_symlinks=reject_file_symlinks,
            )
    finally:
        os.close(session_fd)
    os.rmdir(session_name, dir_fd=root_fd)


def _remove_job_name(session_fd: int, job_name: str, *, reject_file_symlinks: bool) -> None:
    if _JOB.fullmatch(job_name) is None:
        raise ValueError("invalid controlled job directory")
    job_fd = os.open(
        job_name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=session_fd,
    )
    try:
        _validate_owner_only_directory(job_fd)
        names = os.listdir(job_fd)
        if len(names) > _MAX_JOB_FILES:
            raise ValueError("controlled job directory exceeds the file bound")
        for name in names:
            metadata = os.stat(name, dir_fd=job_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) and not reject_file_symlinks:
                os.unlink(name, dir_fd=job_fd)
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ValueError("controlled job directory contains an unsafe entry")
            os.unlink(name, dir_fd=job_fd)
    finally:
        os.close(job_fd)
    os.rmdir(job_name, dir_fd=session_fd)


def _open_owner_only_directory(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _validate_owner_only_directory(descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _validate_owner_only_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("controlled directory must be owner-only")


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


__all__ = [
    "acquire_state_root",
    "cleanup_abandoned_sessions",
    "release_state_root",
    "remove_job_directory",
    "remove_session_directory",
    "validate_output",
]
