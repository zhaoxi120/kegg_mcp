"""Strict renderer-handoff loading and bounded local filesystem access."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kegg_mcp.services.render_contracts import (
    PathwayRenderTarget,
    RenderInput,
)
from pydantic import ValidationError

from kegg_render_mcp._filesystem import open_absolute_directory
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    ErrorCode,
    ErrorDetail,
    RenderMcpError,
    SafeDetail,
)
from kegg_render_mcp.input_validation import validate_tool_input
from kegg_render_mcp.validation_errors import summarize_validation_error

_MAX_PATH_BYTES = 4_096
_MAX_ARTIFACT_NAME_BYTES = 128
_MAX_OUTPUT_DIRECTORY_BYTES = _MAX_PATH_BYTES - 1 - _MAX_ARTIFACT_NAME_BYTES


@dataclass(frozen=True, slots=True)
class ValidatedRenderInput:
    document: RenderInput

    def pathway(self, pathway_id: str) -> PathwayRenderTarget:
        for target in self.document.pathways:
            if target.pathway_id == pathway_id:
                return target
        raise _target_not_found(pathway_id)

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(item.pathway_id for item in self.document.pathways)


def load_render_input(
    path_text: str | None,
    config: RendererRuntimeConfig,
    *,
    render_input_json: str | None = None,
) -> ValidatedRenderInput:
    """Strictly validate exactly one bounded file or inline renderer handoff."""
    if (path_text is None) == (render_input_json is None):
        raise _invalid_input("Provide exactly one renderer input source.")
    if path_text is not None:
        try:
            path, expected_state = _canonical_input_file(path_text)
            descriptor, _ = _open_absolute_path(path, final_kind="file")
            try:
                _assert_input_file_state(path, descriptor, expected_state)
                payload = _bounded_read(descriptor, config.limits.max_input_bytes)
                _assert_input_file_state(path, descriptor, expected_state)
            finally:
                os.close(descriptor)
        except RenderMcpError as error:
            raise _with_path_field(error, "render_input_path") from None
    else:
        assert render_input_json is not None
        try:
            payload = render_input_json.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise _invalid_input("The inline renderer input is not valid UTF-8 JSON.") from error
        if len(payload) > config.limits.max_input_bytes:
            raise _input_limit(config.limits.max_input_bytes)
    return _parse_payload(payload)


def _parse_payload(payload: bytes) -> ValidatedRenderInput:
    """Parse JSON once, then strictly validate the already-decoded object graph."""
    try:
        parsed: object = json.loads(payload)
    except UnicodeDecodeError as error:
        raise _invalid_input("The renderer input is not valid UTF-8 JSON.") from error
    except (json.JSONDecodeError, RecursionError) as error:
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.INVALID_REQUEST,
                message="The renderer input is not a valid bounded JSON document.",
                suggested_action="Rerun core analysis to create a new renderer handoff.",
                safe_details=(SafeDetail(name="stage", value="render_input_json"),),
            )
        ) from error
    if not isinstance(parsed, dict):
        raise _invalid_input("The renderer input root must be a JSON object.")
    raw = cast(dict[str, Any], parsed)
    try:
        document = validate_tool_input(RenderInput, raw)
    except ValidationError as error:
        summary = summarize_validation_error(error)
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.INVALID_REQUEST,
                message="The renderer handoff does not satisfy the complete schema contract.",
                suggested_action="Rerun core analysis instead of editing render_input.json.",
                safe_details=(
                    SafeDetail(name="field_path", value=summary.field_path),
                    SafeDetail(name="validation_issue_count", value=str(summary.issue_count)),
                    SafeDetail(name="stage", value="render_input_schema"),
                ),
            )
        ) from None
    if len(payload) > document.limits.max_serialized_bytes:
        raise _input_limit(document.limits.max_serialized_bytes)
    return ValidatedRenderInput(document=document)


def resolve_output_directory(
    path_text: str | None,
    default_output_roots: tuple[Path, ...],
    state_root: Path,
) -> Path:
    if path_text is None:
        candidate = default_output_roots[-1] / f"kegg-render-{secrets.token_hex(16)}"
        return _output_directory_path(str(candidate))
    try:
        path = canonicalize_output_directory(Path(path_text))
        if path == state_root or path in state_root.parents or state_root in path.parents:
            raise _path_error("The renderer output directory must not overlap private state.")
        _preflight_output_directory(path)
        return path
    except RenderMcpError as error:
        raise _with_path_field(error, "output_directory") from None


def canonicalize_output_directory(path: Path) -> Path:
    """Resolve ordinary symlinks while requiring an existing direct parent."""
    requested = _output_directory_path(str(path))
    try:
        resolved = requested.resolve(strict=True)
    except FileNotFoundError:
        try:
            resolved = requested.parent.resolve(strict=True) / requested.name
        except (OSError, RuntimeError) as error:
            raise _path_error("The renderer output parent directory is unavailable.") from error
    except (OSError, RuntimeError) as error:
        raise _path_error("The renderer output directory is unavailable.") from error
    return _output_directory_path(str(resolved))


def open_output_directory(path: Path) -> tuple[int, bool]:
    """Open or create a validated output directory and report whether it was created."""
    try:
        return _open_absolute_path(
            path,
            final_kind="directory",
            create_final_directory=True,
        )
    except RenderMcpError as error:
        raise _with_path_field(error, "output_directory") from None


def assert_output_directory_identity(
    path: Path,
    descriptor: int,
) -> None:
    """Require the public path to still name the pinned output directory."""
    try:
        pinned = os.fstat(descriptor)
        reopened, _ = _open_absolute_path(path, final_kind="directory")
        try:
            if _directory_identity(os.fstat(reopened)) != _directory_identity(pinned):
                raise OSError("renderer output path no longer resolves to the pinned directory")
        finally:
            os.close(reopened)
    except RenderMcpError as error:
        raise OSError("renderer output path identity could not be validated") from error


def remove_created_empty_output_directory(
    path: Path,
    descriptor: int,
) -> bool:
    """Remove one still-empty created directory only while its pinned identity matches."""
    pinned = os.fstat(descriptor)
    try:
        parent_fd, _ = _open_absolute_path(
            path.parent,
            final_kind="directory",
            validate_final_directory=False,
        )
    except RenderMcpError:
        return False
    try:
        return _remove_named_empty_directory_if_identity(
            parent_fd,
            path.name,
            (pinned.st_dev, pinned.st_ino, pinned.st_uid),
        )
    finally:
        os.close(parent_fd)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid)


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


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
        os.fsync(parent_fd)
        return True
    except OSError:
        return False


def _bounded_read(descriptor: int, limit: int) -> bytes:
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_size > limit:
            raise _input_limit(limit)
        content = bytearray()
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            content.extend(chunk)
            remaining -= len(chunk)
        if len(content) > limit:
            raise _input_limit(limit)
        return bytes(content)
    except RenderMcpError:
        raise
    except OSError as error:
        raise _path_error("The renderer input could not be opened safely.") from error


def _lexical_absolute(
    value: str,
    name: str,
    *,
    max_bytes: int = _MAX_PATH_BYTES,
    limit_message: str | None = None,
) -> Path:
    if len(value.encode("utf-8")) > max_bytes:
        raise _path_error(limit_message or f"{name} exceeds the path-length limit.")
    if "\x00" in value:
        raise _path_error(f"{name} contains a prohibited character.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _path_error(f"{name} must be an absolute traversal-free path.")
    return path


def _output_directory_path(value: str) -> Path:
    return _lexical_absolute(
        value,
        "renderer_output_directory",
        max_bytes=_MAX_OUTPUT_DIRECTORY_BYTES,
        limit_message=(
            "The renderer output directory leaves insufficient path space for artifacts."
        ),
    )


def _canonical_input_file(path_text: str) -> tuple[Path, tuple[int, int, int, int, int]]:
    requested = _lexical_absolute(path_text, "renderer_input_path")
    try:
        resolved = requested.resolve(strict=True)
        path = _lexical_absolute(str(resolved), "renderer_input_path")
        metadata = os.stat(path, follow_symlinks=False)
    except (OSError, RuntimeError) as error:
        raise _path_error("The renderer input file is unavailable.") from error
    if not stat.S_ISREG(metadata.st_mode):
        raise _path_error("The renderer input must resolve to a regular file.")
    return path, _file_state(metadata)


def _assert_input_file_state(
    path: Path,
    descriptor: int,
    expected: tuple[int, int, int, int, int],
) -> None:
    try:
        opened = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
    except OSError as error:
        raise _path_error("The renderer input changed while it was being read.") from error
    if (
        not stat.S_ISREG(opened.st_mode)
        or not stat.S_ISREG(named.st_mode)
        or _file_state(opened) != expected
        or _file_state(named) != expected
    ):
        raise _path_error("The renderer input changed while it was being read.")


def _preflight_output_directory(path: Path) -> None:
    if path == Path(path.anchor):
        descriptor, _ = _open_absolute_path(path, final_kind="directory")
        os.close(descriptor)
        return
    parent_descriptor, _ = _open_absolute_path(
        path.parent,
        final_kind="directory",
        validate_final_directory=False,
    )
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            if not os.access(path.parent, os.W_OK | os.X_OK):
                raise _path_error("The renderer output parent is not writable.") from None
            return
        except OSError as error:
            raise _path_error(
                "The renderer output directory could not be opened safely."
            ) from error
        try:
            _validate_output_directory_fd(descriptor)
            if not os.access(path, os.W_OK | os.X_OK):
                raise _path_error("The renderer output directory is not writable.")
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)


def _open_absolute_path(
    path: Path,
    *,
    final_kind: str,
    create_final_directory: bool = False,
    validate_final_directory: bool = True,
) -> tuple[int, bool]:
    path = _lexical_absolute(str(path), "renderer_path")
    descriptor = open_absolute_directory(Path(path.anchor))
    created_final_directory = False
    try:
        parts = path.parts[1:]
        if not parts:
            if final_kind != "directory":
                raise _path_error("The renderer input must be a regular file.")
            if validate_final_directory:
                _validate_output_directory_fd(descriptor)
            return descriptor, created_final_directory
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            wants_directory = not final or final_kind == "directory"
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            if wants_directory:
                flags |= os.O_DIRECTORY
            created_identity: tuple[int, int, int] | None = None
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not (final and wants_directory and create_final_directory):
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                created_final_directory = True
                created_metadata = os.stat(part, dir_fd=descriptor, follow_symlinks=False)
                if not stat.S_ISDIR(created_metadata.st_mode):
                    raise OSError("created renderer output entry is not a directory") from None
                created_identity = _directory_identity(created_metadata)
                try:
                    next_descriptor = os.open(part, flags, dir_fd=descriptor)
                except BaseException:
                    _remove_named_empty_directory_if_identity(
                        descriptor,
                        part,
                        created_identity,
                    )
                    raise
            try:
                metadata = os.fstat(next_descriptor)
                if (
                    created_identity is not None
                    and _directory_identity(metadata) != created_identity
                ):
                    raise OSError("created renderer output directory was replaced before opening")
                if wants_directory:
                    if final and validate_final_directory:
                        _validate_output_directory_fd(next_descriptor)
                elif not stat.S_ISREG(metadata.st_mode):
                    raise _path_error("The renderer input must be a regular file.")
                if created_identity is not None:
                    os.fsync(descriptor)
            except BaseException:
                os.close(next_descriptor)
                if created_identity is not None:
                    _remove_named_empty_directory_if_identity(
                        descriptor,
                        part,
                        created_identity,
                    )
                raise
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, created_final_directory
    except RenderMcpError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise _path_error("A renderer path component could not be opened safely.") from error


def _validate_output_directory_fd(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise _path_error("The renderer output destination must be a directory.")


def _target_not_found(identifier: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.TARGET_NOT_FOUND,
            message="The requested target is not present in this renderer handoff.",
            suggested_action="Use a target identifier retained by the core analysis.",
            safe_details=(SafeDetail(name="target_id", value=identifier),),
        )
    )


def _path_error(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.INPUT_PATH_REJECTED,
            message=message,
            suggested_action=(
                "Provide an absolute readable local input file or a safe absolute output directory."
            ),
        )
    )


def _with_path_field(error: RenderMcpError, field: str) -> RenderMcpError:
    if error.detail.code is not ErrorCode.INPUT_PATH_REJECTED:
        return error
    return RenderMcpError(
        error.detail.model_copy(
            update={
                "safe_details": (
                    *error.detail.safe_details,
                    SafeDetail(name="field", value=field),
                )
            }
        )
    )


def _input_limit(limit: int) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.INPUT_LIMIT_EXCEEDED,
            message="The renderer input exceeds the configured byte limit.",
            suggested_action="Select fewer bounded render targets in core analysis.",
            safe_details=(
                SafeDetail(name="maximum_bytes", value=str(limit)),
                SafeDetail(name="stage", value="render_input_read"),
            ),
        )
    )


def _invalid_input(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.INVALID_REQUEST,
            message=message,
            suggested_action="Provide the unchanged renderer handoff written by kegg-mcp.",
        )
    )
