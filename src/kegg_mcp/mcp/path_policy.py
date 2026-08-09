"""Allowed-root validation and race-resistant annotation-file materialization."""

from __future__ import annotations

import io
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, NoReturn

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError, SafeDetail
from kegg_mcp.importers import SourceProvenanceInput
from kegg_mcp.services.models import NormalizeAnnotationsRequest


class _UnsafeInputFile(Exception):
    """Internal marker for a path race or unsafe filesystem type."""


class _InputFileLimit(Exception):
    """Internal marker carrying a bounded observed file size."""

    def __init__(self, actual_bytes: int) -> None:
        super().__init__("annotation file exceeds the configured limit")
        self.actual_bytes = actual_bytes


@dataclass(frozen=True, slots=True)
class PinnedAnnotationFile:
    """One regular file held by an unchanged no-follow descriptor walk."""

    path: Path
    stream: io.BufferedReader
    byte_size: int


@dataclass(frozen=True, slots=True)
class _PinnedDescriptor:
    descriptor: int
    path: Path
    byte_size: int


class _BoundedDescriptorReader(io.RawIOBase):
    """Read a pinned descriptor without taking ownership or exceeding the byte limit."""

    def __init__(self, descriptor: int, max_bytes: int) -> None:
        super().__init__()
        self._descriptor = descriptor
        self._max_bytes = max_bytes
        self._bytes_read = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        remaining_with_sentinel = self._max_bytes + 1 - self._bytes_read
        if remaining_with_sentinel <= 0:
            raise _InputFileLimit(self._bytes_read)
        requested = min(len(buffer), 65_536, remaining_with_sentinel)
        chunk = os.read(self._descriptor, requested)
        if not chunk:
            return 0
        self._bytes_read += len(chunk)
        if self._bytes_read > self._max_bytes:
            raise _InputFileLimit(self._bytes_read)
        buffer[: len(chunk)] = chunk
        return len(chunk)


def materialize_annotation_file(
    request: NormalizeAnnotationsRequest,
    allowed_roots: tuple[str, ...],
) -> NormalizeAnnotationsRequest:
    """Read one direct regular file through a bounded no-follow descriptor walk."""
    if request.file_path is None:
        return request
    try:
        content, path = _read_allowed_file(
            request.file_path,
            allowed_roots,
            max_bytes=request.import_limits.max_bytes,
        )
    except _InputFileLimit as error:
        _raise_annotation_file_limit(error, request.import_limits.max_bytes)
    except (OSError, _UnsafeInputFile):
        _raise_unsafe_annotation_file()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise KeggMcpError(
            ErrorDetail(
                code=ErrorCode.UNSUPPORTED_INPUT_FORMAT,
                message="The annotation file is not valid UTF-8 text.",
                recoverable=True,
                suggested_action="Convert the file to UTF-8 and retry.",
            )
        ) from None
    source = bind_annotation_file_source(
        request.source,
        requested_path=request.file_path,
        resolved_path=path,
        allowed_roots=allowed_roots,
        default_source_name="file_handoff",
    )
    return request.model_copy(
        update={
            "text": text,
            "file_path": None,
            "source": source,
        }
    )


@contextmanager
def open_annotation_file_stream(
    value: str,
    allowed_roots: tuple[str, ...],
    *,
    max_bytes: int,
) -> Iterator[PinnedAnnotationFile]:
    """Yield a bounded stream while retaining and revalidating the opened file descriptor."""
    try:
        with _open_allowed_file_descriptor(
            value,
            allowed_roots,
            max_bytes=max_bytes,
        ) as pinned:
            raw_stream = _BoundedDescriptorReader(pinned.descriptor, max_bytes)
            buffered_stream = io.BufferedReader(raw_stream, buffer_size=65_536)
            try:
                yield PinnedAnnotationFile(
                    path=pinned.path,
                    stream=buffered_stream,
                    byte_size=pinned.byte_size,
                )
            finally:
                buffered_stream.close()
    except _InputFileLimit as error:
        _raise_annotation_file_limit(error, max_bytes)
    except (OSError, _UnsafeInputFile):
        _raise_unsafe_annotation_file()


