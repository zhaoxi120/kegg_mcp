"""Low-level local stdio MCP transport for the DeepKOALA companion."""

from __future__ import annotations

import base64
import json
import re
import secrets
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import Any, TypeVar, cast

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
    MAX_RESOURCE_PAGE_BYTES,
    CancelDeepKoalaJobInput,
    CompanionStatus,
    DeepKoalaMcpError,
    DeleteDeepKoalaJobInput,
    DeleteDeepKoalaJobResult,
    ErrorCode,
    ErrorDetail,
    GetDeepKoalaJobInput,
    GetDeepKoalaJobResult,
    GetDeepKoalaStatusInput,
    JobSummary,
    RunDeepKoalaInput,
    RunDeepKoalaResult,
    SafeDetail,
    ToolEnvelope,
)
from deepkoala_mcp.jobs import ArtifactName, DeepKoalaJobManager

TOOL_NAMES = (
    "get_deepkoala_runner_status",
    "run_deepkoala_job",
    "get_deepkoala_job",
    "cancel_deepkoala_job",
    "delete_deepkoala_job",
)
_RESOURCE = re.compile(
    r"^deepkoala://jobs/(job_[a-f0-9]{32})/(annotations|report)"
    r"(?:/(0|[1-9][0-9]{0,7})/([1-9][0-9]{0,5}))?$"
)
_M = TypeVar("_M", bound=BaseModel)


class _RequestValidationError(Exception):
    def __init__(self, issue_count: int) -> None:
        super().__init__("invalid tool input")
        self.issue_count = issue_count


def create_server(manager: DeepKoalaJobManager | None = None) -> Server[object]:
    """Create a process-scoped server with an injectable offline-test manager."""
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
            "Run one bounded local DeepKOALA annotation job directly from an allowlisted protein "
            "FASTA path into an explicit new or empty controlled output directory, or a fresh "
            "service-allocated directory. The runner defaults to CPU and "
            "uses CUDA only after an explicit device request allowed by deployment policy. It uses "
            "detailed output and no worker processes. Multi-domain mode defaults to false and is "
            "available only when the deployment reports it ready and a request explicitly enables "
            "it. The companion never downloads resources. Pass the successful stable CSV path and "
            "source object to core kegg-mcp."
        ),
        lifespan=lifespan,
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:  # pyright: ignore[reportUnusedFunction]
        return _tool_definitions()

    @server.call_tool(validate_input=False)
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        try:
            if name == "get_deepkoala_runner_status":
                _parse(GetDeepKoalaStatusInput, arguments)
                return _success(await state.status(), "Returned redacted local runner status.")
            if name == "run_deepkoala_job":
                request = _parse(RunDeepKoalaInput, arguments)
                result = await state.run(request)
                return _success(result, f"Started DeepKOALA job {result.job.job_id}.")
            if name == "get_deepkoala_job":
                request = _parse(GetDeepKoalaJobInput, arguments)
                result = await state.get_job(request.job_id)
                return _success(result, f"Job is {result.job.state.value}.")
            if name == "cancel_deepkoala_job":
                request = _parse(CancelDeepKoalaJobInput, arguments)
                result = await state.cancel(request.job_id)
                return _success(result, f"Job is {result.state.value}.")
            if name == "delete_deepkoala_job":
                request = _parse(DeleteDeepKoalaJobInput, arguments)
                return _success(
                    await state.delete(request.job_id),
                    "Forgot the terminal job record and retained delivered files.",
                )
            return _error(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The requested MCP tool name is unknown.",
                    suggested_action="Use a tool name returned by tools/list.",
                )
            )
        except _RequestValidationError as error:
            return _error(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The tool input did not satisfy its explicit schema.",
                    suggested_action="Correct the supplied fields using the tool input schema.",
                    safe_details=(
                        SafeDetail(name="validation_issue_count", value=str(error.issue_count)),
                    ),
                )
            )
        except DeepKoalaMcpError as error:
            detail = (
                _internal_error(error, stage=f"tool:{name}")
                if error.detail.code is ErrorCode.INTERNAL_ERROR
                else error.detail
            )
            return _error(detail)
        except Exception as error:
            return _error(_internal_error(error, stage=f"tool:{name}"))

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:  # pyright: ignore[reportUnusedFunction]
        return []

    @server.list_resource_templates()
    async def _list_resource_templates(  # pyright: ignore[reportUnusedFunction]
    ) -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                name="deepkoala-artifact",
                title="Scoped DeepKOALA stable artifact",
                uriTemplate="deepkoala://jobs/{job_id}/{artifact}",
                description=(
                    "Return a small stable artifact directly or a pagination notice for a large "
                    "artifact. Artifacts are annotations or report."
                ),
            ),
            types.ResourceTemplate(
                name="deepkoala-artifact-range",
                title="Bounded DeepKOALA artifact range",
                uriTemplate="deepkoala://jobs/{job_id}/{artifact}/{offset}/{limit}",
                description="Return at most 65536 bytes as base64 with explicit continuation.",
                mimeType="application/json",
            ),
        ]

    @server.read_resource()
    async def _read_resource(  # pyright: ignore[reportUnusedFunction]
        uri: AnyUrl,
    ) -> list[ReadResourceContents]:
        try:
            return [await _resource_contents(str(uri), state)]
        except DeepKoalaMcpError as error:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"{error.detail.code.value}: {error.detail.message}",
                    data=error.detail.model_dump(mode="json"),
                )
            ) from None
        except (UnicodeError, ValueError):
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message="INVALID_RESOURCE_URI: unknown or non-canonical resource URI",
                )
            ) from None

    return server


