"""Deterministic multi-dataset KO, MODULE, and pathway comparison."""

from __future__ import annotations

from kegg_mcp.analysis import (
    ComparisonDatasetInput,
    ComparisonLimits,
    ComparisonPreviewLimits,
    FunctionalComparisonLimits,
    ModuleAnalysisLimits,
    PathwayCoverageLimits,
    compare_ko_datasets,
    compare_module_graphs,
    compare_pathway_references,
    summarize_ko_comparison,
)
from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.models import (
    DETAIL_SECTION,
    CompareDatasetSource,
    CompareKoSetsResult,
    FunctionalComparisonSummary,
)
from kegg_mcp.services.previews import _comparison_preview
from kegg_mcp.services.reference_budget import KeggPrimitiveClient, SharedReferenceBudgetClient
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    ReferenceLoadingLimits,
    load_module_graphs,
    load_pathway_references,
)
from kegg_mcp.services.result_builders import _artifact_metadata, _json_bytes, _resolve_dataset
from kegg_mcp.services.result_store import ResultArtifactInput, SQLiteResultStore


def compare_annotation_sets(
    inputs: tuple[CompareDatasetSource, ...],
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
    client: KeggPrimitiveClient | None = None,
    module_ids: tuple[str, ...] = (),
    pathways: tuple[PathwaySpec, ...] = (),
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    module_limits: ModuleAnalysisLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
    functional_limits: FunctionalComparisonLimits | None = None,
    allow_global_or_overview: bool = False,
    limits: ComparisonLimits | None = None,
    preview_limits: ComparisonPreviewLimits | None = None,
) -> CompareKoSetsResult:
    """Compare inline or scoped retained datasets with deterministic set semantics."""
    datasets = tuple(
        ComparisonDatasetInput(
            label=item.label,
            dataset=_resolve_dataset(item.source, result_store=result_store, scope_id=scope_id),
        )
        for item in inputs
    )
    detail = compare_ko_datasets(datasets, limits=limits)
    summary = summarize_ko_comparison(detail, limits=preview_limits)
    if (module_ids or pathways) and client is None:
        raise AssertionError("functional comparison targets require a KEGG reference client")
    effective_options = options or KeggRequestOptions()
    effective_reference_limits = reference_limits or ReferenceLoadingLimits()
    reference_client = (
        None if client is None else SharedReferenceBudgetClient(client, effective_reference_limits)
    )
    module_comparison = None
    if module_ids:
        if reference_client is None:
            raise AssertionError("MODULE comparison targets require a KEGG reference client")
        graphs = load_module_graphs(
            reference_client,
            module_ids,
            options=effective_options,
            limits=effective_reference_limits,
            analysis_limits=module_limits,
        )
        module_comparison = compare_module_graphs(
            datasets,
            graphs,
            comparison_limits=limits,
            functional_limits=functional_limits,
        )
    pathway_comparison = None
    if pathways:
        if reference_client is None:
            raise AssertionError("pathway comparison targets require a KEGG reference client")
        references = load_pathway_references(
            reference_client,
            pathways,
            options=effective_options,
            limits=effective_reference_limits,
            pathway_limits=pathway_limits,
        )
        pathway_comparison = compare_pathway_references(
            datasets,
            references,
            comparison_limits=limits,
            functional_limits=functional_limits,
            coverage_limits=pathway_limits,
            allow_global_or_overview=allow_global_or_overview,
        )
    payload = _json_bytes(
        {
            "ko_comparison": detail.model_dump(mode="json"),
            "module_comparison": (
                None if module_comparison is None else module_comparison.model_dump(mode="json")
            ),
            "pathway_comparison": (
                None if pathway_comparison is None else pathway_comparison.model_dump(mode="json")
            ),
        }
    )
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    return CompareKoSetsResult(
        result=stored,
        artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
        summary=_comparison_preview(summary),
        functional_summary=FunctionalComparisonSummary(
            module_target_count=(
                0 if module_comparison is None else len(module_comparison.targets)
            ),
            module_differences=(
                ()
                if module_comparison is None
                else tuple(
                    target.module_id
                    for target in module_comparison.targets
                    if target.comparison.outcomes_differ
                )
            ),
            pathway_target_count=(
                0 if pathway_comparison is None else len(pathway_comparison.targets)
            ),
            pathway_differences=(
                ()
                if pathway_comparison is None
                else tuple(
                    target.reference.pathway_id
                    for target in pathway_comparison.targets
                    if target.comparison.outcomes_differ
                )
            ),
        ),
    )


__all__ = ["compare_annotation_sets"]
