"""Low-level MCP 1.x stdio server with explicit schemas and bounded resources."""

from __future__ import annotations

import base64
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, TypeVar, cast

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, BaseModel, ValidationError

from kegg_mcp import __version__
from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError, SafeDetail
from kegg_mcp.importers import SourceProvenanceInput
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
    ConnectivityToolEnvelope,
    EntriesToolEnvelope,
    GetKeggEntriesInput,
    GetServerStatusInput,
    KoMappingTarget,
    MapKoIdsInput,
    MappingToolEnvelope,
    NormalizeAnnotationsRequest,
    NormalizeKoAnnotationsInput,
    NormalizeToolEnvelope,
    OversizedArtifactNotice,
    PrimitiveAnalysisToolEnvelope,
    ProbeKeggConnectivityInput,
    ResultResourceIndex,
    StatusToolEnvelope,
    constrain_mcp_input_schema,
    constrain_mcp_output_schema,
)
from kegg_mcp.services import (
    KeggConnectivityClient,
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
    probe_kegg_connectivity_service,
    read_cached_kegg_entry,
    retrieve_kegg_entries,
)

SERVER_NAME = "kegg-mcp"
SERVER_INSTRUCTIONS = (
    "Prefer analyze_ko_annotations for an existing KO list or annotation table. Call "
    "get_server_status first when deployment state is unknown, and call probe_kegg_connectivity "
    "before the first network-dependent analysis. File paths and output directories require a "
    "configured allowed root. Result IDs are valid only in the same stdio process; use the stable "
    "output bundle for cross-process handoff. K-number assignments are annotations, exact MODULE "
    "completion is separate from block coverage, and pathway KO coverage does not prove pathway "
    "presence, activity, flux, or phenotype. This server never runs an external annotator or "
    "renders pathway graphics."
)
MAX_INLINE_RESOURCE_BYTES = 64 * 1024
TOOL_NAMES = (
    "analyze_ko_annotations",
    "normalize_ko_annotations",
    "get_kegg_entries",
    "map_ko_ids",
    "analyze_modules",
    "analyze_pathways",
    "compare_ko_sets",
    "probe_kegg_connectivity",
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
    allowed_roots: tuple[str, ...] = ()


def build_runtime(config: McpRuntimeConfig | None = None) -> McpRuntime:
    """Construct the default user-local public-academic runtime."""
    effective = config or load_runtime_config()
    return McpRuntime(
        client=KeggClient(effective.kegg),
        result_store=SQLiteResultStore(effective.result_store_path),
        scope_id=f"stdio-{secrets.token_urlsafe(24)}",
        allowed_roots=effective.allowed_roots,
    )


def create_server(runtime: McpRuntime | None = None) -> Server[object]:
    """Create one MCP server; dependencies can be injected by contract tests."""
    state = runtime or build_runtime()
    server: Server[object] = Server(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
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
                    normalization = _materialize_annotation_file(
                        supplied.annotations.to_service_request(), state.allowed_roots
                    )
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
                    pathway_selection=supplied.pathway_selection,
                    allow_global_or_overview=supplied.allow_global_or_overview,
                    output_directory=_resolve_output_directory(
                        supplied.output_directory or normalization.output_directory,
                        state.allowed_roots,
                    ),
                )
                return _success(
                    result,
                    "KO annotations were normalized and the requested KEGG analyses completed.",
                    result.result.result_id,
                )
            if name == "normalize_ko_annotations":
                supplied = _parse(NormalizeKoAnnotationsInput, arguments)
                service_request = supplied.to_service_request()
                materialized = _materialize_annotation_file(service_request, state.allowed_roots)
                result = normalize_annotations(
                    materialized,
                    result_store=state.result_store,
                    scope_id=state.scope_id,
                    output_directory=_resolve_output_directory(
                        supplied.output_directory, state.allowed_roots
                    ),
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
                    allow_global_or_overview=supplied.allow_global_or_overview,
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
                    allowed_root_count=len(state.allowed_roots),
                )
                return _success(result, "Returned redacted local server status.")
            if name == "probe_kegg_connectivity":
                _parse(ProbeKeggConnectivityInput, arguments)
                result = probe_kegg_connectivity_service(cast(KeggConnectivityClient, state.client))
                return _success(
                    result,
                    f"KEGG connectivity preflight completed: {result.state.value}.",
                )
            raise ValueError("Unknown MCP tool name.")
        except ValidationError as error:
            return _error(
                ErrorDetail(
                    code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                    message="The tool input did not satisfy its explicit schema.",
                    recoverable=True,
                    suggested_action="Correct the supplied fields using the tool input schema.",
                    safe_details=_validation_error_details(error),
                )
            )
        except KeggMcpError as error:
            return _error(error.detail)
        except ResultStoreError:
            return _error(
                ErrorDetail(
                    code=ErrorCode.RESULT_STORE_FAILED,
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
                allowed_root_count=len(state.allowed_roots),
            )
            return [_json_resource(status)]
        if value == "ko-analysis://cache/info":
            status = get_server_status_service(
                server_version=__version__,
                client=state.client,
                result_store=state.result_store,
                supported_tools=TOOL_NAMES,
                allowed_root_count=len(state.allowed_roots),
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
                code=ErrorCode.RESULT_STORE_FAILED,
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
    deterministic = types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    open_world = deterministic.model_copy(update={"openWorldHint": True})
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
                "MODULE and pathway analyses in one call; pathway_selection can rank candidates "
                "server-side and load references only for a bounded Top-N."
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
            NormalizeKoAnnotationsInput,
            NormalizeToolEnvelope,
            deterministic,
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
            "probe_kegg_connectivity",
            "Probe KEGG connectivity",
            (
                "Run one explicit low-cost KEGG INFO preflight and classify network or "
                "deployment failures before analysis."
            ),
            ProbeKeggConnectivityInput,
            ConnectivityToolEnvelope,
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


def _validation_error_details(error: ValidationError) -> tuple[SafeDetail, ...]:
    details = [SafeDetail(name="stage", value="input_validation")]
    for issue in error.errors(include_input=False, include_url=False)[:8]:
        location = ".".join(str(part) for part in issue.get("loc", ())) or "$"
        details.append(SafeDetail(name="field_path", value=location[:1_000]))
        details.append(
            SafeDetail(name="issue_type", value=str(issue.get("type", "invalid"))[:1_000])
        )
    details.append(SafeDetail(name="validation_issue_count", value=str(error.error_count())))
    return tuple(details)


def _materialize_annotation_file(
    request: NormalizeAnnotationsRequest,
    allowed_roots: tuple[str, ...],
) -> NormalizeAnnotationsRequest:
    """Load one shared file after canonical allowed-root and size validation."""
    if request.file_path is None:
        return request
    path = _resolve_existing_file(request.file_path, allowed_roots)
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
        )
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
        str(_resolve_existing_file(source.input_path, allowed_roots))
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


def _resolve_existing_file(value: str, allowed_roots: tuple[str, ...]) -> Path:
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


def _resolve_output_directory(
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
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "TOOL_NAMES",
    "McpRuntime",
    "build_runtime",
    "create_server",
    "main",
]
