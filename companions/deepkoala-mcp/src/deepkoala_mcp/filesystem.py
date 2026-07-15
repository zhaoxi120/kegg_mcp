"""Private filesystem primitives for the DeepKOALA companion.

The companion copies every accepted input into a controlled job directory before
launching an external process.  These helpers intentionally expose no source path
or file content through their return values or exception messages.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Final

CONTROLLED_JOB_FILES: Final = frozenset(
    {
        "input.fasta",
        "deepkoala-output.csv",
        "detailed.csv",
        "provenance.json",
        "diagnostics.txt",
    }
)

_SESSION_NAME_PATTERN: Final = re.compile(r"session_[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_JOB_NAME_PATTERN: Final = re.compile(r"job_[A-Za-z0-9][A-Za-z0-9_-]{0,95}\Z")
_COPY_CHUNK_BYTES: Final = 64 * 1024


class FilesystemSecurityError(ValueError):
    """Raised when a path or filesystem object violates the companion boundary."""


class FileSizeLimitError(FilesystemSecurityError):
    """Raised when an input exceeds its configured byte limit."""


def prepare_state_root(state_root: Path | str) -> Path:
    """Create or reopen a private non-root state directory without following symlinks."""
    root = _absolute_path(state_root, "state root")
    if root == Path(root.anchor):
        raise FilesystemSecurityError("state root must not be the filesystem root")
    current_fd = os.open(
        os.sep,
        os.O_RDONLY | _optional_flag("O_DIRECTORY") | _optional_flag("O_CLOEXEC"),
    )
    try:
        for position, component in enumerate(root.parts[1:], start=1):
            is_final = position == len(root.parts) - 1
            next_fd, created = _open_or_create_private_directory_at(current_fd, component)
            os.close(current_fd)
            current_fd = next_fd
            if created:
                os.fchmod(current_fd, 0o700)
            if is_final:
                metadata = os.fstat(current_fd)
                if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise FilesystemSecurityError(
                        "existing state root must be owner-only and owned by the current user"
                    )
        return root
    finally:
        os.close(current_fd)


def create_session_directory(state_root: Path | str, session_name: str) -> Path:
    """Create one non-reusable private session directory below ``state_root``."""
    _validate_controlled_name(session_name, _SESSION_NAME_PATTERN, "session")
    root = _absolute_path(state_root, "state root")
    root_fd = _open_absolute_directory_no_symlinks(root)
    try:
        _mkdir_private_at(root_fd, session_name)
    finally:
        os.close(root_fd)
    return root / session_name


def create_job_directory(session_directory: Path | str, job_name: str) -> Path:
    """Create one non-reusable private job directory below a controlled session."""
    _validate_controlled_name(job_name, _JOB_NAME_PATTERN, "job")
    session = _absolute_path(session_directory, "session directory")
    _validate_controlled_name(session.name, _SESSION_NAME_PATTERN, "session")
    session_fd = _open_absolute_directory_no_symlinks(session)
    try:
        _mkdir_private_at(session_fd, job_name)
    finally:
        os.close(session_fd)
    return session / job_name


def secure_write_bytes(
    job_directory: Path | str,
    filename: str,
    content: bytes,
) -> None:
    """Exclusively create a known private job file and write ``content`` to it."""
    job_fd = _open_controlled_job_directory(job_directory)
    file_fd = -1
    created = False
    try:
        _validate_job_filename(filename)
        file_fd = _open_exclusive_private_file_at(job_fd, filename)
        created = True
        _write_all(file_fd, content)
        os.fsync(file_fd)
    except Exception:
        if file_fd >= 0:
            os.close(file_fd)
            file_fd = -1
        if created:
            _unlink_if_present(job_fd, filename)
        raise
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(job_fd)


def copy_allowed_regular_file(
    source_path: Path | str,
    *,
    allowed_roots: Sequence[Path | str],
    job_directory: Path | str,
    filename: str,
    max_bytes: int,
) -> int:
    """Copy an allowlisted regular file from its opened descriptor into a job.

    Directory components are opened one at a time without following symlinks.  The
    final source is checked with ``lstat`` before and after ``open`` and compared to
    ``fstat``.  Copying then proceeds only from that stable descriptor, eliminating
    source-name TOCTOU from subsequent runner access.
    """
    _validate_positive_int(max_bytes, "max_bytes")
    source_fd, parent_fd, source_name, initial_stat = _open_allowed_regular_source(
        source_path, allowed_roots
    )
    job_fd = -1
    destination_fd = -1
    created = False
    copied = 0
    try:
        job_fd = _open_controlled_job_directory(job_directory)
        _validate_job_filename(filename)
        destination_fd = _open_exclusive_private_file_at(job_fd, filename)
        created = True

        while True:
            chunk = os.read(source_fd, _COPY_CHUNK_BYTES)
            if not chunk:
                break
            copied += len(chunk)
            if copied > max_bytes:
                raise FileSizeLimitError("input exceeds the configured byte limit")
            _write_all(destination_fd, chunk)

        final_fd_stat = os.fstat(source_fd)
        final_name_stat = _lstat_at(parent_fd, source_name)
        if not _same_inode(initial_stat, final_fd_stat) or not _same_inode(
            final_fd_stat, final_name_stat
        ):
            raise FilesystemSecurityError("input file identity changed during intake")
        if not (
            _same_file_state(initial_stat, final_fd_stat)
            and _same_file_state(final_fd_stat, final_name_stat)
        ):
            raise FilesystemSecurityError("input file changed during intake")
        os.fsync(destination_fd)
        return copied
    except Exception:
        if destination_fd >= 0:
            os.close(destination_fd)
            destination_fd = -1
        if created and job_fd >= 0:
            _unlink_if_present(job_fd, filename)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        if job_fd >= 0:
            os.close(job_fd)
        os.close(source_fd)
        os.close(parent_fd)


def read_controlled_file(
    job_directory: Path | str,
    filename: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read one known regular job file without following a replacement symlink."""
    _validate_positive_int(max_bytes, "max_bytes")
    job_fd = _open_controlled_job_directory(job_directory)
    file_fd = -1
    try:
        _validate_job_filename(filename)
        flags = os.O_RDONLY | _optional_flag("O_CLOEXEC") | _optional_flag("O_NOFOLLOW")
        flags |= _optional_flag("O_NONBLOCK")
        try:
            file_fd = os.open(filename, flags, dir_fd=job_fd)
        except OSError as error:
            raise FilesystemSecurityError("controlled file is unavailable") from error
        file_stat = os.fstat(file_fd)
        name_stat = _lstat_at(job_fd, filename)
        if not stat.S_ISREG(file_stat.st_mode) or not _same_inode(file_stat, name_stat):
            raise FilesystemSecurityError("controlled file is not a stable regular file")
        if file_stat.st_size > max_bytes:
            raise FileSizeLimitError("controlled file exceeds the configured byte limit")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(_COPY_CHUNK_BYTES, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise FileSizeLimitError("controlled file exceeds the configured byte limit")
        final_fd_stat = os.fstat(file_fd)
        final_name_stat = _lstat_at(job_fd, filename)
        if (
            not _same_inode(file_stat, final_fd_stat)
            or not _same_inode(final_fd_stat, final_name_stat)
            or not _same_file_state(file_stat, final_fd_stat)
            or not _same_file_state(final_fd_stat, final_name_stat)
        ):
            raise FilesystemSecurityError("controlled file changed while it was read")
        return b"".join(chunks)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(job_fd)


def remove_controlled_file(job_directory: Path | str, filename: str) -> bool:
    """Unlink one known non-directory job entry without following symlinks."""
    job_fd = _open_controlled_job_directory(job_directory)
    try:
        _validate_job_filename(filename)
        try:
            entry_stat = _lstat_at(job_fd, filename)
        except FileNotFoundError:
            return False
        if stat.S_ISDIR(entry_stat.st_mode):
            raise FilesystemSecurityError("controlled file name refers to a directory")
        os.unlink(filename, dir_fd=job_fd)
        return True
    finally:
        os.close(job_fd)


def cleanup_job_directory(job_directory: Path | str) -> None:
    """Delete a controlled job directory containing only fixed, non-directory entries."""
    job = _absolute_path(job_directory, "job directory")
    _validate_controlled_name(job.name, _JOB_NAME_PATTERN, "job")
    parent = job.parent
    parent_fd = _open_absolute_directory_no_symlinks(parent)
    job_fd = -1
    try:
        before = _lstat_at(parent_fd, job.name)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise FilesystemSecurityError("job entry is not a controlled directory")
        job_fd = _open_directory_at(parent_fd, job.name)
        opened = os.fstat(job_fd)
        if not _same_inode(before, opened):
            raise FilesystemSecurityError("job directory identity changed during cleanup")

        entries = os.listdir(job_fd)
        unknown = set(entries).difference(CONTROLLED_JOB_FILES)
        if unknown:
            raise FilesystemSecurityError("job directory contains an unknown entry")
        for entry in entries:
            entry_stat = _lstat_at(job_fd, entry)
            if stat.S_ISDIR(entry_stat.st_mode):
                raise FilesystemSecurityError("job directory contains a nested directory")
        for entry in entries:
            os.unlink(entry, dir_fd=job_fd)

        after = _lstat_at(parent_fd, job.name)
        if not _same_inode(opened, after):
            raise FilesystemSecurityError("job directory identity changed during cleanup")
        os.close(job_fd)
        job_fd = -1
        os.rmdir(job.name, dir_fd=parent_fd)
    finally:
        if job_fd >= 0:
            os.close(job_fd)
        os.close(parent_fd)


def cleanup_session_directory(session_directory: Path | str) -> None:
    """Remove one empty controlled session directory without following symlinks."""
    session = _absolute_path(session_directory, "session directory")
    _validate_controlled_name(session.name, _SESSION_NAME_PATTERN, "session")
    parent_fd = _open_absolute_directory_no_symlinks(session.parent)
    session_fd = -1
    try:
        before = _lstat_at(parent_fd, session.name)
        if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise FilesystemSecurityError("session entry is not a controlled directory")
        session_fd = _open_directory_at(parent_fd, session.name)
        opened = os.fstat(session_fd)
        if not _same_inode(before, opened):
            raise FilesystemSecurityError("session directory identity changed during cleanup")
        if os.listdir(session_fd):
            raise FilesystemSecurityError("session directory is not empty")
        after = _lstat_at(parent_fd, session.name)
        if not _same_inode(opened, after):
            raise FilesystemSecurityError("session directory identity changed during cleanup")
        os.close(session_fd)
        session_fd = -1
        os.rmdir(session.name, dir_fd=parent_fd)
    finally:
        if session_fd >= 0:
            os.close(session_fd)
        os.close(parent_fd)


def _open_allowed_regular_source(
    source_path: Path | str,
    allowed_roots: Sequence[Path | str],
) -> tuple[int, int, str, os.stat_result]:
    source = _absolute_path(source_path, "input path")
    if not allowed_roots:
        raise FilesystemSecurityError("path intake requires at least one allowed root")

    matching: list[tuple[Path, Path]] = []
    for configured_root in allowed_roots:
        root = _absolute_path(configured_root, "allowed root")
        try:
            relative = source.relative_to(root)
        except ValueError:
            continue
        if relative.parts:
            matching.append((root, relative))
    if not matching:
        raise FilesystemSecurityError("input path is outside the configured allowed roots")

    root, relative = max(matching, key=lambda item: len(item[0].parts))
    parent_fd = _open_absolute_directory_no_symlinks(root)
    try:
        for component in relative.parts[:-1]:
            next_fd = _open_directory_at(parent_fd, component)
            os.close(parent_fd)
            parent_fd = next_fd

        source_name = relative.parts[-1]
        before = _lstat_at(parent_fd, source_name)
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise FilesystemSecurityError("input path must identify a regular file")
        flags = os.O_RDONLY | _optional_flag("O_CLOEXEC") | _optional_flag("O_NOFOLLOW")
        flags |= _optional_flag("O_NONBLOCK")
        try:
            source_fd = os.open(source_name, flags, dir_fd=parent_fd)
        except OSError as error:
            raise FilesystemSecurityError("input file could not be opened safely") from error
        try:
            opened = os.fstat(source_fd)
            after = _lstat_at(parent_fd, source_name)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not _same_inode(before, opened)
                or not _same_inode(opened, after)
                or not _same_file_state(before, opened)
                or not _same_file_state(opened, after)
            ):
                raise FilesystemSecurityError("input file identity changed during intake")
        except Exception:
            os.close(source_fd)
            raise
        return source_fd, parent_fd, source_name, opened
    except Exception:
        os.close(parent_fd)
        raise


