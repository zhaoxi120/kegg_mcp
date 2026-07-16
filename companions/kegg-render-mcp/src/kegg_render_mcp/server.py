"""Low-level local stdio MCP transport for the independent renderer companion."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, BaseModel, ValidationError

from kegg_render_mcp import SERVER_NAME, __version__
from kegg_render_mcp.config import RendererRuntimeConfig, load_runtime_config
from kegg_render_mcp.contracts import (
    ConnectivityResult,
    DeleteRenderResult,
    DeleteRenderResultInput,
    EmptyInput,
    ErrorCode,
    ErrorDetail,
    RenderAnalysisBundleInput,
    RendererBounds,
    RendererStatus,
    RenderMcpError,
    RenderModuleInput,
    RenderPathwayInput,
    RenderResult,
    ToolEnvelope,
)
from kegg_render_mcp.input_validation import validate_tool_input
from kegg_render_mcp.pathway_scene import (
    CorePathwayAssetProvider,
    PathwayAssetProvider,
    UnconfiguredAssetProvider,
)
from kegg_render_mcp.render_service import RendererService

TOOL_NAMES = (
    "get_renderer_status",
    "probe_renderer_kegg_connectivity",
    "render_analysis_bundle",
    "render_pathway",
    "render_module",
    "delete_render_result",
)

_RESULT_RE = re.compile(r"kegg-render://results/(render_[A-Za-z0-9_-]{32})\Z")
_ARTIFACT_RE = re.compile(
    r"kegg-render://results/(render_[A-Za-z0-9_-]{32})/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z"
)
_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RendererRuntime:
    config: RendererRuntimeConfig
    service: RendererService


class _RequestValidationError(Exception):
    def __init__(self, count: int) -> None:
        super().__init__("invalid request")
        self.count = count


def build_runtime(config: RendererRuntimeConfig | None = None) -> RendererRuntime:
    effective = config or load_runtime_config()
    provider: PathwayAssetProvider
    if effective.access_mode == "unconfigured":
        provider = UnconfiguredAssetProvider()
    else:
        from kegg_mcp.kegg import (
            KeggClient,
            KeggClientConfig,
            KeggClientLimits,
            LicensedAccess,
            PublicAcademicAccess,
            RetryPolicy,
        )

        access = (
            PublicAcademicAccess(academic_use_confirmed=True)
            if effective.access_mode == "public_academic"
            else LicensedAccess(
                endpoint=effective.licensed_endpoint,  # type: ignore[arg-type]
                endpoint_label="licensed-renderer-endpoint",
                authorized_use_confirmed=True,
            )
        )
        provider = CorePathwayAssetProvider(
            KeggClient(
                KeggClientConfig(
                    access=access,
                    limits=KeggClientLimits(max_response_bytes=effective.limits.max_asset_bytes),
                    retry=RetryPolicy(max_retries=0),
                )
            )
        )
    return RendererRuntime(effective, RendererService(effective, provider))


def create_server(runtime: RendererRuntime | None = None) -> Server[object]:
    state = runtime or build_runtime()

    @asynccontextmanager
    async def lifespan(_: Server[object]) -> AsyncGenerator[object]:
        state.service.open()
        try:
            yield state
        finally:
            state.service.close()

    server: Server[object] = Server(
        SERVER_NAME,
        version=__version__,
        instructions=(
            "Render compatible kegg-mcp render_input.json version 2 handoffs as bounded static "
            "SVG and PNG artifacts. Pathway graphics visualize accepted and policy-defined "
            "uncertain annotations; MODULE diagrams preserve the authoritative core AST and "
            "completion results. Graphics do not prove biological activity or phenotype."
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
            if name == "get_renderer_status":
                _parse(EmptyInput, arguments)
                return _success(_status(state), "Returned redacted renderer capabilities.")
            if name == "probe_renderer_kegg_connectivity":
                _parse(EmptyInput, arguments)
                reachable = await state.service.provider.probe()
                return _success(
                    ConnectivityResult(
                        reachable=reachable,
                        message=(
                            "The configured KEGG endpoint answered one INFO request."
                            if reachable
                            else "The configured KEGG endpoint did not answer the bounded probe."
                        ),
                    ),
                    "Completed exactly one explicit KEGG INFO connectivity request.",
                )
            if name == "render_analysis_bundle":
                request = _parse(RenderAnalysisBundleInput, arguments)
                result = await state.service.render(
                    render_input_path=request.render_input_path,
                    target_ids=request.target_ids,
                    formats=request.formats,
                    output_directory=request.output_directory,
                )
                return _success(result, f"Rendered {len(result.target_ids)} bounded target(s).")
            if name == "render_pathway":
                request = _parse(RenderPathwayInput, arguments)
                result = await state.service.render(
                    render_input_path=request.render_input_path,
                    target_ids=(request.target_id,),
                    formats=request.formats,
                    output_directory=request.output_directory,
                )
                return _success(result, "Rendered one regular pathway evidence overlay.")
            if name == "render_module":
                request = _parse(RenderModuleInput, arguments)
                result = await state.service.render(
                    render_input_path=request.render_input_path,
                    target_ids=(request.target_id,),
                    formats=request.formats,
                    output_directory=request.output_directory,
                )
                return _success(result, "Rendered one MODULE evidence logic diagram.")
            if name == "delete_render_result":
                request = _parse(DeleteRenderResultInput, arguments)
                return _success(
                    state.service.store.delete(request.render_id),
                    "Deleted the scoped retained render result.",
                )
            return _error(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The requested MCP tool name is unknown.",
                    suggested_action="Use a tool name returned by tools/list.",
                )
            )
        except _RequestValidationError:
            return _error(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The tool input did not satisfy its explicit schema.",
                    suggested_action="Correct the fields using the tool input schema.",
                )
            )
        except RenderMcpError as error:
            return _error(error.detail)
        except (OSError, RuntimeError, TypeError, ValueError):
            return _error(
                ErrorDetail(
                    code=ErrorCode.INTERNAL_ERROR,
                    message="The renderer could not complete the local request safely.",
                    suggested_action="Check renderer status and retry the bounded request.",
                )
            )

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:  # pyright: ignore[reportUnusedFunction]
        return [
            types.Resource(
                name="renderer-status",
                title="KEGG renderer status",
                uri=AnyUrl("kegg-render://status"),
                description="Redacted renderer capabilities, bounds, and access state.",
                mimeType="application/json",
            )
        ]

    @server.list_resource_templates()
    async def _list_templates() -> list[types.ResourceTemplate]:  # pyright: ignore[reportUnusedFunction]
        return [
            types.ResourceTemplate(
                name="render-result",
                title="Scoped render result",
                uriTemplate="kegg-render://results/{render_id}",
                description="Metadata and validated artifact URIs for one process-scoped result.",
                mimeType="application/json",
            ),
            types.ResourceTemplate(
                name="render-artifact",
                title="Scoped render artifact",
                uriTemplate="kegg-render://results/{render_id}/{artifact}",
                description="One bounded static SVG, binary PNG, or JSON manifest artifact.",
            ),
        ]

    @server.read_resource()
    async def _read_resource(  # pyright: ignore[reportUnusedFunction]
        uri: AnyUrl,
    ) -> list[ReadResourceContents]:
        value = str(uri)
        try:
            if value == "kegg-render://status":
                return [_json_resource(_status(state))]
            if match := _RESULT_RE.fullmatch(value):
                return [_json_resource(state.service.store.get(match.group(1)))]
            if match := _ARTIFACT_RE.fullmatch(value):
                blob = state.service.store.read(match.group(1), match.group(2))
                content: str | bytes
                if blob.mime_type in {"image/svg+xml", "application/json"}:
                    content = blob.content.decode("utf-8", errors="strict")
                else:
                    content = blob.content
                return [ReadResourceContents(content=content, mime_type=blob.mime_type)]
            raise ValueError("unknown resource")
        except (RenderMcpError, UnicodeDecodeError, ValueError):
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message="RESULT_NOT_FOUND: unknown or unavailable scoped renderer resource",
                )
            ) from None

    return server


def _tool_definitions() -> list[types.Tool]:
    status_annotations = types.ToolAnnotations(
        readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
    )
    probe_annotations = types.ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
    pathway_annotations = types.ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
    )
    module_annotations = types.ToolAnnotations(
        readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
    )
    delete_annotations = types.ToolAnnotations(
        readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
    )
    definitions: tuple[
        tuple[str, str, str, type[BaseModel], type[BaseModel], types.ToolAnnotations], ...
    ] = (
        (
            "get_renderer_status",
            "Get renderer status",
            "Return redacted renderer capabilities, access state, bounds, and retention.",
            EmptyInput,
            RendererStatus,
            status_annotations,
        ),
        (
            "probe_renderer_kegg_connectivity",
            "Probe renderer KEGG connectivity",
            "Make exactly one explicit low-cost KEGG INFO request.",
            EmptyInput,
            ConnectivityResult,
            probe_annotations,
        ),
        (
            "render_analysis_bundle",
            "Render selected analysis targets",
            "Validate one compatible handoff and render one through 32 selected targets.",
            RenderAnalysisBundleInput,
            RenderResult,
            pathway_annotations,
        ),
        (
            "render_pathway",
            "Render one regular pathway",
            "Render one regular pathway evidence overlay from matching PNG and KGML assets.",
            RenderPathwayInput,
            RenderResult,
            pathway_annotations,
        ),
        (
            "render_module",
            "Render one MODULE",
            "Render one closed-world logic diagram from authoritative core AST and states.",
            RenderModuleInput,
            RenderResult,
            module_annotations,
        ),
        (
            "delete_render_result",
            "Delete one render result",
            "Delete one process-scoped retained result and all of its artifacts.",
            DeleteRenderResultInput,
            DeleteRenderResult,
            delete_annotations,
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


def _status(runtime: RendererRuntime) -> RendererStatus:
    limits = runtime.config.limits
    return RendererStatus(
        server_version=__version__,
        ready=True,
        pathway_access_configured=runtime.service.provider.configured,
        access_mode=runtime.config.access_mode,
        allowed_root_count=len(runtime.config.allowed_roots),
        retention_seconds=runtime.config.retention_seconds,
        retained_result_count=runtime.service.store.result_count,
        bounds=RendererBounds(
            max_input_bytes=limits.max_input_bytes,
            max_targets=32,
            max_asset_bytes=limits.max_asset_bytes,
            max_pixels=limits.max_pixels,
            max_svg_bytes=limits.max_svg_bytes,
            max_result_bytes=limits.max_result_bytes,
        ),
    )


def _parse(model: type[_M], arguments: dict[str, Any]) -> _M:
    try:
        return validate_tool_input(model, arguments)
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
        structuredContent={
            "ok": False,
            "result": None,
            "error": detail.model_dump(mode="json"),
        },
        isError=True,
    )


def _json_resource(model: BaseModel) -> ReadResourceContents:
    content = json.dumps(model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return ReadResourceContents(content=content, mime_type="application/json")


async def _run_stdio() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    try:
        anyio.run(_run_stdio)
    except (KeyboardInterrupt, BrokenPipeError):
        return
    except (OSError, RuntimeError, ValueError) as error:
        print(f"kegg-render-mcp startup failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
