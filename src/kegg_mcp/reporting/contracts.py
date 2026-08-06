"""Immutable contracts for deterministic in-memory report artifacts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from kegg_mcp.analysis.comparison_contracts import KoSetComparisonSummary
from kegg_mcp.analysis.contracts import PairedModuleEvaluation
from kegg_mcp.analysis.functional_comparison import (
    ModuleComparisonResult,
    PathwayComparisonResult,
)
from kegg_mcp.analysis.pathway_coverage import PathwayCoverageResult
from kegg_mcp.analysis.pathway_ranking import PathwayRankingRow, PathwaySelection
from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnnotationDataset,
    FrozenModel,
    validate_utf8_text,
)
from kegg_mcp.execution import (
    AnalysisExecutionProvenance,
    ExecutionStage,
    StageMetric,
)
from kegg_mcp.kegg.contracts import KeggBatchProvenance
from kegg_mcp.report_limits import ReportLimits

REPORT_FORMAT_NAME = "kegg_mcp_analysis_report"
REPORT_FORMAT_VERSION = "3"
REPORT_RENDERER_NAME = "kegg_mcp_reporting"
REPORT_RENDERER_VERSION = "2"

NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class ReportSection(StrEnum):
    """Stable logical names for the three MVP report artifacts."""

    STRUCTURED = "structured"
    SUMMARY = "summary"
    ANNOTATIONS = "annotations"


class ReportInput(FrozenModel):
    """One dataset and optional bounded M4 analyses supplied in caller order."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:report-input:3",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    dataset: AnnotationDataset
    execution: AnalysisExecutionProvenance | None = None
    execution_metrics: Annotated[tuple[StageMetric, ...], Field(max_length=6)] = ()
    mapping_provenance: Annotated[tuple[KeggBatchProvenance, ...], Field(max_length=100)] = ()
    module_evaluations: tuple[PairedModuleEvaluation, ...] = ()
    pathway_coverages: tuple[PathwayCoverageResult, ...] = ()
    pathway_selection: PathwaySelection | None = None
    pathway_ranking: tuple[PathwayRankingRow, ...] = ()
    ko_comparison: KoSetComparisonSummary | None = None
    module_comparison: ModuleComparisonResult | None = None
    pathway_comparison: PathwayComparisonResult | None = None

    @model_validator(mode="after")
    def validate_primary_dataset_analyses(self) -> Self:
        if self.execution_metrics and tuple(item.stage for item in self.execution_metrics) != tuple(
            ExecutionStage
        ):
            raise ValueError("execution_metrics must use the canonical six-stage order")
        module_ids = tuple(item.strict.module_id for item in self.module_evaluations)
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("module_evaluations must contain unique MODULE targets")
        pathway_keys = tuple(
            (item.pathway_id, item.evidence_mode) for item in self.pathway_coverages
        )
        if len(pathway_keys) != len(set(pathway_keys)):
            raise ValueError(
                "pathway_coverages must contain unique pathway and evidence-mode targets"
            )
        if any(
            item.strict.dataset_id != self.dataset.dataset_id for item in self.module_evaluations
        ):
            raise ValueError("module evaluations must identify the primary report dataset")
        if any(item.dataset_id != self.dataset.dataset_id for item in self.pathway_coverages):
            raise ValueError("pathway coverage results must identify the primary report dataset")
        if self.pathway_selection is None and self.pathway_ranking:
            raise ValueError("pathway ranking rows require a recorded selection")
        if self.pathway_ranking:
            ranks = tuple(item.rank for item in self.pathway_ranking)
            if ranks != tuple(range(1, len(self.pathway_ranking) + 1)):
                raise ValueError("pathway ranking rows must retain contiguous rank order")
        return self


class StructuredReport(FrozenModel):
    """Canonical complete storage payload before JSON encoding."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:structured-report:3",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    format_name: Literal["kegg_mcp_analysis_report"]
    format_version: Literal["3"]
    renderer_name: Literal["kegg_mcp_reporting"]
    renderer_version: Literal["2"]
    limits: ReportLimits
    report: ReportInput


class ReportArtifact(FrozenModel):
    """One bounded UTF-8 report artifact held entirely in memory."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:report-artifact:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    section: ReportSection
    mime_type: Literal["application/json", "text/markdown", "text/csv"]
    utf8_byte_size: NonNegativeCount
    content: str
    truncated: bool

    @field_validator("content")
    @classmethod
    def validate_content(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="report artifact content")

    @model_validator(mode="after")
    def validate_content_metadata(self) -> Self:
        expected_mime_types = {
            ReportSection.STRUCTURED: "application/json",
            ReportSection.SUMMARY: "text/markdown",
            ReportSection.ANNOTATIONS: "text/csv",
        }
        if self.mime_type != expected_mime_types[self.section]:
            raise ValueError("artifact MIME type is incompatible with its logical section")
        encoded = self.content.encode("utf-8")
        if self.utf8_byte_size != len(encoded):
            raise ValueError("utf8_byte_size must equal the encoded content length")
        if self.section is not ReportSection.SUMMARY and self.truncated:
            raise ValueError("complete structured and annotation artifacts cannot be truncated")
        return self


class RenderedReport(FrozenModel):
    """Complete deterministic artifact bundle returned by the pure renderer."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:rendered-report:2",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    renderer_name: Literal["kegg_mcp_reporting"]
    renderer_version: Literal["2"]
    limits: ReportLimits
    artifacts: Annotated[tuple[ReportArtifact, ...], Field(min_length=3, max_length=3)]

    @model_validator(mode="after")
    def validate_bundle(self) -> Self:
        if tuple(item.section for item in self.artifacts) != tuple(ReportSection):
            raise ValueError("artifacts must use canonical structured, summary, annotation order")
        size_limits = {
            ReportSection.STRUCTURED: self.limits.max_structured_json_bytes,
            ReportSection.SUMMARY: self.limits.max_markdown_bytes,
            ReportSection.ANNOTATIONS: self.limits.max_annotation_csv_bytes,
        }
        if any(item.utf8_byte_size > size_limits[item.section] for item in self.artifacts):
            raise ValueError("artifact content exceeds its serialized report limit")
        return self


__all__ = [
    "REPORT_FORMAT_NAME",
    "REPORT_FORMAT_VERSION",
    "REPORT_RENDERER_NAME",
    "REPORT_RENDERER_VERSION",
    "AnalysisExecutionProvenance",
    "RenderedReport",
    "ReportArtifact",
    "ReportInput",
    "ReportLimits",
    "ReportSection",
    "StructuredReport",
]