def bind_annotation_file_source(
    source: SourceProvenanceInput | None,
    *,
    requested_path: str,
    resolved_path: Path,
    allowed_roots: tuple[str, ...],
    default_source_name: str,
) -> SourceProvenanceInput:
    """Bind provenance to one validated handoff path without rewriting another source path."""
    bound = source or SourceProvenanceInput(
        source_name=default_source_name,
        input_path=str(resolved_path),
    )
    if bound.input_path is None:
        return bound
    source_path = (
        str(resolved_path)
        if Path(bound.input_path) == Path(requested_path)
        else str(resolve_existing_file(bound.input_path, allowed_roots))
    )
    return bound.model_copy(update={"input_path": source_path})


def resolve_existing_file(value: str, allowed_roots: tuple[str, ...]) -> Path:
    """Validate one direct regular file without reading its payload."""
    try:
        _, path = _access_allowed_file(value, allowed_roots, max_bytes=None)
        return path
    except (OSError, _UnsafeInputFile):
        _raise_disallowed_path("file_path")


def resolve_output_directory(
    value: str | None,
    allowed_roots: tuple[str, ...],
    *,
    default_prefix: str | None = None,
) -> Path | None:
    """Resolve a new or existing output directory below one private allowed root."""
    if value is None:
        if default_prefix is None or not allowed_roots:
            return None
        if not default_prefix.isascii() or not default_prefix.replace("-", "").isalnum():
            raise AssertionError("default output prefix must be an ASCII name component")
        value = str(Path(allowed_roots[-1]) / f"{default_prefix}-{secrets.token_hex(16)}")
    candidate = Path(value)
    root = _select_allowed_root(candidate, allowed_roots, field="output_directory")
    _validate_allowed_root(root, field="output_directory")
    current = root
    _validate_private_output_ancestor(current)
    for component in candidate.relative_to(root).parts:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return candidate
        except OSError:
            _raise_disallowed_path("output_directory")
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            _raise_disallowed_path("output_directory")
        _validate_private_output_ancestor(current)
    return candidate


def _read_allowed_file(
    value: str,
    allowed_roots: tuple[str, ...],
    *,
    max_bytes: int,
) -> tuple[bytes, Path]:
    content, path = _access_allowed_file(value, allowed_roots, max_bytes=max_bytes)
    if content is None:
        raise AssertionError("bounded file intake returned no content")
    return content, path


def _access_allowed_file(
    value: str,
    allowed_roots: tuple[str, ...],
    *,
    max_bytes: int | None,
) -> tuple[bytes | None, Path]:
    with _open_allowed_file_descriptor(
        value,
        allowed_roots,
        max_bytes=max_bytes,
    ) as pinned:
        if max_bytes is None:
            return None, pinned.path
        buffered = bytearray()
        while len(buffered) <= max_bytes:
            chunk = os.read(
                pinned.descriptor,
                min(65_536, max_bytes + 1 - len(buffered)),
            )
            if not chunk:
                break
            buffered.extend(chunk)
        if len(buffered) > max_bytes:
            raise _InputFileLimit(len(buffered))
        return bytes(buffered), pinned.path


@contextmanager
def _open_allowed_file_descriptor(
    value: str,
    allowed_roots: tuple[str, ...],
    *,
    max_bytes: int | None,
) -> Iterator[_PinnedDescriptor]:
    """Open and finally revalidate one regular file through no-follow directory descriptors."""
    candidate = Path(value)
    root = _select_allowed_root(candidate, allowed_roots, field="file_path")
    parts = candidate.relative_to(root).parts
    if not parts:
        _raise_disallowed_path("file_path")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0)
    directories: list[int] = []
    descriptor: int | None = None
    try:
        current_fd = _open_allowed_root(root)
        directories.append(current_fd)
        for component in parts[:-1]:
            current_fd = os.open(component, directory_flags, dir_fd=current_fd)
            directories.append(current_fd)
        filename = parts[-1]
        named_before = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        if stat.S_ISLNK(named_before.st_mode) or not stat.S_ISREG(named_before.st_mode):
            raise _UnsafeInputFile
        descriptor = os.open(filename, file_flags, dir_fd=current_fd)
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode) or _file_state(opened_before) != _file_state(
            named_before
        ):
            raise _UnsafeInputFile
        if max_bytes is not None and opened_before.st_size > max_bytes:
            raise _InputFileLimit(opened_before.st_size)
        try:
            yield _PinnedDescriptor(
                descriptor=descriptor,
                path=root.joinpath(*parts),
                byte_size=opened_before.st_size,
            )
        finally:
            _validate_pinned_directory_walk(root, parts, directories)
            opened_after = os.fstat(descriptor)
            named_after = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
            if _file_state(opened_before) != _file_state(opened_after) or _file_state(
                opened_before
            ) != _file_state(named_after):
                raise _UnsafeInputFile
    finally:
        if descriptor is not None:
            os.close(descriptor)
        for directory_fd in reversed(directories):
            os.close(directory_fd)


