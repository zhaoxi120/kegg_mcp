"""Low-level stdio MCP server for the separately installed DeepKOALA companion."""

from __future__ import annotations

import base64
import json
import re
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, BaseModel, ValidationError

from deepkoala_mcp import SERVER_NAME, __version__
from deepkoala_mcp.config import load_runtime_config
from deepkoala_mcp.contracts import (
    CancelDeepKoalaJobInput,
    CancelDeepKoalaJobResult,
    CompanionStatus,
    DeepKoalaMcpError,
    DeleteDeepKoalaJobInput,
    DeleteDeepKoalaJobResult,
    ErrorCode,
    ErrorDetail,
    GetDeepKoalaJobInput,
    GetDeepKoalaJobResult,
    GetDeepKoalaStatusInput,
    PrepareDeepKoalaInput,
    PrepareDeepKoalaResult,
    SubmitDeepKoalaInput,
    SubmitDeepKoalaResult,
    ToolEnvelope,
)
from deepkoala_mcp.jobs import DeepKoalaJobManager

MAX_INLINE_RESOURCE_BYTES = 64 * 1024
MAX_RESOURCE_RANGE_BYTES = 1024 * 1024
TOOL_NAMES = (
    "get_deepkoala_runner_status",
    "prepare_deepkoala_job",
    "submit_deepkoala_job",
    "get_deepkoala_job",
    "cancel_deepkoala_job",
    "delete_deepkoala_job",
)

_SECTION_RE = re.compile(
    r"deepkoala-job://jobs/(job_[A-Za-z0-9_-]{32})/"
    r"(output|provenance|diagnostics)\Z"
)
_RANGE_RE = re.compile(
    r"deepkoala-job://jobs/(job_[A-Za-z0-9_-]{32})/"
    r"(output|provenance|diagnostics)/([0-9]{1,19})/([0-9]{1,7})\Z"
)
_M = TypeVar("_M", bound=BaseModel)


class _RequestValidationError(Exception):
    def __init__(self, issue_count: int) -> None:
        super().__init__("invalid MCP tool input")
        self.issue_count = issue_count


