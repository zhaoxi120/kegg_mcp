"""Bounded KEGG MODULE analysis use case."""

from __future__ import annotations

from kegg_mcp.analysis import ModuleAnalysisLimits, evaluate_module
from kegg_mcp.execution import ExecutionStage
from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.models import AnalyzeModulesResult, DatasetSource
from kegg_mcp.services.previews import _module_preview
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.reference_loading import ReferenceLoadingLimits, load_module_graphs
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
) -> AnalyzeModulesResult:
    """Evaluate bounded MODULE targets using inline or retained annotation evidence."""
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    effective_options = options or KeggRequestOptions()
    effective_reference_limits = reference_limits or ReferenceLoadingLimits()
    effective_analysis_limits = analysis_limits or ModuleAnalysisLimits()
    refs = load_module_graphs(
        client,
        module_ids,
        options=effective_options,
        limits=effective_reference_limits,
        analysis_limits=effective_analysis_limits,
    )
    evaluations = tuple(
        evaluate_module(graph, dataset, effective_analysis_limits) for graph in refs
    )
    reference_provenance = _reference_provenance(evaluations, ())
    metrics = _execution_metrics(
        {stage: 0 for stage in ExecutionStage},
        reference_provenance=reference_provenance,
    )
    warnings = _analysis_warnings(dataset, evaluations, ())
    result, artifacts = _retain_json_detail(
        {
            "analysis_kind": "modules",
            "dataset_provenance": _dataset_provenance_payload(dataset),
            "execution": {
                "kegg_request_options": effective_options.model_dump(mode="json"),
                "reference_loading_limits": effective_reference_limits.model_dump(mode="json"),
                "module_analysis_limits": effective_analysis_limits.model_dump(mode="json"),
                "metrics": [item.model_dump(mode="json") for item in metrics],
            },
            "reference_provenance": [item.model_dump(mode="json") for item in reference_provenance],
            "module_evaluations": [item.model_dump(mode="json") for item in evaluations],
        },
        result_store=result_store,
        scope_id=scope_id,
    )
    previews = tuple(_module_preview(item) for item in evaluations)
    return AnalyzeModulesResult(
        result=result,
        artifacts=artifacts,
        summary=_build_analysis_summary(
            dataset,
            metrics=metrics,
            caveats=(
                (
                    "Exact MODULE completion and project-defined required-block coverage are "
                    "separate results."
                ),
                "K-number assignments are annotation evidence, not experimental validation.",
            ),
            warnings=warnings,
        ),
        module_target_count=len(previews),
        module_previews=previews,
    )


__all__ = ["analyze_module_targets"]
