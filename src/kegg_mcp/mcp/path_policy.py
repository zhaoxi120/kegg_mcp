"""Allowed-root validation and annotation-file materialization."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError, SafeDetail
from kegg_mcp.importers import SourceProvenanceInput
from kegg_mcp.services.models import NormalizeAnnotationsRequest


def materialize_annotation_file(
    request: NormalizeAnnotationsRequest,
    allowed_roots: tuple[str, ...],
) -> NormalizeAnnotationsRequest:
    """Load one shared file after canonical allowed-root and size validation."""
    if request.file_path is None:
        return request
    path = resolve_existing_file(request.file_path, allowed_roots)
    try:
        content = path.read_bytes()
    except OSError:
        raise KeggMcpError(
            ErrorDetail(
                code=ErrorCode.INVALID_ANNOTATION_TABLE,
                message="The configured annotation file could not be read.",
                recoverable=True,
                suggested_action="Check file permissions and retry within an allowed root.",
            )
        ) from None
    if len(content) > request.import_limits.max_bytes:
        raise KeggMcpError(
            ErrorDetail(
                code=ErrorCode.INPUT_LIMIT_EXCEEDED,
                message="The annotation file exceeds the configured input size limit.",
                recoverable=True,
                suggested_action="Provide a smaller annotation file.",
                safe_details=(
                    SafeDetail(name="max_bytes", value=str(request.import_limits.max_bytes)),
                    SafeDetail(name="actual_bytes", value=str(len(content))),
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
    source_path = (
        str(resolve_existing_file(source.input_path, allowed_roots))
        if source.input_path is not None
        else None
    )
    return request.model_copy(
        update={
            "text": text,
            "file_path": None,
            "source": source.model_copy(update={"input_path": source_path}),
        }
    )


def resolve_existing_file(value: str, allowed_roots: tuple[str, ...]) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts or not allowed_roots:
        _raise_disallowed_path("file_path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        _raise_disallowed_path("file_path")
    if not resolved.is_file() or not _within_allowed_root(resolved, allowed_roots):
        _raise_disallowed_path("file_path")
    return resolved


def resolve_output_directory(
    value: str | None,
    allowed_roots: tuple[str, ...],
) -> Path | None:
    if value is None:
        return None
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts or not allowed_roots:
        _raise_disallowed_path("output_directory")
    missing_parts: list[str] = []
    ancestor = candidate
    while not ancestor.exists():
        missing_parts.append(ancestor.name)
        if ancestor.parent == ancestor:
            _raise_disallowed_path("output_directory")
        ancestor = ancestor.parent
    try:
        resolved_ancestor = ancestor.resolve(strict=True)
    except OSError:
        _raise_disallowed_path("output_directory")
    if not resolved_ancestor.is_dir():
        _raise_disallowed_path("output_directory")
    resolved = resolved_ancestor.joinpath(*reversed(missing_parts))
    if not _within_allowed_root(resolved, allowed_roots):
        _raise_disallowed_path("output_directory")
    return resolved


def _within_allowed_root(path: Path, allowed_roots: tuple[str, ...]) -> bool:
    return any(path == Path(root) or path.is_relative_to(root) for root in allowed_roots)


def _raise_disallowed_path(field: str) -> NoReturn:
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            message="A local handoff path is outside the configured allowed roots.",
            recoverable=True,
            suggested_action="Use an absolute path beneath KEGG_MCP_ALLOWED_ROOTS.",
            safe_details=(SafeDetail(name="field", value=field),),
        )
    )


__all__ = ["materialize_annotation_file", "resolve_existing_file", "resolve_output_directory"]
