"""Bounded descriptive pathway analysis use case."""

from __future__ import annotations

from kegg_mcp.analysis import (
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    evaluate_pathway_coverage,
)
from kegg_mcp.domain.annotations import (
    EvidenceMode,
)
from kegg_mcp.execution import ExecutionStage
from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.models import DatasetSource, PrimitiveAnalysisResult
from kegg_mcp.services.previews import _pathway_preview
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    ReferenceLoadingLimits,
    load_pathway_references,
)
from kegg_mcp.services.result_builders import (
    _build_primitive_analysis_result,
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
    evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    allow_global_or_overview: bool = False,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
) -> PrimitiveAnalysisResult:
    """Evaluate bounded descriptive pathway coverage from retained or inline evidence."""
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    refs = load_pathway_references(
        client,
        pathways,
        options=options or KeggRequestOptions(),
        limits=reference_limits,
        pathway_limits=pathway_limits,
    )
    coverages = tuple(
        evaluate_pathway_coverage(
            reference,
            dataset,
            PathwayCoverageParameters(
                reference_namespace=reference.reference_namespace,
                evidence_mode=evidence_mode,
                allow_global_or_overview=allow_global_or_overview,
            ),
            pathway_limits,
        )
        for reference in refs
    )
    result, artifacts = _retain_json_detail(
        {"pathway_coverages": [item.model_dump(mode="json") for item in coverages]},
        result_store=result_store,
        scope_id=scope_id,
    )
    reference_provenance = _reference_provenance((), coverages)
    metrics = _execution_metrics(
        {stage: 0 for stage in ExecutionStage},
        reference_provenance=reference_provenance,
    )
    return _build_primitive_analysis_result(
        dataset,
        evidence_mode=evidence_mode,
        result=result,
        artifacts=artifacts,
        metrics=metrics,
        pathway_previews=tuple(_pathway_preview(item) for item in coverages),
        caveats=(
            (
                "Pathway KO coverage is descriptive and does not establish pathway presence, "
                "activity, or flux."
            ),
            "The reference namespace and unique-KO denominator are explicit in every result.",
        ),
        reference_provenance=reference_provenance,
    )


__all__ = ["analyze_pathway_targets"]
