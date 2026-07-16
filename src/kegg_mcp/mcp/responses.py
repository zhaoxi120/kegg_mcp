"""Schema-conforming MCP envelopes and safe internal-error correlation."""

from __future__ import annotations

import secrets
import sys

from mcp import types
from pydantic import AnyUrl, BaseModel, ValidationError

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, SafeDetail


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
                title="Retained KEGG analysis result",
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


def validation_error(error: ValidationError) -> ErrorDetail:
    details = [SafeDetail(name="stage", value="input_validation")]
    for issue in error.errors(include_input=False, include_url=False)[:8]:
        location = ".".join(str(part) for part in issue.get("loc", ())) or "$"
        details.append(SafeDetail(name="field_path", value=location[:1_000]))
        details.append(
            SafeDetail(name="issue_type", value=str(issue.get("type", "invalid"))[:1_000])
        )
    details.append(SafeDetail(name="validation_issue_count", value=str(error.error_count())))
    return ErrorDetail(
        code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
        message="The tool input did not satisfy its explicit schema.",
        recoverable=True,
        suggested_action="Correct the supplied fields using the tool input schema.",
        safe_details=tuple(details),
    )


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
