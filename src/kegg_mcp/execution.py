"""Neutral serializable limits, metrics, and provenance for analysis execution."""

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp.analysis.contracts import ModuleAnalysisLimits
from kegg_mcp.analysis.pathway_coverage import PathwayCoverageLimits
from kegg_mcp.analysis.pathway_ranking import (
    MODULE_RANKING_METHOD,
    MODULE_RANKING_VERSION,
    PATHWAY_RANKING_METHOD,
    PATHWAY_RANKING_VERSION,
    ModuleSelection,
    PathwaySelection,
)
from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    DecisionPolicyReference,
    EvidenceMode,
    FrozenModel,
)
from kegg_mcp.importers.contracts import ImportLimits
from kegg_mcp.kegg.contracts import KeggRequestOptions
from kegg_mcp.report_limits import ReportLimits

ANALYSIS_SERVICE_NAME = "kegg_mcp_annotation_analysis"
ANALYSIS_SERVICE_VERSION = "3"


class ExecutionStage(StrEnum):
    """Stable high-level stages used by compact performance summaries."""

    ANNOTATION_IMPORT = "annotation_import"
    KO_TARGET_MAPPING = "ko_target_mapping"
    TARGET_RANKING = "target_ranking"
    REFERENCE_LOADING = "reference_loading"
    ANALYSIS = "analysis"
    BUNDLE_WRITE = "bundle_write"


class StageMetric(FrozenModel):
    """Sanitized integer timing, request, cache, and byte counts for one stage."""

    stage: ExecutionStage
    elapsed_ms: int = Field(strict=True, ge=0)
    request_count: int = Field(default=0, strict=True, ge=0)
    network_request_count: int = Field(default=0, strict=True, ge=0)
    cache_hit_count: int = Field(default=0, strict=True, ge=0)
    response_bytes: int = Field(default=0, strict=True, ge=0)


class PathwayRankingExecution(FrozenModel):
    """Compact, reproducible provenance for one automatic pathway selection."""

    method: Literal["selected_unique_ko_count"] = PATHWAY_RANKING_METHOD
    method_version: Literal["1"] = PATHWAY_RANKING_VERSION
    selection: PathwaySelection
    evidence_mode: EvidenceMode
    decision_policy: DecisionPolicyReference
    selected_unique_ko_count: int = Field(strict=True, gt=0)
    candidate_pathway_count: int = Field(strict=True, gt=0)
    selected_pathway_ids: Annotated[
        tuple[Annotated[str, Field(pattern=r"^ko[0-9]{5}$")], ...],
        Field(min_length=1, max_length=25),
    ]
    mapping_request_count: int = Field(strict=True, gt=0)
    mapping_network_request_count: int = Field(strict=True, ge=0)
    mapping_cache_hit_count: int = Field(strict=True, ge=0)
    mapping_response_bytes: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_ranking_summary(self) -> Self:
        if len(self.selected_pathway_ids) > self.selection.top_n:
            raise ValueError("selected pathway identifiers exceed the requested top_n")
        if self.candidate_pathway_count < len(self.selected_pathway_ids):
            raise ValueError("candidate pathway count cannot be smaller than selected targets")
        if self.mapping_cache_hit_count > self.mapping_request_count:
            raise ValueError("mapping cache hits cannot exceed logical requests")
        return self


class ModuleRankingExecution(FrozenModel):
    """Compact, reproducible provenance for one automatic MODULE selection."""

    method: Literal["selected_unique_ko_count"] = MODULE_RANKING_METHOD
    method_version: Literal["1"] = MODULE_RANKING_VERSION
    selection: ModuleSelection
    evidence_mode: EvidenceMode
    decision_policy: DecisionPolicyReference
    selected_unique_ko_count: int = Field(strict=True, gt=0)
    candidate_module_count: int = Field(strict=True, ge=0)
    selected_module_ids: Annotated[
        tuple[Annotated[str, Field(pattern=r"^M[0-9]{5}$")], ...],
        Field(max_length=25),
    ]
    mapping_request_count: int = Field(strict=True, gt=0)
    mapping_network_request_count: int = Field(strict=True, ge=0)
    mapping_cache_hit_count: int = Field(strict=True, ge=0)
    mapping_response_bytes: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_ranking_summary(self) -> Self:
        if len(self.selected_module_ids) > self.selection.top_n:
            raise ValueError("selected MODULE identifiers exceed the requested top_n")
        if self.candidate_module_count < len(self.selected_module_ids):
            raise ValueError("candidate MODULE count cannot be smaller than selected targets")
        if self.mapping_cache_hit_count > self.mapping_request_count:
            raise ValueError("mapping cache hits cannot exceed logical requests")
        return self


