"""Bounded descriptive pathway analysis use case."""

from __future__ import annotations

from kegg_mcp.analysis import (
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    evaluate_pathway_coverage,
)
from kegg_mcp.execution import ExecutionStage
from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.models import AnalyzePathwaysResult, DatasetSource
from kegg_mcp.services.previews import _pathway_preview
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    ReferenceLoadingLimits,
    load_pathway_references,
)
from kegg_mcp.services.result_builders import (
    _analysis_warnings,
    _build_analysis_summary,
    _dataset_provenance_payload,
    _execution_metrics,
    _reference_provenance,
    _resolve_dataset,
    _retain_json_detail,
)
from kegg_mcp.services.result_store import SQLiteResultStore


def analyze_pathway_targets(
    source: DatasetSource,
    pathways: tuple[PathwaySpec, ...],
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    allow_global_or_overview: bool = False,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
) -> AnalyzePathwaysResult:
    """Evaluate bounded descriptive pathway coverage from retained or inline evidence."""
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    effective_options = options or KeggRequestOptions()
    effective_reference_limits = reference_limits or ReferenceLoadingLimits()
    effective_pathway_limits = pathway_limits or PathwayCoverageLimits()
    refs = load_pathway_references(
        client,
        pathways,
        options=effective_options,
        limits=effective_reference_limits,
        pathway_limits=effective_pathway_limits,
    )
    coverages = tuple(
        evaluate_pathway_coverage(
            reference,
            dataset,
            PathwayCoverageParameters(
                reference_namespace=reference.reference_namespace,
                allow_global_or_overview=allow_global_or_overview,
            ),
            effective_pathway_limits,
        )
        for reference in refs
    )
    reference_provenance = _reference_provenance((), coverages)
    metrics = _execution_metrics(
        {stage: 0 for stage in ExecutionStage},
        reference_provenance=reference_provenance,
    )
    warnings = _analysis_warnings(dataset, (), coverages)
    result, artifacts = _retain_json_detail(
        {
            "analysis_kind": "pathways",
            "dataset_provenance": _dataset_provenance_payload(dataset),
            "execution": {
                "allow_global_or_overview": allow_global_or_overview,
                "kegg_request_options": effective_options.model_dump(mode="json"),
                "reference_loading_limits": effective_reference_limits.model_dump(mode="json"),
                "pathway_coverage_limits": effective_pathway_limits.model_dump(mode="json"),
                "metrics": [item.model_dump(mode="json") for item in metrics],
            },
            "reference_provenance": [item.model_dump(mode="json") for item in reference_provenance],
            "pathway_coverages": [item.model_dump(mode="json") for item in coverages],
        },
        result_store=result_store,
        scope_id=scope_id,
    )
    previews = tuple(_pathway_preview(item) for item in coverages)
    return AnalyzePathwaysResult(
        result=result,
        artifacts=artifacts,
        summary=_build_analysis_summary(
            dataset,
            metrics=metrics,
            caveats=(
                (
                    "Pathway KO coverage is descriptive and does not establish pathway presence, "
                    "activity, or flux."
                ),
                "The reference namespace and unique-KO denominator are explicit in every result.",
            ),
            warnings=warnings,
        ),
        pathway_target_count=len(previews),
        pathway_previews=previews,
    )


__all__ = ["analyze_pathway_targets"]
