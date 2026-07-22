"""Symlink-safe temporary state and stable output operations."""

from __future__ import annotations

import contextlib
import csv
import fcntl
import io
import math
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from deepkoala_mcp.contracts import (
    ANNOTATIONS_FILENAME,
    MAX_RESOURCE_PAGE_BYTES,
    MAX_RETAINED_JOBS,
    RUN_REPORT_FILENAME,
)

_JOB = re.compile(r"^job_[a-f0-9]{32}$")
_SESSION = re.compile(r"^session_[a-f0-9]{32}$")
_COORDINATION_LOCK_NAME = ".deepkoala.lock"
_RUNNER_LOCK_NAME = ".deepkoala.runner.lock"
_SESSION_LOCK_NAME = ".session.lock"
_MAX_JOB_FILES = 16
_MAX_REPORT_BYTES = MAX_RESOURCE_PAGE_BYTES
_MAX_OUTPUT_DIRECTORY_ENTRIES = 3
_MAX_SESSIONS = 32
_MAX_STATE_ROOT_ENTRIES = _MAX_SESSIONS + 2
_MAX_SESSION_ENTRIES = MAX_RETAINED_JOBS + 1
_DELIVERED_ARTIFACTS = (ANNOTATIONS_FILENAME, RUN_REPORT_FILENAME)
_REQUIRED_COLUMNS = frozenset({"name", "predict_label", "probability", "threshold", "annotate"})
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_FILE_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class OutputPathError(ValueError):
    """A requested stable output directory is outside policy or unsafe."""


class OutputAlreadyExistsError(OutputPathError):
    """The requested stable output directory exists but is not empty."""


class OutputValidationError(RuntimeError):
    """Runner output or a delivered artifact is missing, unsafe, or malformed."""


@dataclass(frozen=True, slots=True)
class ArtifactSlice:
    """One bounded immutable file slice and the observed total size."""

    content: bytes
    total_bytes: int


@dataclass(slots=True)
class ControlledOutputDirectory:
    """Pinned output root, stable relative name, and open-time directory identity."""

    path: Path
    root_path: Path
    root_fd: int
    root_identity: tuple[int, int, int]
    directory_fd: int
    relative_parts: tuple[str, ...]
    identity: tuple[int, int, int]
    created_by_service: bool
    delivered_identities: tuple[tuple[str, tuple[int, int, int, int, int]], ...] = ()


@dataclass(frozen=True, slots=True)
class StateSession:
    """Pinned state root and one process-owned private session lease."""

    root: Path
    root_fd: int
    session: Path
    session_fd: int
    session_identity: tuple[int, int, int]
    lease_fd: int
    lease_identity: tuple[int, int, int]


