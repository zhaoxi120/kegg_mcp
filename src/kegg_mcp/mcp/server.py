"""Low-level MCP 1.x stdio server with explicit schemas and bounded resources."""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import dataclass
from typing import Any, TypeVar, cast

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, BaseModel, ValidationError

from kegg_mcp import __version__
from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError, SafeDetail
from kegg_mcp.kegg import (
    GetRequest,
    KeggBriteEntryKind,
    KeggClient,
    KeggEntryRef,
    KeggGetDatabase,
    KeggLinkRelationship,
    LinkRequest,
)
from kegg_mcp.mcp.config import McpRuntimeConfig, load_runtime_config
from kegg_mcp.mcp.contracts import (
    AnalyzeKoAnnotationsInput,
    AnalyzeModulesInput,
    AnalyzePathwaysInput,
    ArtifactRangeEnvelope,
    CacheInfoResource,
    CompareKoSetsInput,
    CompareToolEnvelope,
    EntriesToolEnvelope,
    GetKeggEntriesInput,
    GetServerStatusInput,
    KoMappingTarget,
    MapKoIdsInput,
    MappingToolEnvelope,
    NormalizeAnnotationsRequest,
    NormalizeToolEnvelope,
    OversizedArtifactNotice,
    PrimitiveAnalysisToolEnvelope,
    ResultResourceIndex,
    StatusToolEnvelope,
    constrain_mcp_input_schema,
    constrain_mcp_output_schema,
    options,
)
from kegg_mcp.services import (
    KeggPrimitiveClient,
    ResultStoreError,
    SQLiteResultStore,
    analyze_annotation_targets,
    analyze_module_targets,
    analyze_pathway_targets,
    compare_annotation_sets,
    get_server_status_service,
    map_ko_identifiers,
    normalize_annotations,
    read_cached_kegg_entry,
    retrieve_kegg_entries,
)

SERVER_NAME = "kegg-mcp"
MAX_INLINE_RESOURCE_BYTES = 64 * 1024
TOOL_NAMES = (
    "analyze_ko_annotations",
    "normalize_ko_annotations",
    "get_kegg_entries",
    "map_ko_ids",
    "analyze_modules",
    "analyze_pathways",
    "compare_ko_sets",
    "get_server_status",
)

_RESULT_RE = re.compile(r"ko-analysis://results/(res_[A-Za-z0-9_-]{32})\Z")
_SECTION_RE = re.compile(
    r"ko-analysis://results/(res_[A-Za-z0-9_-]{32})/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z"
)
_RANGE_RE = re.compile(
    r"ko-analysis://results/(res_[A-Za-z0-9_-]{32})/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/([0-9]{1,19})/([0-9]{1,5})\Z"
)
_CACHE_ENTRY_RE = re.compile(r"kegg-cache://entries/([a-z]+)/([A-Za-z0-9.-]{1,100})\Z")

