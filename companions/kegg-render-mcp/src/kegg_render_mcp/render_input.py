"""Strict renderer-handoff loading and allowlisted filesystem access."""

from __future__ import annotations

import json
import os
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

from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError, SafeDetail
from kegg_render_mcp.input_validation import validate_tool_input
from kegg_render_mcp.validation_errors import summarize_validation_error


@dataclass(frozen=True, slots=True)
class ValidatedRenderInput:
    document: RenderInput

    @property
    def accepted_ko_ids(self) -> frozenset[str]:
        return frozenset(self.document.evidence.accepted_ko_ids)

    @property
    def uncertain_ko_ids(self) -> frozenset[str]:
        return frozenset(self.document.evidence.uncertain_ko_ids)

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
        _, descriptor = _open_beneath(path_text, config.allowed_roots, final_kind="file")
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
    version: object = raw.get("schema_version")
    if version != "3":
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.INCOMPATIBLE_SCHEMA,
                message="Only renderer handoff schema version 3 is compatible.",
                suggested_action="Rerun analysis with a compatible kegg-mcp version.",
                safe_details=(SafeDetail(name="received_version", value=str(version)[:32]),),
            )
        )
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


def resolve_output_directory(path_text: str | None, roots: tuple[Path, ...]) -> Path | None:
    if path_text is None:
        return None
    path, descriptor = _open_beneath(
        path_text, roots, final_kind="directory", create_final_directory=True
    )
    os.close(descriptor)
    return path


def open_allowed_directory(path: Path, roots: tuple[Path, ...]) -> int:
    """Open a validated directory beneath an allowlist root and return its owned FD."""
    _, descriptor = _open_beneath(str(path), roots, final_kind="directory")
    return descriptor


def _bounded_read(descriptor: int, limit: int) -> bytes:
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise _path_error("The renderer input must be a direct regular file.")
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


def _lexical_absolute(value: str, name: str) -> Path:
    if len(value.encode("utf-8")) > 4096:
        raise _path_error(f"{name} exceeds the path-length limit.")
    if "\x00" in value:
        raise _path_error(f"{name} contains a prohibited character.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise _path_error(f"{name} must be an absolute traversal-free path.")
    return path


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
) -> tuple[Path, int]:
    path = _lexical_absolute(path_text, "renderer_path")
    root = _containing_root(path, roots)
    try:
        relative = path.relative_to(root)
    except ValueError as error:  # pragma: no cover - guarded above
        raise _path_error("The path is outside the configured allowed roots.") from error
    descriptor = _open_absolute_directory(root)
    try:
        _validate_private_directory_fd(descriptor)
        parts = relative.parts
        if not parts:
            if final_kind != "directory":
                raise _path_error("The renderer input must be a file below an allowed root.")
            return path, descriptor
        for index, part in enumerate(parts):
            final = index == len(parts) - 1
            wants_directory = not final or final_kind == "directory"
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if wants_directory:
                flags |= os.O_DIRECTORY
            try:
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not (final and wants_directory and create_final_directory):
                    raise
                os.mkdir(part, mode=0o700, dir_fd=descriptor)
                next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            if wants_directory:
                _validate_private_directory_fd(descriptor)
            elif not stat.S_ISREG(metadata.st_mode):
                raise _path_error("The renderer input must be a direct regular file.")
        return path, descriptor
    except RenderMcpError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise _path_error("A renderer path component could not be opened safely.") from error


def _open_absolute_directory(path: Path) -> int:
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


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