def _open_absolute_directory_no_symlinks(path: Path) -> int:
    if os.name != "posix":
        raise FilesystemSecurityError("secure path intake requires a POSIX filesystem")
    parts = path.parts
    if not parts or path.anchor != os.sep:
        raise FilesystemSecurityError("controlled directory must be absolute")
    flags = os.O_RDONLY | _optional_flag("O_DIRECTORY") | _optional_flag("O_CLOEXEC")
    current_fd = os.open(os.sep, flags)
    try:
        for component in parts[1:]:
            next_fd = _open_directory_at(current_fd, component)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_directory_at(parent_fd: int, component: str) -> int:
    if component in {"", ".", ".."} or os.sep in component:
        raise FilesystemSecurityError("directory path contains an unsafe component")
    try:
        before = _lstat_at(parent_fd, component)
    except OSError as error:
        raise FilesystemSecurityError("controlled directory is unavailable") from error
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise FilesystemSecurityError("controlled path contains a symlink or non-directory")
    flags = os.O_RDONLY | _optional_flag("O_DIRECTORY") | _optional_flag("O_CLOEXEC")
    flags |= _optional_flag("O_NOFOLLOW")
    try:
        child_fd = os.open(component, flags, dir_fd=parent_fd)
    except OSError as error:
        raise FilesystemSecurityError("controlled directory could not be opened safely") from error
    try:
        opened = os.fstat(child_fd)
        after = _lstat_at(parent_fd, component)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or not _same_inode(before, opened)
            or not _same_inode(opened, after)
        ):
            raise FilesystemSecurityError("controlled directory identity changed")
    except Exception:
        os.close(child_fd)
        raise
    return child_fd


