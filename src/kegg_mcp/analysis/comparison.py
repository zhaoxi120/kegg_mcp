"""Bounded deterministic comparison of immutable KO annotation datasets."""

from __future__ import annotations

import hashlib
import json
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
    build_ko_evidence_view,
    normalize_identifier_label,
    validate_utf8_text,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail

KO_COMPARISON_METHOD = "deterministic_ko_membership"
KO_COMPARISON_VERSION = "1"

NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ComparedKoClass(StrEnum):
    """Status-derived KO sets partitioned independently during comparison."""

    ACCEPTED = "accepted"
    UNCERTAIN_RECORD = "uncertain_record"
    LENIENT_ADDITIONAL = "lenient_additional"
    LENIENT = "lenient"


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
    dataset_sha256: Sha256
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
    accepted_ko_count: NonNegativeCount
    uncertain_record_ko_count: NonNegativeCount
    lenient_additional_ko_count: NonNegativeCount
    lenient_ko_count: NonNegativeCount

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

    @model_validator(mode="after")
    def validate_ko_counts(self) -> Self:
        if self.lenient_additional_ko_count > self.uncertain_record_ko_count:
            raise ValueError("lenient-additional KOs must be a subset of uncertain-record KOs")
        if self.accepted_ko_count + self.lenient_additional_ko_count != self.lenient_ko_count:
            raise ValueError("lenient KO count must equal accepted plus lenient-additional KOs")
        return self


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


