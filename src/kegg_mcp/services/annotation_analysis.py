"""High-level annotation-to-MODULE/pathway analysis workflow."""

from __future__ import annotations

import time
from pathlib import Path

from kegg_mcp.analysis import (
    ModuleAnalysisLimits,
    ModuleRankingResult,
    ModuleSelection,
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    PathwayRankingResult,
    PathwaySelection,
    evaluate_module_pair,
    evaluate_pathway_coverage,
    rank_modules,
    rank_pathways,
)
from kegg_mcp.domain.annotations import (
    AnnotationDataset,
    EvidenceMode,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.execution import (
    ANALYSIS_SERVICE_NAME,
    AnalysisExecutionProvenance,
    AnalysisServiceLimits,
    ExecutionStage,
    ModuleRankingExecution,
    PathwayExecutionParameters,
    PathwayRankingExecution,
)
from kegg_mcp.kegg import (
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggPairRow,
)
from kegg_mcp.reporting import ReportInput, ReportLimits, render_report
from kegg_mcp.services.models import (
    MAX_DIRECT_ANALYSIS_TARGETS,
    AnalyzeKoAnnotationsResult,
    AutomaticModuleSelectionSummary,
    AutomaticPathwaySelectionSummary,
    NormalizeAnnotationsRequest,
    SelectedModuleSummary,
    SelectedPathwaySummary,
)
from kegg_mcp.services.normalization import _import_dataset
from kegg_mcp.services.output_bundle import write_analysis_bundle
from kegg_mcp.services.previews import (
    _module_preview,
    _pathway_preview,
)
from kegg_mcp.services.reference_budget import (
    KeggPrimitiveClient,
    SharedReferenceBudgetClient,
)
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    ReferenceLoadingLimits,
    load_module_graphs,
    load_pathway_references,
)
from kegg_mcp.services.result_builders import (
    _analysis_warnings,
    _artifact_metadata,
    _build_analysis_summary,
    _elapsed_ms,
    _execution_metrics,
    _json_bytes,
    _reference_provenance,
    _validate_report_capacity,
)
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    ResultArtifactMetadata,
    SQLiteResultStore,
    compensate_created_result,
)


