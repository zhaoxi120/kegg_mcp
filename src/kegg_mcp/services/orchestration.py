"""One-call orchestration for bounded plain-KO analysis and retained reports."""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from kegg_mcp.analysis.module_evaluation import evaluate_module_pair
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageParameters,
    evaluate_pathway_coverage,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.execution import (
    AnalysisExecutionProvenance,
    PathwayExecutionParameters,
    ReferenceLoadingLimits,
)
from kegg_mcp.importers.plain_ko import import_plain_ko
from kegg_mcp.kegg.contracts import (
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
)
from kegg_mcp.reporting.contracts import ReportInput, ReportLimits
from kegg_mcp.reporting.render import render_report
from kegg_mcp.services.contracts import (
    AnalysisServiceLimits,
    ImportSummary,
    ModuleAnalysisPreview,
    PathwayAnalysisPreview,
    PlainKoAnalysisRequest,
    PlainKoAnalysisResult,
)
from kegg_mcp.services.reference_loading import (
    KeggReferenceClient,
    load_module_graphs,
    load_pathway_references,
)
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    ResultArtifactMetadata,
    ResultStoreLimits,
    SQLiteResultStore,
)

_ANNOTATION_CAVEAT = "K-number assignments are annotation evidence, not experimental validation."
_ABSENCE_CAVEAT = (
    "A rejected, missing, or unclassified prediction does not demonstrate biological absence."
)
_MODULE_CAVEAT = (
    "Exact MODULE completion and project-defined required-block coverage are separate results."
)
_PATHWAY_CAVEAT = (
    "Pathway KO coverage is descriptive and does not establish pathway presence, completeness, "
    "expression, activity, flux, phenotype, or statistical significance."
)


def analyze_plain_ko(
    request: PlainKoAnalysisRequest,
    *,
    client: KeggReferenceClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    limits: AnalysisServiceLimits | None = None,
    now: datetime | None = None,
) -> PlainKoAnalysisResult:
    """Import, retrieve references, analyze, render, and retain one plain-KO request."""
    output_limits = limits or AnalysisServiceLimits()
    _validate_report_store_compatibility(request.report_limits, result_store.limits)
    result_store.list_results(scope_id, limit=1, now=now)
    budgeted_client = _SharedReferenceBudgetClient(client, request.reference_limits)
    dataset = import_plain_ko(
        request.ko_text,
        limits=request.import_limits,
        analysis_unit=request.analysis_unit,
        sample_id=request.sample_id,
        taxon_id=request.taxon_id,
        kegg_organism_code=request.kegg_organism_code,
        metadata=request.metadata,
        source=request.source,
    )

    module_graphs = (
        load_module_graphs(
            budgeted_client,
            request.module_ids,
            options=request.kegg_options,
            limits=request.reference_limits,
            analysis_limits=request.module_limits,
        )
        if request.module_ids
        else ()
    )
    module_evaluations = tuple(
        evaluate_module_pair(graph, dataset, request.module_limits) for graph in module_graphs
    )

    pathway_references = (
        load_pathway_references(
            budgeted_client,
            request.pathways,
            options=request.kegg_options,
            limits=request.reference_limits,
            pathway_limits=request.pathway_limits,
        )
        if request.pathways
        else ()
    )
    pathway_coverages = tuple(
        evaluate_pathway_coverage(
            reference,
            dataset,
            PathwayCoverageParameters(
                reference_namespace=reference.reference_namespace,
                evidence_mode=request.pathway_evidence_mode,
                allow_global_or_overview=request.allow_global_or_overview,
            ),
            request.pathway_limits,
        )
        for reference in pathway_references
    )

    rendered = render_report(
        ReportInput(
            dataset=dataset,
            execution=AnalysisExecutionProvenance(
                import_limits=request.import_limits,
                kegg_request_options=request.kegg_options,
                reference_loading_limits=request.reference_limits,
                module_analysis_limits=request.module_limits,
                pathway_parameters=PathwayExecutionParameters(
                    evidence_mode=request.pathway_evidence_mode,
                    allow_global_or_overview=request.allow_global_or_overview,
                ),
                pathway_coverage_limits=request.pathway_limits,
                report_limits=request.report_limits,
                direct_result_limits=output_limits,
            ),
            module_evaluations=module_evaluations,
            pathway_coverages=pathway_coverages,
        ),
        limits=request.report_limits,
    )
    stored_artifacts = tuple(
        ResultArtifactInput(
            section=artifact.section.value,
            mime_type=artifact.mime_type,
            content=artifact.content.encode("utf-8"),
        )
        for artifact in rendered.artifacts
    )
    result_metadata = result_store.create(scope_id, stored_artifacts, now=now)
    artifact_metadata = tuple(
        ResultArtifactMetadata(
            section=artifact.section.value,
            mime_type=artifact.mime_type,
            byte_size=artifact.utf8_byte_size,
        )
        for artifact in rendered.artifacts
    )

    module_preview_count = min(
        len(module_evaluations),
        output_limits.max_module_previews,
    )
    pathway_preview_count = min(
        len(pathway_coverages),
        output_limits.max_pathway_previews,
    )
    return PlainKoAnalysisResult(
        result=result_metadata,
        artifacts=artifact_metadata,
        import_summary=ImportSummary(
            dataset_id=dataset.dataset_id,
            analysis_unit=dataset.analysis_unit,
            input_rows=dataset.import_report.input_rows,
            emitted_records=dataset.import_report.emitted_records,
            skipped_rows=dataset.import_report.skipped_rows,
            duplicate_count=dataset.import_report.duplicate_count,
            conflict_count=dataset.import_report.conflict_count,
            status_counts=dataset.import_report.status_counts,
        ),
        module_target_count=len(module_evaluations),
        module_previews=tuple(
            ModuleAnalysisPreview(
                module_id=pair.strict.module_id,
                module_name=pair.strict.module_name,
                strict_status=pair.strict.evaluation_status,
                strict_is_complete=pair.strict.is_complete,
                strict_block_coverage=pair.strict.block_coverage,
                lenient_status=pair.lenient.evaluation_status,
                lenient_is_complete=pair.lenient.is_complete,
                lenient_block_coverage=pair.lenient.block_coverage,
                strict_to_lenient_changed=pair.strict_to_lenient_changed,
            )
            for pair in module_evaluations[:module_preview_count]
        ),
        module_previews_truncated=module_preview_count < len(module_evaluations),
        pathway_target_count=len(pathway_coverages),
        pathway_previews=tuple(
            PathwayAnalysisPreview(
                pathway_id=result.pathway_id,
                pathway_name=result.pathway_name,
                reference_namespace=result.reference_namespace,
                reference_scope=result.reference_scope,
                evidence_mode=result.evidence_mode,
                evaluation_status=result.evaluation_status,
                detected_unique_ko_count=result.detected_unique_ko_count,
                reference_unique_ko_count=result.reference_unique_ko_count,
                coverage_ratio=result.coverage_ratio,
                warning_codes=tuple(warning.code.value for warning in result.warnings),
            )
            for result in pathway_coverages[:pathway_preview_count]
        ),
        pathway_previews_truncated=pathway_preview_count < len(pathway_coverages),
        caveats=_caveats(
            has_module_results=bool(module_evaluations),
            has_pathway_results=bool(pathway_coverages),
        ),
        limits=output_limits,
    )