class KoClassPartition(FrozenModel):
    """Lossless bounded membership partition for one status-derived KO class."""

    ko_class: ComparedKoClass
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
            "$id": "urn:kegg-mcp:schema:ko-set-comparison-detail:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    datasets: Annotated[tuple[ComparisonDatasetProvenance, ...], Field(min_length=2)]
    partitions: Annotated[tuple[KoClassPartition, ...], Field(min_length=4, max_length=4)]
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
        expected_classes = tuple(ComparedKoClass)
        if tuple(item.ko_class for item in self.partitions) != expected_classes:
            raise ValueError("comparison partitions must use canonical KO-class order")
        for partition in self.partitions:
            partition_labels = tuple(item.label for item in partition.set_specific)
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
            if dataset.lenient_ko_count > self.limits.max_unique_kos_per_set:
                raise ValueError("one comparison dataset exceeds the recorded unique-KO limit")
            if len(dataset.sources) > self.limits.max_sources_per_set:
                raise ValueError("one comparison dataset exceeds the recorded source limit")
            if len(dataset.sample_labels) > self.limits.max_sample_labels_per_set:
                raise ValueError("one comparison dataset exceeds the recorded sample-label limit")
        membership_entries = 0
        for partition in self.partitions:
            input_count = len(self.datasets)
            membership_entries += len(partition.shared_by_all) * input_count
            membership_entries += sum(len(item.ko_ids) for item in partition.set_specific)
            membership_entries += sum(
                len(pattern.ko_ids) * len(pattern.member_set_indexes)
                for pattern in partition.partially_shared
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


class KoClassComparisonSummary(FrozenModel):
    """Exact partition counts with bounded KO and membership-pattern previews."""

    ko_class: ComparedKoClass
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
            "$id": "urn:kegg-mcp:schema:ko-set-comparison-summary:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    datasets: Annotated[tuple[ComparisonDatasetProvenance, ...], Field(min_length=2)]
    partitions: Annotated[
        tuple[KoClassComparisonSummary, ...],
        Field(min_length=4, max_length=4),
    ]
    detail_sha256: Sha256
    calculation_method: CalculationMethodReference
    warnings: tuple[ComparisonWarning, ...]
    detail_limits: ComparisonLimits
    preview_limits: ComparisonPreviewLimits

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if tuple(item.ko_class for item in self.partitions) != tuple(ComparedKoClass):
            raise ValueError("comparison summaries must use canonical KO-class order")
        for partition in self.partitions:
            previews = [partition.shared_by_all]
            previews.extend(item.ko_set for item in partition.set_specific)
            previews.extend(item.ko_set for item in partition.partially_shared_patterns_preview)
            if any(len(preview.ko_ids) > self.preview_limits.max_ko_ids for preview in previews):
                raise ValueError("KO previews exceed the recorded output limit")
            if (
                len(partition.partially_shared_patterns_preview)
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


def annotation_dataset_digest(dataset: AnnotationDataset) -> str:
    """Hash canonical dataset evidence and context while excluding its opaque instance ID."""
    canonical = json.dumps(
        dataset.model_dump(mode="json", exclude={"dataset_id"}),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compare_ko_datasets(
    inputs: tuple[ComparisonDatasetInput, ...],
    *,
    limits: ComparisonLimits | None = None,
) -> KoSetComparisonDetail:
    """Partition accepted, uncertain, and lenient KOs across two or more datasets."""
    effective_limits = limits or ComparisonLimits()
    _validate_comparison_inputs(inputs, effective_limits)

    views = tuple(build_ko_evidence_view(item.dataset) for item in inputs)
    policies = tuple(view.policy for view in views)
    if any(policy != policies[0] for policy in policies[1:]):
        policy_preview = tuple(
            SafeDetail(name=f"policy_{index}", value=policy.identifier)
            for index, policy in enumerate(policies[:31])
        )
        safe_details = (
            policy_preview
            if len(policies) <= 31
            else (
                *policy_preview,
                SafeDetail(
                    name="omitted_policy_count",
                    value=str(len(policies) - len(policy_preview)),
                ),
            )
        )
        fail(
            ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE,
            "KO datasets normalized with different decision policies cannot be compared.",
            suggested_action="Re-normalize every dataset with the same named policy and version.",
            safe_details=safe_details,
        )

    class_sets: dict[ComparedKoClass, tuple[frozenset[str], ...]] = {
        ComparedKoClass.ACCEPTED: tuple(frozenset(view.accepted_kos) for view in views),
        ComparedKoClass.UNCERTAIN_RECORD: tuple(frozenset(view.uncertain_kos) for view in views),
    }
    class_sets[ComparedKoClass.LENIENT_ADDITIONAL] = tuple(
        uncertain - accepted
        for accepted, uncertain in zip(
            class_sets[ComparedKoClass.ACCEPTED],
            class_sets[ComparedKoClass.UNCERTAIN_RECORD],
            strict=True,
        )
    )
    class_sets[ComparedKoClass.LENIENT] = tuple(
        accepted | uncertain
        for accepted, uncertain in zip(
            class_sets[ComparedKoClass.ACCEPTED],
            class_sets[ComparedKoClass.UNCERTAIN_RECORD],
            strict=True,
        )
    )

    for index, ko_ids in enumerate(class_sets[ComparedKoClass.LENIENT]):
        if len(ko_ids) > effective_limits.max_unique_kos_per_set:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "A comparison input exceeds the configured unique-KO limit.",
                suggested_action="Reduce the input KO set or raise the bounded comparison limit.",
                safe_details=(
                    SafeDetail(name="input_index", value=str(index)),
                    SafeDetail(name="ko_count", value=str(len(ko_ids))),
                    SafeDetail(
                        name="max_unique_kos_per_set",
                        value=str(effective_limits.max_unique_kos_per_set),
                    ),
                ),
            )
    membership_entries = sum(len(ko_ids) for sets in class_sets.values() for ko_ids in sets)
    if membership_entries > effective_limits.max_total_membership_entries:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The comparison exceeds the configured KO-membership limit.",
            suggested_action="Compare fewer or smaller datasets, or raise the bounded limit.",
            safe_details=(
                SafeDetail(name="membership_entries", value=str(membership_entries)),
                SafeDetail(
                    name="max_total_membership_entries",
                    value=str(effective_limits.max_total_membership_entries),
                ),
            ),
        )

    labels = tuple(item.label for item in inputs)
    partitions = tuple(
        _partition_ko_class(ko_class, class_sets[ko_class], labels) for ko_class in ComparedKoClass
    )
    datasets = tuple(
        _dataset_provenance(index, item, class_sets, views[index].policy)
        for index, item in enumerate(inputs)
    )
    return KoSetComparisonDetail(
        datasets=datasets,
        partitions=partitions,
        calculation_method=CalculationMethodReference(
            name=KO_COMPARISON_METHOD,
            version=KO_COMPARISON_VERSION,
        ),
        warnings=_comparison_warnings(inputs),
        limits=effective_limits,
    )


def summarize_ko_comparison(
    detail: KoSetComparisonDetail,
    *,
    limits: ComparisonPreviewLimits | None = None,
) -> KoSetComparisonSummary:
    """Create a deterministic bounded preview without changing exact partition counts."""
    preview_limits = limits or ComparisonPreviewLimits()
    summaries: list[KoClassComparisonSummary] = []
    preview_truncated = False
    for partition in detail.partitions:
        shared = _ko_preview(partition.shared_by_all, preview_limits.max_ko_ids)
        specifics = tuple(
            DatasetSpecificKoPreview(
                input_index=item.input_index,
                label=item.label,
                ko_set=_ko_preview(item.ko_ids, preview_limits.max_ko_ids),
            )
            for item in partition.set_specific
        )
        selected_patterns = partition.partially_shared[: preview_limits.max_membership_patterns]
        patterns = tuple(
            KoMembershipPatternPreview(
                member_set_indexes=item.member_set_indexes,
                member_labels=item.member_labels,
                ko_set=_ko_preview(item.ko_ids, preview_limits.max_ko_ids),
            )
            for item in selected_patterns
        )
        patterns_truncated = len(selected_patterns) < len(partition.partially_shared)
        preview_truncated = preview_truncated or shared.truncated or patterns_truncated
        preview_truncated = preview_truncated or any(item.ko_set.truncated for item in specifics)
        preview_truncated = preview_truncated or any(item.ko_set.truncated for item in patterns)
        summaries.append(
            KoClassComparisonSummary(
                ko_class=partition.ko_class,
                union_count=partition.union_count,
                shared_by_all=shared,
                set_specific=specifics,
                partially_shared_pattern_count=len(partition.partially_shared),
                partially_shared_patterns_preview=patterns,
                partially_shared_patterns_truncated=patterns_truncated,
            )
        )

    warnings = detail.warnings
    if preview_truncated:
        warnings = (
            *warnings,
            ComparisonWarning(
                code=ComparisonWarningCode.PREVIEW_TRUNCATED,
                message=(
                    "At least one KO or membership-pattern preview was truncated; exact counts "
                    "remain available in this summary."
                ),
            ),
        )
    return KoSetComparisonSummary(
        datasets=detail.datasets,
        partitions=tuple(summaries),
        detail_sha256=_model_digest(detail),
        calculation_method=detail.calculation_method,
        warnings=warnings,
        detail_limits=detail.limits,
        preview_limits=preview_limits,
    )


def _validate_comparison_inputs(
    inputs: tuple[ComparisonDatasetInput, ...],
    limits: ComparisonLimits,
) -> None:
    if len(inputs) < 2:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "A deterministic KO comparison requires at least two datasets.",
            suggested_action="Supply two or more explicitly labelled annotation datasets.",
        )
    if len(inputs) > limits.max_sets:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The comparison contains too many datasets.",
            suggested_action="Compare fewer datasets or raise the bounded set limit.",
            safe_details=(
                SafeDetail(name="set_count", value=str(len(inputs))),
                SafeDetail(name="max_sets", value=str(limits.max_sets)),
            ),
        )
    labels = tuple(item.label for item in inputs)
    if len(labels) != len(set(labels)):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Comparison labels must be unique.",
            suggested_action="Assign one distinct stable label to each comparison input.",
        )
    dataset_ids = tuple(item.dataset.dataset_id for item in inputs)
    if len(dataset_ids) != len(set(dataset_ids)):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The same dataset instance cannot appear more than once in one comparison.",
            suggested_action="Remove duplicate dataset inputs or construct explicit derived sets.",
        )
    total_records = 0
    for index, item in enumerate(inputs):
        record_count = len(item.dataset.records)
        total_records += record_count
        if record_count > limits.max_records_per_set:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "A comparison input exceeds the configured record limit.",
                suggested_action="Reduce the input records or raise the bounded record limit.",
                safe_details=(
                    SafeDetail(name="input_index", value=str(index)),
                    SafeDetail(name="record_count", value=str(record_count)),
                    SafeDetail(
                        name="max_records_per_set",
                        value=str(limits.max_records_per_set),
                    ),
                ),
            )
        if len(item.dataset.sources) > limits.max_sources_per_set:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "A comparison input exceeds the configured source-provenance limit.",
                suggested_action="Reduce source partitions or raise the bounded source limit.",
                safe_details=(
                    SafeDetail(name="input_index", value=str(index)),
                    SafeDetail(name="source_count", value=str(len(item.dataset.sources))),
                ),
            )
        sample_label_count = len({record.sample_id for record in item.dataset.records})
        if sample_label_count > limits.max_sample_labels_per_set:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "A comparison input exceeds the configured sample-label limit.",
                suggested_action="Split the dataset or raise the bounded sample-label limit.",
                safe_details=(
                    SafeDetail(name="input_index", value=str(index)),
                    SafeDetail(name="sample_label_count", value=str(sample_label_count)),
                ),
            )
    if total_records > limits.max_total_records:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The comparison exceeds the configured total record limit.",
            suggested_action="Compare fewer or smaller datasets, or raise the bounded limit.",
            safe_details=(
                SafeDetail(name="total_records", value=str(total_records)),
                SafeDetail(name="max_total_records", value=str(limits.max_total_records)),
            ),
        )