def analyze_annotation_targets(
    request: NormalizeAnnotationsRequest,
    *,
    module_ids: tuple[str, ...],
    pathways: tuple[PathwaySpec, ...],
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    pathway_evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    pathway_selection: PathwaySelection | None = None,
    allow_global_or_overview: bool = False,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    module_limits: ModuleAnalysisLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
    report_limits: ReportLimits | None = None,
    output_directory: Path | None = None,
) -> AnalyzeKoAnnotationsResult:
    """Normalize any supported inline format and analyze all selected targets in one call."""
    effective_report_limits = report_limits or ReportLimits()
    effective_module_limits = module_limits or ModuleAnalysisLimits()
    effective_pathway_limits = pathway_limits or PathwayCoverageLimits()
    _validate_report_capacity(effective_report_limits, result_store)
    result_store.list_results(scope_id, limit=1)
    stage_elapsed = {stage: 0 for stage in ExecutionStage}
    started = time.perf_counter_ns()
    dataset = _import_dataset(request)
    stage_elapsed[ExecutionStage.ANNOTATION_IMPORT] = _elapsed_ms(started)
    effective_options = options or KeggRequestOptions(refresh=False)
    effective_reference_limits = reference_limits or ReferenceLoadingLimits()
    budgeted_client = SharedReferenceBudgetClient(client, effective_reference_limits)
    module_mapping_provenance: tuple[KeggBatchProvenance, ...] = ()
    pathway_mapping_provenance: tuple[KeggBatchProvenance, ...] = ()
    module_selection: ModuleSelection | None = None
    module_ranking: ModuleRankingResult | None = None
    module_ranking_execution: ModuleRankingExecution | None = None
    ranking: PathwayRankingResult | None = None
    ranking_execution: PathwayRankingExecution | None = None
    if pathway_selection is None and not module_ids and not pathways:
        module_selection = ModuleSelection(top_n=5)
        pathway_selection = PathwaySelection(top_n=5)
    if module_selection is not None:
        if module_selection.top_n > effective_reference_limits.max_module_roots:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The default Top-N MODULE count exceeds the deployment MODULE bound.",
                suggested_action="Raise the deployment-owned MODULE root limit.",
            )
        started = time.perf_counter_ns()
        module_relationship_rows, module_mapping_provenance = _map_selected_ko_relationships(
            dataset,
            evidence_mode=pathway_evidence_mode,
            relationship=KeggLinkRelationship.KO_TO_MODULE,
            client=budgeted_client,
            options=effective_options,
        )
        stage_elapsed[ExecutionStage.KO_TARGET_MAPPING] += _elapsed_ms(started)
        started = time.perf_counter_ns()
        module_ranking = rank_modules(
            dataset,
            module_relationship_rows,
            pathway_evidence_mode,
        )
        stage_elapsed[ExecutionStage.TARGET_RANKING] += _elapsed_ms(started)
        selected_module_rows = module_ranking.rows[: module_selection.top_n]
        module_ids = tuple(row.module_id for row in selected_module_rows)
        module_ranking_execution = _module_ranking_execution(
            module_ranking,
            module_selection,
            dataset=dataset,
            mapping_provenance=module_mapping_provenance,
        )
    if pathway_selection is not None:
        if pathway_selection.top_n > effective_reference_limits.max_pathway_specs:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The requested Top-N pathway count exceeds the deployment pathway bound.",
                suggested_action="Lower top_n or raise the deployment-owned pathway limit.",
            )
        started = time.perf_counter_ns()
        relationship_rows, pathway_mapping_provenance = _map_selected_ko_relationships(
            dataset,
            evidence_mode=pathway_evidence_mode,
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            client=budgeted_client,
            options=effective_options,
        )
        stage_elapsed[ExecutionStage.KO_TARGET_MAPPING] += _elapsed_ms(started)
        started = time.perf_counter_ns()
        ranking = rank_pathways(dataset, relationship_rows, pathway_evidence_mode)
        stage_elapsed[ExecutionStage.TARGET_RANKING] += _elapsed_ms(started)
        if not ranking.rows:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "No reference pathway could be ranked from the selected K numbers.",
                suggested_action="Supply explicit pathway targets or different KO evidence.",
            )
        selected_rows = ranking.rows[: pathway_selection.top_n]
        pathways = tuple(PathwaySpec(pathway_id=row.pathway_id) for row in selected_rows)
        ranking_execution = _pathway_ranking_execution(
            ranking,
            pathway_selection,
            dataset=dataset,
            mapping_provenance=pathway_mapping_provenance,
        )
    started = time.perf_counter_ns()
    graphs = (
        load_module_graphs(
            budgeted_client,
            module_ids,
            options=effective_options,
            limits=effective_reference_limits,
            analysis_limits=effective_module_limits,
        )
        if module_ids
        else ()
    )
    references = load_pathway_references(
        budgeted_client,
        pathways,
        options=effective_options,
        limits=effective_reference_limits,
        pathway_limits=effective_pathway_limits,
    )
    stage_elapsed[ExecutionStage.REFERENCE_LOADING] = _elapsed_ms(started)

    started = time.perf_counter_ns()
    modules = tuple(
        evaluate_module_pair(graph, dataset, effective_module_limits) for graph in graphs
    )
    coverages = tuple(
        evaluate_pathway_coverage(
            reference,
            dataset,
            PathwayCoverageParameters(
                reference_namespace=reference.reference_namespace,
                evidence_mode=pathway_evidence_mode,
                allow_global_or_overview=allow_global_or_overview,
            ),
            effective_pathway_limits,
        )
        for reference in references
    )
    execution = AnalysisExecutionProvenance(
        service_name=ANALYSIS_SERVICE_NAME,
        import_limits=request.import_limits,
        kegg_request_options=effective_options,
        reference_loading_limits=effective_reference_limits,
        module_analysis_limits=effective_module_limits,
        module_ranking=module_ranking_execution,
        pathway_parameters=PathwayExecutionParameters(
            evidence_mode=pathway_evidence_mode,
            allow_global_or_overview=allow_global_or_overview,
            ranking=ranking_execution,
        ),
        pathway_coverage_limits=effective_pathway_limits,
        report_limits=effective_report_limits,
        direct_result_limits=AnalysisServiceLimits(
            max_module_previews=MAX_DIRECT_ANALYSIS_TARGETS,
            max_pathway_previews=MAX_DIRECT_ANALYSIS_TARGETS,
        ),
    )
    stage_elapsed[ExecutionStage.ANALYSIS] = _elapsed_ms(started)
    mapping_provenance = (*module_mapping_provenance, *pathway_mapping_provenance)
    reference_provenance = _reference_provenance(modules, coverages)
    metrics = _execution_metrics(
        stage_elapsed,
        mapping_provenance=mapping_provenance,
        reference_provenance=reference_provenance,
    )
    rendered = render_report(
        ReportInput(
            dataset=dataset,
            execution=execution,
            execution_metrics=metrics,
            mapping_provenance=mapping_provenance,
            module_evaluations=modules,
            pathway_coverages=coverages,
            pathway_selection=pathway_selection if ranking is not None else None,
            pathway_ranking=ranking.rows if ranking is not None else (),
        ),
        limits=effective_report_limits,
    )
    stored_inputs = [
        ResultArtifactInput(
            section=artifact.section.value,
            mime_type=artifact.mime_type,
            content=artifact.content.encode("utf-8"),
        )
        for artifact in rendered.artifacts
    ]
    artifact_metadata = [
        ResultArtifactMetadata(
            section=artifact.section.value,
            mime_type=artifact.mime_type,
            byte_size=artifact.utf8_byte_size,
        )
        for artifact in rendered.artifacts
    ]
    if ranking is not None:
        ranking_content = _json_bytes(
            {
                "decision_policy": dataset.import_report.decision_policy.model_dump(mode="json"),
                "mapping_provenance": [
                    batch.model_dump(mode="json") for batch in pathway_mapping_provenance
                ],
                "ranking": ranking.model_dump(mode="json", exclude={"relationships"}),
            }
        )
        relationship_content = _json_bytes(
            {"relationships": [item.model_dump(mode="json") for item in ranking.relationships]}
        )
        for section, content in (
            ("pathway_ranking", ranking_content),
            ("ko_pathway_relationships", relationship_content),
        ):
            stored_inputs.append(
                ResultArtifactInput(
                    section=section,
                    mime_type="application/json",
                    content=content,
                )
            )
            artifact_metadata.append(_artifact_metadata(section, "application/json", content))
    if module_ranking is not None:
        module_ranking_content = _json_bytes(
            {
                "decision_policy": dataset.import_report.decision_policy.model_dump(mode="json"),
                "mapping_provenance": [
                    batch.model_dump(mode="json") for batch in module_mapping_provenance
                ],
                "ranking": module_ranking.model_dump(mode="json", exclude={"relationships"}),
            }
        )
        module_relationship_content = _json_bytes(
            {
                "relationships": [
                    item.model_dump(mode="json") for item in module_ranking.relationships
                ]
            }
        )
        for section, content in (
            ("module_ranking", module_ranking_content),
            ("ko_module_relationships", module_relationship_content),
        ):
            stored_inputs.append(
                ResultArtifactInput(
                    section=section,
                    mime_type="application/json",
                    content=content,
                )
            )
            artifact_metadata.append(_artifact_metadata(section, "application/json", content))
    result = result_store.create(scope_id, tuple(stored_inputs))
    output_bundle = None
    started = time.perf_counter_ns()
    try:
        if output_directory is not None:
            summary_artifact = next(
                artifact for artifact in rendered.artifacts if artifact.section.value == "summary"
            )
            output_bundle = write_analysis_bundle(
                dataset,
                graphs,
                modules,
                references,
                coverages,
                execution=execution,
                analysis_report=summary_artifact.content,
                output_directory=output_directory,
                module_ranking=module_ranking,
                pathway_ranking=ranking,
                manifest_path_mode=request.manifest_path_mode,
            )
    except BaseException:
        compensate_created_result(
            result_store,
            scope_id,
            result.result_id,
            result.created_at,
        )
        raise
    stage_elapsed[ExecutionStage.BUNDLE_WRITE] = _elapsed_ms(started)
    artifacts = tuple(artifact_metadata)
    caveats = ["K-number assignments are annotation evidence, not experimental validation."]
    if modules:
        caveats.append(
            "Exact MODULE completion and project-defined required-block coverage are separate."
        )
    if coverages:
        caveats.append(
            "Pathway KO coverage is descriptive and does not establish presence, activity, or flux."
        )
    warnings = _analysis_warnings(dataset, modules, coverages)
    final_metrics = _execution_metrics(
        stage_elapsed,
        mapping_provenance=mapping_provenance,
        reference_provenance=reference_provenance,
    )
    selected_pathways = (
        tuple(
            SelectedPathwaySummary(
                rank=row.rank,
                pathway_id=row.pathway_id,
                pathway_number=row.pathway_number,
                detected_unique_ko_count=row.detected_unique_ko_count,
                relationship_row_count=row.relationship_row_count,
            )
            for row in ranking.rows[: pathway_selection.top_n]
        )
        if ranking is not None and pathway_selection is not None
        else ()
    )
    automatic_selection = (
        AutomaticPathwaySelectionSummary(
            parameters=pathway_selection,
            candidate_pathway_count=len(ranking.rows),
            selected_pathways=selected_pathways,
        )
        if ranking is not None and pathway_selection is not None
        else None
    )
    selected_modules = (
        tuple(
            SelectedModuleSummary(
                rank=row.rank,
                module_id=row.module_id,
                detected_unique_ko_count=row.detected_unique_ko_count,
                relationship_row_count=row.relationship_row_count,
            )
            for row in module_ranking.rows[: module_selection.top_n]
        )
        if module_ranking is not None and module_selection is not None
        else ()
    )
    automatic_module_selection = (
        AutomaticModuleSelectionSummary(
            parameters=module_selection,
            evidence_mode=module_ranking.evidence_mode,
            candidate_module_count=len(module_ranking.rows),
            selected_modules=selected_modules,
        )
        if module_ranking is not None and module_selection is not None
        else None
    )
    return AnalyzeKoAnnotationsResult(
        result=result,
        artifacts=artifacts,
        summary=_build_analysis_summary(
            dataset,
            evidence_mode=pathway_evidence_mode,
            metrics=final_metrics,
            caveats=tuple(caveats),
            warnings=warnings,
        ),
        module_target_count=len(modules),
        module_previews=tuple(_module_preview(item) for item in modules),
        automatic_module_selection=automatic_module_selection,
        pathway_target_count=len(coverages),
        pathway_previews=tuple(_pathway_preview(item) for item in coverages),
        automatic_pathway_selection=automatic_selection,
        output_bundle=output_bundle,
    )