def _caveats(*, has_module_results: bool, has_pathway_results: bool) -> tuple[str, ...]:
    values = [_ANNOTATION_CAVEAT, _ABSENCE_CAVEAT]
    if has_module_results:
        values.append(_MODULE_CAVEAT)
    if has_pathway_results:
        values.append(_PATHWAY_CAVEAT)
    return tuple(values)


def _validate_report_store_compatibility(
    report_limits: ReportLimits,
    store_limits: ResultStoreLimits,
) -> None:
    """Fail before KEGG I/O when configured report maxima cannot be retained."""
    section_limits = (
        ("max_structured_json_bytes", report_limits.max_structured_json_bytes),
        ("max_markdown_bytes", report_limits.max_markdown_bytes),
        ("max_annotation_csv_bytes", report_limits.max_annotation_csv_bytes),
    )
    for limit_name, maximum in section_limits:
        if maximum > store_limits.max_artifact_bytes:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "A report artifact limit exceeds the configured result-store artifact limit.",
                suggested_action=(
                    "Lower the report artifact limit or configure a compatible bounded result "
                    "store."
                ),
                safe_details=(
                    SafeDetail(name="report_limit", value=limit_name),
                    SafeDetail(name="report_maximum", value=str(maximum)),
                    SafeDetail(
                        name="store_max_artifact_bytes",
                        value=str(store_limits.max_artifact_bytes),
                    ),
                ),
            )
    maximum_bundle_bytes = sum(maximum for _, maximum in section_limits)
    store_bundle_limit = min(store_limits.max_result_bytes, store_limits.quota_bytes)
    if maximum_bundle_bytes > store_bundle_limit:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The configured report bundle limits exceed the result-store result limit.",
            suggested_action=(
                "Lower the report byte limits or configure a compatible bounded result store."
            ),
            safe_details=(
                SafeDetail(name="report_bundle_maximum", value=str(maximum_bundle_bytes)),
                SafeDetail(name="store_bundle_limit", value=str(store_bundle_limit)),
            ),
        )


class _SharedReferenceBudgetClient:
    """Apply one request/response budget across both high-level reference loaders."""

    def __init__(self, client: KeggReferenceClient, limits: ReferenceLoadingLimits) -> None:
        self._client = client
        self._limits = limits
        self._requests = 0
        self._response_bytes = 0

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        self._reserve_request()
        result = self._client.get(request, options=options)
        self._record_response(result.batches)
        return result

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        self._reserve_request()
        result = self._client.link(request, options=options)
        self._record_response(result.batches)
        return result

    def _reserve_request(self) -> None:
        projected = self._requests + 1
        if projected > self._limits.max_total_kegg_requests:
            _fail_shared_reference_limit(
                "total_kegg_requests",
                projected,
                "max_total_kegg_requests",
                self._limits.max_total_kegg_requests,
            )
        self._requests = projected

    def _record_response(self, batches: tuple[KeggBatchProvenance, ...]) -> None:
        projected = self._response_bytes + sum(batch.response_bytes for batch in batches)
        if projected > self._limits.max_total_response_bytes:
            _fail_shared_reference_limit(
                "total_response_bytes",
                projected,
                "max_total_response_bytes",
                self._limits.max_total_response_bytes,
            )
        self._response_bytes = projected


def _fail_shared_reference_limit(
    metric: str,
    observed: int,
    limit_name: str,
    maximum: int,
) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "The combined one-call reference loading budget was exceeded.",
        suggested_action="Request fewer references or raise the explicit bounded service limit.",
        safe_details=(
            SafeDetail(name="metric", value=metric),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit_name", value=limit_name),
            SafeDetail(name="limit", value=str(maximum)),
        ),
    )


__all__ = ["analyze_plain_ko"]
