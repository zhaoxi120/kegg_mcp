"""Symlink-safe local state and output operations for DeepKOALA jobs."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from deepkoala_mcp.contracts import MAX_OUTPUT_BYTES

_JOB = re.compile(r"^job_[a-f0-9]{32}$")
_SESSION = re.compile(r"^session_[a-f0-9]{32}$")


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


def prepare_state_root(path: Path) -> Path:
    existed = path.exists()
    if existed:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("state root must be a direct directory")
    else:
        path.mkdir(parents=True, mode=0o700)
    resolved = path.resolve(strict=True)
    metadata = resolved.lstat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        if not existed:
            os.chmod(resolved, 0o700)
        else:
            raise ValueError("existing state root must be owner-only and owned by this user")
    return resolved


def remove_job_directory(directory: Path, session: Path) -> None:
    if directory.parent != session or _JOB.fullmatch(directory.name) is None:
        raise ValueError("invalid controlled job directory")
    if not directory.exists() and not directory.is_symlink():
        return
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("controlled job entry is not a direct directory")
    shutil.rmtree(directory)


def remove_session_directory(session: Path) -> None:
    if _SESSION.fullmatch(session.name) is None:
        raise ValueError("invalid controlled session directory")
    if not session.exists() and not session.is_symlink():
        return
    metadata = session.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("controlled session entry is not a direct directory")
    shutil.rmtree(session)


__all__ = [
    "prepare_state_root",
    "remove_job_directory",
    "remove_session_directory",
    "validate_output",
]
