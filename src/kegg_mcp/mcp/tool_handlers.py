"""Thin MCP adapters that call public service-layer use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from kegg_mcp import __version__
from kegg_mcp.kegg import GetRequest, KeggLinkRelationship, LinkRequest
from kegg_mcp.mcp.contracts import (
    AnalyzeKoAnnotationsInput,
    AnalyzeModulesInput,
    AnalyzePathwaysInput,
    CompareKoSetsInput,
    DeleteAnalysisResultInput,
    GetKeggEntriesInput,
    GetServerStatusInput,
    KoMappingTarget,
    ListAnalysisResultsInput,
    MapKoIdsInput,
    NormalizeKoAnnotationsInput,
    ProbeKeggConnectivityInput,
)
from kegg_mcp.mcp.path_policy import materialize_annotation_file, resolve_output_directory
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.services.annotation_analysis import analyze_annotation_targets
from kegg_mcp.services.comparison import compare_annotation_sets
from kegg_mcp.services.kegg_mapping import map_ko_identifiers, retrieve_kegg_entries
from kegg_mcp.services.models import NormalizeAnnotationsRequest
from kegg_mcp.services.module_analysis import analyze_module_targets
from kegg_mcp.services.normalization import normalize_annotations
from kegg_mcp.services.operational import (
    delete_analysis_result,
    get_server_status_service,
    list_analysis_results,
    probe_kegg_connectivity_service,
)
from kegg_mcp.services.pathway_analysis import analyze_pathway_targets
from kegg_mcp.services.reference_budget import KeggConnectivityClient


@dataclass(frozen=True, slots=True)
class ToolContext:
    runtime: McpRuntime
    supported_tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    data: BaseModel
    summary: str
    result_id: str | None = None


ToolHandler = Callable[[ToolContext, BaseModel], ToolOutcome]


def analyze_annotations(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(AnalyzeKoAnnotationsInput, model)
    runtime = context.runtime
    if request.annotations is not None:
        normalization = materialize_annotation_file(
            request.annotations.to_service_request(), runtime.allowed_roots
        )
    else:
        if request.ko_text is None:  # pragma: no cover - guarded by the input model
            raise AssertionError("validated analysis input omitted its annotation source")
        normalization = NormalizeAnnotationsRequest(
            text=request.ko_text,
            analysis_unit=request.analysis_unit,
            sample_id=request.sample_id,
        )
    requested_output = request.output_directory or normalization.output_directory
    resolved_output = resolve_output_directory(
        requested_output,
        runtime.allowed_roots,
        default_prefix="kegg-analysis",
    )
    result = analyze_annotation_targets(
        normalization,
        module_ids=request.module_ids,
        pathways=request.pathways,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
        pathway_evidence_mode=request.pathway_evidence_mode,
        pathway_selection=request.pathway_selection,
        allow_global_or_overview=request.allow_global_or_overview,
        output_directory=resolved_output,
        remove_created_output_on_failure=requested_output is None and resolved_output is not None,
    )
    return ToolOutcome(
        result,
        "KO annotations were normalized and the requested KEGG analyses completed.",
        result.result.result_id,
    )


def normalize(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(NormalizeKoAnnotationsInput, model)
    runtime = context.runtime
    materialized = materialize_annotation_file(request.to_service_request(), runtime.allowed_roots)
    resolved_output = resolve_output_directory(
        request.output_directory,
        runtime.allowed_roots,
        default_prefix="kegg-normalization",
    )
    result = normalize_annotations(
        materialized,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
        output_directory=resolved_output,
        remove_created_output_on_failure=(
            request.output_directory is None and resolved_output is not None
        ),
    )
    return ToolOutcome(
        result,
        "Annotations were normalized and retained as a scoped typed dataset.",
        result.result.result_id,
    )


def get_entries(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(GetKeggEntriesInput, model)
    runtime = context.runtime
    result = retrieve_kegg_entries(
        GetRequest(entries=request.entries),
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        f"Retrieved {result.returned_count} of {result.requested_count} KEGG entries.",
        result.result.result_id,
    )


def map_identifiers(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(MapKoIdsInput, model)
    runtime = context.runtime
    result = map_ko_identifiers(
        LinkRequest(
            relationship=_relationship(request.target),
            source_identifiers=request.ko_ids,
        ),
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        f"Mapped selected K numbers to {request.target.value}; full rows are retained.",
        result.result.result_id,
    )


def analyze_modules(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(AnalyzeModulesInput, model)
    runtime = context.runtime
    result = analyze_module_targets(
        request.source,
        request.module_ids,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        "MODULE exact completion and block coverage were evaluated separately.",
        result.result.result_id,
    )


def analyze_pathways(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(AnalyzePathwaysInput, model)
    runtime = context.runtime
    result = analyze_pathway_targets(
        request.source,
        request.pathways,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
        evidence_mode=request.evidence_mode,
        allow_global_or_overview=request.allow_global_or_overview,
    )
    return ToolOutcome(
        result,
        "Descriptive unique-KO pathway coverage was evaluated with explicit denominators.",
        result.result.result_id,
    )


def compare_sets(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(CompareKoSetsInput, model)
    runtime = context.runtime
    result = compare_annotation_sets(
        request.inputs,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
        client=runtime.client,
        module_ids=request.module_ids,
        pathways=request.pathways,
        allow_global_or_overview=request.allow_global_or_overview,
    )
    return ToolOutcome(
        result,
        "Deterministic KO set differences were computed; no statistical inference was made.",
        result.result.result_id,
    )


def probe_connectivity(context: ToolContext, model: BaseModel) -> ToolOutcome:
    cast(ProbeKeggConnectivityInput, model)
    result = probe_kegg_connectivity_service(cast(KeggConnectivityClient, context.runtime.client))
    return ToolOutcome(
        result,
        f"KEGG connectivity preflight completed: {result.state.value}.",
    )


def delete_result(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(DeleteAnalysisResultInput, model)
    runtime = context.runtime
    result = delete_analysis_result(
        request.result_id,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        "Deleted the current-session retained result and all of its artifacts.",
    )


def list_results(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(ListAnalysisResultsInput, model)
    runtime = context.runtime
    result = list_analysis_results(
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
        offset=request.offset,
        limit=request.limit,
    )
    return ToolOutcome(result, f"Returned {len(result.items)} current-session retained results.")


def get_status(context: ToolContext, model: BaseModel) -> ToolOutcome:
    cast(GetServerStatusInput, model)
    runtime = context.runtime
    result = get_server_status_service(
        server_version=__version__,
        client=runtime.client,
        result_store=runtime.result_store,
        supported_tools=context.supported_tools,
        allowed_root_count=len(runtime.allowed_roots),
    )
    return ToolOutcome(result, "Returned redacted local server status.")


def _relationship(target: KoMappingTarget) -> KeggLinkRelationship:
    return {
        KoMappingTarget.PATHWAY: KeggLinkRelationship.KO_TO_PATHWAY,
        KoMappingTarget.MODULE: KeggLinkRelationship.KO_TO_MODULE,
        KoMappingTarget.REACTION: KeggLinkRelationship.KO_TO_REACTION,
        KoMappingTarget.EC: KeggLinkRelationship.KO_TO_ENZYME,
        KoMappingTarget.BRITE: KeggLinkRelationship.KO_TO_BRITE,
    }[target]


__all__ = [
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "analyze_annotations",
    "analyze_modules",
    "analyze_pathways",
    "compare_sets",
    "delete_result",
    "get_entries",
    "get_status",
    "list_results",
    "map_identifiers",
    "normalize",
    "probe_connectivity",
]