def create_output_directory(
    path: Path,
    output_roots: tuple[Path, ...],
) -> ControlledOutputDirectory:
    """Open or atomically create one owner-only empty directory below an allowed root."""
    if not path.is_absolute() or ".." in path.parts or not output_roots:
        raise OutputPathError("output directory is not allowed")
    parent = path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
        parent_metadata = parent.lstat()
    except OSError as error:
        raise OutputPathError("output parent is unavailable") from error
    if (
        resolved_parent != parent
        or stat.S_ISLNK(parent_metadata.st_mode)
        or not stat.S_ISDIR(parent_metadata.st_mode)
    ):
        raise OutputPathError("output parent must be a direct directory without symlinks")
    root = next(
        (
            root
            for root in output_roots
            if resolved_parent == root or resolved_parent.is_relative_to(root)
        ),
        None,
    )
    if root is None:
        raise OutputPathError("output directory escapes the configured roots")
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError as error:
        raise OutputPathError("output directory escapes the configured roots") from error
    if not relative_parts:
        raise OutputAlreadyExistsError("output directory already exists")

    root_fd: int | None = None
    parent_fd: int | None = None
    directory_fd: int | None = None
    created = False
    created_identity: tuple[int, int, int] | None = None
    try:
        root_fd = _open_output_root(root)
        try:
            parent_fd = _open_directory_parts(root_fd, relative_parts[:-1])
        except (OSError, ValueError) as error:
            raise OutputPathError("output parent could not be opened safely") from error
        try:
            os.stat(relative_parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(relative_parts[-1], mode=0o700, dir_fd=parent_fd)
                created = True
                created_metadata = os.stat(
                    relative_parts[-1],
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                if not stat.S_ISDIR(created_metadata.st_mode):
                    raise OutputPathError("created output entry is not a directory")
                created_identity = _directory_identity(created_metadata)
            except FileExistsError as error:
                raise OutputAlreadyExistsError(
                    "output directory was created concurrently"
                ) from error
            except OSError as error:
                raise OutputPathError("output directory could not be created") from error
        try:
            directory_fd = os.open(
                relative_parts[-1],
                _DIRECTORY_FLAGS,
                dir_fd=parent_fd,
            )
        except OSError as error:
            raise OutputPathError("output directory could not be opened safely") from error
        if created:
            if (
                created_identity is None
                or _directory_identity(os.fstat(directory_fd)) != created_identity
            ):
                raise OutputPathError("created output directory was replaced before opening")
            os.fchmod(directory_fd, 0o700)
            _validate_owner_only_directory(directory_fd)
        else:
            try:
                _validate_owner_only_directory(directory_fd)
                if _directory_has_entry(directory_fd):
                    raise OutputAlreadyExistsError("output directory is not empty")
            except OutputAlreadyExistsError:
                raise
            except (OSError, ValueError) as error:
                raise OutputPathError("existing output directory is unsafe") from error
        identity = _directory_identity(os.fstat(directory_fd))
        return ControlledOutputDirectory(
            path=path,
            root_path=root,
            root_fd=root_fd,
            root_identity=_directory_identity(os.fstat(root_fd)),
            directory_fd=directory_fd,
            relative_parts=relative_parts,
            identity=identity,
            created_by_service=created,
        )
    except (OSError, ValueError):
        if created and parent_fd is not None and created_identity is not None:
            _remove_named_empty_directory_if_identity(
                parent_fd,
                relative_parts[-1],
                created_identity,
            )
        if directory_fd is not None:
            os.close(directory_fd)
        if root_fd is not None:
            os.close(root_fd)
        raise
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def publish_artifacts(
    *,
    raw_output: Path,
    output_directory: ControlledOutputDirectory,
    report: str,
    max_output_bytes: int,
) -> tuple[Path, Path, int]:
    """Validate detailed CSV and atomically publish both stable named artifacts."""
    annotations, output_bytes = _validated_detailed_csv(raw_output, max_output_bytes)
    report_bytes = report.encode("utf-8")
    if not report_bytes or len(report_bytes) > _MAX_REPORT_BYTES:
        raise OutputValidationError("run report exceeds the bounded size")
    directory_fd = _open_controlled_directory(output_directory)
    installed_identities: list[tuple[str, tuple[int, int, int, int, int]]] = []
    try:
        if _directory_has_entry(directory_fd):
            raise OutputValidationError("stable output directory is no longer empty")
        installed_identities.append(
            (
                ANNOTATIONS_FILENAME,
                _write_noreplace(directory_fd, ANNOTATIONS_FILENAME, annotations),
            )
        )
        installed_identities.append(
            (
                RUN_REPORT_FILENAME,
                _write_noreplace(directory_fd, RUN_REPORT_FILENAME, report_bytes),
            )
        )
        os.fsync(directory_fd)
        identities = _capture_delivered_identities(directory_fd, max_output_bytes)
        if identities != tuple(installed_identities):
            raise OutputValidationError("stable artifacts changed during publication")
        verification_fd = _open_controlled_directory(output_directory)
        try:
            if _capture_delivered_identities(verification_fd, max_output_bytes) != identities:
                raise OutputValidationError("stable artifacts changed during publication")
        finally:
            os.close(verification_fd)
        postcondition_fd = _open_controlled_directory(output_directory)
        os.close(postcondition_fd)
        output_directory.delivered_identities = identities
    except OutputValidationError:
        _rollback_published_artifacts(directory_fd, tuple(installed_identities))
        raise
    except (OSError, UnicodeError, csv.Error, ValueError) as error:
        _rollback_published_artifacts(directory_fd, tuple(installed_identities))
        raise OutputValidationError("stable output publication failed") from error
    finally:
        os.close(directory_fd)
    return (
        output_directory.path / ANNOTATIONS_FILENAME,
        output_directory.path / RUN_REPORT_FILENAME,
        output_bytes,
    )


def validate_delivered_artifacts(
    output_directory: ControlledOutputDirectory,
    *,
    max_output_bytes: int,
) -> None:
    """Fail closed if either stable artifact was removed or replaced."""
    directory_fd = _open_controlled_directory(output_directory)
    try:
        metadata = _validate_delivered_directory(directory_fd, output_directory)
        if (
            metadata[ANNOTATIONS_FILENAME].st_size > max_output_bytes
            or metadata[RUN_REPORT_FILENAME].st_size > _MAX_REPORT_BYTES
        ):
            raise OutputValidationError("delivered artifact exceeds its size bound")
    finally:
        os.close(directory_fd)


def read_artifact_slice(
    output_directory: ControlledOutputDirectory,
    artifact_name: str,
    *,
    max_bytes: int,
    offset: int,
    limit: int,
) -> ArtifactSlice:
    """Read one bounded byte range from a direct stable regular file."""
    if offset < 0 or not 1 <= limit <= MAX_RESOURCE_PAGE_BYTES:
        raise OutputValidationError("artifact range is outside the supported bounds")
    directory_fd = _open_controlled_directory(output_directory)
    try:
        metadata = _validate_delivered_directory(directory_fd, output_directory)
        named = metadata.get(artifact_name)
        if named is None or named.st_size > max_bytes:
            raise OutputValidationError("artifact is unavailable or outside its size bound")
        if offset >= named.st_size:
            raise OutputValidationError("artifact offset is outside the file")
        descriptor = os.open(
            artifact_name,
            _FILE_READ_FLAGS,
            dir_fd=directory_fd,
        )
        try:
            before = os.fstat(descriptor)
            expected = _expected_artifact_identity(output_directory, artifact_name)
            if (
                _file_identity(named) != _file_identity(before)
                or _file_identity(before) != expected
            ):
                raise OutputValidationError("artifact changed before reading")
            content = os.pread(descriptor, min(limit, before.st_size - offset), offset)
            after = os.fstat(descriptor)
            if _file_identity(after) != expected:
                raise OutputValidationError("artifact changed while reading")
            return ArtifactSlice(content=content, total_bytes=before.st_size)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise OutputValidationError("artifact could not be read safely") from error
    finally:
        os.close(directory_fd)


def artifact_size(
    output_directory: ControlledOutputDirectory,
    artifact_name: str,
    *,
    max_bytes: int,
) -> int:
    """Return the validated bounded size of one stable artifact."""
    directory_fd = _open_controlled_directory(output_directory)
    try:
        named = _validate_delivered_directory(directory_fd, output_directory).get(artifact_name)
        if named is None or named.st_size > max_bytes:
            raise OutputValidationError("artifact is unavailable or outside its size bound")
        return named.st_size
    finally:
        os.close(directory_fd)


def cleanup_output_directory(output_directory: ControlledOutputDirectory) -> None:
    """Remove known files and remove the directory only when the companion created it."""
    directory_fd = _open_controlled_directory(output_directory)
    parent_fd: int | None = None
    try:
        if output_directory.created_by_service:
            parent_fd = _open_controlled_parent(output_directory)
        names = _bounded_output_names(directory_fd)
        if output_directory.delivered_identities:
            _validate_delivered_directory(directory_fd, output_directory)
            for name in _DELIVERED_ARTIFACTS:
                if not _unlink_matching_identity(
                    directory_fd,
                    name,
                    _expected_artifact_identity(output_directory, name),
                ):
                    raise OutputValidationError("delivered artifact changed during cleanup")
        elif names:
            raise ValueError("controlled output directory changed before publication")
        if parent_fd is not None:
            _require_named_directory_identity(parent_fd, output_directory)
            os.rmdir(output_directory.relative_parts[-1], dir_fd=parent_fd)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(directory_fd)


def close_output_directory(output_directory: ControlledOutputDirectory) -> None:
    """Release pinned descriptors without deleting delivered stable files."""
    if output_directory.directory_fd >= 0:
        descriptor = output_directory.directory_fd
        output_directory.directory_fd = -1
        os.close(descriptor)
    if output_directory.root_fd >= 0:
        descriptor = output_directory.root_fd
        output_directory.root_fd = -1
        os.close(descriptor)


def _validated_detailed_csv(path: Path, max_bytes: int) -> tuple[bytes, int]:
    named = _artifact_metadata(path, max_bytes)
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        before = os.fstat(descriptor)
        if _file_identity(named) != _file_identity(before):
            raise OutputValidationError("DeepKOALA output changed before validation")
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) > max_bytes or _file_identity(before) != _file_identity(after):
            raise OutputValidationError("DeepKOALA output changed or exceeded its limit")
    except OSError as error:
        raise OutputValidationError("DeepKOALA output could not be read safely") from error
    finally:
        os.close(descriptor)

    try:
        text = bytes(content).decode("utf-8-sig")
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader)
        if (
            not header
            or len(header) != len(set(header))
            or not _REQUIRED_COLUMNS.issubset(header)
            or (("start" in header) != ("end" in header))
        ):
            raise OutputValidationError("DeepKOALA detailed CSV has an unsupported header")
        indexes = {name: header.index(name) for name in _REQUIRED_COLUMNS}
        coordinate_indexes = (
            (header.index("start"), header.index("end")) if "start" in header else None
        )
        rows = 0
        for row in reader:
            if len(row) != len(header):
                raise OutputValidationError("DeepKOALA detailed CSV has a malformed row")
            rows += 1
            if not row[indexes["name"]]:
                raise OutputValidationError("DeepKOALA detailed CSV has a missing identifier")
            prediction = row[indexes["predict_label"]]
            if not prediction:
                coordinates = (
                    []
                    if coordinate_indexes is None
                    else [row[coordinate_indexes[0]], row[coordinate_indexes[1]]]
                )
                if any(
                    (
                        row[indexes["probability"]],
                        row[indexes["threshold"]],
                        row[indexes["annotate"]],
                        *coordinates,
                    )
                ):
                    raise OutputValidationError(
                        "DeepKOALA unclassified rows must have empty evidence"
                    )
                continue
            probability = _bounded_probability(row[indexes["probability"]])
            threshold = _bounded_probability(row[indexes["threshold"]])
            if probability is None or threshold is None:
                raise OutputValidationError("DeepKOALA detailed CSV has invalid score evidence")
            if row[indexes["annotate"]] not in {"", "*"}:
                raise OutputValidationError("DeepKOALA detailed CSV has an invalid marker")
            if coordinate_indexes is not None:
                start_value = row[coordinate_indexes[0]]
                end_value = row[coordinate_indexes[1]]
                if bool(start_value) != bool(end_value):
                    raise OutputValidationError(
                        "DeepKOALA detailed CSV has incomplete domain coordinates"
                    )
                if start_value:
                    start = _bounded_coordinate(start_value)
                    end = _bounded_coordinate(end_value)
                    if start is None or end is None or start > end:
                        raise OutputValidationError(
                            "DeepKOALA detailed CSV has invalid domain coordinates"
                        )
        if rows == 0:
            raise OutputValidationError("DeepKOALA detailed CSV has no prediction rows")
    except (UnicodeError, csv.Error, StopIteration, ValueError) as error:
        if isinstance(error, OutputValidationError):
            raise
        raise OutputValidationError("DeepKOALA detailed CSV is malformed") from error
    return bytes(content), named.st_size


