"""Shared retained-result assembly, metrics, and dataset resolution."""

from __future__ import annotations

import json
import time

from kegg_mcp.analysis import ModuleEvaluationResult, PathwayCoverageResult
from kegg_mcp.domain.annotations import (
    AnnotationDataset,
    NormalizedStatus,
)
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.domain.projections import (
    KoAnalysisEvidence,
    analysis_accepted_ko_ids,
    analysis_diagnostic_count,
    analysis_diagnostic_preview,
    analysis_input_rows,
    analysis_status_counts,
)
from kegg_mcp.execution import ExecutionStage, StageMetric
from kegg_mcp.importers import import_plain_ko
from kegg_mcp.kegg import ResponseOrigin
from kegg_mcp.kegg.contracts import KeggBatchProvenance
from kegg_mcp.reporting import ReportLimits
from kegg_mcp.services.models import (
    DATASET_SECTION,
    DEFAULT_IMPORT_LIMITS,
    DETAIL_SECTION,
    MAX_DIRECT_REFERENCE_BATCHES,
    MAX_DIRECT_WARNING_CHARACTERS,
    MAX_DIRECT_WARNINGS,
    AnalysisResultSummary,
    DatasetSource,
)
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    ResultArtifactMetadata,
    ResultMetadata,
    SQLiteResultStore,
)


def _resolve_dataset(
    source: DatasetSource,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> AnnotationDataset:
    if source.ko_text is not None:
        return import_plain_ko(
            source.ko_text,
            limits=DEFAULT_IMPORT_LIMITS,
            analysis_unit=source.analysis_unit,
            sample_id=source.sample_id,
        )
    if source.result_id is None:
        raise AssertionError("dataset source validation omitted both source variants")
    chunks: list[bytes] = []
    offset = 0
    while True:
        page = result_store.read_artifact(
            scope_id,
            source.result_id,
            DATASET_SECTION,
            offset=offset,
            limit=result_store.limits.max_range_bytes,
        )
        chunks.append(page.content)
        if page.next_offset is None:
            break
        offset = page.next_offset
    try:
        return AnnotationDataset.model_validate_json(b"".join(chunks))
    except ValueError:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The retained result does not contain a valid annotation dataset.",
            suggested_action="Use the result_id returned by normalize_ko_annotations.",
        )


def _elapsed_ms(started_ns: int) -> int:
    """Return a non-negative integral elapsed time without exposing wall-clock data."""
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def _status_record_counts(evidence: KoAnalysisEvidence) -> dict[NormalizedStatus, int]:
    return {item.status: item.count for item in analysis_status_counts(evidence)}


def _execution_metrics(
    elapsed: dict[ExecutionStage, int],
    *,
    mapping_provenance: tuple[KeggBatchProvenance, ...] = (),
    reference_provenance: tuple[KeggBatchProvenance, ...] = (),
) -> tuple[StageMetric, ...]:
    """Build the fixed six-row direct metric summary."""
    values: list[StageMetric] = []
    for stage in ExecutionStage:
        if stage is ExecutionStage.KO_TARGET_MAPPING:
            batches = mapping_provenance
        elif stage is ExecutionStage.REFERENCE_LOADING:
            batches = reference_provenance
        else:
            batches = ()
        values.append(
            StageMetric(
                stage=stage,
                elapsed_ms=elapsed.get(stage, 0),
                request_count=len(batches),
                network_request_count=sum(
                    batch.attempt_count
                    for batch in batches
                    if batch.origin is ResponseOrigin.NETWORK
                ),
                cache_hit_count=sum(batch.origin is ResponseOrigin.CACHE for batch in batches),
                response_bytes=sum(batch.response_bytes for batch in batches),
            )
        )
    return tuple(values)


def _reference_provenance(
    modules: tuple[ModuleEvaluationResult, ...],
    pathways: tuple[PathwayCoverageResult, ...],
    *,
    additional: tuple[KeggBatchProvenance, ...] = (),
) -> tuple[KeggBatchProvenance, ...]:
    batches = list(additional)
    batches.extend(
        batch for module in modules for batch in module.reference_retrieval_provenance
    )
    batches.extend(
        batch
        for pathway in pathways
        for batch in (
            *pathway.reference_link_provenance,
            *pathway.reference_metadata_provenance,
        )
    )
    unique: list[KeggBatchProvenance] = []
    seen: set[str] = set()
    for batch in batches:
        key = batch.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        unique.append(batch)
    if len(unique) > MAX_DIRECT_REFERENCE_BATCHES:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Reference provenance exceeds the direct-result batch bound.",
            suggested_action="Request fewer MODULE or pathway references.",
        )
    return tuple(unique)


