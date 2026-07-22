"""Small serializable previews shared by analysis services."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import Field, model_validator

from kegg_mcp.analysis.contracts import ModuleEvaluationStatus
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageStatus,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
)
from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    EvidenceMode,
    FrozenModel,
    NormalizedStatus,
    StatusCount,
)

ModuleId = Annotated[str, Field(pattern=r"^M[0-9]{5}$")]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class ImportSummary(FrozenModel):
    """Concise normalization summary without caller-supplied raw rows."""

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    analysis_unit: AnalysisUnit
    input_rows: NonNegativeCount
    emitted_records: NonNegativeCount
    skipped_rows: NonNegativeCount
    duplicate_count: NonNegativeCount
    conflict_count: NonNegativeCount
    status_counts: Annotated[
        tuple[StatusCount, ...],
        Field(min_length=len(NormalizedStatus), max_length=len(NormalizedStatus)),
    ]

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        statuses = tuple(item.status for item in self.status_counts)
        if len(statuses) != len(set(statuses)) or set(statuses) != set(NormalizedStatus):
            raise ValueError("status_counts must identify every normalized status exactly once")
        if sum(item.count for item in self.status_counts) != self.emitted_records:
            raise ValueError("status_counts must sum to emitted_records")
        parsed_rows = self.input_rows - self.skipped_rows
        if parsed_rows < 0:
            raise ValueError("skipped_rows must not exceed input_rows")
        if parsed_rows == 0 and self.emitted_records != 0:
            raise ValueError("emitted_records require at least one parsed input row")
        if parsed_rows > 0 and self.emitted_records < parsed_rows:
            raise ValueError("each parsed input row must emit at least one record")
        return self


class ModuleAnalysisPreview(FrozenModel):
    """Small exact-completion and block-coverage preview for one MODULE target."""

    module_id: ModuleId
    module_name: str | None = Field(default=None, max_length=1_000)
    strict_status: ModuleEvaluationStatus
    strict_is_complete: bool | None
    strict_block_coverage: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    lenient_status: ModuleEvaluationStatus
    lenient_is_complete: bool | None
    lenient_block_coverage: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    strict_to_lenient_changed: bool


class PathwayAnalysisPreview(FrozenModel):
    """Small descriptive unique-KO coverage preview for one pathway target."""

    pathway_id: str = Field(min_length=7, max_length=9)
    pathway_name: str = Field(min_length=1, max_length=1_000)
    reference_namespace: PathwayReferenceNamespace
    reference_scope: PathwayReferenceScope
    evidence_mode: EvidenceMode
    evaluation_status: PathwayCoverageStatus
    detected_unique_ko_count: NonNegativeCount
    reference_unique_ko_count: NonNegativeCount
    coverage_ratio: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    warning_codes: Annotated[
        tuple[Annotated[str, Field(max_length=100)], ...], Field(max_length=16)
    ]


__all__ = [
    "ImportSummary",
    "ModuleAnalysisPreview",
    "PathwayAnalysisPreview",
]
