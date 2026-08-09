"""Compact, explicitly lossy KO projections for bounded downstream analysis."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self, TypeAlias

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

MAX_PROJECTION_INPUT_BYTES = 1 << 30
MAX_PROJECTION_INPUT_ROWS = 10_000_000
MAX_PROJECTION_EXPANDED_ASSIGNMENTS = 20_000_000
MAX_PROJECTION_UNIQUE_KO_IDS = 100_000
MAX_PROJECTION_COLUMNS = 64
MAX_PROJECTION_FIELD_LENGTH = 16_384
MAX_PROJECTION_DIAGNOSTIC_PREVIEW = 100


class AnnotationRetention(StrEnum):
    """Record-retention choices exposed by the high-level analysis contract."""

    FULL_RECORDS = "full_records"
    UNIQUE_ACCEPTED_KO_PROJECTION = "unique_accepted_ko_projection"


class KoAnalysisProjection(FrozenModel):
    """Unique accepted KOs plus exact aggregate intake accounting.

    This contract never represents a normalized annotation dataset. Source rows, record-level
    evidence, sequence-to-KO mappings, and duplicate/conflict indexes are intentionally absent.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:ko-analysis-projection:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    accepted_ko_ids: Annotated[
        tuple[KNumber, ...],
        Field(max_length=MAX_PROJECTION_UNIQUE_KO_IDS),
    ]
    input_bytes: int = Field(strict=True, ge=1, le=MAX_PROJECTION_INPUT_BYTES)
    input_rows: int = Field(strict=True, ge=0, le=MAX_PROJECTION_INPUT_ROWS)
    expanded_assignments: int = Field(
        strict=True,
        ge=0,
        le=MAX_PROJECTION_EXPANDED_ASSIGNMENTS,
    )
    skipped_rows: int = Field(strict=True, ge=0, le=MAX_PROJECTION_INPUT_ROWS)
    source_columns: Annotated[
        tuple[SourceColumnName, ...],
        Field(min_length=1, max_length=MAX_PROJECTION_COLUMNS),
    ]
    status_counts: tuple[StatusCount, ...]
    diagnostic_count: int = Field(strict=True, ge=0)
    diagnostic_preview: Annotated[
        tuple[ImportDiagnostic, ...],
        Field(max_length=MAX_PROJECTION_DIAGNOSTIC_PREVIEW),
    ] = ()
    diagnostics_truncated: bool = False
    decision_policy: DecisionPolicyReference
    sources: Annotated[tuple[SourceProvenance, ...], Field(min_length=1, max_length=1)]
    analysis_unit: AnalysisUnit
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")
    metadata: Annotated[tuple[EvidenceField, ...], Field(max_length=128)] = ()
    annotation_retention: Literal["unique_accepted_ko_projection"] = (
        "unique_accepted_ko_projection"
    )
    record_level_evidence_retained: Literal[False] = False
    protein_ko_mapping_available: Literal[False] = False
    duplicate_conflict_accounting: Literal["not_evaluated"] = "not_evaluated"

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.accepted_ko_ids != tuple(sorted(set(self.accepted_ko_ids))):
            raise ValueError("accepted_ko_ids must be a sorted tuple of unique K numbers")
        if self.skipped_rows > self.input_rows:
            raise ValueError("skipped_rows must not exceed input_rows")
        statuses = tuple(item.status for item in self.status_counts)
        if statuses != tuple(NormalizedStatus):
            raise ValueError("status_counts must contain every normalized status in enum order")
        if sum(item.count for item in self.status_counts) != self.expanded_assignments:
            raise ValueError("status_counts must sum to expanded_assignments")
        parsed_rows = self.input_rows - self.skipped_rows
        if (parsed_rows == 0 and self.expanded_assignments != 0) or (
            parsed_rows > 0 and self.expanded_assignments < parsed_rows
        ):
            raise ValueError("each non-skipped source row must emit at least one assignment")
        accepted_count = next(
            item.count
            for item in self.status_counts
            if item.status is NormalizedStatus.ACCEPTED
        )
        if len(self.accepted_ko_ids) > accepted_count:
            raise ValueError("unique accepted K numbers cannot exceed accepted assignments")
        if self.diagnostic_count < len(self.diagnostic_preview):
            raise ValueError("diagnostic_count cannot be smaller than diagnostic_preview")
        if self.diagnostics_truncated != (
            self.diagnostic_count > len(self.diagnostic_preview)
        ):
            raise ValueError("diagnostics_truncated must match the diagnostic preview count")
        if len(self.source_columns) != len(set(self.source_columns)):
            raise ValueError("source_columns must be unique")
        return self


