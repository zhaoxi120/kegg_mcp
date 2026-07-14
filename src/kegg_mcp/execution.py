"""Neutral serializable limits and provenance for high-level analysis execution."""

from typing import Literal, Self

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp.domain.annotations import JSON_SCHEMA_DIALECT, FrozenModel
from kegg_mcp.importers.contracts import ImportLimits
from kegg_mcp.kegg.contracts import KeggRequestOptions

ANALYSIS_SERVICE_NAME = "kegg_mcp_plain_ko_analysis"
ANNOTATION_ANALYSIS_SERVICE_NAME = "kegg_mcp_annotation_analysis"
ANALYSIS_SERVICE_VERSION = "1"


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


class AnalysisExecutionProvenance(FrozenModel):
    """Sanitized one-call parameters needed to reproduce a stored analysis run."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:analysis-execution-provenance:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    service_name: Literal[
        "kegg_mcp_plain_ko_analysis",
        "kegg_mcp_annotation_analysis",
    ] = ANALYSIS_SERVICE_NAME
    service_version: Literal["1"] = ANALYSIS_SERVICE_VERSION
    import_limits: ImportLimits
    kegg_request_options: KeggRequestOptions
    reference_loading_limits: ReferenceLoadingLimits
    direct_result_limits: AnalysisServiceLimits


__all__ = [
    "ANALYSIS_SERVICE_NAME",
    "ANALYSIS_SERVICE_VERSION",
    "ANNOTATION_ANALYSIS_SERVICE_NAME",
    "AnalysisExecutionProvenance",
    "AnalysisServiceLimits",
    "ReferenceLoadingLimits",
]