def _select_allowed_root(
    candidate: Path,
    allowed_roots: tuple[str, ...],
    *,
    field: str,
) -> Path:
    if not candidate.is_absolute() or ".." in candidate.parts or not allowed_roots:
        _raise_disallowed_path(field)
    root = next(
        (
            Path(value)
            for value in allowed_roots
            if candidate == Path(value) or candidate.is_relative_to(value)
        ),
        None,
    )
    if root is None:
        _raise_disallowed_path(field)
    return root


def _open_allowed_root(root: Path) -> int:
    try:
        named_before = root.lstat()
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(root, flags)
    except OSError:
        raise _UnsafeInputFile from None
    try:
        opened = os.fstat(descriptor)
        named_after = root.lstat()
        identities = {
            (named_before.st_dev, named_before.st_ino),
            (opened.st_dev, opened.st_ino),
            (named_after.st_dev, named_after.st_ino),
        }
        if (
            len(identities) != 1
            or stat.S_ISLNK(named_before.st_mode)
            or stat.S_ISLNK(named_after.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise _UnsafeInputFile
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _validate_allowed_root(root: Path, *, field: str) -> None:
    try:
        descriptor = _open_allowed_root(root)
    except (OSError, _UnsafeInputFile):
        _raise_disallowed_path(field)
    os.close(descriptor)


def _validate_private_output_ancestor(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        _raise_disallowed_path("output_directory")
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _raise_disallowed_path("output_directory")


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _validate_pinned_directory_walk(
    root: Path,
    parts: tuple[str, ...],
    directories: list[int],
) -> None:
    root_named = root.lstat()
    root_opened = os.fstat(directories[0])
    if (
        stat.S_ISLNK(root_named.st_mode)
        or not stat.S_ISDIR(root_named.st_mode)
        or not stat.S_ISDIR(root_opened.st_mode)
        or _file_identity(root_named) != _file_identity(root_opened)
    ):
        raise _UnsafeInputFile
    for index, component in enumerate(parts[:-1]):
        named = os.stat(component, dir_fd=directories[index], follow_symlinks=False)
        opened = os.fstat(directories[index + 1])
        if (
            stat.S_ISLNK(named.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or not stat.S_ISDIR(opened.st_mode)
            or _file_identity(named) != _file_identity(opened)
        ):
            raise _UnsafeInputFile


def _file_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _raise_annotation_file_limit(error: _InputFileLimit, max_bytes: int) -> NoReturn:
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.INPUT_LIMIT_EXCEEDED,
            message="The annotation file exceeds the configured input size limit.",
            recoverable=True,
            suggested_action="Provide a smaller annotation file.",
            safe_details=(
                SafeDetail(name="max_bytes", value=str(max_bytes)),
                SafeDetail(name="actual_bytes", value=str(error.actual_bytes)),
            ),
        )
    ) from None


def _raise_unsafe_annotation_file() -> NoReturn:
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.INVALID_ANNOTATION_TABLE,
            message="The configured annotation file could not be read safely.",
            recoverable=True,
            suggested_action=(
                "Use an unchanged direct regular file beneath a configured allowed root."
            ),
        )
    ) from None


def _raise_disallowed_path(field: str) -> NoReturn:
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            message="A local handoff path is outside the configured allowed roots.",
            recoverable=True,
            suggested_action="Use an absolute direct path beneath KEGG_MCP_ALLOWED_ROOTS.",
            safe_details=(SafeDetail(name="field", value=field),),
        )
    )


__all__ = [
    "PinnedAnnotationFile",
    "bind_annotation_file_source",
    "materialize_annotation_file",
    "open_annotation_file_stream",
    "resolve_existing_file",
    "resolve_output_directory",
]
