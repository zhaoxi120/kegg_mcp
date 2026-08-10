"""Immutable contracts for bounded deterministic KO dataset comparison."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from kegg_mcp.analysis.contracts import CalculationMethodReference
from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnalysisUnit,
    AnnotationDataset,
    DecisionPolicyReference,
    FrozenModel,
    KNumber,
    SourceProvenance,
    normalize_identifier_label,
    validate_utf8_text,
)

KO_COMPARISON_METHOD = "deterministic_ko_membership"
KO_COMPARISON_VERSION = "2"

NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]


class ComparisonWarningCode(StrEnum):
    """Stable limitations attached to deterministic set comparisons."""

    DETERMINISTIC_SET_DIFFERENCE_ONLY = "DETERMINISTIC_SET_DIFFERENCE_ONLY"
    ANNOTATION_ABSENCE_NOT_BIOLOGICAL_ABSENCE = "ANNOTATION_ABSENCE_NOT_BIOLOGICAL_ABSENCE"
    ANNOTATION_PIPELINE_MISMATCH = "ANNOTATION_PIPELINE_MISMATCH"
    ANALYSIS_UNIT_MISMATCH = "ANALYSIS_UNIT_MISMATCH"
    TAXONOMIC_CONTEXT_MISMATCH = "TAXONOMIC_CONTEXT_MISMATCH"
    UNKNOWN_OR_MIXED_ANALYSIS_UNIT = "UNKNOWN_OR_MIXED_ANALYSIS_UNIT"
    POOLED_COMMUNITY_POTENTIAL = "POOLED_COMMUNITY_POTENTIAL"
    POOLED_PANGENOME_POTENTIAL = "POOLED_PANGENOME_POTENTIAL"
    MAG_QUALITY_SENSITIVE = "MAG_QUALITY_SENSITIVE"
    MULTI_SAMPLE_DATASET = "MULTI_SAMPLE_DATASET"
    PREVIEW_TRUNCATED = "PREVIEW_TRUNCATED"


class ComparisonLimits(FrozenModel):
    """Hard bounds applied before constructing KO membership partitions."""

    max_sets: int = Field(default=20, strict=True, ge=2, le=100)
    max_records_per_set: int = Field(default=100_000, strict=True, gt=0, le=10_000_000)
    max_total_records: int = Field(default=500_000, strict=True, gt=0, le=50_000_000)
    max_unique_kos_per_set: int = Field(default=50_000, strict=True, gt=0, le=1_000_000)
    max_total_membership_entries: int = Field(
        default=200_000,
        strict=True,
        gt=0,
        le=10_000_000,
    )
    max_sources_per_set: int = Field(default=100, strict=True, gt=0, le=100)
    max_sample_labels_per_set: int = Field(default=1_000, strict=True, gt=0, le=10_000)


class ComparisonPreviewLimits(FrozenModel):
    """Output-only bounds used to summarize a complete bounded comparison detail."""

    max_ko_ids: int = Field(default=100, strict=True, gt=0, le=10_000)
    max_membership_patterns: int = Field(default=256, strict=True, gt=0, le=10_000)


class ComparisonDatasetInput(FrozenModel):
    """One caller-labelled dataset retained in comparison input order."""

    label: str = Field(min_length=1, max_length=128)
    dataset: AnnotationDataset

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        return normalize_identifier_label(value, field_name="comparison label")


class ComparisonDatasetProvenance(FrozenModel):
    """Serializable evidence and biological context for one compared dataset."""

    input_index: NonNegativeCount
    label: str = Field(min_length=1, max_length=128)
    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    decision_policy: DecisionPolicyReference
    analysis_unit: AnalysisUnit
    taxon_id: PositiveCount | None
    kegg_organism_code: str | None = Field(pattern=r"^[a-z][a-z0-9]{1,7}$")
    sample_labels: Annotated[tuple[str, ...], Field(max_length=10_000)]
    sources: Annotated[
        tuple[SourceProvenance, ...],
        Field(min_length=1, max_length=100),
    ]
    record_count: NonNegativeCount
    selected_unique_ko_count: NonNegativeCount

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        return normalize_identifier_label(value, field_name="comparison label")

    @field_validator("sample_labels")
    @classmethod
    def validate_sample_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("sample labels must be unique in first-seen order")
        for label in value:
            normalize_identifier_label(label, field_name="sample label")
        return value


class DatasetSpecificKoSet(FrozenModel):
    """Complete bounded KO set found in exactly one comparison input."""

    input_index: NonNegativeCount
    label: str = Field(min_length=1, max_length=128)
    ko_ids: tuple[KNumber, ...]

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.ko_ids != tuple(sorted(set(self.ko_ids))):
            raise ValueError("set-specific K numbers must be sorted and unique")
        return self


class KoMembershipPattern(FrozenModel):
    """Complete bounded KO set shared by a proper subset of comparison inputs."""

    member_set_indexes: Annotated[tuple[NonNegativeCount, ...], Field(min_length=2)]
    member_labels: Annotated[tuple[str, ...], Field(min_length=2)]
    ko_ids: Annotated[tuple[KNumber, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_pattern(self) -> Self:
        if self.member_set_indexes != tuple(sorted(set(self.member_set_indexes))):
            raise ValueError("membership indexes must be sorted and unique")
        if len(self.member_labels) != len(self.member_set_indexes):
            raise ValueError("membership labels must align with membership indexes")
        if len(self.member_labels) != len(set(self.member_labels)):
            raise ValueError("membership labels must be unique")
        if self.ko_ids != tuple(sorted(set(self.ko_ids))):
            raise ValueError("membership K numbers must be sorted and unique")
        return self


class KoMembershipPartition(FrozenModel):
    """Lossless bounded membership partition for selected unique K numbers."""

    union_count: NonNegativeCount
    shared_by_all: tuple[KNumber, ...]
    set_specific: Annotated[tuple[DatasetSpecificKoSet, ...], Field(min_length=2)]
    partially_shared: tuple[KoMembershipPattern, ...]

    @model_validator(mode="after")
    def validate_partition(self) -> Self:
        if self.shared_by_all != tuple(sorted(set(self.shared_by_all))):
            raise ValueError("shared K numbers must be sorted and unique")
        indexes = tuple(item.input_index for item in self.set_specific)
        labels = tuple(item.label for item in self.set_specific)
        if indexes != tuple(range(len(self.set_specific))):
            raise ValueError("set-specific entries must retain contiguous input order")
        if len(labels) != len(set(labels)):
            raise ValueError("set-specific labels must be unique")
        pattern_keys = tuple(pattern.member_set_indexes for pattern in self.partially_shared)
        if pattern_keys != tuple(sorted(set(pattern_keys))):
            raise ValueError("partial membership patterns must use deterministic order")
        input_count = len(self.set_specific)
        if any(
            len(pattern.member_set_indexes) >= input_count
            or pattern.member_set_indexes[-1] >= input_count
            for pattern in self.partially_shared
        ):
            raise ValueError("partial membership must identify a proper subset of inputs")
        for pattern in self.partially_shared:
            expected_labels = tuple(labels[index] for index in pattern.member_set_indexes)
            if pattern.member_labels != expected_labels:
                raise ValueError("partial membership labels must match input indexes")
        groups = [set(self.shared_by_all)]
        groups.extend(set(item.ko_ids) for item in self.set_specific)
        groups.extend(set(pattern.ko_ids) for pattern in self.partially_shared)
        union: set[str] = set()
        for group in groups:
            if union & group:
                raise ValueError("KO membership partition groups must not overlap")
            union.update(group)
        if len(union) != self.union_count:
            raise ValueError("union_count must match the complete membership partition")
        return self


class ComparisonWarning(FrozenModel):
    """One machine-readable interpretation boundary for a comparison."""

    code: ComparisonWarningCode
    message: str = Field(min_length=1, max_length=1_000)
    affected_input_indexes: tuple[NonNegativeCount, ...] = ()

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="comparison warning message")

    @model_validator(mode="after")
    def validate_indexes(self) -> Self:
        if self.affected_input_indexes != tuple(sorted(set(self.affected_input_indexes))):
            raise ValueError("warning input indexes must be sorted and unique")
        return self


class KoSetComparisonDetail(FrozenModel):
    """Complete deterministic partition within explicitly serialized hard limits."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:ko-set-comparison-detail:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    datasets: Annotated[tuple[ComparisonDatasetProvenance, ...], Field(min_length=2)]
    partition: KoMembershipPartition
    calculation_method: CalculationMethodReference
    warnings: tuple[ComparisonWarning, ...]
    limits: ComparisonLimits

    @model_validator(mode="after")
    def validate_detail(self) -> Self:
        indexes = tuple(item.input_index for item in self.datasets)
        labels = tuple(item.label for item in self.datasets)
        if indexes != tuple(range(len(self.datasets))):
            raise ValueError("comparison datasets must retain contiguous input order")
        if len(labels) != len(set(labels)):
            raise ValueError("comparison dataset labels must be unique")
        partition_labels = tuple(item.label for item in self.partition.set_specific)
        if partition_labels != labels:
            raise ValueError("partition labels must match comparison dataset order")
        if len(self.datasets) > self.limits.max_sets:
            raise ValueError("comparison datasets exceed the recorded set limit")
        total_records = sum(item.record_count for item in self.datasets)
        if total_records > self.limits.max_total_records:
            raise ValueError("comparison records exceed the recorded total limit")
        for dataset in self.datasets:
            if dataset.record_count > self.limits.max_records_per_set:
                raise ValueError("one comparison dataset exceeds the recorded record limit")
            if dataset.selected_unique_ko_count > self.limits.max_unique_kos_per_set:
                raise ValueError("one comparison dataset exceeds the recorded unique-KO limit")
            if len(dataset.sources) > self.limits.max_sources_per_set:
                raise ValueError("one comparison dataset exceeds the recorded source limit")
            if len(dataset.sample_labels) > self.limits.max_sample_labels_per_set:
                raise ValueError("one comparison dataset exceeds the recorded sample-label limit")
        input_count = len(self.datasets)
        membership_entries = len(self.partition.shared_by_all) * input_count
        membership_entries += sum(len(item.ko_ids) for item in self.partition.set_specific)
        membership_entries += sum(
            len(pattern.ko_ids) * len(pattern.member_set_indexes)
            for pattern in self.partition.partially_shared
        )
        if membership_entries > self.limits.max_total_membership_entries:
            raise ValueError("KO memberships exceed the recorded comparison limit")
        if self.calculation_method != CalculationMethodReference(
            name=KO_COMPARISON_METHOD,
            version=KO_COMPARISON_VERSION,
        ):
            raise ValueError("calculation_method is incompatible with this result schema")
        warning_codes = tuple(item.code for item in self.warnings)
        if len(warning_codes) != len(set(warning_codes)):
            raise ValueError("comparison warning codes must be unique")
        return self


