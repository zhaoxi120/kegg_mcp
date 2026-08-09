"""Bounded direct-result previews and provenance summaries."""

from __future__ import annotations

from kegg_mcp.analysis import KoSetComparisonSummary, ModuleEvaluationResult, PathwayCoverageResult
from kegg_mcp.domain.annotations import AnnotationDataset, AnnotationRecord, SourceProvenance
from kegg_mcp.services.contracts import ImportSummary, ModuleAnalysisPreview, PathwayAnalysisPreview
from kegg_mcp.services.models import (
    MAX_DIRECT_SOURCE_PREVIEWS,
    AnnotationProvenanceSummary,
    AnnotationRecordPreview,
    AnnotationSourceSummary,
    ComparisonDatasetSummary,
    KoSetComparisonPreview,
)


def _import_summary(dataset: AnnotationDataset) -> ImportSummary:
    report = dataset.import_report
    return ImportSummary(
        dataset_id=dataset.dataset_id,
        analysis_unit=dataset.analysis_unit,
        input_rows=report.input_rows,
        emitted_records=report.emitted_records,
        skipped_rows=report.skipped_rows,
        duplicate_count=report.duplicate_count,
        conflict_count=report.conflict_count,
        status_counts=report.status_counts,
    )


def _module_preview(item: ModuleEvaluationResult) -> ModuleAnalysisPreview:
    return ModuleAnalysisPreview(
        module_id=item.module_id,
        module_name=item.module_name,
        evaluation_status=item.evaluation_status,
        is_complete=item.is_complete,
        block_coverage=item.block_coverage,
    )


def _pathway_preview(item: PathwayCoverageResult) -> PathwayAnalysisPreview:
    return PathwayAnalysisPreview(
        pathway_id=item.pathway_id,
        pathway_name=item.pathway_name,
        reference_namespace=item.reference_namespace,
        reference_scope=item.reference_scope,
        evaluation_status=item.evaluation_status,
        detected_unique_ko_count=item.detected_unique_ko_count,
        reference_unique_ko_count=item.reference_unique_ko_count,
        coverage_ratio=item.coverage_ratio,
        warning_codes=tuple(warning.code.value for warning in item.warnings),
    )


def _annotation_record_preview(record: AnnotationRecord) -> AnnotationRecordPreview:
    return AnnotationRecordPreview(
        record_id=record.record_id,
        sample_id=record.sample_id,
        sequence_id=record.sequence_id,
        ko_id=record.ko_id,
        normalized_status=record.normalized_status,
        status_reason=record.status_reason,
        score=record.score,
        score_type=record.score_type,
        threshold=record.threshold,
        threshold_rule=record.threshold_rule,
        rank=record.rank,
        domain_start=record.domain_start,
        domain_end=record.domain_end,
    )


def _annotation_source_summary(source: SourceProvenance) -> AnnotationSourceSummary:
    return AnnotationSourceSummary(
        source_name=source.source_name,
        source_version=source.source_version,
        model_name=source.model_name,
        model_version=source.model_version,
        annotation_date=source.annotation_date,
        input_path=source.input_path,
        importer_name=source.importer_name,
        importer_version=source.importer_version,
    )


def _annotation_provenance(dataset: AnnotationDataset) -> AnnotationProvenanceSummary:
    source_preview = tuple(
        _annotation_source_summary(source)
        for source in dataset.sources[:MAX_DIRECT_SOURCE_PREVIEWS]
    )
    return AnnotationProvenanceSummary(
        dataset_id=dataset.dataset_id,
        decision_policy=dataset.import_report.decision_policy,
        analysis_unit=dataset.analysis_unit,
        taxon_id=dataset.taxon_id,
        kegg_organism_code=dataset.kegg_organism_code,
        source_count=len(dataset.sources),
        source_preview=source_preview,
        sources_truncated=len(source_preview) < len(dataset.sources),
    )


def _comparison_preview(summary: KoSetComparisonSummary) -> KoSetComparisonPreview:
    datasets: list[ComparisonDatasetSummary] = []
    for item in summary.datasets:
        source_preview = tuple(
            _annotation_source_summary(source)
            for source in item.sources[:MAX_DIRECT_SOURCE_PREVIEWS]
        )
        datasets.append(
            ComparisonDatasetSummary(
                input_index=item.input_index,
                label=item.label,
                annotation=AnnotationProvenanceSummary(
                    dataset_id=item.dataset_id,
                    decision_policy=item.decision_policy,
                    analysis_unit=item.analysis_unit,
                    taxon_id=item.taxon_id,
                    kegg_organism_code=item.kegg_organism_code,
                    source_count=len(item.sources),
                    source_preview=source_preview,
                    sources_truncated=len(source_preview) < len(item.sources),
                ),
                sample_label_count=len(item.sample_labels),
                record_count=item.record_count,
                selected_unique_ko_count=item.selected_unique_ko_count,
            )
        )
    return KoSetComparisonPreview(
        datasets=tuple(datasets),
        partition=summary.partition,
        calculation_method=summary.calculation_method,
        warnings=summary.warnings,
        detail_limits=summary.detail_limits,
        preview_limits=summary.preview_limits,
    )


__all__ = [
    "_annotation_provenance",
    "_annotation_record_preview",
    "_annotation_source_summary",
    "_comparison_preview",
    "_import_summary",
    "_module_preview",
    "_pathway_preview",
]
