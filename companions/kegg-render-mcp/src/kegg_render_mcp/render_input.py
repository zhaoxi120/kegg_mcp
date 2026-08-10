"""Strict renderer-handoff loading and allowlisted filesystem access."""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kegg_mcp.services.render_contracts import (
    ModuleRenderTarget,
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

    def module(self, module_id: str) -> ModuleRenderTarget:
        for target in self.document.modules:
            if target.module_id == module_id:
                return target
        raise _target_not_found(module_id)

    @property
    def target_ids(self) -> tuple[str, ...]:
        values = [item.pathway_id for item in self.document.pathways]
        values.extend(item.module_id for item in self.document.modules)
        return tuple(values)


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
        descriptor, _ = _open_beneath(
            path_text,
            config.allowed_roots,
            final_kind="file",
        )
        try:
            payload = _bounded_read(descriptor, config.limits.max_input_bytes)
        finally:
            os.close(descriptor)
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


def resolve_output_directory(path_text: str | None, roots: tuple[Path, ...]) -> Path:
    if path_text is None:
        candidate = roots[-1] / f"kegg-render-{secrets.token_hex(16)}"
        return _output_directory_path(str(candidate))
    path = _output_directory_path(path_text)
    root = _containing_root(path, roots)
    if path == root:
        descriptor, _ = _open_beneath(path_text, roots, final_kind="directory")
        os.close(descriptor)
        return path

    parent_descriptor, _ = _open_beneath(str(path.parent), roots, final_kind="directory")
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_descriptor,
            )
        except FileNotFoundError:
            return path
        except OSError as error:
            raise _path_error(
                "The renderer output directory could not be opened safely."
            ) from error
        try:
            _validate_private_directory_fd(descriptor)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent_descriptor)
    return path


def open_allowed_directory(path: Path, roots: tuple[Path, ...]) -> tuple[int, bool]:
    """Open or create a validated output directory and report whether it was created."""
    return _open_beneath(
        str(path),
        roots,
        final_kind="directory",
        create_final_directory=True,
    )


def assert_allowed_directory_identity(
    path: Path,
    roots: tuple[Path, ...],
    descriptor: int,
) -> None:
    """Require the public path to still name the pinned output directory."""
    try:
        pinned = os.fstat(descriptor)
        reopened, _ = _open_beneath(str(path), roots, final_kind="directory")
        try:
            if _directory_identity(os.fstat(reopened)) != _directory_identity(pinned):
                raise OSError("renderer output path no longer resolves to the pinned directory")
        finally:
            os.close(reopened)
    except RenderMcpError as error:
        raise OSError("renderer output path identity could not be validated") from error


def remove_created_empty_directory(
    path: Path,
    roots: tuple[Path, ...],
    descriptor: int,
) -> bool:
    """Remove one still-empty created directory only while its pinned identity matches."""
    pinned = os.fstat(descriptor)
    try:
        parent_fd, _ = _open_beneath(str(path.parent), roots, final_kind="directory")
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


def _containing_root(path: Path, roots: tuple[Path, ...]) -> Path:
    matches = tuple(root for root in roots if path == root or root in path.parents)
    if not matches:
        raise _path_error("The path is outside the configured allowed roots.")
    return max(matches, key=lambda item: len(item.parts))


def _open_beneath(
    path_text: str,
    roots: tuple[Path, ...],
    *,
    final_kind: str,
    create_final_directory: bool = False,
) -> tuple[int, bool]:
    path = _lexical_absolute(path_text, "renderer_path")
    root = _containing_root(path, roots)
    relative = path.relative_to(root)
    descriptor = open_absolute_directory(root)
    created_final_directory = False
    try:
        _validate_private_directory_fd(descriptor)
        parts = relative.parts
        if not parts:
            if final_kind != "directory":
                raise _path_error("The renderer input must be a file below an allowed root.")
            return descriptor, created_final_directory
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            wants_directory = not final or final_kind == "directory"
            flags = os.O_RDONLY | os.O_NOFOLLOW
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
                    _validate_private_directory_fd(next_descriptor)
                elif not stat.S_ISREG(metadata.st_mode):
                    raise _path_error("The renderer input must be a direct regular file.")
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


def _validate_private_directory_fd(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
    ):
        raise _path_error("Renderer paths cannot traverse an unsafe writable directory.")


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
            suggested_action="Use a direct path beneath a configured allowed root.",
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