def create_server(manager: DeepKoalaJobManager | None = None) -> Server[object]:
    """Create one process-scoped server with an injectable offline-test supervisor."""
    state = manager or DeepKoalaJobManager(load_runtime_config())

    @asynccontextmanager
    async def lifespan(_: Server[object]) -> AsyncGenerator[object]:
        await state.open()
        try:
            yield state
        finally:
            await state.close()

    server: Server[object] = Server(
        SERVER_NAME,
        version=__version__,
        instructions=(
            "Prepare and run bounded local DeepKOALA jobs only after an execution notice is "
            "acknowledged. This server does not download weights and does not interpret KO "
            "predictions; detailed CSV must pass through the core kegg-mcp importer."
        ),
        lifespan=lifespan,
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:  # pyright: ignore[reportUnusedFunction]
        return _tool_definitions()

    @server.call_tool(validate_input=False)
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        try:
            if name == "get_deepkoala_runner_status":
                _parse(GetDeepKoalaStatusInput, arguments)
                return _success(await state.status(), "Returned redacted companion readiness.")
            if name == "prepare_deepkoala_job":
                supplied = _parse(PrepareDeepKoalaInput, arguments)
                result = await state.prepare(supplied)
                return _success(
                    result,
                    "Prepared a bounded job without starting DeepKOALA; review the notice.",
                )
            if name == "submit_deepkoala_job":
                supplied = _parse(SubmitDeepKoalaInput, arguments)
                result = await state.submit(
                    plan_id=supplied.plan_id,
                    notice_sha256=supplied.notice_sha256,
                )
                return _success(result, f"Job is {result.job.state.value}.")
            if name == "get_deepkoala_job":
                supplied = _parse(GetDeepKoalaJobInput, arguments)
                result = await state.get_job(supplied.job_id)
                return _success(result, f"Job is {result.job.state.value}.")
            if name == "cancel_deepkoala_job":
                supplied = _parse(CancelDeepKoalaJobInput, arguments)
                result = await state.cancel(supplied.job_id)
                return _success(result, f"Job is {result.job.state.value}.")
            if name == "delete_deepkoala_job":
                supplied = _parse(DeleteDeepKoalaJobInput, arguments)
                result = await state.delete(supplied.job_id)
                return _success(result, "Deleted the terminal job artifacts.")
            raise ValueError("unknown tool")
        except _RequestValidationError as error:
            return _error(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The tool input did not satisfy its explicit schema.",
                    recoverable=True,
                    suggested_action="Correct the supplied fields using the tool input schema.",
                    safe_details=(),
                ),
                validation_issue_count=error.issue_count,
            )
        except DeepKoalaMcpError as error:
            return _error(error.detail)
        except (OSError, RuntimeError, TypeError, ValidationError, ValueError):
            return _error(
                ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="The companion could not complete the request safely.",
                    recoverable=True,
                    suggested_action="Check runner status and retry the bounded request.",
                )
            )

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:  # pyright: ignore[reportUnusedFunction]
        return [
            types.Resource(
                name="runner-status",
                title="DeepKOALA companion status",
                uri=AnyUrl("deepkoala-job://status"),
                description="Redacted local readiness, defaults, limits, and queue counts.",
                mimeType="application/json",
            )
        ]

    @server.list_resource_templates()
    async def _list_resource_templates(  # pyright: ignore[reportUnusedFunction]
    ) -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                name="job-artifact",
                title="Scoped DeepKOALA job artifact",
                uriTemplate="deepkoala-job://jobs/{job_id}/{section}",
                description="Small output, provenance, or sanitized diagnostic artifact.",
            ),
            types.ResourceTemplate(
                name="job-artifact-range",
                title="Scoped DeepKOALA job artifact byte range",
                uriTemplate="deepkoala-job://jobs/{job_id}/{section}/{offset}/{limit}",
                description="Binary-safe bounded byte range for a large job artifact.",
                mimeType="application/json",
            ),
        ]

    async def _read_resource_impl(value: str) -> list[ReadResourceContents]:
        if value == "deepkoala-job://status":
            return [_json_resource(await state.status())]
        if match := _SECTION_RE.fullmatch(value):
            job_id, section = match.groups()
            artifact = await state.read_artifact(
                job_id,
                section,
                offset=0,
                limit=MAX_INLINE_RESOURCE_BYTES,
            )
            if artifact.next_offset is None:
                return [
                    ReadResourceContents(
                        content=artifact.content.decode("utf-8"),
                        mime_type=artifact.mime_type,
                    )
                ]
            notice = {
                "kind": "artifact_requires_pagination",
                "total_bytes": artifact.total_bytes,
                "sha256": artifact.sha256,
                "maximum_range_bytes": MAX_RESOURCE_RANGE_BYTES,
                "next_uri": f"{value}/0/{MAX_RESOURCE_RANGE_BYTES}",
            }
            return [
                ReadResourceContents(
                    content=json.dumps(notice, sort_keys=True, separators=(",", ":")),
                    mime_type="application/json",
                )
            ]
        if match := _RANGE_RE.fullmatch(value):
            job_id, section, offset_text, limit_text = match.groups()
            offset = int(offset_text)
            limit = int(limit_text)
            if limit < 1 or limit > MAX_RESOURCE_RANGE_BYTES:
                raise ValueError("invalid resource range")
            artifact = await state.read_artifact(
                job_id,
                section,
                offset=offset,
                limit=limit,
            )
            next_uri = (
                f"deepkoala-job://jobs/{job_id}/{section}/{artifact.next_offset}/{limit}"
                if artifact.next_offset is not None
                else None
            )
            envelope = {
                "content_base64": base64.b64encode(artifact.content).decode("ascii"),
                "mime_type": artifact.mime_type,
                "next_uri": next_uri,
                "offset": offset,
                "returned_bytes": len(artifact.content),
                "sha256": artifact.sha256,
                "total_bytes": artifact.total_bytes,
            }
            return [
                ReadResourceContents(
                    content=json.dumps(envelope, sort_keys=True, separators=(",", ":")),
                    mime_type="application/json",
                )
            ]
        raise ValueError("unknown resource")

    @server.read_resource()
    async def _read_resource(  # pyright: ignore[reportUnusedFunction]
        uri: AnyUrl,
    ) -> list[ReadResourceContents]:
        try:
            return await _read_resource_impl(str(uri))
        except DeepKoalaMcpError as error:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"{error.detail.code.value}: {error.detail.message}",
                    data=error.detail.model_dump(mode="json"),
                )
            ) from None
        except (OSError, UnicodeError, ValueError):
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message="INVALID_RESOURCE_URI: unknown or unavailable scoped resource",
                )
            ) from None

    original_call_handler = server.request_handlers[types.CallToolRequest]

    async def _reject_unknown_tools(request: types.CallToolRequest) -> types.ServerResult:
        if request.params.name not in TOOL_NAMES:
            raise McpError(
                types.ErrorData(code=types.INVALID_PARAMS, message="Unknown MCP tool name.")
            )
        return await original_call_handler(request)

    server.request_handlers[types.CallToolRequest] = _reject_unknown_tools
    return server