def _open_or_create_private_directory_at(parent_fd: int, component: str) -> tuple[int, bool]:
    if component in {"", ".", ".."} or os.sep in component:
        raise FilesystemSecurityError("directory path contains an unsafe component")
    created = False
    try:
        _lstat_at(parent_fd, component)
    except FileNotFoundError:
        try:
            os.mkdir(component, mode=0o700, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise FilesystemSecurityError("state directory could not be created") from error
    child_fd = _open_directory_at(parent_fd, component)
    return child_fd, created


def _mkdir_private_at(parent_fd: int, name: str) -> None:
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError as error:
        raise FilesystemSecurityError("controlled directory already exists") from error
    except OSError as error:
        raise FilesystemSecurityError("controlled directory could not be created") from error
    child_fd = -1
    try:
        child_fd = _open_directory_at(parent_fd, name)
        os.fchmod(child_fd, 0o700)
    except Exception:
        with suppress(OSError):
            os.rmdir(name, dir_fd=parent_fd)
        raise
    finally:
        if child_fd >= 0:
            os.close(child_fd)


def _open_controlled_job_directory(job_directory: Path | str) -> int:
    job = _absolute_path(job_directory, "job directory")
    _validate_controlled_name(job.name, _JOB_NAME_PATTERN, "job")
    return _open_absolute_directory_no_symlinks(job)


def _open_exclusive_private_file_at(directory_fd: int, filename: str) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _optional_flag("O_CLOEXEC")
    flags |= _optional_flag("O_NOFOLLOW")
    try:
        file_fd = os.open(filename, flags, 0o600, dir_fd=directory_fd)
    except OSError as error:
        if error.errno == errno.EEXIST:
            raise FilesystemSecurityError("controlled file already exists") from error
        raise FilesystemSecurityError("controlled file could not be created safely") from error
    try:
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise FilesystemSecurityError("controlled file is not regular")
        os.fchmod(file_fd, 0o600)
    except Exception:
        os.close(file_fd)
        _unlink_if_present(directory_fd, filename)
        raise
    return file_fd


def _write_all(file_fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError("private file write made no progress")
        remaining = remaining[written:]


def _unlink_if_present(directory_fd: int, filename: str) -> None:
    with suppress(FileNotFoundError):
        os.unlink(filename, dir_fd=directory_fd)


def _lstat_at(directory_fd: int, name: str) -> os.stat_result:
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
        and left.st_ctime_ns == right.st_ctime_ns
    )


def _absolute_path(value: Path | str, label: str) -> Path:
    try:
        path = Path(value)
    except TypeError as error:
        raise FilesystemSecurityError(f"{label} is invalid") from error
    if "\x00" in os.fspath(path):
        raise FilesystemSecurityError(f"{label} contains an invalid character")
    if not path.is_absolute():
        raise FilesystemSecurityError(f"{label} must be absolute")
    if ".." in path.parts:
        raise FilesystemSecurityError(f"{label} must not contain parent traversal")
    return path


def _validate_controlled_name(name: str, pattern: re.Pattern[str], label: str) -> None:
    if pattern.fullmatch(name) is None:
        raise FilesystemSecurityError(f"invalid controlled {label} name")


def _validate_job_filename(filename: str) -> None:
    if filename not in CONTROLLED_JOB_FILES:
        raise FilesystemSecurityError("unknown controlled job filename")


def _validate_positive_int(value: int, label: str) -> None:
    if isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")


def _optional_flag(name: str) -> int:
    value = getattr(os, name, 0)
    return value if isinstance(value, int) else 0