class ReferenceLoadingLimits(FrozenModel):
    """Strict request, traversal, retrieval, and aggregate reference bounds."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:reference-loading-limits:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    max_module_roots: int = Field(default=100, strict=True, gt=0, le=1_000)
    max_pathway_specs: int = Field(default=25, strict=True, gt=0, le=1_000)
    max_module_rounds: int = Field(default=16, strict=True, gt=0, le=256)
    max_module_entries: int = Field(default=256, strict=True, gt=0, le=1_000)
    max_module_reference_occurrences: int = Field(
        default=2_048,
        strict=True,
        gt=0,
        le=100_000,
    )
    max_total_response_bytes: int = Field(
        default=50_000_000,
        strict=True,
        gt=0,
        le=500_000_000,
    )
    max_total_kegg_requests: int = Field(default=100, strict=True, gt=0, le=5_000)
    max_total_pathway_relationship_rows: int = Field(
        default=500_000,
        strict=True,
        gt=0,
        le=10_000_000,
    )
    max_total_pathway_reference_kos: int = Field(
        default=250_000,
        strict=True,
        gt=0,
        le=5_000_000,
    )
    max_total_pathway_reference_exclusions: int = Field(
        default=25_000,
        strict=True,
        gt=0,
        le=1_000_000,
    )

    @model_validator(mode="after")
    def require_compatible_module_bounds(self) -> Self:
        if self.max_module_roots > self.max_module_entries:
            raise ValueError("max_module_roots must not exceed max_module_entries")
        return self


class AnalysisServiceLimits(FrozenModel):
    """Hard bounds for the concise result returned directly to a caller."""

    max_module_previews: int = Field(default=10, strict=True, ge=0, le=1_000)
    max_pathway_previews: int = Field(default=10, strict=True, ge=0, le=1_000)


class PathwayExecutionParameters(FrozenModel):
    """Pathway semantics and optional deterministic target-ranking provenance."""

    evidence_mode: EvidenceMode = EvidenceMode.STRICT
    allow_global_or_overview: bool = False
    ranking: PathwayRankingExecution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


class AnalysisExecutionProvenance(FrozenModel):
    """Sanitized one-call parameters needed to reproduce a stored analysis run."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:analysis-execution-provenance:3",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    service_name: Literal["kegg_mcp_annotation_analysis"] = ANALYSIS_SERVICE_NAME
    service_version: Literal["3"] = ANALYSIS_SERVICE_VERSION
    import_limits: ImportLimits
    kegg_request_options: KeggRequestOptions
    reference_loading_limits: ReferenceLoadingLimits
    module_analysis_limits: ModuleAnalysisLimits = Field(default_factory=ModuleAnalysisLimits)
    module_ranking: ModuleRankingExecution | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    pathway_parameters: PathwayExecutionParameters = Field(
        default_factory=PathwayExecutionParameters
    )
    pathway_coverage_limits: PathwayCoverageLimits = Field(default_factory=PathwayCoverageLimits)
    report_limits: ReportLimits = Field(default_factory=ReportLimits)
    direct_result_limits: AnalysisServiceLimits


__all__ = [
    "ANALYSIS_SERVICE_NAME",
    "ANALYSIS_SERVICE_VERSION",
    "AnalysisExecutionProvenance",
    "AnalysisServiceLimits",
    "ExecutionStage",
    "ModuleRankingExecution",
    "PathwayExecutionParameters",
    "PathwayRankingExecution",
    "ReferenceLoadingLimits",
    "StageMetric",
]
