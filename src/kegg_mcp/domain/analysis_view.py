"""Compact accepted-KO views for bounded downstream analysis."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnalysisUnit,
    AnnotationDataset,
    DecisionPolicyReference,
    EvidenceField,
    FrozenModel,
    ImportDiagnostic,
    KNumber,
    NormalizedStatus,
    SourceColumnName,
    SourceProvenance,
    StatusCount,
)

MAX_ANALYSIS_VIEW_INPUT_BYTES = 1 << 30
MAX_ANALYSIS_VIEW_INPUT_ROWS = 10_000_000
MAX_ANALYSIS_VIEW_EXPANDED_ASSIGNMENTS = 20_000_000
MAX_ANALYSIS_VIEW_UNIQUE_KO_IDS = 100_000
MAX_ANALYSIS_VIEW_COLUMNS = 64
MAX_ANALYSIS_VIEW_FIELD_LENGTH = 16_384
MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW = 100


class KoAnalysisView(FrozenModel):
    """Sorted unique accepted KOs plus aggregate intake provenance.

    The view is the only input accepted by MODULE, pathway, ranking, and rendering analysis.
    It intentionally omits source rows, record-level evidence, sequence-to-KO mappings, and
    duplicate/conflict indexes. Full normalization and annotation audit retain those separately.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:ko-analysis-view:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    accepted_ko_ids: Annotated[
        tuple[KNumber, ...],
        Field(max_length=MAX_ANALYSIS_VIEW_UNIQUE_KO_IDS),
    ]
    input_bytes: int | None = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_ANALYSIS_VIEW_INPUT_BYTES,
    )
    input_rows: int = Field(strict=True, ge=0, le=MAX_ANALYSIS_VIEW_INPUT_ROWS)
    assignment_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_ANALYSIS_VIEW_EXPANDED_ASSIGNMENTS,
    )
    skipped_rows: int = Field(strict=True, ge=0, le=MAX_ANALYSIS_VIEW_INPUT_ROWS)
    source_columns: Annotated[
        tuple[SourceColumnName, ...],
        Field(max_length=MAX_ANALYSIS_VIEW_COLUMNS),
    ]
    status_counts: tuple[StatusCount, ...]
    diagnostic_count: int = Field(strict=True, ge=0)
    diagnostic_preview: Annotated[
        tuple[ImportDiagnostic, ...],
        Field(max_length=MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW),
    ] = ()
    diagnostics_truncated: bool = False
    decision_policy: DecisionPolicyReference
    sources: Annotated[tuple[SourceProvenance, ...], Field(min_length=1, max_length=128)]
    analysis_unit: AnalysisUnit
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")
    metadata: Annotated[tuple[EvidenceField, ...], Field(max_length=128)] = ()

    @model_validator(mode="after")
    def validate_view(self) -> Self:
        if self.accepted_ko_ids != tuple(sorted(set(self.accepted_ko_ids))):
            raise ValueError("accepted_ko_ids must be a sorted tuple of unique K numbers")
        if self.skipped_rows > self.input_rows:
            raise ValueError("skipped_rows must not exceed input_rows")
        statuses = tuple(item.status for item in self.status_counts)
        if statuses != tuple(NormalizedStatus):
            raise ValueError("status_counts must contain every normalized status in enum order")
        if sum(item.count for item in self.status_counts) != self.assignment_count:
            raise ValueError("status_counts must sum to assignment_count")
        parsed_rows = self.input_rows - self.skipped_rows
        if (parsed_rows == 0 and self.assignment_count != 0) or (
            parsed_rows > 0 and self.assignment_count < parsed_rows
        ):
            raise ValueError("each non-skipped source row must emit at least one assignment")
        accepted_count = next(
            item.count for item in self.status_counts if item.status is NormalizedStatus.ACCEPTED
        )
        if len(self.accepted_ko_ids) > accepted_count:
            raise ValueError("unique accepted K numbers cannot exceed accepted assignments")
        if self.diagnostic_count < len(self.diagnostic_preview):
            raise ValueError("diagnostic_count cannot be smaller than diagnostic_preview")
        if self.diagnostics_truncated != (self.diagnostic_count > len(self.diagnostic_preview)):
            raise ValueError("diagnostics_truncated must match the diagnostic preview count")
        if len(self.source_columns) != len(set(self.source_columns)):
            raise ValueError("source_columns must be unique")
        return self


def build_ko_analysis_view(
    dataset: AnnotationDataset,
    *,
    input_bytes: int | None = None,
) -> KoAnalysisView:
    """Reduce complete normalized evidence into the one compact analysis contract."""
    counts = {item.status: item.count for item in dataset.import_report.status_counts}
    diagnostics = dataset.import_report.diagnostics
    return KoAnalysisView(
        dataset_id=dataset.dataset_id,
        accepted_ko_ids=tuple(
            sorted(
                {
                    record.ko_id
                    for record in dataset.records
                    if record.normalized_status is NormalizedStatus.ACCEPTED
                    and record.ko_id is not None
                }
            )
        ),
        input_bytes=input_bytes,
        input_rows=dataset.import_report.input_rows,
        assignment_count=len(dataset.records),
        skipped_rows=dataset.import_report.skipped_rows,
        source_columns=dataset.import_report.source_columns,
        status_counts=tuple(
            StatusCount(status=status, count=counts[status]) for status in NormalizedStatus
        ),
        diagnostic_count=len(diagnostics),
        diagnostic_preview=diagnostics[:MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW],
        diagnostics_truncated=len(diagnostics) > MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW,
        decision_policy=dataset.import_report.decision_policy,
        sources=dataset.sources,
        analysis_unit=dataset.analysis_unit,
        taxon_id=dataset.taxon_id,
        kegg_organism_code=dataset.kegg_organism_code,
        metadata=dataset.metadata,
    )


__all__ = [
    "MAX_ANALYSIS_VIEW_COLUMNS",
    "MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW",
    "MAX_ANALYSIS_VIEW_EXPANDED_ASSIGNMENTS",
    "MAX_ANALYSIS_VIEW_FIELD_LENGTH",
    "MAX_ANALYSIS_VIEW_INPUT_BYTES",
    "MAX_ANALYSIS_VIEW_INPUT_ROWS",
    "MAX_ANALYSIS_VIEW_UNIQUE_KO_IDS",
    "KoAnalysisView",
    "build_ko_analysis_view",
]