_M = TypeVar("_M", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class McpRuntime:
    """Injected process-local services and one opaque result scope."""

    client: KeggPrimitiveClient
    result_store: SQLiteResultStore
    scope_id: str


def build_runtime(config: McpRuntimeConfig | None = None) -> McpRuntime:
    """Construct the default user-local offline-safe runtime."""
    effective = config or load_runtime_config()
    return McpRuntime(
        client=KeggClient(effective.kegg),
        result_store=SQLiteResultStore(effective.result_store_path),
        scope_id=f"stdio-{secrets.token_urlsafe(24)}",
    )


def create_server(runtime: McpRuntime | None = None) -> Server[object]:
    """Create one MCP server; dependencies can be injected by offline contract tests."""
    state = runtime or build_runtime()
    server: Server[object] = Server(
        SERVER_NAME,
        version=__version__,
        instructions=(
            "Analyze user-supplied KO annotations conservatively. K-number assignments are "
            "annotations, pathway coverage is descriptive, and external annotators are not run."
        ),
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:  # pyright: ignore[reportUnusedFunction]
        return _tool_definitions()

    @server.call_tool(validate_input=False)
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        try:
            if name == "analyze_ko_annotations":
                supplied = _parse(AnalyzeKoAnnotationsInput, arguments)
                if supplied.annotations is not None:
                    normalization = supplied.annotations
                else:
                    if supplied.ko_text is None:
                        raise AssertionError(
                            "validated analysis input omitted its annotation source"
                        )
                    normalization = NormalizeAnnotationsRequest(
                        text=supplied.ko_text,
                        analysis_unit=supplied.analysis_unit,
                        sample_id=supplied.sample_id,
                    )
                result = analyze_annotation_targets(
                    normalization,
                    module_ids=supplied.module_ids,
                    pathways=supplied.pathways,
                    client=state.client,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                    pathway_evidence_mode=supplied.pathway_evidence_mode,
                    allow_global_or_overview=supplied.allow_global_or_overview,
                    options=options(supplied.refresh, supplied.allow_stale),
                )
                return _success(
                    result,
                    "KO annotations were normalized and the requested KEGG analyses completed.",
                    result.result.result_id,
                )
            if name == "normalize_ko_annotations":
                supplied = _parse(NormalizeAnnotationsRequest, arguments)
                result = normalize_annotations(
                    supplied,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                )
                return _success(
                    result,
                    "Annotations were normalized and retained as a scoped typed dataset.",
                    result.result.result_id,
                )
            if name == "get_kegg_entries":
                supplied = _parse(GetKeggEntriesInput, arguments)
                result = retrieve_kegg_entries(
                    GetRequest(entries=supplied.entries),
                    client=state.client,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                    options=options(supplied.refresh, supplied.allow_stale),
                )
                return _success(
                    result,
                    f"Retrieved {result.returned_count} of {result.requested_count} KEGG entries.",
                    result.result.result_id,
                )
            if name == "map_ko_ids":
                supplied = _parse(MapKoIdsInput, arguments)
                result = map_ko_identifiers(
                    LinkRequest(
                        relationship=_relationship(supplied.target),
                        source_identifiers=supplied.ko_ids,
                    ),
                    client=state.client,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                    options=options(supplied.refresh, supplied.allow_stale),
                    preview_limit=supplied.preview_limit,
                )
                return _success(
                    result,
                    (
                        f"Mapped selected K numbers to {supplied.target.value}; "
                        "full rows are retained."
                    ),
                    result.result.result_id,
                )
            if name == "analyze_modules":
                supplied = _parse(AnalyzeModulesInput, arguments)
                result = analyze_module_targets(
                    supplied.source,
                    supplied.module_ids,
                    client=state.client,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                    options=options(supplied.refresh, supplied.allow_stale),
                    reference_limits=supplied.reference_limits,
                    analysis_limits=supplied.analysis_limits,
                )
                return _success(
                    result,
                    "MODULE exact completion and block coverage were evaluated separately.",
                    result.result.result_id,
                )
            if name == "analyze_pathways":
                supplied = _parse(AnalyzePathwaysInput, arguments)
                result = analyze_pathway_targets(
                    supplied.source,
                    supplied.pathways,
                    client=state.client,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                    evidence_mode=supplied.evidence_mode,
                    allow_global_or_overview=supplied.allow_global_or_overview,
                    options=options(supplied.refresh, supplied.allow_stale),
                    reference_limits=supplied.reference_limits,
                    pathway_limits=supplied.pathway_limits,
                )
                return _success(
                    result,
                    (
                        "Descriptive unique-KO pathway coverage was evaluated with "
                        "explicit denominators."
                    ),
                    result.result.result_id,
                )
            if name == "compare_ko_sets":
                supplied = _parse(CompareKoSetsInput, arguments)
                result = compare_annotation_sets(
                    supplied.inputs,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                    client=state.client,
                    module_ids=supplied.module_ids,
                    pathways=supplied.pathways,
                    options=options(supplied.refresh, supplied.allow_stale),
                    reference_limits=supplied.reference_limits,
                    module_limits=supplied.module_limits,
                    pathway_limits=supplied.pathway_limits,
                    functional_limits=supplied.functional_limits,
                    allow_global_or_overview=supplied.allow_global_or_overview,
                    limits=supplied.limits,
                    preview_limits=supplied.preview_limits,
                )
                return _success(
                    result,
                    (
                        "Deterministic KO set differences were computed; "
                        "no statistical inference was made."
                    ),
                    result.result.result_id,
                )
            if name == "get_server_status":
                _parse(GetServerStatusInput, arguments)
                result = get_server_status_service(
                    server_version=__version__,
                    client=state.client,
                    result_store=state.result_store,
                    supported_tools=TOOL_NAMES,
                )
                return _success(result, "Returned redacted local server status.")
            raise ValueError("Unknown MCP tool name.")
        except ValidationError as error:
            return _error(
                ErrorDetail(
                    code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                    message="The tool input did not satisfy its explicit schema.",
                    recoverable=True,
                    suggested_action="Correct the supplied fields using the tool input schema.",
                    safe_details=(
                        SafeDetail(name="validation_issue_count", value=str(error.error_count())),
                    ),
                )
            )
        except KeggMcpError as error:
            return _error(error.detail)
        except ResultStoreError:
            return _error(
                ErrorDetail(
                    code=ErrorCode.CACHE_FAILED,
                    message="The local retained-result store could not be used safely.",
                    recoverable=True,
                    suggested_action="Check local storage permissions and retry.",
                )
            )
        except (TypeError, ValueError):
            return _error(
                ErrorDetail(
                    code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                    message="The tool request could not be configured safely.",
                    recoverable=True,
                    suggested_action="Inspect the declared schema and correct the request.",
                )
            )

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:  # pyright: ignore[reportUnusedFunction]
        return [
            types.Resource(
                name="server-status",
                title="KEGG MCP server status",
                uri=AnyUrl("ko-analysis://status"),
                description="Redacted server capabilities and access mode.",
                mimeType="application/json",
            ),
            types.Resource(
                name="cache-info",
                title="KEGG cache information",
                uri=AnyUrl("ko-analysis://cache/info"),
                description="Redacted local cache configuration without paths or credentials.",
                mimeType="application/json",
            ),
        ]

    @server.list_resource_templates()
    async def _list_resource_templates(  # pyright: ignore[reportUnusedFunction]
    ) -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                name="result-index",
                title="Scoped retained result",
                uriTemplate="ko-analysis://results/{result_id}",
                description="Metadata and validated section links for one scoped result.",
                mimeType="application/json",
            ),
            types.ResourceTemplate(
                name="result-section",
                title="Scoped retained result section",
                uriTemplate="ko-analysis://results/{result_id}/{section}",
                description=(
                    "Returns a small section directly or a pagination notice for a large section."
                ),
            ),
            types.ResourceTemplate(
                name="result-section-range",
                title="Bounded retained result byte range",
                uriTemplate="ko-analysis://results/{result_id}/{section}/{offset}/{limit}",
                description=(
                    "Returns at most 65536 bytes as base64 with an explicit continuation URI."
                ),
                mimeType="application/json",
            ),
            types.ResourceTemplate(
                name="cached-kegg-entry",
                title="Cached KEGG entry",
                uriTemplate="kegg-cache://entries/{database}/{identifier}",
                description=(
                    "Reads only the configured local cache namespace; never triggers network I/O."
                ),
                mimeType="application/json",
            ),
        ]

    async def _read_resource_impl(value: str) -> list[ReadResourceContents]:
        if value == "ko-analysis://status":
            status = get_server_status_service(
                server_version=__version__,
                client=state.client,
                result_store=state.result_store,
                supported_tools=TOOL_NAMES,
            )
            return [_json_resource(status)]
        if value == "ko-analysis://cache/info":
            status = get_server_status_service(
                server_version=__version__,
                client=state.client,
                result_store=state.result_store,
                supported_tools=TOOL_NAMES,
            )
            return [
                _json_resource(
                    CacheInfoResource(
                        access_mode=status.access_mode.value,
                        cache_endpoint_class=status.cache_endpoint_class.value,
                        network_enabled=status.network_enabled,
                    )
                )
            ]
        if match := _RESULT_RE.fullmatch(value):
            result_id = match.group(1)
            metadata = state.result_store.get_result(state.scope_id, result_id)
            artifacts = state.result_store.list_artifacts(state.scope_id, result_id)
            return [
                _json_resource(
                    ResultResourceIndex(
                        result=metadata,
                        artifacts=artifacts.items,
                        section_uris=tuple(
                            f"ko-analysis://results/{result_id}/{item.section}"
                            for item in artifacts.items
                        ),
                    )
                )
            ]
        if match := _RANGE_RE.fullmatch(value):
            result_id, section, offset_text, limit_text = match.groups()
            offset = int(offset_text)
            limit = int(limit_text)
            if offset > (1 << 63) - 1:
                raise ValueError("Resource range offset exceeds the supported integer bound.")
            if limit < 1 or limit > MAX_INLINE_RESOURCE_BYTES:
                raise ValueError("Resource range limit must be between 1 and 65536 bytes.")
            page = state.result_store.read_artifact(
                state.scope_id,
                result_id,
                section,
                offset=offset,
                limit=limit,
            )
            next_uri = (
                None
                if page.next_offset is None
                else f"ko-analysis://results/{result_id}/{section}/{page.next_offset}/{limit}"
            )
            return [
                _json_resource(
                    ArtifactRangeEnvelope(
                        result_id=result_id,
                        section=section,
                        mime_type=page.mime_type,
                        sha256=page.sha256,
                        total_bytes=page.total_bytes,
                        offset=page.offset,
                        returned_bytes=page.returned_bytes,
                        content_base64=base64.b64encode(page.content).decode("ascii"),
                        next_uri=next_uri,
                    )
                )
            ]
        if match := _SECTION_RE.fullmatch(value):
            result_id, section = match.groups()
            page = state.result_store.read_artifact(
                state.scope_id,
                result_id,
                section,
                limit=MAX_INLINE_RESOURCE_BYTES,
            )
            if page.next_offset is None:
                content: str | bytes
                if page.mime_type.startswith("text/") or page.mime_type == "application/json":
                    try:
                        content = page.content.decode("utf-8", errors="strict")
                    except UnicodeDecodeError as error:
                        raise ValueError(
                            "A textual artifact did not contain valid UTF-8."
                        ) from error
                else:
                    content = page.content
                return [ReadResourceContents(content=content, mime_type=page.mime_type)]
            return [
                _json_resource(
                    OversizedArtifactNotice(
                        result_id=result_id,
                        section=section,
                        mime_type=page.mime_type,
                        total_bytes=page.total_bytes,
                        sha256=page.sha256,
                        next_uri=(
                            f"ko-analysis://results/{result_id}/{section}/0/"
                            f"{MAX_INLINE_RESOURCE_BYTES}"
                        ),
                        maximum_range_bytes=MAX_INLINE_RESOURCE_BYTES,
                    )
                )
            ]
        if match := _CACHE_ENTRY_RE.fullmatch(value):
            database_text, identifier = match.groups()
            try:
                database = KeggGetDatabase(database_text)
            except ValueError as error:
                raise ValueError("Unsupported cached KEGG database.") from error
            entry = KeggEntryRef(
                database=database,
                identifier=identifier,
                brite_kind=(
                    KeggBriteEntryKind.HIERARCHY if database is KeggGetDatabase.BRITE else None
                ),
            )
            result = read_cached_kegg_entry(
                GetRequest(entries=(entry,)),
                client=state.client,
            )
            return [_json_resource(result)]
        raise ValueError("Unknown or non-canonical resource URI.")

    @server.read_resource()
    async def _read_resource(  # pyright: ignore[reportUnusedFunction]
        uri: AnyUrl,
    ) -> list[ReadResourceContents]:
        try:
            return await _read_resource_impl(str(uri))
        except KeggMcpError as error:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message=f"{error.detail.code.value}: {error.detail.message}",
                    data=error.detail.model_dump(mode="json"),
                )
            ) from None
        except ResultStoreError:
            detail = ErrorDetail(
                code=ErrorCode.CACHE_FAILED,
                message="The local retained-result store could not be used safely.",
                recoverable=True,
                suggested_action="Check local storage permissions and retry.",
            )
            raise McpError(
                types.ErrorData(
                    code=types.INTERNAL_ERROR,
                    message=f"{detail.code.value}: {detail.message}",
                    data=detail.model_dump(mode="json"),
                )
            ) from None
        except (TypeError, ValueError):
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message="INVALID_RESOURCE_URI: unknown or non-canonical resource URI",
                )
            ) from None

    original_call_handler = server.request_handlers[types.CallToolRequest]

    async def _reject_unknown_tools(request: types.CallToolRequest) -> types.ServerResult:
        if request.params.name not in TOOL_NAMES:
            raise McpError(
                types.ErrorData(
                    code=types.INVALID_PARAMS,
                    message="Unknown MCP tool name.",
                )
            )
        return await original_call_handler(request)

    server.request_handlers[types.CallToolRequest] = _reject_unknown_tools
    return server