def _partition_ko_class(
    ko_class: ComparedKoClass,
    sets: tuple[frozenset[str], ...],
    labels: tuple[str, ...],
) -> KoClassPartition:
    memberships: dict[tuple[int, ...], list[str]] = {}
    union: set[str] = set()
    for ko_set in sets:
        union.update(ko_set)
    for ko_id in sorted(union):
        members = tuple(index for index, ko_set in enumerate(sets) if ko_id in ko_set)
        memberships.setdefault(members, []).append(ko_id)
    all_members = tuple(range(len(sets)))
    shared = tuple(memberships.get(all_members, ()))
    specifics = tuple(
        DatasetSpecificKoSet(
            input_index=index,
            label=label,
            ko_ids=tuple(memberships.get((index,), ())),
        )
        for index, label in enumerate(labels)
    )
    partial = tuple(
        KoMembershipPattern(
            member_set_indexes=members,
            member_labels=tuple(labels[index] for index in members),
            ko_ids=tuple(memberships[members]),
        )
        for members in sorted(memberships)
        if 1 < len(members) < len(sets)
    )
    return KoClassPartition(
        ko_class=ko_class,
        union_count=len(union),
        shared_by_all=shared,
        set_specific=specifics,
        partially_shared=partial,
    )


def _dataset_provenance(
    index: int,
    item: ComparisonDatasetInput,
    class_sets: dict[ComparedKoClass, tuple[frozenset[str], ...]],
    policy: DecisionPolicyReference,
) -> ComparisonDatasetProvenance:
    dataset = item.dataset
    sample_labels = tuple(dict.fromkeys(record.sample_id for record in dataset.records))
    return ComparisonDatasetProvenance(
        input_index=index,
        label=item.label,
        dataset_id=dataset.dataset_id,
        dataset_sha256=annotation_dataset_digest(dataset),
        decision_policy=policy,
        analysis_unit=dataset.analysis_unit,
        taxon_id=dataset.taxon_id,
        kegg_organism_code=dataset.kegg_organism_code,
        sample_labels=sample_labels,
        sources=dataset.sources,
        record_count=len(dataset.records),
        accepted_ko_count=len(class_sets[ComparedKoClass.ACCEPTED][index]),
        uncertain_record_ko_count=len(class_sets[ComparedKoClass.UNCERTAIN_RECORD][index]),
        lenient_additional_ko_count=len(class_sets[ComparedKoClass.LENIENT_ADDITIONAL][index]),
        lenient_ko_count=len(class_sets[ComparedKoClass.LENIENT][index]),
    )