KoAnalysisEvidence: TypeAlias = AnnotationDataset | KoAnalysisProjection


def analysis_accepted_ko_ids(evidence: KoAnalysisEvidence) -> tuple[str, ...]:
    """Return the sorted unique accepted K numbers selected by either evidence contract."""
    if isinstance(evidence, AnnotationDataset):
        return tuple(
            sorted(
                {
                    record.ko_id
                    for record in evidence.records
                    if record.normalized_status is NormalizedStatus.ACCEPTED
                    and record.ko_id is not None
                }
            )
        )
    return evidence.accepted_ko_ids


def analysis_decision_policy(evidence: KoAnalysisEvidence) -> DecisionPolicyReference:
    """Return the one named decision policy used to classify the analysis evidence."""
    if isinstance(evidence, AnnotationDataset):
        return evidence.import_report.decision_policy
    return evidence.decision_policy


def analysis_status_counts(evidence: KoAnalysisEvidence) -> tuple[StatusCount, ...]:
    """Return exact normalized assignment counts in enum order."""
    if isinstance(evidence, AnnotationDataset):
        counts = {item.status: item.count for item in evidence.import_report.status_counts}
        return tuple(
            StatusCount(status=status, count=counts[status]) for status in NormalizedStatus
        )
    return evidence.status_counts


def analysis_input_rows(evidence: KoAnalysisEvidence) -> int:
    """Return the exact number of non-empty logical source rows read."""
    if isinstance(evidence, AnnotationDataset):
        return evidence.import_report.input_rows
    return evidence.input_rows


def analysis_assignment_count(evidence: KoAnalysisEvidence) -> int:
    """Return the normalized record or expanded-assignment count used by analysis."""
    if isinstance(evidence, AnnotationDataset):
        return len(evidence.records)
    return evidence.expanded_assignments


def analysis_diagnostic_count(evidence: KoAnalysisEvidence) -> int:
    """Return the exact diagnostic count even when only a bounded preview is retained."""
    if isinstance(evidence, AnnotationDataset):
        return len(evidence.import_report.diagnostics)
    return evidence.diagnostic_count


def analysis_diagnostic_preview(
    evidence: KoAnalysisEvidence,
) -> tuple[ImportDiagnostic, ...]:
    """Return at most the first one hundred intake diagnostics."""
    if isinstance(evidence, AnnotationDataset):
        return evidence.import_report.diagnostics[:MAX_PROJECTION_DIAGNOSTIC_PREVIEW]
    return evidence.diagnostic_preview


__all__ = [
    "MAX_PROJECTION_COLUMNS",
    "MAX_PROJECTION_DIAGNOSTIC_PREVIEW",
    "MAX_PROJECTION_EXPANDED_ASSIGNMENTS",
    "MAX_PROJECTION_FIELD_LENGTH",
    "MAX_PROJECTION_INPUT_BYTES",
    "MAX_PROJECTION_INPUT_ROWS",
    "MAX_PROJECTION_UNIQUE_KO_IDS",
    "AnnotationRetention",
    "KoAnalysisEvidence",
    "KoAnalysisProjection",
    "analysis_accepted_ko_ids",
    "analysis_assignment_count",
    "analysis_decision_policy",
    "analysis_diagnostic_count",
    "analysis_diagnostic_preview",
    "analysis_input_rows",
    "analysis_status_counts",
]