class KoPreview(FrozenModel):
    """Exact count plus a bounded lexical preview of one KO set."""

    count: NonNegativeCount
    ko_ids: tuple[KNumber, ...]
    truncated: bool

    @model_validator(mode="after")
    def validate_preview(self) -> Self:
        if self.ko_ids != tuple(sorted(set(self.ko_ids))):
            raise ValueError("KO previews must be sorted and unique")
        if len(self.ko_ids) > self.count:
            raise ValueError("KO preview length cannot exceed its exact count")
        if self.truncated != (len(self.ko_ids) < self.count):
            raise ValueError("KO preview truncation must match its exact count")
        return self


class DatasetSpecificKoPreview(FrozenModel):
    """Bounded preview for one exact set-specific KO count."""

    input_index: NonNegativeCount
    label: str = Field(min_length=1, max_length=128)
    ko_set: KoPreview


class KoMembershipPatternPreview(FrozenModel):
    """Bounded KO preview for one complete partial-membership pattern."""

    member_set_indexes: Annotated[tuple[NonNegativeCount, ...], Field(min_length=2)]
    member_labels: Annotated[tuple[str, ...], Field(min_length=2)]
    ko_set: KoPreview


class KoMembershipComparisonSummary(FrozenModel):
    """Exact partition counts with bounded KO and membership-pattern previews."""

    union_count: NonNegativeCount
    shared_by_all: KoPreview
    set_specific: Annotated[tuple[DatasetSpecificKoPreview, ...], Field(min_length=2)]
    partially_shared_pattern_count: NonNegativeCount
    partially_shared_patterns_preview: tuple[KoMembershipPatternPreview, ...]
    partially_shared_patterns_truncated: bool

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.shared_by_all.count > self.union_count:
            raise ValueError("shared KO count cannot exceed union_count")
        if len(self.partially_shared_patterns_preview) > self.partially_shared_pattern_count:
            raise ValueError("membership preview count cannot exceed its exact count")
        expected_truncated = (
            len(self.partially_shared_patterns_preview) < self.partially_shared_pattern_count
        )
        if self.partially_shared_patterns_truncated != expected_truncated:
            raise ValueError("membership-pattern truncation must match its exact count")
        return self