def _analysis_warnings(
    evidence: KoAnalysisEvidence,
    modules: tuple[ModuleEvaluationResult, ...],
    pathways: tuple[PathwayCoverageResult, ...],
) -> tuple[str, ...]:
    values = [diagnostic.message for diagnostic in analysis_diagnostic_preview(evidence)]
    values.extend(warning.message for module in modules for warning in module.warnings)
    values.extend(warning.message for pathway in pathways for warning in pathway.warnings)
    return tuple(dict.fromkeys(values))


def _analysis_warning_count(
    evidence: KoAnalysisEvidence,
    modules: tuple[ModuleEvaluationResult, ...],
    pathways: tuple[PathwayCoverageResult, ...],
) -> int:
    """Count diagnostics exactly even when a lossy projection retains only a preview."""
    return (
        analysis_diagnostic_count(evidence)
        + sum(len(module.warnings) for module in modules)
        + sum(len(pathway.warnings) for pathway in pathways)
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _artifact_metadata(section: str, mime_type: str, content: bytes) -> ResultArtifactMetadata:
    return ResultArtifactMetadata(
        section=section,
        mime_type=mime_type,
        byte_size=len(content),
    )


def _retain_json_detail(
    value: object,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> tuple[ResultMetadata, tuple[ResultArtifactMetadata, ...]]:
    """Retain one canonical JSON detail artifact and return its direct metadata."""
    content = _json_bytes(value)
    result = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION,
                mime_type="application/json",
                content=content,
            ),
        ),
    )
    return result, (_artifact_metadata(DETAIL_SECTION, "application/json", content),)


def _build_analysis_summary(
    evidence: KoAnalysisEvidence,
    *,
    metrics: tuple[StageMetric, ...],
    caveats: tuple[str, ...],
    warnings: tuple[str, ...] = (),
    warning_count: int | None = None,
) -> AnalysisResultSummary:
    """Build the small count and message summary shared by analysis tools."""
    status_counts = _status_record_counts(evidence)
    selected_ko_ids = analysis_accepted_ko_ids(evidence)
    warning_preview = tuple(
        warning[:MAX_DIRECT_WARNING_CHARACTERS] for warning in warnings[:MAX_DIRECT_WARNINGS]
    )
    effective_warning_count = len(warnings) if warning_count is None else warning_count
    return AnalysisResultSummary(
        input_records=analysis_input_rows(evidence),
        accepted_records=status_counts[NormalizedStatus.ACCEPTED],
        rejected_records=status_counts[NormalizedStatus.REJECTED],
        unclassified_records=status_counts[NormalizedStatus.UNCLASSIFIED],
        invalid_records=status_counts[NormalizedStatus.INVALID],
        selected_unique_ko_count=len(selected_ko_ids),
        kegg_request_count=sum(item.request_count for item in metrics),
        network_request_count=sum(item.network_request_count for item in metrics),
        cache_hit_count=sum(item.cache_hit_count for item in metrics),
        kegg_response_bytes=sum(item.response_bytes for item in metrics),
        caveats=caveats,
        warning_count=effective_warning_count,
        warnings=warning_preview,
        warnings_truncated=len(warning_preview) < effective_warning_count,
    )


def _dataset_provenance_payload(dataset: AnnotationDataset) -> dict[str, object]:
    """Serialize complete dataset-level provenance without duplicating annotation records."""
    return {
        "dataset_id": dataset.dataset_id,
        "analysis_unit": dataset.analysis_unit.value,
        "taxon_id": dataset.taxon_id,
        "kegg_organism_code": dataset.kegg_organism_code,
        "metadata": [item.model_dump(mode="json") for item in dataset.metadata],
        "sources": [item.model_dump(mode="json") for item in dataset.sources],
        "import_report": dataset.import_report.model_dump(mode="json"),
    }


def _validate_report_capacity(limits: ReportLimits, store: SQLiteResultStore) -> None:
    maxima = (
        limits.max_structured_json_bytes,
        limits.max_markdown_bytes,
        limits.max_annotation_csv_bytes,
    )
    if any(maximum > store.limits.max_artifact_bytes for maximum in maxima):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "A report artifact limit exceeds the retained-result artifact limit.",
            suggested_action="Use compatible report and result-store byte limits.",
        )
    if sum(maxima) > min(store.limits.max_result_bytes, store.limits.quota_bytes):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The report bundle limits exceed retained-result capacity.",
            suggested_action="Use compatible report and result-store bundle limits.",
        )


__all__ = [
    "_analysis_warning_count",
    "_analysis_warnings",
    "_artifact_metadata",
    "_build_analysis_summary",
    "_dataset_provenance_payload",
    "_elapsed_ms",
    "_execution_metrics",
    "_json_bytes",
    "_reference_provenance",
    "_resolve_dataset",
    "_retain_json_detail",
    "_status_record_counts",
    "_validate_report_capacity",
]