def _bounded_probability(value: str) -> float | None:
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) and 0.0 <= parsed <= 1.0 else None


def _bounded_coordinate(value: str) -> int | None:
    if not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if 1 <= parsed <= 100_000 else None


def _write_noreplace(
    directory_fd: int,
    name: str,
    content: bytes,
) -> tuple[int, int, int, int, int]:
    temporary = f".deepkoala-{secrets.token_hex(16)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    linked = False
    temporary_exists = True
    linked_inode: tuple[int, int] | None = None
    identity: tuple[int, int, int, int, int] | None = None
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(
            temporary,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        linked = True
        linked_metadata = os.fstat(descriptor)
        linked_inode = (linked_metadata.st_dev, linked_metadata.st_ino)
        os.unlink(temporary, dir_fd=directory_fd)
        temporary_exists = False
        identity = _file_identity(os.fstat(descriptor))
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_identity(named) != identity:
            raise OutputValidationError("stable artifact changed during publication")
        return identity
    except BaseException:
        if linked:
            if identity is not None:
                _unlink_matching_identity(directory_fd, name, identity)
            elif linked_inode is not None:
                _unlink_matching_inode(directory_fd, name, linked_inode)
        raise
    finally:
        os.close(descriptor)
        if temporary_exists:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory_fd)


