"""Allowed-root validation and race-resistant annotation-file materialization."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import NoReturn

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
        raise KeggMcpError(
            ErrorDetail(
                code=ErrorCode.INPUT_LIMIT_EXCEEDED,
                message="The annotation file exceeds the configured input size limit.",
                recoverable=True,
                suggested_action="Provide a smaller annotation file.",
                safe_details=(
                    SafeDetail(name="max_bytes", value=str(request.import_limits.max_bytes)),
                    SafeDetail(name="actual_bytes", value=str(error.actual_bytes)),
                ),
            )
        ) from None
    except (OSError, _UnsafeInputFile):
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
    source = request.source or SourceProvenanceInput(
        source_name="file_handoff",
        input_path=str(path),
    )
    source_path: str | None = None
    if source.input_path is not None:
        source_path = (
            str(path)
            if Path(source.input_path) == Path(request.file_path)
            else str(resolve_existing_file(source.input_path, allowed_roots))
        )
    return request.model_copy(
        update={
            "text": text,
            "file_path": None,
            "source": source.model_copy(update={"input_path": source_path}),
        }
    )


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
) -> Path | None:
    """Resolve a new or existing output directory below one private allowed root."""
    if value is None:
        return None
    candidate = Path(value)
    root = _select_allowed_root(candidate, allowed_roots, field="output_directory")
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
        current_fd = os.open(root, directory_flags)
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
        if max_bytes is None:
            content: bytes | None = None
        else:
            if opened_before.st_size > max_bytes:
                raise _InputFileLimit(opened_before.st_size)
            buffered = bytearray()
            while len(buffered) <= max_bytes:
                chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(buffered)))
                if not chunk:
                    break
                buffered.extend(chunk)
            if len(buffered) > max_bytes:
                raise _InputFileLimit(len(buffered))
            content = bytes(buffered)
        opened_after = os.fstat(descriptor)
        named_after = os.stat(filename, dir_fd=current_fd, follow_symlinks=False)
        if _file_state(opened_before) != _file_state(opened_after) or _file_state(
            opened_before
        ) != _file_state(named_after):
            raise _UnsafeInputFile
        return content, root.joinpath(*parts)
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


__all__ = ["materialize_annotation_file", "resolve_existing_file", "resolve_output_directory"]
