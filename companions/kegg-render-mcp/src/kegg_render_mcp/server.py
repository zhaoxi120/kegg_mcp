"""Low-level local stdio MCP transport for the independent renderer companion."""

from __future__ import annotations

import json
import re
import secrets
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import anyio
from kegg_mcp.services.render_contracts import RENDER_INPUT_SCHEMA_VERSION
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, BaseModel, ValidationError

from kegg_render_mcp import SERVER_NAME, __version__
from kegg_render_mcp.config import RendererRuntimeConfig, load_runtime_config
from kegg_render_mcp.contracts import (
    ARTIFACT_NAME_PATTERN,
    MAX_TARGETS,
    RENDER_ID_PATTERN,
    ConnectivityResult,
    ConnectivityStatus,
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
    SafeDetail,
    ToolEnvelope,
)
from kegg_render_mcp.input_validation import validate_tool_input
from kegg_render_mcp.pathway_scene import (
    CorePathwayAssetProvider,
    PathwayAssetProvider,
    UnconfiguredAssetProvider,
)
from kegg_render_mcp.render_service import RendererService
from kegg_render_mcp.validation_errors import ValidationIssueSummary, summarize_validation_error

_RESULT_RE = re.compile(rf"kegg-render://results/({RENDER_ID_PATTERN})\Z")
_ARTIFACT_RE = re.compile(
    rf"kegg-render://results/({RENDER_ID_PATTERN})/({ARTIFACT_NAME_PATTERN})\Z"
)
_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class RendererRuntime:
    config: RendererRuntimeConfig
    service: RendererService


@dataclass(frozen=True, slots=True)
class _ToolExecution:
    output: BaseModel
    narrative: str


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    """One authoritative renderer tool definition and dispatch target."""

    name: str
    title: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    annotations: types.ToolAnnotations
    handler: Callable[[RendererRuntime, BaseModel], Awaitable[_ToolExecution]]


class _RequestValidationError(Exception):
    def __init__(self, summary: ValidationIssueSummary) -> None:
        super().__init__("invalid request")
        self.summary = summary


async def _handle_status(runtime: RendererRuntime, request: BaseModel) -> _ToolExecution:
    assert isinstance(request, EmptyInput)
    return _ToolExecution(_status(runtime), "Returned redacted renderer capabilities.")


async def _handle_probe(runtime: RendererRuntime, request: BaseModel) -> _ToolExecution:
    assert isinstance(request, EmptyInput)
    classification = await runtime.service.provider.probe()
    return _ToolExecution(
        ConnectivityResult(
            reachable=classification is ConnectivityStatus.REACHABLE,
            classification=classification,
            request_count=(
                0
                if classification
                in {ConnectivityStatus.NOT_CONFIGURED, ConnectivityStatus.OFFLINE_CACHE}
                else 1
            ),
            message=_connectivity_message(classification),
        ),
        "Completed the bounded renderer KEGG connectivity preflight.",
    )


async def _handle_bundle(runtime: RendererRuntime, request: BaseModel) -> _ToolExecution:
    assert isinstance(request, RenderAnalysisBundleInput)
    result = await runtime.service.render(
        render_input_path=request.render_input_path,
        render_input_json=request.render_input_json,
        target_ids=request.target_ids,
        formats=request.formats,
        output_directory=request.output_directory,
    )
    return _ToolExecution(result, f"Rendered {len(result.target_ids)} bounded target(s).")


async def _handle_pathway(runtime: RendererRuntime, request: BaseModel) -> _ToolExecution:
    assert isinstance(request, RenderPathwayInput)
    result = await runtime.service.render(
        render_input_path=request.render_input_path,
        render_input_json=request.render_input_json,
        target_ids=(request.target_id,),
        formats=request.formats,
        output_directory=request.output_directory,
    )
    return _ToolExecution(result, "Rendered one regular pathway evidence overlay.")