async def _resource_contents(
    uri: str,
    manager: DeepKoalaJobManager,
) -> ReadResourceContents:
    match = _RESOURCE.fullmatch(uri)
    if match is None:
        raise ValueError("unknown resource")
    job_id, raw_artifact, raw_offset, raw_limit = match.groups()
    artifact = cast(ArtifactName, raw_artifact)
    mime_type = "text/csv" if artifact == "annotations" else "text/markdown"
    if raw_offset is None and raw_limit is None:
        total = await manager.artifact_size(job_id, artifact)
        if total <= MAX_RESOURCE_PAGE_BYTES:
            page = await manager.read_artifact(job_id, artifact, offset=0, limit=total)
            return ReadResourceContents(
                content=page.content.decode("utf-8", errors="strict"),
                mime_type=mime_type,
            )
        notice = {
            "schema_version": "1",
            "artifact": artifact,
            "encoding": "base64",
            "total_bytes": total,
            "page_size": MAX_RESOURCE_PAGE_BYTES,
            "next_uri": f"deepkoala://jobs/{job_id}/{artifact}/0/{MAX_RESOURCE_PAGE_BYTES}",
        }
        return _json_resource(notice)
    if raw_offset is None or raw_limit is None:
        raise ValueError("incomplete range")
    offset = int(raw_offset)
    limit = int(raw_limit)
    if limit > MAX_RESOURCE_PAGE_BYTES:
        raise ValueError("range exceeds maximum")
    page = await manager.read_artifact(job_id, artifact, offset=offset, limit=limit)
    next_offset = offset + len(page.content)
    next_uri = (
        f"deepkoala://jobs/{job_id}/{artifact}/{next_offset}/{limit}"
        if next_offset < page.total_bytes
        else None
    )
    return _json_resource(
        {
            "schema_version": "1",
            "artifact": artifact,
            "encoding": "base64",
            "offset": offset,
            "returned_bytes": len(page.content),
            "total_bytes": page.total_bytes,
            "content_base64": base64.b64encode(page.content).decode("ascii"),
            "next_uri": next_uri,
        }
    )


def _json_resource(value: Mapping[str, object]) -> ReadResourceContents:
    return ReadResourceContents(
        content=json.dumps(value, sort_keys=True, separators=(",", ":")),
        mime_type="application/json",
    )


def _tool_definitions() -> list[types.Tool]:
    read_only = types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    run = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
    cancel = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
    delete = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    )
    definitions: tuple[
        tuple[str, str, str, type[BaseModel], type[BaseModel], types.ToolAnnotations], ...
    ] = (
        (
            "get_deepkoala_runner_status",
            "Get local DeepKOALA runner status",
            "Return redacted base and optional multi-domain readiness, policy, bounds, and job "
            "counts.",
            GetDeepKoalaStatusInput,
            CompanionStatus,
            read_only,
        ),
        (
            "run_deepkoala_job",
            "Run a local DeepKOALA job",
            "Validate paths and policy, stage FASTA, and start detailed annotation atomically; "
            "multi-domain mode requires explicit request and deployment readiness.",
            RunDeepKoalaInput,
            RunDeepKoalaResult,
            run,
        ),
        (
            "get_deepkoala_job",
            "Get a local DeepKOALA job",
            "Read state and the stable file handoff after success.",
            GetDeepKoalaJobInput,
            GetDeepKoalaJobResult,
            read_only,
        ),
        (
            "cancel_deepkoala_job",
            "Cancel a local DeepKOALA job",
            "Terminate and reap the one running DeepKOALA process group.",
            CancelDeepKoalaJobInput,
            JobSummary,
            cancel,
        ),
        (
            "delete_deepkoala_job",
            "Delete a terminal DeepKOALA job record",
            "Forget one terminal process record while retaining delivered files.",
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
            inputSchema=input_model.model_json_schema(mode="validation"),
            outputSchema=ToolEnvelope[output_model].model_json_schema(mode="serialization"),
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


def _error(detail: ErrorDetail) -> types.CallToolResult:
    structured = {"ok": False, "result": None, "error": detail.model_dump(mode="json")}
    return types.CallToolResult(
        content=[
            types.TextContent(
                type="text",
                text=(
                    f"{detail.code.value}: {detail.message} "
                    f"Suggested action: {detail.suggested_action}"
                ),
            )
        ],
        structuredContent=structured,
        isError=True,
    )


def _internal_error(error: Exception, *, stage: str) -> ErrorDetail:
    correlation_id = f"err_{secrets.token_urlsafe(9)}"
    print(
        f"deepkoala-mcp internal error correlation_id={correlation_id} "
        f"stage={stage} type={type(error).__name__}",
        file=sys.stderr,
    )
    return ErrorDetail(
        code=ErrorCode.INTERNAL_ERROR,
        message="The companion could not complete the local request safely.",
        suggested_action="Retry once, then report the correlation ID if the failure repeats.",
        safe_details=(
            SafeDetail(name="correlation_id", value=correlation_id),
            SafeDetail(name="stage", value=stage),
        ),
    )


async def _run_stdio() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    """Run stdio transport and restrict startup diagnostics to stderr."""
    try:
        anyio.run(_run_stdio)
    except (KeyboardInterrupt, BrokenPipeError):
        return
    except (OSError, RuntimeError, ValueError) as error:
        print(f"{SERVER_NAME} startup failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(2) from None


__all__ = ["TOOL_NAMES", "create_server", "main"]
