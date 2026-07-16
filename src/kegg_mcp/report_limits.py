"""Serializable hard bounds for deterministic report rendering."""

from pydantic import Field

from kegg_mcp.domain.annotations import FrozenModel


class ReportLimits(FrozenModel):
    """Serializable hard bounds and Markdown preview limits for one render."""

    max_input_rows: int = Field(default=100_000, strict=True, gt=0, le=10_000_000)
    max_annotation_records: int = Field(
        default=100_000,
        strict=True,
        ge=0,
        le=10_000_000,
    )
    max_source_entries: int = Field(default=10_000, strict=True, gt=0, le=1_000_000)
    max_module_targets: int = Field(default=1_000, strict=True, ge=0, le=100_000)
    max_pathway_targets: int = Field(default=1_000, strict=True, ge=0, le=100_000)
    max_total_targets: int = Field(default=2_000, strict=True, ge=0, le=200_000)
    max_warning_entries: int = Field(default=10_000, strict=True, ge=0, le=1_000_000)
    max_structured_json_bytes: int = Field(
        default=64 * 1024 * 1024,
        strict=True,
        gt=0,
        le=512 * 1024 * 1024,
    )
    max_markdown_bytes: int = Field(
        default=64 * 1024,
        strict=True,
        ge=1_024,
        le=16 * 1024 * 1024,
    )
    max_annotation_csv_bytes: int = Field(
        default=128 * 1024 * 1024,
        strict=True,
        gt=0,
        le=1024 * 1024 * 1024,
    )
    max_markdown_sources: int = Field(default=10, strict=True, ge=0, le=10_000)
    max_markdown_module_targets: int = Field(default=25, strict=True, ge=0, le=10_000)
    max_markdown_pathway_targets: int = Field(default=25, strict=True, ge=0, le=10_000)
    max_markdown_comparison_targets: int = Field(
        default=25,
        strict=True,
        ge=0,
        le=10_000,
    )
    max_markdown_warnings: int = Field(default=20, strict=True, ge=0, le=10_000)


__all__ = ["ReportLimits"]