async def _handle_module(runtime: RendererRuntime, request: BaseModel) -> _ToolExecution:
    assert isinstance(request, RenderModuleInput)
    result = await runtime.service.render(
        render_input_path=request.render_input_path,
        render_input_json=request.render_input_json,
        target_ids=(request.target_id,),
        formats=request.formats,
        output_directory=request.output_directory,
    )
    return _ToolExecution(result, "Rendered one MODULE evidence logic diagram.")


async def _handle_delete(runtime: RendererRuntime, request: BaseModel) -> _ToolExecution:
    assert isinstance(request, DeleteRenderResultInput)
    return _ToolExecution(
        runtime.service.store.delete(request.render_id),
        "Deleted the scoped retained render result.",
    )


_STATUS_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
_PROBE_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_PATHWAY_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
_MODULE_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_DELETE_ANNOTATIONS = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)

_TOOL_SPECS = (
    _ToolSpec(
        name="get_renderer_status",
        title="Get renderer status",
        description="Return redacted renderer capabilities, access state, bounds, and retention.",
        input_model=EmptyInput,
        output_model=RendererStatus,
        annotations=_STATUS_ANNOTATIONS,
        handler=_handle_status,
    ),
    _ToolSpec(
        name="probe_renderer_kegg_connectivity",
        title="Probe renderer KEGG connectivity",
        description="Make one low-cost KEGG INFO request for live access; offline modes make none.",
        input_model=EmptyInput,
        output_model=ConnectivityResult,
        annotations=_PROBE_ANNOTATIONS,
        handler=_handle_probe,
    ),
    _ToolSpec(
        name="render_analysis_bundle",
        title="Render selected analysis targets",
        description=(
            "Validate exactly one path or inline handoff and render one through "
            f"{MAX_TARGETS} selected targets."
        ),
        input_model=RenderAnalysisBundleInput,
        output_model=RenderResult,
        annotations=_PATHWAY_ANNOTATIONS,
        handler=_handle_bundle,
    ),
    _ToolSpec(
        name="render_pathway",
        title="Render one regular pathway",
        description=(
            "Render one regular pathway evidence overlay from matching PNG and KGML assets."
        ),
        input_model=RenderPathwayInput,
        output_model=RenderResult,
        annotations=_PATHWAY_ANNOTATIONS,
        handler=_handle_pathway,
    ),
    _ToolSpec(
        name="render_module",
        title="Render one MODULE",
        description="Render one closed-world logic diagram from authoritative core AST and states.",
        input_model=RenderModuleInput,
        output_model=RenderResult,
        annotations=_MODULE_ANNOTATIONS,
        handler=_handle_module,
    ),
    _ToolSpec(
        name="delete_render_result",
        title="Delete one render result",
        description="Delete one process-scoped retained result and all of its artifacts.",
        input_model=DeleteRenderResultInput,
        output_model=DeleteRenderResult,
        annotations=_DELETE_ANNOTATIONS,
        handler=_handle_delete,
    ),
)
TOOL_NAMES = tuple(spec.name for spec in _TOOL_SPECS)
_TOOL_SPECS_BY_NAME = {spec.name: spec for spec in _TOOL_SPECS}
if len(_TOOL_SPECS_BY_NAME) != len(_TOOL_SPECS):  # pragma: no cover - import-time invariant
    raise RuntimeError("renderer tool registry contains duplicate names")


