"""Small POSIX descriptor helpers shared inside the renderer distribution."""

from __future__ import annotations

import os
from pathlib import Path


def bounded_directory_names(descriptor: int, limit: int, label: str) -> tuple[str, ...]:
    """List no more than a caller-provided number of direct entries."""
    names: list[str] = []
    with os.scandir(descriptor) as entries:
        for entry in entries:
            if len(names) >= limit:
                raise ValueError(f"{label} exceeds its entry-count limit")
            names.append(entry.name)
    return tuple(names)


def open_absolute_directory(path: Path) -> int:
    """Open an absolute directory by walking no-follow directory descriptors."""
    descriptor = os.open(
        "/",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
    )
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


__all__ = ["bounded_directory_names", "open_absolute_directory"]
