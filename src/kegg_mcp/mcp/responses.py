"""Schema-conforming MCP envelopes and safe internal-error correlation."""

from __future__ import annotations

import secrets
import sys
from typing import cast

from mcp import types
from pydantic import AnyUrl, BaseModel, ValidationError

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, SafeDetail

_NESTED_CONTEXT_CONFLICT = "nested_annotation_context_conflict"
_SAFE_NESTED_CONTEXT_FIELDS = frozenset(
    {
        "analysis_unit",
        "annotations.analysis_unit",
        "sample_id",
        "annotations.sample_id",
    }
)


def success(
    data: BaseModel,
    summary: str,
    result_id: str | None = None,
) -> types.CallToolResult:
    uri = None if result_id is None else f"ko-analysis://results/{result_id}"
    structured = {
        "ok": True,
        "result": {"data": data.model_dump(mode="json"), "resource_uri": uri},
        "error": None,
    }
    content: list[types.ContentBlock] = [types.TextContent(type="text", text=summary)]
    if uri is not None:
        content.append(
            types.ResourceLink(
                type="resource_link",
                name=f"result-{result_id}",
                title="Retained KEGG result",
                uri=AnyUrl(uri),
                description="Scoped metadata and bounded section links.",
                mimeType="application/json",
            )
        )
    return types.CallToolResult(content=content, structuredContent=structured, isError=False)


def error_result(detail: ErrorDetail) -> types.CallToolResult:
    action = f" Suggested action: {detail.suggested_action}" if detail.suggested_action else ""
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=f"{detail.code.value}: {detail.message}{action}")
        ],
        structuredContent={
            "ok": False,
            "result": None,
            "error": detail.model_dump(mode="json"),
        },
        isError=True,
    )


def validation_error(
    error: ValidationError,
    *,
    input_model: type[BaseModel],
) -> ErrorDetail:
    details = [SafeDetail(name="stage", value="input_validation")]
    safe_field_names = _schema_field_names(input_model)
    for issue in error.errors(include_input=False, include_url=False)[:8]:
        issue_type = str(issue.get("type", "invalid"))[:1_000]
        location = _safe_validation_location(
            issue.get("loc", ()),
            issue_type=issue_type,
            safe_field_names=safe_field_names,
        )
        safe_conflict_fields: tuple[str, ...] = ()
        context = issue.get("ctx")
        if issue_type == _NESTED_CONTEXT_CONFLICT and isinstance(context, dict):
            raw_fields = context.get("conflict_fields")
            if isinstance(raw_fields, str):
                conflict_fields = tuple(raw_fields.split(","))
                if conflict_fields and set(conflict_fields) <= _SAFE_NESTED_CONTEXT_FIELDS:
                    safe_conflict_fields = conflict_fields
                    location = conflict_fields[0]
        details.append(SafeDetail(name="field_path", value=location[:1_000]))
        details.append(SafeDetail(name="issue_type", value=issue_type))
        if safe_conflict_fields:
            details.append(SafeDetail(name="conflict_fields", value=",".join(safe_conflict_fields)))
    details.append(SafeDetail(name="validation_issue_count", value=str(error.error_count())))
    return ErrorDetail(
        code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
        message="The tool input did not satisfy its explicit schema.",
        recoverable=True,
        suggested_action="Correct the supplied fields using the tool input schema.",
        safe_details=tuple(details),
    )


def _schema_field_names(model: type[BaseModel]) -> frozenset[str]:
    names: set[str] = set()
    pending: list[object] = [model.model_json_schema(mode="validation")]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            mapping = cast(dict[object, object], node)
            properties = mapping.get("properties")
            if isinstance(properties, dict):
                property_mapping = cast(dict[object, object], properties)
                names.update(name for name in property_mapping if isinstance(name, str))
            pending.extend(mapping.values())
        elif isinstance(node, list):
            pending.extend(cast(list[object], node))
    return frozenset(names)


def _safe_validation_location(
    raw_location: object,
    *,
    issue_type: str,
    safe_field_names: frozenset[str],
) -> str:
    if issue_type == "extra_forbidden":
        return "$.<unknown_field>"
    if not isinstance(raw_location, tuple):
        return "$.<invalid_component>"
    components: list[str] = []
    for component in cast(tuple[object, ...], raw_location):
        if isinstance(component, int) and not isinstance(component, bool) and component >= 0:
            components.append(str(component))
        elif isinstance(component, str) and component in safe_field_names:
            components.append(component)
        else:
            components.append("<invalid_component>")
    return ".".join(components) or "$"


def internal_error(exception: Exception, *, stage: str) -> ErrorDetail:
    correlation_id = f"err_{secrets.token_urlsafe(9)}"
    print(
        f"KEGG MCP internal error correlation_id={correlation_id} "
        f"stage={stage} type={type(exception).__name__}",
        file=sys.stderr,
    )
    return ErrorDetail(
        code=ErrorCode.INTERNAL_ERROR,
        message="The server could not complete the request safely.",
        recoverable=True,
        suggested_action="Retry once, then report the correlation ID if the failure repeats.",
        safe_details=(
            SafeDetail(name="correlation_id", value=correlation_id),
            SafeDetail(name="stage", value=stage),
        ),
    )


__all__ = ["error_result", "internal_error", "success", "validation_error"]
