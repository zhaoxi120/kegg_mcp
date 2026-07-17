"""Bounded deterministic comparison of immutable KO annotation datasets."""

from __future__ import annotations

from kegg_mcp.analysis.comparison_contracts import (
    KO_COMPARISON_METHOD,
    KO_COMPARISON_VERSION,
    ComparedKoClass,
    ComparisonDatasetInput,
    ComparisonDatasetProvenance,
    ComparisonLimits,
    ComparisonPreviewLimits,
    ComparisonWarning,
    ComparisonWarningCode,
    DatasetSpecificKoPreview,
    DatasetSpecificKoSet,
    KoClassComparisonSummary,
    KoClassPartition,
    KoMembershipPattern,
    KoMembershipPatternPreview,
    KoPreview,
    KoSetComparisonDetail,
    KoSetComparisonSummary,
)
from kegg_mcp.analysis.contracts import CalculationMethodReference
from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    DecisionPolicyReference,
    SourceProvenance,
    build_ko_evidence_view,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail


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


__all__ = ["compare_ko_datasets", "summarize_ko_comparison"]