def build_runtime(config: RendererRuntimeConfig | None = None) -> RendererRuntime:
    effective = config or load_runtime_config()
    provider: PathwayAssetProvider
    if effective.access_mode == "unconfigured":
        provider = UnconfiguredAssetProvider()
    else:
        from kegg_mcp.kegg import (
            CachePolicy,
            KeggClient,
            KeggClientConfig,
            KeggClientLimits,
            LicensedAccess,
            OfflineCacheAccess,
            PublicAcademicAccess,
            RateLimitPolicy,
            RetrievalEndpointClass,
            RetryPolicy,
            endpoint_fingerprint,
        )

        if effective.access_mode == "public_academic":
            access = PublicAcademicAccess(academic_use_confirmed=True)
        elif effective.access_mode == "licensed":
            access = LicensedAccess(
                endpoint=effective.licensed_endpoint,  # type: ignore[arg-type]
                endpoint_label="licensed-renderer-endpoint",
                authorized_use_confirmed=True,
            )
        elif effective.licensed_endpoint is None:
            access = OfflineCacheAccess()
        else:
            licensed_namespace = LicensedAccess(
                endpoint=effective.licensed_endpoint,
                endpoint_label="licensed-renderer-cache",
                authorized_use_confirmed=True,
            )
            access = OfflineCacheAccess(
                retrieval_endpoint_class=RetrievalEndpointClass.LICENSED,
                endpoint=licensed_namespace.endpoint,
                endpoint_fingerprint=endpoint_fingerprint(licensed_namespace.endpoint),
                endpoint_label=licensed_namespace.endpoint_label,
            )
        cache = (
            CachePolicy(path=str(effective.cache_path))
            if effective.cache_path is not None
            else CachePolicy()
        )
        provider = CorePathwayAssetProvider(
            KeggClient(
                KeggClientConfig(
                    access=access,
                    cache=cache,
                    limits=KeggClientLimits(max_response_bytes=effective.limits.max_asset_bytes),
                    retry=RetryPolicy(max_retries=0),
                    rate_limit=RateLimitPolicy(state_root=effective.rate_limit_root),
                )
            ),
            allow_stale=effective.offline_allow_stale,
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
            "Render compatible kegg-mcp render_input.json version "
            f"{RENDER_INPUT_SCHEMA_VERSION} handoffs as bounded static SVG and PNG artifacts. "
            "Pathway graphics visualize accepted and policy-defined uncertain annotations; "
            "MODULE diagrams preserve the authoritative core AST and completion results. "
            "Graphics do not prove biological activity or phenotype."
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
            spec = _TOOL_SPECS_BY_NAME.get(name)
            if spec is None:
                return _error(
                    ErrorDetail(
                        code=ErrorCode.INVALID_REQUEST,
                        message="The requested MCP tool name is unknown.",
                        suggested_action="Use a tool name returned by tools/list.",
                    )
                )
            request = _parse(spec.input_model, arguments)
            execution = await spec.handler(state, request)
            if not isinstance(execution.output, spec.output_model):
                raise RuntimeError("renderer tool handler returned the wrong output contract")
            return _success(execution.output, execution.narrative)
        except _RequestValidationError as error:
            return _error(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The tool input did not satisfy its explicit schema.",
                    suggested_action="Correct the fields using the tool input schema.",
                    safe_details=(
                        SafeDetail(name="field_path", value=error.summary.field_path),
                        SafeDetail(
                            name="validation_issue_count",
                            value=str(error.summary.issue_count),
                        ),
                        SafeDetail(name="stage", value="tool_input"),
                    ),
                )
            )
        except RenderMcpError as error:
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
    return [
        types.Tool(
            name=spec.name,
            title=spec.title,
            description=spec.description,
            inputSchema=_mcp_input_schema(spec.input_model),
            outputSchema=ToolEnvelope[spec.output_model].model_json_schema(mode="serialization"),
            annotations=spec.annotations,
        )
        for spec in _TOOL_SPECS
    ]


def _mcp_input_schema(input_model: type[BaseModel]) -> dict[str, object]:
    """Return a self-contained schema with explicit renderer source alternatives."""
    schema = input_model.model_json_schema(mode="validation")
    _inline_local_schema_references(schema)
    if input_model in {
        RenderAnalysisBundleInput,
        RenderPathwayInput,
        RenderModuleInput,
    }:
        _expand_render_source_alternatives(schema)
    return schema


