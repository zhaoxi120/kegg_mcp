"""Serializable contracts for the high-level plain-KO analysis service."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from kegg_mcp.analysis.contracts import ModuleAnalysisLimits, ModuleEvaluationStatus
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageLimits,
    PathwayCoverageStatus,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
)
from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnalysisUnit,
    EvidenceField,
    EvidenceMode,
    FrozenModel,
    NormalizedStatus,
    StatusCount,
    normalize_identifier_label,
    validate_utf8_text,
)
from kegg_mcp.execution import AnalysisServiceLimits
from kegg_mcp.importers.contracts import ImportLimits, SourceProvenanceInput
from kegg_mcp.kegg.contracts import KeggRequestOptions
from kegg_mcp.reporting.contracts import ReportLimits
from kegg_mcp.services.reference_loading import PathwaySpec, ReferenceLoadingLimits
from kegg_mcp.services.result_store import ResultArtifactMetadata, ResultMetadata

ModuleId = Annotated[str, Field(pattern=r"^M[0-9]{5}$")]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class PlainKoAnalysisRequest(FrozenModel):
    """One bounded plain-KO import and its requested KEGG analysis targets."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:plain-ko-analysis-request:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    ko_text: str
    import_limits: ImportLimits
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN
    sample_id: str = Field(default="sample-1", min_length=1, max_length=256)
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9]{1,7}$",
    )
    metadata: Annotated[tuple[EvidenceField, ...], Field(max_length=128)] = ()
    source: SourceProvenanceInput | None = None
    module_ids: Annotated[tuple[ModuleId, ...], Field(max_length=1_000)] = ()
    pathways: Annotated[tuple[PathwaySpec, ...], Field(max_length=1_000)] = ()
    pathway_evidence_mode: EvidenceMode = EvidenceMode.STRICT
    allow_global_or_overview: bool = False
    kegg_options: KeggRequestOptions = Field(default_factory=KeggRequestOptions)
    reference_limits: ReferenceLoadingLimits = Field(default_factory=ReferenceLoadingLimits)
    module_limits: ModuleAnalysisLimits = Field(default_factory=ModuleAnalysisLimits)
    pathway_limits: PathwayCoverageLimits = Field(default_factory=PathwayCoverageLimits)
    report_limits: ReportLimits = Field(default_factory=ReportLimits)

    @field_validator("ko_text")
    @classmethod
    def validate_ko_text(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="ko_text")

    @field_validator("sample_id")
    @classmethod
    def normalize_sample_id(cls, value: str) -> str:
        return normalize_identifier_label(value, field_name="sample_id")

    @model_validator(mode="after")
    def validate_targets_and_limits(self) -> Self:
        if not self.module_ids and not self.pathways:
            raise ValueError("at least one MODULE or pathway target is required")
        if len(self.module_ids) != len(set(self.module_ids)):
            raise ValueError("module_ids must contain unique targets in caller order")
        pathway_ids = tuple(item.pathway_id for item in self.pathways)
        if len(pathway_ids) != len(set(pathway_ids)):
            raise ValueError("pathways must contain unique identifiers in caller order")
        if any(
            item.reference_namespace is PathwayReferenceNamespace.ORGANISM for item in self.pathways
        ):
            raise ValueError("plain KO input cannot request an organism-specific pathway reference")
        if len(self.module_ids) > self.reference_limits.max_module_roots:
            raise ValueError("module targets exceed reference_limits.max_module_roots")
        if len(self.pathways) > self.reference_limits.max_pathway_specs:
            raise ValueError("pathway targets exceed reference_limits.max_pathway_specs")
        if len(self.module_ids) > self.report_limits.max_module_targets:
            raise ValueError("module targets exceed report_limits.max_module_targets")
        if len(self.pathways) > self.report_limits.max_pathway_targets:
            raise ValueError("pathway targets exceed report_limits.max_pathway_targets")
        if len(self.module_ids) + len(self.pathways) > self.report_limits.max_total_targets:
            raise ValueError("analysis targets exceed report_limits.max_total_targets")
        return self


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
        if self.input_rows != self.emitted_records + self.skipped_rows:
            raise ValueError("input_rows must equal emitted_records plus skipped_rows")
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


class PlainKoAnalysisResult(FrozenModel):
    """Bounded direct response with full artifacts retained in the scoped store."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:plain-ko-analysis-result:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    result: ResultMetadata
    artifacts: Annotated[
        tuple[ResultArtifactMetadata, ...],
        Field(min_length=3, max_length=3),
    ]
    import_summary: ImportSummary
    module_target_count: NonNegativeCount
    module_previews: tuple[ModuleAnalysisPreview, ...]
    module_previews_truncated: bool
    pathway_target_count: NonNegativeCount
    pathway_previews: tuple[PathwayAnalysisPreview, ...]
    pathway_previews_truncated: bool
    caveats: Annotated[tuple[str, ...], Field(min_length=1, max_length=8)]
    limits: AnalysisServiceLimits

    @model_validator(mode="after")
    def validate_previews(self) -> Self:
        if self.result.artifact_count != len(self.artifacts):
            raise ValueError("result artifact_count must match artifact metadata")
        if len(self.module_previews) > min(
            self.module_target_count,
            self.limits.max_module_previews,
        ):
            raise ValueError("module previews exceed their configured bound")
        if self.module_previews_truncated != (self.module_target_count > len(self.module_previews)):
            raise ValueError("module_previews_truncated is inconsistent with target count")
        if len(self.pathway_previews) > min(
            self.pathway_target_count,
            self.limits.max_pathway_previews,
        ):
            raise ValueError("pathway previews exceed their configured bound")
        if self.pathway_previews_truncated != (
            self.pathway_target_count > len(self.pathway_previews)
        ):
            raise ValueError("pathway_previews_truncated is inconsistent with target count")
        return self


__all__ = [
    "AnalysisServiceLimits",
    "ImportSummary",
    "ModuleAnalysisPreview",
    "PathwayAnalysisPreview",
    "PlainKoAnalysisRequest",
    "PlainKoAnalysisResult",
]
