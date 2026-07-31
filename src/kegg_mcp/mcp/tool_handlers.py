"""Thin MCP adapters that call public service-layer use cases."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from kegg_mcp import __version__
from kegg_mcp.kegg import GetRequest
from kegg_mcp.mcp.contracts import (
    AnalyzeKoAnnotationsInput,
    AnalyzeModulesInput,
    AnalyzePathwaysInput,
    AuditAnnotationMappingInput,
    CompareKeggReferenceSnapshotsInput,
    CompareKoSetsInput,
    DeleteAnalysisResultInput,
    GetKeggEntriesInput,
    GetServerStatusInput,
    ListAnalysisResultsInput,
    MapBriteHierarchyInput,
    NormalizeKoAnnotationsInput,
    ProbeKeggConnectivityInput,
    ResolveKeggEntitiesInput,
    SearchKeggEntriesInput,
    TraceKeggRelationsInput,
)
from kegg_mcp.mcp.path_policy import materialize_annotation_file, resolve_output_directory
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.services.annotation_analysis import analyze_annotation_targets
from kegg_mcp.services.annotation_audit import (
    AnnotationMappingExecutionStatus,
    audit_annotation_mapping,
)
from kegg_mcp.services.brite_hierarchy import map_brite_hierarchy
from kegg_mcp.services.comparison import compare_annotation_sets
from kegg_mcp.services.entity_resolution import resolve_kegg_entities
from kegg_mcp.services.kegg_entries import retrieve_kegg_entries
from kegg_mcp.services.kegg_search import search_kegg_entries
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
from kegg_mcp.services.query_models import KeggSearchMode
from kegg_mcp.services.reference_snapshots import compare_kegg_reference_snapshots
from kegg_mcp.services.relation_tracing import trace_kegg_relations


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
        projection=request.projection,
    )
    projection_summary = (
        " Typed cards and a versioned local comparison snapshot were retained."
        if request.projection.value == "card"
        else ""
    )
    return ToolOutcome(
        result,
        (
            f"Retrieved {result.returned_count} of {result.requested_count} KEGG entries."
            f"{projection_summary}"
        ),
        result.result.result_id,
    )


def search_entries(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(SearchKeggEntriesInput, model)
    runtime = context.runtime
    result = search_kegg_entries(
        request,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    identification_caveat = (
        (
            f" Exact-mass matches are {request.database.value} candidates, not "
            "chemical identifications."
        )
        if result.mode is KeggSearchMode.EXACT_MASS
        else ""
    )
    return ToolOutcome(
        result,
        (
            f"Returned {result.candidate_count} bounded endpoint candidates; "
            f"no best match or relevance score was inferred.{identification_caveat}"
        ),
        result.result.result_id,
    )


def resolve_entities(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(ResolveKeggEntitiesInput, model)
    runtime = context.runtime
    result = resolve_kegg_entities(
        request.root,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        (
            f"Resolved {result.mapped_input_count} of {result.input_count} inputs while "
            "preserving ambiguous and unmapped outcomes."
        ),
        result.result.result_id,
    )


def trace_relations(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(TraceKeggRelationsInput, model)
    runtime = context.runtime
    result = trace_kegg_relations(
        request,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        (
            f"Traced {result.edge_count} bounded typed KEGG cross-reference edges; "
            "no causal or activity inference was made."
        ),
        result.result.result_id,
    )


def map_brite(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(MapBriteHierarchyInput, model)
    runtime = context.runtime
    result = map_brite_hierarchy(
        request,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        (
            f"Evaluated {result.entity_count} supplied entities across "
            f"{result.resolved_brite_count} retrieved BRITE hierarchies; complete paths are "
            "retained."
        ),
        result.result.result_id,
    )


def audit_mapping(context: ToolContext, model: BaseModel) -> ToolOutcome:
    request = cast(AuditAnnotationMappingInput, model)
    runtime = context.runtime
    result = audit_annotation_mapping(
        request.source,
        client=runtime.client,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
        quality_context=request.quality_context,
        mapping_targets=request.mapping_targets,
    )
    if result.mapping_execution.status is AnnotationMappingExecutionStatus.COMPLETED:
        summary = (
            "Audited annotation evidence and completed the selected KEGG relationship mappings."
        )
    elif result.mapping_execution.status is AnnotationMappingExecutionStatus.NOT_REQUESTED:
        summary = (
            "Completed the annotation evidence audit; no KEGG relationship mapping was requested."
        )
    elif result.mapping_execution.status is AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT:
        summary = (
            "Completed the annotation evidence audit; KEGG relationship mapping was skipped "
            "before network access because the planned request count exceeded the limit."
        )
    else:
        limit_label = (
            "relationship-row"
            if result.mapping_execution.status
            is AnnotationMappingExecutionStatus.INCOMPLETE_ROW_LIMIT
            else "response-byte"
        )
        summary = (
            "Completed the annotation evidence audit and retained only fully completed KEGG "
            f"relationship mappings; an in-progress target exceeded the {limit_label} limit, "
            "so no partial mapping yield was reported for it."
        )
    return ToolOutcome(
        result,
        summary,
        result.result.result_id,
    )


def compare_reference_snapshots(
    context: ToolContext,
    model: BaseModel,
) -> ToolOutcome:
    request = cast(CompareKeggReferenceSnapshotsInput, model)
    runtime = context.runtime
    result = compare_kegg_reference_snapshots(
        request,
        result_store=runtime.result_store,
        scope_id=runtime.scope_id,
    )
    return ToolOutcome(
        result,
        (
            "Compared two retained KEGG entry-card snapshots locally and reported "
            f"{result.added_entry_count + result.removed_entry_count} membership changes and "
            f"{result.field_change_count} selected field changes; no biological gain, loss, "
            "or validation was inferred."
        ),
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
    result = probe_kegg_connectivity_service(context.runtime.client)
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


__all__ = [
    "ToolContext",
    "ToolHandler",
    "ToolOutcome",
    "analyze_annotations",
    "analyze_modules",
    "analyze_pathways",
    "audit_mapping",
    "compare_reference_snapshots",
    "compare_sets",
    "delete_result",
    "get_entries",
    "get_status",
    "list_results",
    "map_brite",
    "normalize",
    "probe_connectivity",
    "resolve_entities",
    "search_entries",
    "trace_relations",
]