def _expand_render_source_alternatives(schema: dict[str, object]) -> None:
    """Keep the source XOR while making every Codex-visible branch a full object."""
    properties_value = schema.get("properties")
    if not isinstance(properties_value, dict):  # pragma: no cover - Pydantic contract guard
        raise RuntimeError("renderer MCP input schema does not expose object properties")
    properties = cast(dict[str, object], properties_value)
    for source_name in ("render_input_path", "render_input_json"):
        if not isinstance(properties.get(source_name), dict):  # pragma: no cover - contract guard
            raise RuntimeError("renderer MCP input schema omits a handoff source")

    schema["oneOf"] = [
        _render_source_alternative(
            properties,
            active_name="render_input_path",
            inactive_name="render_input_json",
        ),
        _render_source_alternative(
            properties,
            active_name="render_input_json",
            inactive_name="render_input_path",
        ),
    ]


def _render_source_alternative(
    properties: dict[str, object],
    *,
    active_name: str,
    inactive_name: str,
) -> dict[str, object]:
    branch_properties = deepcopy(properties)
    active_property = cast(dict[str, object], branch_properties[active_name])
    alternatives_value = active_property.get("anyOf")
    if not isinstance(alternatives_value, list):  # pragma: no cover - Pydantic contract guard
        raise RuntimeError("renderer MCP handoff source is not nullable")
    non_null_alternatives: list[dict[str, object]] = []
    for item in cast(list[object], alternatives_value):
        if isinstance(item, dict):
            item_mapping = cast(dict[str, object], item)
            if item_mapping.get("type") != "null":
                non_null_alternatives.append(deepcopy(item_mapping))
    if len(non_null_alternatives) != 1:  # pragma: no cover - Pydantic contract guard
        raise RuntimeError("renderer MCP handoff source has an unsupported schema")
    active_schema = non_null_alternatives[0]
    for annotation in ("description", "title"):
        if annotation in active_property:
            active_schema[annotation] = active_property[annotation]
    branch_properties[active_name] = active_schema
    branch_properties[inactive_name] = {
        "type": "null",
        "description": f"Must be omitted or null when {active_name} is provided.",
    }
    return {
        "type": "object",
        "properties": branch_properties,
        "required": [active_name],
        "additionalProperties": False,
    }


def _inline_local_schema_references(schema: dict[str, object]) -> None:
    """Inline Pydantic references for MCP clients that do not expand ``$defs``."""
    definitions_value = schema.get("$defs")
    if not isinstance(definitions_value, dict):
        return
    definitions = cast(dict[str, object], definitions_value)
    root = {key: value for key, value in schema.items() if key != "$defs"}
    expanded = _expand_local_schema_node(root, definitions, stack=())
    if not isinstance(expanded, dict):  # pragma: no cover - root is always a mapping
        raise RuntimeError("renderer MCP input schema expansion changed the root type")
    schema.clear()
    schema.update(cast(dict[str, object], expanded))


def _expand_local_schema_node(
    value: object,
    definitions: dict[str, object],
    *,
    stack: tuple[str, ...],
) -> object:
    if isinstance(value, list):
        return [
            _expand_local_schema_node(item, definitions, stack=stack)
            for item in cast(list[object], value)
        ]
    if not isinstance(value, dict):
        return value

    mapping = cast(dict[str, object], value)
    reference = mapping.get("$ref")
    if isinstance(reference, str):
        prefix = "#/$defs/"
        if not reference.startswith(prefix):
            raise RuntimeError("renderer MCP input schema has an unsupported reference")
        name = reference.removeprefix(prefix)
        definition = definitions.get(name)
        if not isinstance(definition, dict):
            raise RuntimeError("renderer MCP input schema has an unresolved reference")
        if name in stack:
            raise RuntimeError("renderer MCP input schema has a recursive reference")
        expanded = _expand_local_schema_node(
            cast(dict[str, object], definition),
            definitions,
            stack=(*stack, name),
        )
        if not isinstance(expanded, dict):  # pragma: no cover - guarded above
            raise RuntimeError("renderer MCP schema reference did not resolve to an object")
        expanded_mapping = cast(dict[str, object], expanded)
        for key, sibling in mapping.items():
            if key != "$ref":
                expanded_mapping[key] = _expand_local_schema_node(
                    sibling,
                    definitions,
                    stack=stack,
                )
        return expanded_mapping

    return {
        key: _expand_local_schema_node(item, definitions, stack=stack)
        for key, item in mapping.items()
    }


