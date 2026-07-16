"""Bounded KEGG MODULE analysis use case."""

from __future__ import annotations

from kegg_mcp.analysis import ModuleAnalysisLimits, evaluate_module_pair
from kegg_mcp.domain.annotations import EvidenceMode
from kegg_mcp.execution import ExecutionStage
from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.models import DatasetSource, PrimitiveAnalysisResult
from kegg_mcp.services.previews import _module_preview
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.reference_loading import ReferenceLoadingLimits, load_module_graphs
from kegg_mcp.services.result_builders import (
    _build_primitive_analysis_result,
    _execution_metrics,
    _reference_provenance,
    _resolve_dataset,
    _retain_json_detail,
)
from kegg_mcp.services.result_store import SQLiteResultStore


def analyze_module_targets(
    source: DatasetSource,
    module_ids: tuple[str, ...],
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    analysis_limits: ModuleAnalysisLimits | None = None,
) -> PrimitiveAnalysisResult:
    """Evaluate bounded MODULE targets using inline or retained annotation evidence."""
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    refs = load_module_graphs(
        client,
        module_ids,
        options=options or KeggRequestOptions(),
        limits=reference_limits,
        analysis_limits=analysis_limits,
    )
    pairs = tuple(evaluate_module_pair(graph, dataset, analysis_limits) for graph in refs)
    result, artifacts = _retain_json_detail(
        {"module_evaluations": [item.model_dump(mode="json") for item in pairs]},
        result_store=result_store,
        scope_id=scope_id,
    )
    reference_provenance = _reference_provenance(pairs, ())
    metrics = _execution_metrics(
        {stage: 0 for stage in ExecutionStage},
        reference_provenance=reference_provenance,
    )
    return _build_primitive_analysis_result(
        dataset,
        evidence_mode=EvidenceMode.STRICT,
        result=result,
        artifacts=artifacts,
        metrics=metrics,
        module_previews=tuple(_module_preview(item) for item in pairs),
        caveats=(
            (
                "Exact MODULE completion and project-defined required-block coverage are "
                "separate results."
            ),
            "K-number assignments are annotation evidence, not experimental validation.",
        ),
        reference_provenance=reference_provenance,
    )


__all__ = ["analyze_module_targets"]
