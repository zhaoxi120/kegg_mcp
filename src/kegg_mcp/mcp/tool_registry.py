"""Single authoritative registry for core MCP tool metadata and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from anyio import lowlevel, to_thread
from mcp import types
from pydantic import BaseModel, ValidationError

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError
from kegg_mcp.mcp.contracts import (
    AnalyzeKoAnnotationsInput,
    AnalyzeKoAnnotationsToolEnvelope,
    AnalyzeModulesInput,
    AnalyzeModulesToolEnvelope,
    AnalyzePathwaysInput,
    AnalyzePathwaysToolEnvelope,
    AnnotationAuditToolEnvelope,
    AuditAnnotationMappingInput,
    BriteHierarchyToolEnvelope,
    CompareKoSetsInput,
    CompareToolEnvelope,
    ConnectivityToolEnvelope,
    DeleteAnalysisResultInput,
    DeleteToolEnvelope,
    EntriesToolEnvelope,
    GetKeggEntriesInput,
    GetServerStatusInput,
    ListAnalysisResultsInput,
    ListResultsToolEnvelope,
    MapBriteHierarchyInput,
    NormalizeKoAnnotationsInput,
    NormalizeToolEnvelope,
    ProbeKeggConnectivityInput,
    ResolveEntitiesToolEnvelope,
    ResolveKeggEntitiesInput,
    SearchEntriesToolEnvelope,
    SearchKeggEntriesInput,
    StatusToolEnvelope,
    TraceKeggRelationsInput,
    TraceRelationsToolEnvelope,
    constrain_mcp_input_schema,
    constrain_mcp_output_schema,
)
from kegg_mcp.mcp.input_validation import validate_tool_input
from kegg_mcp.mcp.responses import error_result, internal_error, success, validation_error
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.tool_handlers import (
    ToolContext,
    ToolHandler,
    analyze_annotations,
    analyze_modules,
    analyze_pathways,
    audit_mapping,
    compare_sets,
    delete_result,
    get_entries,
    get_status,
    list_results,
    map_brite,
    normalize,
    probe_connectivity,
    resolve_entities,
    search_entries,
    trace_relations,
)
from kegg_mcp.services.result_store import ResultStoreError


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    title: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    annotations: types.ToolAnnotations
    handler: ToolHandler


_ADDITIVE_CLOSED = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
_ADDITIVE_OPEN = _ADDITIVE_CLOSED.model_copy(update={"openWorldHint": True})
_DESTRUCTIVE_CLOSED = types.ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=False,
)
_READ_ONLY_CLOSED = types.ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)

TOOL_SPECS = (
    ToolSpec(
        "analyze_ko_annotations",
        "Analyze KO annotations",
        (
            "Normalize an inline KO list or supported annotation table and run requested "
            "MODULE and pathway analyses in one call; pathway_selection can rank candidates "
            "server-side and load references only for a bounded Top-N."
        ),
        AnalyzeKoAnnotationsInput,
        AnalyzeKoAnnotationsToolEnvelope,
        _ADDITIVE_OPEN,
        analyze_annotations,
    ),
    ToolSpec(
        "normalize_ko_annotations",
        "Normalize KO annotations",
        "Normalize an inline plain, generic table, or DeepKOALA detailed payload and retain it.",
        NormalizeKoAnnotationsInput,
        NormalizeToolEnvelope,
        _ADDITIVE_CLOSED,
        normalize,
    ),
    ToolSpec(
        "get_kegg_entries",
        "Get KEGG entries",
        "Retrieve allowlisted KEGG entries with bounded batching; this is not a URL proxy.",
        GetKeggEntriesInput,
        EntriesToolEnvelope,
        _ADDITIVE_OPEN,
        get_entries,
    ),
    ToolSpec(
        "search_kegg_entries",
        "Search KEGG entries",
        (
            "Search one allowlisted KEGG database with bounded FIND semantics. Results are "
            "endpoint candidates without an invented relevance score or selected best match."
        ),
        SearchKeggEntriesInput,
        SearchEntriesToolEnvelope,
        _ADDITIVE_OPEN,
        search_entries,
    ),
    ToolSpec(
        "resolve_kegg_entities",
        "Resolve KEGG entities",
        (
            "Resolve bounded gene or organism identifiers through typed FIND, GET, CONV, LINK, "
            "and optional organism-pathway LIST steps while preserving ambiguity, mismatch, and "
            "unmapped outcomes."
        ),
        ResolveKeggEntitiesInput,
        ResolveEntitiesToolEnvelope,
        _ADDITIVE_OPEN,
        resolve_entities,
    ),
    ToolSpec(
        "trace_kegg_relations",
        "Trace KEGG relations",
        (
            "Trace one or two bounded levels of allowlisted typed KEGG cross-references. "
            "Returned edges do not establish regulation, causality, activity, or phenotype."
        ),
        TraceKeggRelationsInput,
        TraceRelationsToolEnvelope,
        _ADDITIVE_OPEN,
        trace_relations,
    ),
    ToolSpec(
        "map_brite_hierarchy",
        "Map BRITE hierarchy",
        (
            "Map typed KEGG entities into bounded BRITE hierarchy paths. Omitting brite_ids "
            "uses KO-only BRITE discovery; counts are descriptive unique-input classifications."
        ),
        MapBriteHierarchyInput,
        BriteHierarchyToolEnvelope,
        _ADDITIVE_OPEN,
        map_brite,
    ),
    ToolSpec(
        "audit_annotation_mapping",
        "Audit annotation mapping",
        (
            "Audit strict and lenient KO evidence and optional descriptive mapping yields for "
            "selected KEGG pathway, MODULE, reaction, enzyme, and BRITE relationships."
        ),
        AuditAnnotationMappingInput,
        AnnotationAuditToolEnvelope,
        _ADDITIVE_OPEN,
        audit_mapping,
    ),
    ToolSpec(
        "analyze_modules",
        "Analyze KEGG MODULEs",
        "Evaluate exact MODULE completion and block coverage against inline or retained evidence.",
        AnalyzeModulesInput,
        AnalyzeModulesToolEnvelope,
        _ADDITIVE_OPEN,
        analyze_modules,
    ),
    ToolSpec(
        "analyze_pathways",
        "Analyze KEGG pathways",
        "Compute descriptive unique-KO coverage with an explicit reference namespace.",
        AnalyzePathwaysInput,
        AnalyzePathwaysToolEnvelope,
        _ADDITIVE_OPEN,
        analyze_pathways,
    ),
    ToolSpec(
        "compare_ko_sets",
        "Compare KO sets",
        "Compute deterministic KO set differences without statistical interpretation.",
        CompareKoSetsInput,
        CompareToolEnvelope,
        _ADDITIVE_OPEN,
        compare_sets,
    ),
    ToolSpec(
        "probe_kegg_connectivity",
        "Probe KEGG connectivity",
        (
            "Run one explicit low-cost KEGG INFO preflight and classify network or deployment "
            "failures before analysis."
        ),
        ProbeKeggConnectivityInput,
        ConnectivityToolEnvelope,
        _ADDITIVE_OPEN,
        probe_connectivity,
    ),
    ToolSpec(
        "list_analysis_results",
        "List retained analysis results",
        (
            "List one bounded metadata page of active results owned by the current stdio "
            "session; other sessions are never visible."
        ),
        ListAnalysisResultsInput,
        ListResultsToolEnvelope,
        _READ_ONLY_CLOSED,
        list_results,
    ),
    ToolSpec(
        "delete_analysis_result",
        "Delete retained analysis result",
        (
            "Immediately delete one retained result from the current stdio session; unknown, "
            "expired, deleted, and cross-scope identifiers remain indistinguishable."
        ),
        DeleteAnalysisResultInput,
        DeleteToolEnvelope,
        _DESTRUCTIVE_CLOSED,
        delete_result,
    ),
    ToolSpec(
        "get_server_status",
        "Get KEGG MCP status",
        "Return redacted capabilities, access mode, and local retention limits.",
        GetServerStatusInput,
        StatusToolEnvelope,
        _READ_ONLY_CLOSED,
        get_status,
    ),
)

TOOL_NAMES = tuple(spec.name for spec in TOOL_SPECS)
_TOOL_BY_NAME = {spec.name: spec for spec in TOOL_SPECS}
if len(_TOOL_BY_NAME) != len(TOOL_SPECS):  # pragma: no cover - import-time contract guard
    raise RuntimeError("core MCP tool registry names must be unique")

_CLIENT_TOOL_NAMES = frozenset(
    {
        "analyze_ko_annotations",
        "get_kegg_entries",
        "search_kegg_entries",
        "resolve_kegg_entities",
        "trace_kegg_relations",
        "map_brite_hierarchy",
        "audit_annotation_mapping",
        "analyze_modules",
        "analyze_pathways",
        "compare_ko_sets",
        "probe_kegg_connectivity",
    }
)
_LOCAL_TOOL_NAMES = frozenset(
    {
        "normalize_ko_annotations",
        "list_analysis_results",
        "delete_analysis_result",
    }
)
_INLINE_TOOL_NAMES = frozenset({"get_server_status"})
_CLASSIFIED_TOOL_NAMES = _CLIENT_TOOL_NAMES | _LOCAL_TOOL_NAMES | _INLINE_TOOL_NAMES
if (  # pragma: no cover - import-time contract guard
    frozenset(TOOL_NAMES) != _CLASSIFIED_TOOL_NAMES
    or _CLIENT_TOOL_NAMES & _LOCAL_TOOL_NAMES
    or _CLIENT_TOOL_NAMES & _INLINE_TOOL_NAMES
    or _LOCAL_TOOL_NAMES & _INLINE_TOOL_NAMES
):
    raise RuntimeError("every registered tool must have exactly one execution class")


def tool_definitions() -> list[types.Tool]:
    return [_tool(spec) for spec in TOOL_SPECS]


async def dispatch_tool(
    name: str,
    arguments: dict[str, Any],
    runtime: McpRuntime,
) -> types.CallToolResult:
    spec = _TOOL_BY_NAME.get(name)
    if spec is None:  # pragma: no cover - protocol guard rejects this first
        return error_result(
            ErrorDetail(
                code=ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                message="The requested MCP tool name is unknown.",
                recoverable=True,
                suggested_action="Use a tool name returned by tools/list.",
            )
        )
    try:
        request = validate_tool_input(spec.input_model, arguments)
    except ValidationError as exception:
        return error_result(
            validation_error(
                exception,
                input_model=spec.input_model,
            )
        )
    try:
        if name in _INLINE_TOOL_NAMES:
            outcome = spec.handler(ToolContext(runtime, TOOL_NAMES), request)
        else:
            limiter = (
                runtime.client_handler_limiter
                if name in _CLIENT_TOOL_NAMES
                else runtime.local_handler_limiter
            )
            try:
                outcome = await to_thread.run_sync(
                    spec.handler,
                    ToolContext(runtime, TOOL_NAMES),
                    request,
                    abandon_on_cancel=False,
                    limiter=limiter,
                )
            finally:
                await lowlevel.checkpoint_if_cancelled()
    except KeggMcpError as exception:
        return error_result(exception.detail)
    except ResultStoreError:
        return error_result(
            ErrorDetail(
                code=ErrorCode.RESULT_STORE_FAILED,
                message="The local retained-result store could not be used safely.",
                recoverable=True,
                suggested_action="Check local storage permissions and retry.",
            )
        )
    except Exception as exception:
        return error_result(internal_error(exception, stage=f"tool:{name}"))
    return success(outcome.data, outcome.summary, outcome.result_id)


def _tool(spec: ToolSpec) -> types.Tool:
    input_schema = spec.input_model.model_json_schema(mode="validation")
    if spec.input_model.__pydantic_root_model__:
        input_schema["type"] = "object"
    output_schema = spec.output_model.model_json_schema(mode="serialization")
    constrain_mcp_input_schema(input_schema)
    constrain_mcp_output_schema(output_schema)
    _inline_local_schema_references(input_schema)
    discriminator = input_schema.get("discriminator")
    if isinstance(discriminator, dict):
        cast(dict[str, object], discriminator).pop("mapping", None)
    _remove_nested_schema_identities(input_schema)
    _remove_nested_schema_identities(output_schema)
    return types.Tool(
        name=spec.name,
        title=spec.title,
        description=spec.description,
        inputSchema=input_schema,
        outputSchema=output_schema,
        annotations=spec.annotations,
    )


def _inline_local_schema_references(schema: dict[str, object]) -> None:
    """Inline Pydantic local references for clients that do not expand ``$defs``."""
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return
    definition_map = cast(dict[str, object], definitions)
    root = {key: value for key, value in schema.items() if key != "$defs"}
    expanded = _expand_local_schema_node(root, definition_map, stack=())
    if not isinstance(expanded, dict):  # pragma: no cover - root is always a mapping
        raise RuntimeError("MCP input schema expansion did not preserve the root object")
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
            raise RuntimeError("MCP input schema contains an unsupported reference")
        name = reference.removeprefix(prefix)
        definition = definitions.get(name)
        if not isinstance(definition, dict):
            raise RuntimeError("MCP input schema contains an unresolved local reference")
        if name in stack:
            raise RuntimeError("MCP input schema contains a recursive local reference")
        expanded_definition = _expand_local_schema_node(
            cast(dict[str, object], definition),
            definitions,
            stack=(*stack, name),
        )
        if not isinstance(expanded_definition, dict):  # pragma: no cover - guarded above
            raise RuntimeError("MCP input schema reference did not resolve to an object")
        expanded_mapping = cast(dict[str, object], expanded_definition)
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


def _remove_nested_schema_identities(value: object) -> None:
    if isinstance(value, dict):
        mapping = cast(dict[str, object], value)
        mapping.pop("$id", None)
        mapping.pop("$schema", None)
        for nested in mapping.values():
            _remove_nested_schema_identities(nested)
    elif isinstance(value, list):
        for nested in cast(list[object], value):
            _remove_nested_schema_identities(nested)


__all__ = ["TOOL_NAMES", "TOOL_SPECS", "ToolSpec", "dispatch_tool", "tool_definitions"]
