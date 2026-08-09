"""High-level annotation-to-MODULE/pathway analysis workflow."""

from __future__ import annotations

import time
from pathlib import Path

from kegg_mcp.analysis import (
    MODULE_RANKING_METHOD,
    MODULE_RANKING_VERSION,
    PATHWAY_RANKING_METHOD,
    PATHWAY_RANKING_VERSION,
    ModuleAnalysisLimits,
    ModuleRankingResult,
    ModuleSelection,
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    PathwayRankingResult,
    PathwayRankingRow,
    PathwaySelection,
    evaluate_module,
    evaluate_pathway_coverage,
    rank_modules,
    rank_pathways,
)
from kegg_mcp.domain.analysis_view import KoAnalysisView
from kegg_mcp.domain.decisions import DEEPKOALA_DETAILED
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.execution import (
    ANALYSIS_SERVICE_NAME,
    ANALYSIS_SERVICE_VERSION,
    AnalysisExecutionProvenance,
    AnalysisServiceLimits,
    ExecutionStage,
    ModuleRankingExecution,
    PathwayExecutionParameters,
    PathwayRankingExecution,
)
from kegg_mcp.importers import AnalysisViewImportLimits
from kegg_mcp.importers._common import IMPORTER_VERSION
from kegg_mcp.kegg import (
    KeggLinkRelationship,
    KeggRequestOptions,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggPairRow,
)
from kegg_mcp.reporting import ReportInput, ReportLimits, render_report
from kegg_mcp.services.kegg_relations import bounded_relation_batches
from kegg_mcp.services.models import (
    MAX_DIRECT_ANALYSIS_TARGETS,
    AnalyzeKoAnnotationsResult,
    AnnotationInputFormat,
    AutomaticModuleSelectionSummary,
    AutomaticPathwaySelectionSummary,
    NormalizeAnnotationsRequest,
    SelectedModuleSummary,
    SelectedPathwaySummary,
)
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
    _analysis_warning_count,
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
    create_retained_result,
)

# Current KEGG Global, Overview, and higher-level Overview KO reference maps.
# Checked against https://www.kegg.jp/kegg/pathway.html on 2026-07-22. These
# special line/arrow maps are outside the regular-reference automatic-selection
# policy.
_AUTOMATIC_PATHWAY_EXCLUSIONS = frozenset(
    {
        "ko01100",
        "ko01110",
        "ko01120",
        "ko01200",
        "ko01210",
        "ko01212",
        "ko01220",
        "ko01230",
        "ko01232",
        "ko01240",
        "ko01250",
        "ko01310",
        "ko01320",
    }
)