class KoSetComparisonSummary(FrozenModel):
    """Default bounded summary suitable for later service and MCP presentation."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:ko-set-comparison-summary:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    datasets: Annotated[tuple[ComparisonDatasetProvenance, ...], Field(min_length=2)]
    partition: KoMembershipComparisonSummary
    calculation_method: CalculationMethodReference
    warnings: tuple[ComparisonWarning, ...]
    detail_limits: ComparisonLimits
    preview_limits: ComparisonPreviewLimits

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        previews = [self.partition.shared_by_all]
        previews.extend(item.ko_set for item in self.partition.set_specific)
        previews.extend(item.ko_set for item in self.partition.partially_shared_patterns_preview)
        if any(len(preview.ko_ids) > self.preview_limits.max_ko_ids for preview in previews):
            raise ValueError("KO previews exceed the recorded output limit")
        if (
            len(self.partition.partially_shared_patterns_preview)
            > self.preview_limits.max_membership_patterns
        ):
            raise ValueError("membership-pattern previews exceed the recorded output limit")
        if self.calculation_method != CalculationMethodReference(
            name=KO_COMPARISON_METHOD,
            version=KO_COMPARISON_VERSION,
        ):
            raise ValueError("calculation_method is incompatible with this summary schema")
        warning_codes = tuple(item.code for item in self.warnings)
        if len(warning_codes) != len(set(warning_codes)):
            raise ValueError("comparison summary warning codes must be unique")
        return self


__all__ = [
    "KO_COMPARISON_METHOD",
    "KO_COMPARISON_VERSION",
    "ComparisonDatasetInput",
    "ComparisonDatasetProvenance",
    "ComparisonLimits",
    "ComparisonPreviewLimits",
    "ComparisonWarning",
    "ComparisonWarningCode",
    "DatasetSpecificKoPreview",
    "DatasetSpecificKoSet",
    "KoMembershipComparisonSummary",
    "KoMembershipPartition",
    "KoMembershipPattern",
    "KoMembershipPatternPreview",
    "KoPreview",
    "KoSetComparisonDetail",
    "KoSetComparisonSummary",
]