def _comparison_warnings(
    inputs: tuple[ComparisonDatasetInput, ...],
) -> tuple[ComparisonWarning, ...]:
    warnings = [
        ComparisonWarning(
            code=ComparisonWarningCode.DETERMINISTIC_SET_DIFFERENCE_ONLY,
            message=(
                "This output is a deterministic KO set comparison, not a statistical "
                "differential-function or enrichment analysis."
            ),
        ),
        ComparisonWarning(
            code=ComparisonWarningCode.ANNOTATION_ABSENCE_NOT_BIOLOGICAL_ABSENCE,
            message=(
                "A KO absent from one annotation set may reflect sequencing, assembly, gene "
                "calling, annotation, or database coverage and is not evidence of biological "
                "absence."
            ),
        ),
    ]
    source_signatures = tuple(_source_signature(item.dataset.sources) for item in inputs)
    if any(signature != source_signatures[0] for signature in source_signatures[1:]):
        warnings.append(
            ComparisonWarning(
                code=ComparisonWarningCode.ANNOTATION_PIPELINE_MISMATCH,
                message=(
                    "Compared datasets report different annotation tool, model, importer, or "
                    "version provenance; set differences may reflect pipeline differences."
                ),
                affected_input_indexes=tuple(range(len(inputs))),
            )
        )
    units = tuple(item.dataset.analysis_unit for item in inputs)
    if any(unit is not units[0] for unit in units[1:]):
        warnings.append(
            ComparisonWarning(
                code=ComparisonWarningCode.ANALYSIS_UNIT_MISMATCH,
                message=(
                    "Compared datasets use different analysis units, so biological "
                    "interpretation is not directly equivalent."
                ),
                affected_input_indexes=tuple(range(len(inputs))),
            )
        )
    contexts = tuple((item.dataset.taxon_id, item.dataset.kegg_organism_code) for item in inputs)
    if any(context != contexts[0] for context in contexts[1:]):
        warnings.append(
            ComparisonWarning(
                code=ComparisonWarningCode.TAXONOMIC_CONTEXT_MISMATCH,
                message=(
                    "Compared datasets report different or unknown taxonomic contexts; the set "
                    "arithmetic remains deterministic but interpretation is context-dependent."
                ),
                affected_input_indexes=tuple(range(len(inputs))),
            )
        )
    _append_unit_warning(
        warnings,
        inputs,
        {AnalysisUnit.UNKNOWN, AnalysisUnit.MIXED},
        ComparisonWarningCode.UNKNOWN_OR_MIXED_ANALYSIS_UNIT,
        "These datasets have unknown or mixed analysis units, which limits interpretation.",
    )
    _append_unit_warning(
        warnings,
        inputs,
        {AnalysisUnit.METAGENOMIC_COMMUNITY},
        ComparisonWarningCode.POOLED_COMMUNITY_POTENTIAL,
        (
            "Community-level KO sets describe pooled encoded potential and do not represent a "
            "complete functional repertoire in one organism."
        ),
    )
    _append_unit_warning(
        warnings,
        inputs,
        {AnalysisUnit.PANGENOME},
        ComparisonWarningCode.POOLED_PANGENOME_POTENTIAL,
        "Pangenome KO sets describe a pooled repertoire and not one isolate genome.",
    )
    _append_unit_warning(
        warnings,
        inputs,
        {AnalysisUnit.MAG},
        ComparisonWarningCode.MAG_QUALITY_SENSITIVE,
        (
            "MAG set differences are sensitive to assembly completeness, contamination, gene "
            "calling, and annotation coverage."
        ),
    )
    multi_sample_indexes = tuple(
        index
        for index, item in enumerate(inputs)
        if len({record.sample_id for record in item.dataset.records}) > 1
    )
    if multi_sample_indexes:
        warnings.append(
            ComparisonWarning(
                code=ComparisonWarningCode.MULTI_SAMPLE_DATASET,
                message=(
                    "At least one comparison input pools multiple retained sample labels; the "
                    "dataset label, sample labels, and input order are preserved explicitly."
                ),
                affected_input_indexes=multi_sample_indexes,
            )
        )
    return tuple(warnings)


