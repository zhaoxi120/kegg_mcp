"""Single authoritative registry for core MCP tool metadata and dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

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
    MapKoIdsInput,
    MappingToolEnvelope,
    NormalizeKoAnnotationsInput,
    NormalizeToolEnvelope,
    ProbeKeggConnectivityInput,
    StatusToolEnvelope,
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
    compare_sets,
    delete_result,
    get_entries,
    get_status,
    list_results,
    map_identifiers,
    normalize,
    probe_connectivity,
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
        "map_ko_ids",
        "Map KO identifiers",
        "Map selected K numbers to pathways, modules, reactions, EC numbers, or BRITE.",
        MapKoIdsInput,
        MappingToolEnvelope,
        _ADDITIVE_OPEN,
        map_identifiers,
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
        return error_result(validation_error(exception))
    try:
        outcome = await spec.handler(ToolContext(runtime, TOOL_NAMES), request)
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
    output_schema = spec.output_model.model_json_schema(mode="serialization")
    constrain_mcp_input_schema(input_schema)
    constrain_mcp_output_schema(output_schema)
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