def _map_selected_ko_relationships(
    dataset: AnnotationDataset,
    *,
    evidence_mode: EvidenceMode,
    relationship: KeggLinkRelationship,
    client: SharedReferenceBudgetClient,
    options: KeggRequestOptions,
) -> tuple[tuple[KeggPairRow, ...], tuple[KeggBatchProvenance, ...]]:
    """Issue bounded KO-to-target calls and merge rows without changing their semantics."""
    selected_ko_ids = select_ko_ids(build_ko_evidence_view(dataset), evidence_mode)
    if not selected_ko_ids:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Automatic target ranking requires at least one selected K number.",
            suggested_action="Provide selected KO evidence or explicit MODULE/pathway targets.",
        )
    maximum_per_call = min(100, client.config.limits.max_identifiers)
    rows: list[KeggPairRow] = []
    provenance: list[KeggBatchProvenance] = []
    for start in range(0, len(selected_ko_ids), maximum_per_call):
        result = client.link(
            LinkRequest(
                relationship=relationship,
                source_identifiers=selected_ko_ids[start : start + maximum_per_call],
            ),
            options=options,
        )
        batch_offset = len(provenance)
        rows.extend(
            KeggPairRow(
                batch_index=row.batch_index + batch_offset,
                line_number=row.line_number,
                source_id=row.source_id,
                target_id=row.target_id,
            )
            for row in result.rows
        )
        provenance.extend(result.batches)
    return tuple(rows), tuple(provenance)