def _tool_definitions() -> list[types.Tool]:
    mutating = types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
    open_world = mutating.model_copy(update={"openWorldHint": True})
    status = types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    return [
        _tool(
            "analyze_ko_annotations",
            "Analyze KO annotations",
            (
                "Normalize an inline KO list or supported annotation table and run requested "
                "MODULE and pathway analyses in one call."
            ),
            AnalyzeKoAnnotationsInput,
            PrimitiveAnalysisToolEnvelope,
            open_world,
        ),
        _tool(
            "normalize_ko_annotations",
            "Normalize KO annotations",
            (
                "Normalize an inline plain, generic table, or DeepKOALA detailed payload "
                "and retain it."
            ),
            NormalizeAnnotationsRequest,
            NormalizeToolEnvelope,
            mutating,
        ),
        _tool(
            "get_kegg_entries",
            "Get KEGG entries",
            "Retrieve allowlisted KEGG entries with bounded batching; this is not a URL proxy.",
            GetKeggEntriesInput,
            EntriesToolEnvelope,
            open_world,
        ),
        _tool(
            "map_ko_ids",
            "Map KO identifiers",
            "Map selected K numbers to pathways, modules, reactions, EC numbers, or BRITE.",
            MapKoIdsInput,
            MappingToolEnvelope,
            open_world,
        ),
        _tool(
            "analyze_modules",
            "Analyze KEGG MODULEs",
            (
                "Evaluate exact MODULE completion and block coverage against inline or "
                "retained evidence."
            ),
            AnalyzeModulesInput,
            PrimitiveAnalysisToolEnvelope,
            open_world,
        ),
        _tool(
            "analyze_pathways",
            "Analyze KEGG pathways",
            "Compute descriptive unique-KO coverage with an explicit reference namespace.",
            AnalyzePathwaysInput,
            PrimitiveAnalysisToolEnvelope,
            open_world,
        ),
        _tool(
            "compare_ko_sets",
            "Compare KO sets",
            "Compute deterministic KO set differences without statistical interpretation.",
            CompareKoSetsInput,
            CompareToolEnvelope,
            open_world,
        ),
        _tool(
            "get_server_status",
            "Get KEGG MCP status",
            "Return redacted capabilities, access mode, and local retention limits.",
            GetServerStatusInput,
            StatusToolEnvelope,
            status,
        ),
    ]