def _append_unit_warning(
    warnings: list[ComparisonWarning],
    inputs: tuple[ComparisonDatasetInput, ...],
    selected_units: set[AnalysisUnit],
    code: ComparisonWarningCode,
    message: str,
) -> None:
    affected = tuple(
        index for index, item in enumerate(inputs) if item.dataset.analysis_unit in selected_units
    )
    if affected:
        warnings.append(
            ComparisonWarning(
                code=code,
                message=message,
                affected_input_indexes=affected,
            )
        )


def _source_signature(
    sources: tuple[SourceProvenance, ...],
) -> tuple[tuple[str | None, ...], ...]:
    return tuple(
        (
            source.source_name,
            source.source_version,
            source.model_name,
            source.model_version,
            source.importer_name,
            source.importer_version,
        )
        for source in sources
    )


def _ko_preview(ko_ids: tuple[str, ...], limit: int) -> KoPreview:
    selected = ko_ids[:limit]
    return KoPreview(
        count=len(ko_ids),
        ko_ids=selected,
        truncated=len(selected) < len(ko_ids),
    )


def _model_digest(model: FrozenModel) -> str:
    canonical = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "KO_COMPARISON_METHOD",
    "KO_COMPARISON_VERSION",
    "ComparedKoClass",
    "ComparisonDatasetInput",
    "ComparisonDatasetProvenance",
    "ComparisonLimits",
    "ComparisonPreviewLimits",
    "ComparisonWarning",
    "ComparisonWarningCode",
    "DatasetSpecificKoPreview",
    "DatasetSpecificKoSet",
    "KoClassComparisonSummary",
    "KoClassPartition",
    "KoMembershipPattern",
    "KoMembershipPatternPreview",
    "KoPreview",
    "KoSetComparisonDetail",
    "KoSetComparisonSummary",
    "annotation_dataset_digest",
    "compare_ko_datasets",
    "summarize_ko_comparison",
]