def _tool_definitions() -> list[types.Tool]:
    read_only = types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    prepare = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    submit = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    cancel = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=True,
    )
    delete = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
    definitions: tuple[
        tuple[str, str, str, type[BaseModel], type[BaseModel], types.ToolAnnotations], ...
    ] = (
        (
            "get_deepkoala_runner_status",
            "Get DeepKOALA runner status",
            "Return redacted installation readiness, defaults, bounds, and queue counts.",
            GetDeepKoalaStatusInput,
            CompanionStatus,
            read_only,
        ),
        (
            "prepare_deepkoala_job",
            "Prepare a DeepKOALA job",
            "Validate and privately stage protein FASTA, then return a notice without inference.",
            PrepareDeepKoalaInput,
            PrepareDeepKoalaResult,
            prepare,
        ),
        (
            "submit_deepkoala_job",
            "Submit a reviewed DeepKOALA job",
            "Acknowledge the exact notice digest and idempotently start or queue that plan.",
            SubmitDeepKoalaInput,
            SubmitDeepKoalaResult,
            submit,
        ),
        (
            "get_deepkoala_job",
            "Get a DeepKOALA job",
            "Read current state and, after success, the core importer handoff.",
            GetDeepKoalaJobInput,
            GetDeepKoalaJobResult,
            read_only,
        ),
        (
            "cancel_deepkoala_job",
            "Cancel a DeepKOALA job",
            "Cancel one queued job or terminate and reap one running process group.",
            CancelDeepKoalaJobInput,
            CancelDeepKoalaJobResult,
            cancel,
        ),
        (
            "delete_deepkoala_job",
            "Delete a terminal DeepKOALA job",
            "Delete one terminal job and its retained local artifacts.",
            DeleteDeepKoalaJobInput,
            DeleteDeepKoalaJobResult,
            delete,
        ),
    )
    return [
        types.Tool(
            name=name,
            title=title,
            description=description,
            inputSchema=input_model.model_json_schema(),
            outputSchema=ToolEnvelope[output_model].model_json_schema(),
            annotations=annotations,
        )
        for name, title, description, input_model, output_model, annotations in definitions
    ]


def _parse(model: type[_M], arguments: dict[str, Any]) -> _M:
    try:
        return model.model_validate(arguments, strict=True)
    except ValidationError as error:
        raise _RequestValidationError(error.error_count()) from None


def _success(model: BaseModel, narrative: str) -> types.CallToolResult:
    structured = {"ok": True, "result": {"data": model.model_dump(mode="json")}, "error": None}
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=narrative)],
        structuredContent=structured,
        isError=False,
    )


def _error(
    detail: ErrorDetail,
    *,
    validation_issue_count: int | None = None,
) -> types.CallToolResult:
    serialized = detail.model_dump(mode="json")
    if validation_issue_count is not None:
        serialized["safe_details"] = [
            {"name": "validation_issue_count", "value": str(validation_issue_count)}
        ]
    structured = {"ok": False, "result": None, "error": serialized}
    action = f" Suggested action: {detail.suggested_action}" if detail.suggested_action else ""
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=f"{detail.code.value}: {detail.message}{action}")
        ],
        structuredContent=structured,
        isError=True,
    )


def _json_resource(model: BaseModel) -> ReadResourceContents:
    return ReadResourceContents(content=model.model_dump_json(), mime_type="application/json")


async def _run_stdio() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Run stdio transport with diagnostics restricted to stderr."""
    try:
        anyio.run(_run_stdio)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"{SERVER_NAME} startup failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(2) from None


__all__ = ["MAX_INLINE_RESOURCE_BYTES", "TOOL_NAMES", "create_server", "main"]