def _status(runtime: RendererRuntime) -> RendererStatus:
    limits = runtime.config.limits
    snapshot = runtime.service.store.snapshot()
    return RendererStatus(
        server_version=__version__,
        ready=True,
        pathway_access_configured=runtime.service.provider.configured,
        access_mode=runtime.config.access_mode,
        allowed_root_count=len(runtime.config.allowed_roots),
        retention_seconds=runtime.config.retention_seconds,
        retained_result_count=snapshot.active_result_count,
        cleanup_pending_result_count=snapshot.cleanup_pending_result_count,
        retained_bytes=snapshot.retained_bytes,
        retained_storage_bytes=snapshot.retained_storage_bytes,
        bounds=RendererBounds(
            max_input_bytes=limits.max_input_bytes,
            max_targets=MAX_TARGETS,
            max_results=limits.max_results,
            max_asset_bytes=limits.max_asset_bytes,
            max_pixels=limits.max_pixels,
            max_svg_bytes=limits.max_svg_bytes,
            max_result_bytes=limits.max_result_bytes,
            max_disk_bytes=limits.max_disk_bytes,
        ),
    )


def _parse(model: type[_M], arguments: dict[str, Any]) -> _M:
    try:
        return validate_tool_input(model, arguments)
    except ValidationError as error:
        raise _RequestValidationError(summarize_validation_error(error)) from None


def _connectivity_message(classification: ConnectivityStatus) -> str:
    return {
        ConnectivityStatus.REACHABLE: "The configured KEGG endpoint answered one INFO request.",
        ConnectivityStatus.NOT_CONFIGURED: "KEGG access is not configured for this renderer.",
        ConnectivityStatus.OFFLINE_CACHE: (
            "Network access is disabled by the renderer offline-cache deployment policy."
        ),
        ConnectivityStatus.DNS_FAILURE: "The configured KEGG endpoint name could not be resolved.",
        ConnectivityStatus.CONNECTION_FAILURE: "The configured KEGG endpoint could not be reached.",
        ConnectivityStatus.TIMEOUT: "The bounded KEGG INFO request timed out.",
        ConnectivityStatus.TLS_FAILURE: "TLS validation failed for the configured KEGG endpoint.",
        ConnectivityStatus.PERMISSION_DENIED: "The KEGG endpoint or environment denied access.",
        ConnectivityStatus.RATE_LIMITED: "The configured KEGG endpoint rate-limited the probe.",
        ConnectivityStatus.ENDPOINT_REJECTED: (
            "The configured endpoint returned an unsafe response."
        ),
        ConnectivityStatus.UNKNOWN_FAILURE: (
            "The bounded KEGG preflight failed for an unknown reason."
        ),
    }[classification]


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


def _internal_error(error: Exception, *, stage: str) -> ErrorDetail:
    correlation_id = f"err_{secrets.token_urlsafe(9)}"
    print(
        f"kegg-render-mcp internal error correlation_id={correlation_id} "
        f"stage={stage} type={type(error).__name__}",
        file=sys.stderr,
    )
    return ErrorDetail(
        code=ErrorCode.INTERNAL_ERROR,
        message="The renderer could not complete the local request safely.",
        suggested_action="Retry once, then report the correlation ID if the failure repeats.",
        safe_details=(
            SafeDetail(name="correlation_id", value=correlation_id),
            SafeDetail(name="stage", value=stage),
        ),
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