def _tool(
    name: str,
    title: str,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    annotations: types.ToolAnnotations,
) -> types.Tool:
    input_schema = input_model.model_json_schema(mode="validation")
    output_schema = output_model.model_json_schema(mode="serialization")
    constrain_mcp_input_schema(input_schema)
    constrain_mcp_output_schema(output_schema)
    _remove_nested_schema_identities(input_schema)
    _remove_nested_schema_identities(output_schema)
    return types.Tool(
        name=name,
        title=title,
        description=description,
        inputSchema=input_schema,
        outputSchema=output_schema,
        annotations=annotations,
    )


def _remove_nested_schema_identities(value: object) -> None:
    """Keep bundled local references rooted at the tool schema, not nested model IDs."""
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        mapping.pop("$id", None)
        mapping.pop("$schema", None)
        for nested in mapping.values():
            _remove_nested_schema_identities(nested)
    elif isinstance(value, list):
        for nested in cast(list[object], value):
            _remove_nested_schema_identities(nested)


def _parse(model: type[_M], arguments: dict[str, Any]) -> _M:
    return model.model_validate_json(json.dumps(arguments, ensure_ascii=False))


def _success(
    data: BaseModel,
    summary: str,
    result_id: str | None = None,
) -> types.CallToolResult:
    uri = None if result_id is None else f"ko-analysis://results/{result_id}"
    structured = {
        "ok": True,
        "result": {
            "data": data.model_dump(mode="json"),
            "resource_uri": uri,
        },
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


def _error(detail: ErrorDetail) -> types.CallToolResult:
    structured = {"ok": False, "result": None, "error": detail.model_dump(mode="json")}
    action = f" Suggested action: {detail.suggested_action}" if detail.suggested_action else ""
    return types.CallToolResult(
        content=[
            types.TextContent(type="text", text=f"{detail.code.value}: {detail.message}{action}")
        ],
        structuredContent=structured,
        isError=True,
    )


def _relationship(target: KoMappingTarget) -> KeggLinkRelationship:
    return {
        KoMappingTarget.PATHWAY: KeggLinkRelationship.KO_TO_PATHWAY,
        KoMappingTarget.MODULE: KeggLinkRelationship.KO_TO_MODULE,
        KoMappingTarget.REACTION: KeggLinkRelationship.KO_TO_REACTION,
        KoMappingTarget.EC: KeggLinkRelationship.KO_TO_ENZYME,
        KoMappingTarget.BRITE: KeggLinkRelationship.KO_TO_BRITE,
    }[target]


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
    """Run the local stdio server without application output on stdout."""
    try:
        anyio.run(_run_stdio)
    except (OSError, ValueError) as error:
        raise SystemExit(
            "Invalid KEGG MCP local configuration; review the documented environment variables."
        ) from error


__all__ = [
    "MAX_INLINE_RESOURCE_BYTES",
    "SERVER_NAME",
    "TOOL_NAMES",
    "McpRuntime",
    "build_runtime",
    "create_server",
    "main",
]
