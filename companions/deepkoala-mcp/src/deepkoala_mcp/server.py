"""Low-level local stdio MCP transport for the DeepKOALA companion."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, TypeVar

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from pydantic import BaseModel, ValidationError

from deepkoala_mcp import SERVER_NAME, __version__
from deepkoala_mcp.config import load_runtime_config
from deepkoala_mcp.contracts import (
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
    PrepareDeepKoalaInput,
    PrepareDeepKoalaResult,
    SafeDetail,
    SubmitDeepKoalaInput,
    ToolEnvelope,
)
from deepkoala_mcp.jobs import DeepKoalaJobManager

TOOL_NAMES = (
    "get_deepkoala_runner_status",
    "prepare_deepkoala_job",
    "submit_deepkoala_job",
    "get_deepkoala_job",
    "cancel_deepkoala_job",
    "delete_deepkoala_job",
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
            "Prepare and run bounded local CPU-only DeepKOALA jobs after explicit acknowledgement. "
            "This server never downloads weights and never interprets KO predictions. Pass the "
            "successful file handoff through the core kegg-mcp DeepKOALA importer."
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
            if name == "prepare_deepkoala_job":
                request = _parse(PrepareDeepKoalaInput, arguments)
                return _success(
                    await state.prepare(request),
                    "Prepared a CPU-only job without starting DeepKOALA; review the notice.",
                )
            if name == "submit_deepkoala_job":
                request = _parse(SubmitDeepKoalaInput, arguments)
                result = await state.submit(request.job_id)
                return _success(result, f"Job is {result.state.value}.")
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
                return _success(await state.delete(request.job_id), "Deleted the terminal job.")
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
            return _error(error.detail)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _error(
                ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="The companion could not complete the local request safely.",
                    suggested_action="Check runner status and retry the bounded request.",
                )
            )

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
        openWorldHint=False,
    )
    submit = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
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
        idempotentHint=False,
        openWorldHint=False,
    )
    definitions: tuple[
        tuple[str, str, str, type[BaseModel], type[BaseModel], types.ToolAnnotations], ...
    ] = (
        (
            "get_deepkoala_runner_status",
            "Get local DeepKOALA runner status",
            "Return redacted CPU-only readiness, bounds, and scheduler counts.",
            GetDeepKoalaStatusInput,
            CompanionStatus,
            read_only,
        ),
        (
            "prepare_deepkoala_job",
            "Prepare a local DeepKOALA job",
            "Validate and privately stage protein FASTA, then return a CPU execution notice.",
            PrepareDeepKoalaInput,
            PrepareDeepKoalaResult,
            prepare,
        ),
        (
            "submit_deepkoala_job",
            "Submit an acknowledged DeepKOALA job",
            "Start or queue one retained plan only after acknowledged=true.",
            SubmitDeepKoalaInput,
            JobSummary,
            submit,
        ),
        (
            "get_deepkoala_job",
            "Get a local DeepKOALA job",
            "Read lifecycle state and the core importer file handoff after success.",
            GetDeepKoalaJobInput,
            GetDeepKoalaJobResult,
            read_only,
        ),
        (
            "cancel_deepkoala_job",
            "Cancel a local DeepKOALA job",
            "Cancel a retained plan or terminate and reap its running process group.",
            CancelDeepKoalaJobInput,
            JobSummary,
            cancel,
        ),
        (
            "delete_deepkoala_job",
            "Delete a terminal DeepKOALA job",
            "Delete one terminal job and its retained local files.",
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


async def _run_stdio() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Run stdio transport and restrict startup diagnostics to stderr."""
    try:
        anyio.run(_run_stdio)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"{SERVER_NAME} startup failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(2) from None


__all__ = ["TOOL_NAMES", "create_server", "main"]