def analyze_annotation_targets(
    request: NormalizeAnnotationsRequest,
    *,
    module_ids: tuple[str, ...],
    pathways: tuple[PathwaySpec, ...],
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    pathway_selection: PathwaySelection | None = None,
    allow_global_or_overview: bool = False,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    module_limits: ModuleAnalysisLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
    report_limits: ReportLimits | None = None,
    analysis_view: KoAnalysisView,
    stream_import_limits: AnalysisViewImportLimits | None = None,
    annotation_import_elapsed_ms: int = 0,
    output_directory: Path | None = None,
    remove_created_output_on_failure: bool = False,
) -> AnalyzeKoAnnotationsResult:
    """Analyze one compact sorted unique accepted-KO view."""
    effective_report_limits = report_limits or ReportLimits()
    effective_module_limits = module_limits or ModuleAnalysisLimits()
    effective_pathway_limits = pathway_limits or PathwayCoverageLimits()
    _validate_report_capacity(effective_report_limits, result_store)
    result_store.list_results(scope_id, limit=1)
    stage_elapsed = {stage: 0 for stage in ExecutionStage}
    if stream_import_limits is not None:
        if request.text is not None or request.file_path is None:
            raise ValueError("streamed analysis_view requires the unchanged file-backed request")
        if request.input_format is not AnnotationInputFormat.DEEPKOALA_DETAILED:
            raise ValueError("streamed analysis_view requires DeepKOALA detailed input")
        if analysis_view.decision_policy != DEEPKOALA_DETAILED.reference:
            raise ValueError("streamed analysis_view requires the DeepKOALA decision policy")
        input_bytes = analysis_view.input_bytes
        if input_bytes is None:
            raise ValueError("streamed analysis_view requires an exact input byte count")
        analysis_source = analysis_view.sources[0]
        request_source = request.source
        if request_source is None or (
            analysis_source.source_name != request_source.source_name
            or analysis_source.source_version != request_source.source_version
            or analysis_source.model_name != request_source.model_name
            or analysis_source.model_version != request_source.model_version
            or analysis_source.annotation_date != request_source.annotation_date
            or analysis_source.input_uri != request_source.input_uri
            or analysis_source.input_path != request_source.input_path
            or analysis_source.source_metadata != request_source.source_metadata
            or analysis_source.importer_name != "deepkoala_analysis_view"
            or analysis_source.importer_version != IMPORTER_VERSION
        ):
            raise ValueError("analysis_view source must match the annotation request")
        if any(
            observed > maximum
            for observed, maximum in (
                (input_bytes, stream_import_limits.max_bytes),
                (analysis_view.input_rows, stream_import_limits.max_rows),
                (
                    analysis_view.assignment_count,
                    stream_import_limits.max_expanded_assignments,
                ),
                (
                    len(analysis_view.accepted_ko_ids),
                    stream_import_limits.max_unique_ko_ids,
                ),
                (len(analysis_view.source_columns), stream_import_limits.max_columns),
                (
                    max(map(len, analysis_view.source_columns)),
                    stream_import_limits.max_field_length,
                ),
                (
                    len(analysis_view.diagnostic_preview),
                    stream_import_limits.max_diagnostic_preview,
                ),
            )
        ):
            raise ValueError("analysis_view exceeds its recorded stream_import_limits")
    else:
        if request.text is None or request.file_path is not None:
            raise ValueError("bounded analysis_view requires a materialized annotation request")
        input_bytes = analysis_view.input_bytes
        if input_bytes is None or input_bytes != len(request.text.encode()):
            raise ValueError("analysis_view byte count must match the materialized request")
        if any(
            observed > maximum
            for observed, maximum in (
                (input_bytes, request.import_limits.max_bytes),
                (analysis_view.input_rows, request.import_limits.max_rows),
                (len(analysis_view.source_columns), request.import_limits.max_columns),
                (
                    max(map(len, analysis_view.source_columns), default=0),
                    request.import_limits.max_field_length,
                ),
            )
        ):
            raise ValueError("analysis_view exceeds its recorded import_limits")
    if (
        analysis_view.analysis_unit is not request.analysis_unit
        or analysis_view.taxon_id != request.taxon_id
        or analysis_view.kegg_organism_code != request.kegg_organism_code
    ):
        raise ValueError("analysis_view context must match the annotation request")
    if annotation_import_elapsed_ms < 0:
        raise ValueError("annotation_import_elapsed_ms must be non-negative")
    stage_elapsed[ExecutionStage.ANNOTATION_IMPORT] = annotation_import_elapsed_ms
    evidence = analysis_view
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
    selected_pathway_rows: tuple[PathwayRankingRow, ...] = ()
    accepted_ko_ids_empty = not evidence.accepted_ko_ids
    default_automatic_selection = pathway_selection is None and not module_ids and not pathways
    automatic_selection_skipped = accepted_ko_ids_empty and (
        pathway_selection is not None or default_automatic_selection
    )
    if accepted_ko_ids_empty:
        pathway_selection = None
    elif default_automatic_selection:
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
            evidence,
            relationship=KeggLinkRelationship.KO_TO_MODULE,
            client=budgeted_client,
            options=effective_options,
        )
        stage_elapsed[ExecutionStage.KO_TARGET_MAPPING] += _elapsed_ms(started)
        started = time.perf_counter_ns()
        module_ranking = rank_modules(
            evidence,
            module_relationship_rows,
        )
        stage_elapsed[ExecutionStage.TARGET_RANKING] += _elapsed_ms(started)
        selected_module_rows = module_ranking.rows[: module_selection.top_n]
        module_ids = tuple(row.module_id for row in selected_module_rows)
        module_ranking_execution = _module_ranking_execution(
            module_ranking,
            module_selection,
            evidence=evidence,
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
            evidence,
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            client=budgeted_client,
            options=effective_options,
        )
        stage_elapsed[ExecutionStage.KO_TARGET_MAPPING] += _elapsed_ms(started)
        started = time.perf_counter_ns()
        ranking = rank_pathways(evidence, relationship_rows)
        stage_elapsed[ExecutionStage.TARGET_RANKING] += _elapsed_ms(started)
        if not ranking.rows:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "No reference pathway could be ranked from the selected K numbers.",
                suggested_action="Supply explicit pathway targets or different KO evidence.",
            )
        selected_pathway_rows = tuple(
            row for row in ranking.rows if row.pathway_id not in _AUTOMATIC_PATHWAY_EXCLUSIONS
        )[: pathway_selection.top_n]
        if not selected_pathway_rows:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "No supported reference pathway remained after automatic exclusions.",
                suggested_action="Supply an explicit supported pathway target.",
            )
        pathways = tuple(PathwaySpec(pathway_id=row.pathway_id) for row in selected_pathway_rows)
        ranking_execution = _pathway_ranking_execution(
            ranking,
            pathway_selection,
            selected_rows=selected_pathway_rows,
            evidence=evidence,
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
    references = (
        load_pathway_references(
            budgeted_client,
            pathways,
            options=effective_options,
            limits=effective_reference_limits,
            pathway_limits=effective_pathway_limits,
        )
        if pathways
        else ()
    )
    stage_elapsed[ExecutionStage.REFERENCE_LOADING] = _elapsed_ms(started)

    started = time.perf_counter_ns()
    modules = tuple(evaluate_module(graph, evidence, effective_module_limits) for graph in graphs)
    coverages = tuple(
        evaluate_pathway_coverage(
            reference,
            evidence,
            PathwayCoverageParameters(
                reference_namespace=reference.reference_namespace,
                allow_global_or_overview=allow_global_or_overview,
            ),
            effective_pathway_limits,
        )
        for reference in references
    )
    execution = AnalysisExecutionProvenance(
        service_name=ANALYSIS_SERVICE_NAME,
        service_version=ANALYSIS_SERVICE_VERSION,
        import_limits=request.import_limits if stream_import_limits is None else None,
        stream_import_limits=stream_import_limits,
        kegg_request_options=effective_options,
        reference_loading_limits=effective_reference_limits,
        module_analysis_limits=effective_module_limits,
        module_ranking=module_ranking_execution,
        pathway_parameters=PathwayExecutionParameters(
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
            dataset=evidence,
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
                "decision_policy": evidence.decision_policy.model_dump(mode="json"),
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
                "decision_policy": evidence.decision_policy.model_dump(mode="json"),
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
    with create_retained_result(result_store, scope_id, tuple(stored_inputs)) as result:
        output_bundle = None
        started = time.perf_counter_ns()
        if output_directory is not None:
            summary_artifact = next(
                artifact for artifact in rendered.artifacts if artifact.section.value == "summary"
            )
            output_bundle = write_analysis_bundle(
                evidence,
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
                remove_created_directory_on_failure=remove_created_output_on_failure,
            )
    stage_elapsed[ExecutionStage.BUNDLE_WRITE] = _elapsed_ms(started)
    artifacts = tuple(artifact_metadata)
    caveats = ["K-number assignments are annotation evidence, not experimental validation."]
    compact_view_caveat = (
        "The analysis used a compact unique accepted-KO view; record-level evidence, "
        "protein-to-KO mappings, and duplicate/conflict accounting were not retained."
    )
    if automatic_selection_skipped:
        compact_view_caveat += (
            " No accepted K numbers were selected, so automatic target selection was skipped."
        )
    if accepted_ko_ids_empty and (module_ids or pathways):
        compact_view_caveat += (
            " Any explicit MODULE or pathway targets were evaluated against the empty "
            "accepted-KO set."
        )
    caveats.append(compact_view_caveat)
    if modules:
        caveats.append(
            "Exact MODULE completion and project-defined required-block coverage are separate."
        )
    if coverages:
        caveats.append(
            "Pathway KO coverage is descriptive and does not establish presence, activity, or flux."
        )
    warnings = _analysis_warnings(evidence, modules, coverages)
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
            for row in selected_pathway_rows
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
            evidence,
            metrics=final_metrics,
            caveats=tuple(caveats),
            warnings=warnings,
            warning_count=_analysis_warning_count(evidence, modules, coverages),
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
    evidence: KoAnalysisView,
    *,
    relationship: KeggLinkRelationship,
    client: SharedReferenceBudgetClient,
    options: KeggRequestOptions,
) -> tuple[tuple[KeggPairRow, ...], tuple[KeggBatchProvenance, ...]]:
    """Issue bounded KO-to-target calls and merge rows without changing their semantics."""
    selected_ko_ids = evidence.accepted_ko_ids
    if not selected_ko_ids:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Automatic target ranking requires at least one selected K number.",
            suggested_action="Provide selected KO evidence or explicit MODULE/pathway targets.",
        )
    result = bounded_relation_batches(
        selected_ko_ids,
        relationship=relationship,
        client=client,
        options=options,
    )
    return result.rows, result.batches


def _pathway_ranking_execution(
    ranking: PathwayRankingResult,
    selection: PathwaySelection,
    *,
    selected_rows: tuple[PathwayRankingRow, ...],
    evidence: KoAnalysisView,
    mapping_provenance: tuple[KeggBatchProvenance, ...],
) -> PathwayRankingExecution:
    return PathwayRankingExecution(
        method=PATHWAY_RANKING_METHOD,
        method_version=PATHWAY_RANKING_VERSION,
        selection=selection,
        decision_policy=evidence.decision_policy,
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
    evidence: KoAnalysisView,
    mapping_provenance: tuple[KeggBatchProvenance, ...],
) -> ModuleRankingExecution:
    selected_rows = ranking.rows[: selection.top_n]
    return ModuleRankingExecution(
        method=MODULE_RANKING_METHOD,
        method_version=MODULE_RANKING_VERSION,
        selection=selection,
        decision_policy=evidence.decision_policy,
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
