"""Shared private filesystem primitives for local SQLite stores."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def prepare_private_parent(parent: Path) -> None:
    """Create a SQLite parent tree and enforce one owner-only final directory."""
    missing_directories: list[Path] = []
    candidate = parent
    while not candidate.exists() and candidate != candidate.parent:
        missing_directories.append(candidate)
        candidate = candidate.parent
    reject_symlink_components(candidate)
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    for directory in missing_directories:
        tighten_directory_permissions(directory)
    validate_private_directory(parent)


def validate_private_directory(path: Path) -> os.stat_result:
    """Validate one absolute, real, owner-only directory and its ancestry."""
    reject_symlink_components(path)
    path_stat = path.lstat()
    if not stat.S_ISDIR(path_stat.st_mode) or stat.S_ISLNK(path_stat.st_mode):
        raise OSError("SQLite storage parent must be a real directory")
    if hasattr(os, "geteuid") and path_stat.st_uid != os.geteuid():
        raise OSError("SQLite storage parent must be owned by the current user")
    if stat.S_IMODE(path_stat.st_mode) & 0o022:
        raise OSError("SQLite storage parent must not be group- or world-writable")
    return path_stat


def reject_symlink_components(path: Path) -> None:
    """Reject every existing symlink or non-directory path component."""
    if not path.is_absolute():
        raise OSError("SQLite storage parent must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise OSError("SQLite storage parent must not contain symlinks")
        if not stat.S_ISDIR(component_stat.st_mode):
            raise OSError("SQLite storage parent components must be directories")


def tighten_directory_permissions(path: Path) -> None:
    """Best-effort tightening for one newly created directory."""
    try:
        path_stat = path.lstat()
        if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
            path.chmod(0o700)
    except OSError:
        pass


def tighten_file_permissions(path: Path) -> None:
    """Best-effort tightening for one SQLite database file."""
    try:
        path_stat = path.lstat()
        if stat.S_ISREG(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
            path.chmod(0o600)
    except OSError:
        pass


__all__ = [
    "prepare_private_parent",
    "reject_symlink_components",
    "tighten_file_permissions",
    "validate_private_directory",
]