def _artifact_metadata(path: Path, max_bytes: int) -> os.stat_result:
    try:
        named = path.lstat()
    except OSError as error:
        raise OutputValidationError("artifact is unavailable") from error
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or named.st_uid != os.geteuid()
        or not 1 <= named.st_size <= max_bytes
    ):
        raise OutputValidationError("artifact is unsafe or outside its size bound")
    return named


def _prepare_state_root(path: Path) -> Path:
    created = False
    if not os.path.lexists(path):
        try:
            path.mkdir(parents=True, mode=0o700)
            created = True
        except FileExistsError:
            pass
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("state root must be a direct directory")
    resolved = path.resolve(strict=True)
    if resolved != path:
        raise ValueError("state root must not contain symlinks")
    metadata = resolved.lstat()
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
        if created:
            os.chmod(resolved, 0o700)
        else:
            raise ValueError("existing state root must be owner-only and owned by this user")
    return resolved


def open_state_session(path: Path) -> StateSession:
    """Create one leased session while briefly coordinating the shared state root."""
    root = _prepare_state_root(path)
    root_fd = os.open(root, _DIRECTORY_FLAGS)
    coordination_fd: int | None = None
    session_name: str | None = None
    session_fd: int | None = None
    lease_fd: int | None = None
    try:
        _validate_owner_only_directory(root_fd)
        coordination_fd = _acquire_coordination_lock(root_fd)
        active_sessions = cleanup_abandoned_sessions(root_fd)
        if active_sessions >= _MAX_SESSIONS:
            raise ValueError("state root reached the fixed session limit")
        for _ in range(4):
            candidate = f"session_{secrets.token_hex(16)}"
            try:
                os.mkdir(candidate, mode=0o700, dir_fd=root_fd)
            except FileExistsError:
                continue
            session_name = candidate
            break
        if session_name is None:
            raise ValueError("could not allocate a unique private session")
        session_fd = os.open(session_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
        os.fchmod(session_fd, 0o700)
        _validate_owner_only_directory(session_fd)
        lease_fd = _create_lock_file(session_fd, _SESSION_LOCK_NAME)
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _validate_named_lock(session_fd, _SESSION_LOCK_NAME, lease_fd)
        session = root / session_name
        state = StateSession(
            root=root,
            root_fd=root_fd,
            session=session,
            session_fd=session_fd,
            session_identity=_directory_identity(os.fstat(session_fd)),
            lease_fd=lease_fd,
            lease_identity=_lock_identity(os.fstat(lease_fd)),
        )
        _release_lock(coordination_fd)
        coordination_fd = None
        return state
    except (OSError, ValueError):
        if session_name is not None:
            with contextlib.suppress(OSError, ValueError):
                if session_fd is not None and lease_fd is not None:
                    _remove_session_contents(
                        root_fd,
                        session_name,
                        session_fd,
                        lease_fd,
                        reject_file_symlinks=False,
                    )
                else:
                    os.rmdir(session_name, dir_fd=root_fd)
        if lease_fd is not None:
            _release_lock(lease_fd)
        if session_fd is not None:
            os.close(session_fd)
        if coordination_fd is not None:
            _release_lock(coordination_fd)
        os.close(root_fd)
        raise ValueError("state root session could not be opened safely") from None


def close_state_session(state: StateSession) -> None:
    """Remove only this process session and release every pinned descriptor."""
    cleanup_error: Exception | None = None
    coordination_fd: int | None = None
    try:
        coordination_fd = _acquire_coordination_lock(state.root_fd)
        _remove_session_contents(
            state.root_fd,
            state.session.name,
            state.session_fd,
            state.lease_fd,
            reject_file_symlinks=False,
            expected_session_identity=state.session_identity,
            expected_lease_identity=state.lease_identity,
        )
    except (OSError, ValueError) as error:
        cleanup_error = error
    finally:
        if coordination_fd is not None:
            _release_lock(coordination_fd)
        _release_lock(state.lease_fd)
        os.close(state.session_fd)
        os.close(state.root_fd)
    if cleanup_error is not None:
        raise ValueError("private session cleanup failed") from cleanup_error


def cleanup_abandoned_sessions(directory_fd: int) -> int:
    """Remove unlocked strict sessions and return the number of live sessions."""
    names = _bounded_names(
        directory_fd,
        maximum=_MAX_STATE_ROOT_ENTRIES,
        message="state root exceeds the fixed entry bound",
    )
    allowed_locks = {_COORDINATION_LOCK_NAME, _RUNNER_LOCK_NAME}
    session_names: list[str] = []
    for name in names:
        if name in allowed_locks:
            lock_fd = _open_existing_lock(directory_fd, name)
            os.close(lock_fd)
        elif _SESSION.fullmatch(name) is not None:
            session_names.append(name)
        else:
            raise ValueError("state root contains an unexpected entry")
    if len(session_names) > _MAX_SESSIONS:
        raise ValueError("state root exceeds the fixed session bound")
    live_sessions = 0
    for name in session_names:
        try:
            removed = _remove_abandoned_session(directory_fd, name)
        except OSError as error:
            raise ValueError("abandoned session state is unsafe") from error
        if not removed:
            live_sessions += 1
    return live_sessions


def try_acquire_runner_lock(state: StateSession) -> int | None:
    """Acquire the deployment runner lease without staging any job state."""
    coordination_fd = _acquire_coordination_lock(state.root_fd)
    runner_fd: int | None = None
    try:
        runner_fd = _open_or_create_lock(state.root_fd, _RUNNER_LOCK_NAME)
        try:
            fcntl.flock(runner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(runner_fd)
            runner_fd = None
            return None
        _validate_named_lock(state.root_fd, _RUNNER_LOCK_NAME, runner_fd)
        return runner_fd
    except BaseException:
        if runner_fd is not None:
            os.close(runner_fd)
        raise
    finally:
        _release_lock(coordination_fd)


def release_runner_lock(lock_fd: int) -> None:
    """Release one acquired deployment runner lease."""
    _release_lock(lock_fd)


def remove_job_directory(directory: Path, state: StateSession) -> None:
    if directory.parent != state.session or _JOB.fullmatch(directory.name) is None:
        raise ValueError("invalid controlled job directory")
    _validate_owner_only_directory(state.session_fd)
    _validate_named_session(
        state.root_fd,
        state.session.name,
        state.session_fd,
        state.session_identity,
    )
    if not _entry_exists(state.session_fd, directory.name):
        return
    _remove_job_name(state.session_fd, directory.name, reject_file_symlinks=False)


def _remove_abandoned_session(root_fd: int, session_name: str) -> bool:
    if _SESSION.fullmatch(session_name) is None:
        raise ValueError("invalid controlled session directory")
    session_fd = os.open(session_name, _DIRECTORY_FLAGS, dir_fd=root_fd)
    lease_fd: int | None = None
    try:
        _validate_owner_only_directory(session_fd)
        lease_fd = _open_existing_lock(session_fd, _SESSION_LOCK_NAME)
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        _validate_named_lock(session_fd, _SESSION_LOCK_NAME, lease_fd)
        _remove_session_contents(
            root_fd,
            session_name,
            session_fd,
            lease_fd,
            reject_file_symlinks=True,
        )
        return True
    finally:
        if lease_fd is not None:
            _release_lock(lease_fd)
        os.close(session_fd)


def _remove_session_contents(
    root_fd: int,
    session_name: str,
    session_fd: int,
    lease_fd: int,
    *,
    reject_file_symlinks: bool,
    expected_session_identity: tuple[int, int, int] | None = None,
    expected_lease_identity: tuple[int, int, int] | None = None,
) -> None:
    if _SESSION.fullmatch(session_name) is None:
        raise ValueError("invalid controlled session directory")
    _validate_owner_only_directory(session_fd)
    _validate_named_session(root_fd, session_name, session_fd, expected_session_identity)
    _validate_named_lock(session_fd, _SESSION_LOCK_NAME, lease_fd)
    observed_lease_identity = _lock_identity(os.fstat(lease_fd))
    if expected_lease_identity is not None and observed_lease_identity != expected_lease_identity:
        raise ValueError("controlled session lease was replaced")
    names = _bounded_names(
        session_fd,
        maximum=_MAX_SESSION_ENTRIES,
        message="controlled session exceeds the bounded job count",
    )
    if _SESSION_LOCK_NAME not in names:
        raise ValueError("controlled session lease is unavailable")
    job_names = tuple(name for name in names if name != _SESSION_LOCK_NAME)
    if any(_JOB.fullmatch(name) is None for name in job_names):
        raise ValueError("controlled session contains an unexpected entry")
    for job_name in job_names:
        _remove_job_name(session_fd, job_name, reject_file_symlinks=reject_file_symlinks)
    _validate_named_session(root_fd, session_name, session_fd, expected_session_identity)
    _validate_named_lock(session_fd, _SESSION_LOCK_NAME, lease_fd)
    os.unlink(_SESSION_LOCK_NAME, dir_fd=session_fd)
    if _bounded_names(
        session_fd,
        maximum=1,
        message="controlled session changed during cleanup",
    ):
        raise ValueError("controlled session changed during cleanup")
    _validate_named_session(root_fd, session_name, session_fd, expected_session_identity)
    os.rmdir(session_name, dir_fd=root_fd)


def _validate_named_session(
    root_fd: int,
    session_name: str,
    session_fd: int,
    expected_identity: tuple[int, int, int] | None,
) -> None:
    named = os.stat(session_name, dir_fd=root_fd, follow_symlinks=False)
    observed_identity = _directory_identity(os.fstat(session_fd))
    if (
        not stat.S_ISDIR(named.st_mode)
        or _directory_identity(named) != observed_identity
        or (expected_identity is not None and observed_identity != expected_identity)
    ):
        raise ValueError("controlled session directory was replaced")


def _remove_job_name(session_fd: int, job_name: str, *, reject_file_symlinks: bool) -> None:
    if _JOB.fullmatch(job_name) is None:
        raise ValueError("invalid controlled job directory")
    job_fd = os.open(job_name, _DIRECTORY_FLAGS, dir_fd=session_fd)
    try:
        _validate_owner_only_directory(job_fd)
        _validate_named_job(session_fd, job_name, job_fd)
        names = _bounded_names(
            job_fd,
            maximum=_MAX_JOB_FILES,
            message="controlled job directory exceeds the file bound",
        )
        for name in names:
            metadata = os.stat(name, dir_fd=job_fd, follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode) and not reject_file_symlinks:
                os.unlink(name, dir_fd=job_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
                raise ValueError("controlled job directory contains an unsafe entry")
            os.unlink(name, dir_fd=job_fd)
        _validate_named_job(session_fd, job_name, job_fd)
        os.rmdir(job_name, dir_fd=session_fd)
    finally:
        os.close(job_fd)


def _validate_named_job(session_fd: int, job_name: str, job_fd: int) -> None:
    named = os.stat(job_name, dir_fd=session_fd, follow_symlinks=False)
    opened_identity = _directory_identity(os.fstat(job_fd))
    if not stat.S_ISDIR(named.st_mode) or _directory_identity(named) != opened_identity:
        raise ValueError("controlled job directory was replaced")


def _acquire_coordination_lock(root_fd: int) -> int:
    lock_fd = _open_or_create_lock(root_fd, _COORDINATION_LOCK_NAME)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _validate_named_lock(root_fd, _COORDINATION_LOCK_NAME, lock_fd)
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def _create_lock_file(directory_fd: int, name: str) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    lock_fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(lock_fd, 0o600)
        _validate_named_lock(directory_fd, name, lock_fd)
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def _open_or_create_lock(directory_fd: int, name: str) -> int:
    try:
        return _create_lock_file(directory_fd, name)
    except FileExistsError:
        return _open_existing_lock(directory_fd, name)


def _open_existing_lock(directory_fd: int, name: str) -> int:
    flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    lock_fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        _validate_named_lock(directory_fd, name, lock_fd)
        return lock_fd
    except BaseException:
        os.close(lock_fd)
        raise


def _validate_named_lock(directory_fd: int, name: str, lock_fd: int) -> None:
    opened = os.fstat(lock_fd)
    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or opened.st_uid != os.geteuid()
        or named.st_uid != os.geteuid()
        or stat.S_IMODE(opened.st_mode) != 0o600
        or stat.S_IMODE(named.st_mode) != 0o600
        or _lock_identity(opened) != _lock_identity(named)
    ):
        raise ValueError("state lock must be a stable owner-only regular file")


def _bounded_names(directory_fd: int, *, maximum: int, message: str) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            if len(names) >= maximum:
                raise ValueError(message)
            names.append(entry.name)
    return tuple(names)


def _release_lock(lock_fd: int) -> None:
    os.close(lock_fd)


def _open_output_root(path: Path) -> int:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise OutputPathError("output root could not be opened safely") from error
    try:
        _validate_output_ancestor(descriptor)
    except (OSError, ValueError) as error:
        os.close(descriptor)
        raise OutputPathError("output root is not a private user-owned directory") from error
    return descriptor


def _open_directory_parts(root_fd: int, parts: tuple[str, ...]) -> int:
    current = os.dup(root_fd)
    try:
        _validate_output_ancestor(current)
        for component in parts:
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = next_descriptor
            _validate_output_ancestor(current)
        return current
    except BaseException:
        os.close(current)
        raise


def _open_controlled_directory(output_directory: ControlledOutputDirectory) -> int:
    if output_directory.root_fd < 0 or output_directory.directory_fd < 0:
        raise OutputValidationError("controlled output directory is no longer retained")
    root_fd: int | None = None
    descriptor: int | None = None
    try:
        pinned = os.fstat(output_directory.directory_fd)
        _validate_owner_only_directory(output_directory.directory_fd)
        if _directory_identity(pinned) != output_directory.identity:
            raise ValueError("pinned output directory identity changed")
        root_fd = _open_named_root(output_directory)
        descriptor = _open_directory_parts(
            root_fd,
            output_directory.relative_parts,
        )
        _validate_owner_only_directory(descriptor)
        if _directory_identity(os.fstat(descriptor)) != output_directory.identity:
            raise ValueError("named output directory was replaced")
        return descriptor
    except (OSError, ValueError) as error:
        if descriptor is not None:
            os.close(descriptor)
        raise OutputValidationError(
            "controlled output directory changed or became unsafe"
        ) from error
    finally:
        if root_fd is not None:
            os.close(root_fd)


def _open_controlled_parent(output_directory: ControlledOutputDirectory) -> int:
    if output_directory.root_fd < 0:
        raise OutputValidationError("controlled output directory is no longer retained")
    root_fd: int | None = None
    try:
        root_fd = _open_named_root(output_directory)
        descriptor = _open_directory_parts(
            root_fd,
            output_directory.relative_parts[:-1],
        )
    except (OSError, ValueError) as error:
        raise OutputValidationError("controlled output parent changed or became unsafe") from error
    finally:
        if root_fd is not None:
            os.close(root_fd)
    try:
        _require_named_directory_identity(descriptor, output_directory)
    except (OSError, ValueError, OutputValidationError):
        os.close(descriptor)
        raise
    return descriptor


def _open_named_root(output_directory: ControlledOutputDirectory) -> int:
    descriptor = os.open(output_directory.root_path, _DIRECTORY_FLAGS)
    try:
        _validate_output_ancestor(descriptor)
        pinned = os.fstat(output_directory.root_fd)
        if (
            _directory_identity(pinned) != output_directory.root_identity
            or _directory_identity(os.fstat(descriptor)) != output_directory.root_identity
        ):
            raise ValueError("configured output root was replaced")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _require_named_directory_identity(
    parent_fd: int,
    output_directory: ControlledOutputDirectory,
) -> None:
    try:
        metadata = os.stat(
            output_directory.relative_parts[-1],
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise OutputValidationError("controlled output directory is unavailable") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or _directory_identity(metadata) != output_directory.identity
    ):
        raise OutputValidationError("controlled output directory was replaced")


def _validate_output_ancestor(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise ValueError("output ancestors must be user-owned and not group-writable")


def _directory_has_entry(directory_fd: int) -> bool:
    with os.scandir(directory_fd) as entries:
        return next(entries, None) is not None


def _bounded_output_names(directory_fd: int) -> tuple[str, ...]:
    names: list[str] = []
    with os.scandir(directory_fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > _MAX_OUTPUT_DIRECTORY_ENTRIES:
                raise ValueError("controlled output directory exceeds the entry bound")
    return tuple(names)


def _capture_delivered_identities(
    directory_fd: int,
    max_output_bytes: int,
) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
    try:
        names = _bounded_output_names(directory_fd)
    except ValueError as error:
        raise OutputValidationError("stable output directory has unexpected entries") from error
    if len(names) != len(_DELIVERED_ARTIFACTS) or set(names) != set(_DELIVERED_ARTIFACTS):
        raise OutputValidationError("stable output directory must contain exactly two artifacts")
    maximums = {
        ANNOTATIONS_FILENAME: max_output_bytes,
        RUN_REPORT_FILENAME: _MAX_REPORT_BYTES,
    }
    return tuple(
        (name, _file_identity(_artifact_metadata_at(directory_fd, name, maximums[name])))
        for name in _DELIVERED_ARTIFACTS
    )


def _validate_delivered_directory(
    directory_fd: int,
    output_directory: ControlledOutputDirectory,
) -> dict[str, os.stat_result]:
    try:
        names = _bounded_output_names(directory_fd)
    except ValueError as error:
        raise OutputValidationError("delivered output directory has unexpected entries") from error
    if len(names) != len(_DELIVERED_ARTIFACTS) or set(names) != set(_DELIVERED_ARTIFACTS):
        raise OutputValidationError(
            "delivered output directory no longer has exactly two artifacts"
        )
    metadata: dict[str, os.stat_result] = {}
    for name in _DELIVERED_ARTIFACTS:
        expected = _expected_artifact_identity(output_directory, name)
        named = _artifact_metadata_at(directory_fd, name, expected[2])
        if _file_identity(named) != expected:
            raise OutputValidationError("delivered artifact was replaced or changed")
        metadata[name] = named
    return metadata


def _expected_artifact_identity(
    output_directory: ControlledOutputDirectory,
    artifact_name: str,
) -> tuple[int, int, int, int, int]:
    for name, identity in output_directory.delivered_identities:
        if name == artifact_name:
            return identity
    raise OutputValidationError("artifact has no retained publication identity")


def _rollback_published_artifacts(
    directory_fd: int,
    installed_identities: tuple[tuple[str, tuple[int, int, int, int, int]], ...],
) -> None:
    for name, identity in reversed(installed_identities):
        _unlink_matching_identity(directory_fd, name, identity)


def _unlink_matching_identity(
    directory_fd: int,
    name: str,
    identity: tuple[int, int, int, int, int],
) -> bool:
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if _file_identity(named) == identity:
            os.unlink(name, dir_fd=directory_fd)
            return True
    except OSError:
        return False
    return False


def _unlink_matching_inode(
    directory_fd: int,
    name: str,
    identity: tuple[int, int],
) -> bool:
    try:
        named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) == identity:
            os.unlink(name, dir_fd=directory_fd)
            return True
    except OSError:
        return False
    return False


def _artifact_metadata_at(
    directory_fd: int,
    artifact_name: str,
    max_bytes: int,
) -> os.stat_result:
    if artifact_name not in {ANNOTATIONS_FILENAME, RUN_REPORT_FILENAME}:
        raise OutputValidationError("artifact name is not supported")
    try:
        named = os.stat(artifact_name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise OutputValidationError("artifact is unavailable") from error
    if (
        stat.S_ISLNK(named.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or named.st_uid != os.geteuid()
        or not 1 <= named.st_size <= max_bytes
    ):
        raise OutputValidationError("artifact is unsafe or outside its size bound")
    return named


def _validate_owner_only_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("controlled directory must be owner-only")


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_uid


def _remove_named_empty_directory_if_identity(
    parent_fd: int,
    name: str,
    identity: tuple[int, int, int],
) -> bool:
    """Best-effort rmdir for an unchanged named directory; rmdir enforces emptiness."""
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or _directory_identity(metadata) != identity:
            return False
        os.rmdir(name, dir_fd=parent_fd)
        return True
    except OSError:
        return False


def _lock_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_uid


def _entry_exists(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


__all__ = [
    "ArtifactSlice",
    "ControlledOutputDirectory",
    "OutputAlreadyExistsError",
    "OutputPathError",
    "OutputValidationError",
    "StateSession",
    "artifact_size",
    "cleanup_abandoned_sessions",
    "cleanup_output_directory",
    "close_output_directory",
    "close_state_session",
    "create_output_directory",
    "open_state_session",
    "publish_artifacts",
    "read_artifact_slice",
    "release_runner_lock",
    "remove_job_directory",
    "try_acquire_runner_lock",
    "validate_delivered_artifacts",
]