def _pathway_ranking_execution(
    ranking: PathwayRankingResult,
    selection: PathwaySelection,
    *,
    dataset: AnnotationDataset,
    mapping_provenance: tuple[KeggBatchProvenance, ...],
) -> PathwayRankingExecution:
    selected_rows = ranking.rows[: selection.top_n]
    return PathwayRankingExecution(
        selection=selection,
        evidence_mode=ranking.evidence_mode,
        decision_policy=dataset.import_report.decision_policy,
        selected_unique_ko_count=len(ranking.selected_ko_ids),
        candidate_pathway_count=len(ranking.rows),
        selected_pathway_ids=tuple(row.pathway_id for row in selected_rows),
        mapping_request_count=len(mapping_provenance),
        mapping_network_request_count=sum(
            batch.attempt_count
            for batch in mapping_provenance
            if batch.origin is ResponseOrigin.NETWORK
        ),
        mapping_cache_hit_count=sum(
            batch.origin is ResponseOrigin.CACHE for batch in mapping_provenance
        ),
        mapping_response_bytes=sum(batch.response_bytes for batch in mapping_provenance),
    )


def _module_ranking_execution(
    ranking: ModuleRankingResult,
    selection: ModuleSelection,
    *,
    dataset: AnnotationDataset,
    mapping_provenance: tuple[KeggBatchProvenance, ...],
) -> ModuleRankingExecution:
    selected_rows = ranking.rows[: selection.top_n]
    return ModuleRankingExecution(
        selection=selection,
        evidence_mode=ranking.evidence_mode,
        decision_policy=dataset.import_report.decision_policy,
        selected_unique_ko_count=len(ranking.selected_ko_ids),
        candidate_module_count=len(ranking.rows),
        selected_module_ids=tuple(row.module_id for row in selected_rows),
        mapping_request_count=len(mapping_provenance),
        mapping_network_request_count=sum(
            batch.attempt_count
            for batch in mapping_provenance
            if batch.origin is ResponseOrigin.NETWORK
        ),
        mapping_cache_hit_count=sum(
            batch.origin is ResponseOrigin.CACHE for batch in mapping_provenance
        ),
        mapping_response_bytes=sum(batch.response_bytes for batch in mapping_provenance),
    )


__all__ = ["analyze_annotation_targets"]
