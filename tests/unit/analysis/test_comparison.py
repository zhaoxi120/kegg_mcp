"""Tests for bounded deterministic multi-dataset KO set comparison."""

import pytest

from kegg_mcp.analysis.comparison import (
    ComparedKoClass,
    ComparisonDatasetInput,
    ComparisonLimits,
    ComparisonPreviewLimits,
    ComparisonWarningCode,
    KoSetComparisonDetail,
    KoSetComparisonSummary,
    annotation_dataset_digest,
    compare_ko_datasets,
    summarize_ko_comparison,
)
from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS_V1,
    USER_SUPPLIED_KO_V1,
    AnalysisUnit,
)
from kegg_mcp.domain.decisions import DecisionPolicy
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    SourceProvenanceInput,
    TableDialect,
    import_generic_table,
)

_LIMITS = ImportLimits(
    max_bytes=100_000,
    max_rows=1_000,
    max_columns=20,
    max_field_length=1_000,
)


def _dataset(
    rows: tuple[tuple[str, str, str], ...],
    *,
    unit: AnalysisUnit = AnalysisUnit.ISOLATE_PROTEOME,
    taxon_id: int | None = 9606,
    organism_code: str | None = "hsa",
    source_name: str = "synthetic",
    policy: DecisionPolicy = CANONICAL_SOURCE_STATUS_V1,
):
    payload = "sequence,ko,status\n" + "".join(
        f"{sequence},{ko_id},{status}\n" for sequence, ko_id, status in rows
    )
    return import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=policy,
        limits=_LIMITS,
        analysis_unit=unit,
        taxon_id=taxon_id,
        kegg_organism_code=organism_code,
        source=SourceProvenanceInput(source_name=source_name),
    )


def _three_inputs() -> tuple[ComparisonDatasetInput, ...]:
    first = _dataset(
        (
            ("a1", "K00001", "accepted"),
            ("a2", "K00002", "accepted"),
            ("a3", "K00003", "accepted"),
            ("a9", "K00009", "accepted"),
            ("a4", "K00004", "uncertain"),
            ("a8a", "K00008", "accepted"),
            ("a8u", "K00008", "uncertain"),
            ("a-rejected", "K00010", "rejected"),
        )
    )
    second = _dataset(
        (
            ("b2", "K00002", "accepted"),
            ("b3", "K00003", "accepted"),
            ("b5", "K00005", "accepted"),
            ("b9", "K00009", "accepted"),
            ("b4", "K00004", "uncertain"),
            ("b8", "K00008", "uncertain"),
        )
    )
    third = _dataset(
        (
            ("c2", "K00002", "accepted"),
            ("c3", "K00003", "accepted"),
            ("c7", "K00007", "accepted"),
            ("c6", "K00006", "uncertain"),
        )
    )
    return (
        ComparisonDatasetInput(label="first", dataset=first),
        ComparisonDatasetInput(label="second", dataset=second),
        ComparisonDatasetInput(label="third", dataset=third),
    )


def _partition(detail: KoSetComparisonDetail, ko_class: ComparedKoClass):
    return next(item for item in detail.partitions if item.ko_class is ko_class)


def _warning_codes(detail: KoSetComparisonDetail) -> set[ComparisonWarningCode]:
    return {warning.code for warning in detail.warnings}


def test_three_set_comparison_retains_shared_specific_and_partial_memberships() -> None:
    detail = compare_ko_datasets(_three_inputs())
    accepted = _partition(detail, ComparedKoClass.ACCEPTED)

    assert accepted.union_count == 7
    assert accepted.shared_by_all == ("K00002", "K00003")
    assert [item.label for item in accepted.set_specific] == ["first", "second", "third"]
    assert [item.ko_ids for item in accepted.set_specific] == [
        ("K00001", "K00008"),
        ("K00005",),
        ("K00007",),
    ]
    assert len(accepted.partially_shared) == 1
    assert accepted.partially_shared[0].member_set_indexes == (0, 1)
    assert accepted.partially_shared[0].member_labels == ("first", "second")
    assert accepted.partially_shared[0].ko_ids == ("K00009",)
    assert "K00010" not in {
        ko_id
        for partition in detail.partitions
        for item in partition.set_specific
        for ko_id in item.ko_ids
    }

    assert [item.input_index for item in detail.datasets] == [0, 1, 2]
    assert [item.label for item in detail.datasets] == ["first", "second", "third"]
    assert all(item.sources for item in detail.datasets)
    assert ComparisonWarningCode.DETERMINISTIC_SET_DIFFERENCE_ONLY in _warning_codes(detail)
    assert ComparisonWarningCode.ANNOTATION_ABSENCE_NOT_BIOLOGICAL_ABSENCE in _warning_codes(detail)
    assert KoSetComparisonDetail.model_validate_json(detail.model_dump_json()) == detail


def test_uncertain_record_and_lenient_additional_classes_are_not_conflated() -> None:
    detail = compare_ko_datasets(_three_inputs())
    uncertain = _partition(detail, ComparedKoClass.UNCERTAIN_RECORD)
    additional = _partition(detail, ComparedKoClass.LENIENT_ADDITIONAL)
    lenient = _partition(detail, ComparedKoClass.LENIENT)

    assert "K00008" in uncertain.partially_shared[0].ko_ids
    assert "K00008" not in additional.set_specific[0].ko_ids
    assert "K00008" in additional.set_specific[1].ko_ids
    assert detail.datasets[0].uncertain_record_ko_count == 2
    assert detail.datasets[0].lenient_additional_ko_count == 1
    assert detail.datasets[0].lenient_ko_count == detail.datasets[0].accepted_ko_count + 1
    assert "K00008" in lenient.partially_shared[0].ko_ids


def test_summary_has_exact_counts_bounded_previews_and_stable_detail_digest() -> None:
    detail = compare_ko_datasets(_three_inputs())
    summary = summarize_ko_comparison(
        detail,
        limits=ComparisonPreviewLimits(max_ko_ids=1, max_membership_patterns=1),
    )
    accepted = next(
        item for item in summary.partitions if item.ko_class is ComparedKoClass.ACCEPTED
    )

    assert accepted.union_count == 7
    assert accepted.shared_by_all.count == 2
    assert accepted.shared_by_all.ko_ids == ("K00002",)
    assert accepted.shared_by_all.truncated is True
    assert accepted.set_specific[0].ko_set.count == 2
    assert accepted.set_specific[0].ko_set.ko_ids == ("K00001",)
    assert ComparisonWarningCode.PREVIEW_TRUNCATED in {warning.code for warning in summary.warnings}
    assert len(summary.detail_sha256) == 64
    assert summary == summarize_ko_comparison(
        detail,
        limits=ComparisonPreviewLimits(max_ko_ids=1, max_membership_patterns=1),
    )
    assert KoSetComparisonSummary.model_validate_json(summary.model_dump_json()) == summary


def test_dataset_digest_excludes_only_the_opaque_dataset_instance_id() -> None:
    dataset = _three_inputs()[0].dataset
    copied = dataset.model_copy(update={"dataset_id": "dataset-another-instance"})
    changed = dataset.model_copy(update={"analysis_unit": AnalysisUnit.MAG})

    assert annotation_dataset_digest(dataset) == annotation_dataset_digest(copied)
    assert annotation_dataset_digest(dataset) != annotation_dataset_digest(changed)


def test_decision_policy_mismatch_fails_with_structured_provenance_error() -> None:
    canonical = _dataset((("one", "K00001", "accepted"),))
    supplied = _dataset(
        (("two", "K00002", "ignored"),),
        policy=USER_SUPPLIED_KO_V1,
    )

    with pytest.raises(KeggMcpError) as caught:
        compare_ko_datasets(
            (
                ComparisonDatasetInput(label="canonical", dataset=canonical),
                ComparisonDatasetInput(label="supplied", dataset=supplied),
            )
        )

    assert caught.value.detail.code is ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE
    assert {item.name for item in caught.value.detail.safe_details} == {"policy_0", "policy_1"}


def test_large_policy_mismatch_returns_bounded_structured_error() -> None:
    inputs = tuple(
        ComparisonDatasetInput(
            label=f"input-{index}",
            dataset=_dataset(
                ((f"sequence-{index}", "K00001", "accepted"),),
                policy=(USER_SUPPLIED_KO_V1 if index == 99 else CANONICAL_SOURCE_STATUS_V1),
            ),
        )
        for index in range(100)
    )

    with pytest.raises(KeggMcpError) as caught:
        compare_ko_datasets(inputs, limits=ComparisonLimits(max_sets=100))

    assert caught.value.detail.code is ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE
    details = caught.value.detail.safe_details
    assert len(details) == 32
    assert tuple(item.name for item in details[:31]) == tuple(
        f"policy_{index}" for index in range(31)
    )
    assert details[-1].name == "omitted_policy_count"
    assert details[-1].value == "69"


def test_input_shape_and_hard_limits_fail_before_membership_expansion() -> None:
    first, second, *_ = _three_inputs()

    with pytest.raises(KeggMcpError) as caught:
        compare_ko_datasets((first,))
    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID

    with pytest.raises(KeggMcpError) as caught:
        compare_ko_datasets(
            (
                first,
                ComparisonDatasetInput(label=first.label, dataset=second.dataset),
            )
        )
    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID

    with pytest.raises(KeggMcpError) as caught:
        compare_ko_datasets(
            (first, second),
            limits=ComparisonLimits(max_records_per_set=1),
        )
    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED

    with pytest.raises(KeggMcpError) as caught:
        compare_ko_datasets(
            (first, second),
            limits=ComparisonLimits(max_total_membership_entries=1),
        )
    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_context_and_pipeline_differences_warn_without_changing_set_arithmetic() -> None:
    isolate = _dataset(
        (("one", "K00001", "accepted"),),
        source_name="pipeline-a",
    )
    community = _dataset(
        (("two", "K00002", "accepted"),),
        unit=AnalysisUnit.METAGENOMIC_COMMUNITY,
        taxon_id=None,
        organism_code=None,
        source_name="pipeline-b",
    )
    detail = compare_ko_datasets(
        (
            ComparisonDatasetInput(label="isolate", dataset=isolate),
            ComparisonDatasetInput(label="community", dataset=community),
        )
    )
    codes = _warning_codes(detail)

    assert ComparisonWarningCode.ANNOTATION_PIPELINE_MISMATCH in codes
    assert ComparisonWarningCode.ANALYSIS_UNIT_MISMATCH in codes
    assert ComparisonWarningCode.TAXONOMIC_CONTEXT_MISMATCH in codes
    assert ComparisonWarningCode.POOLED_COMMUNITY_POTENTIAL in codes
    accepted = _partition(detail, ComparedKoClass.ACCEPTED)
    assert [item.ko_ids for item in accepted.set_specific] == [
        ("K00001",),
        ("K00002",),
    ]


def test_multi_sample_labels_preserve_first_seen_order_and_emit_warning() -> None:
    payload = (
        "sample,sequence,ko,status\n"
        "sample-b,one,K00001,accepted\n"
        "sample-a,two,K00002,accepted\n"
        "sample-b,three,K00003,accepted\n"
    )
    pooled = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sample_id="sample",
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=CANONICAL_SOURCE_STATUS_V1,
        limits=_LIMITS,
        analysis_unit=AnalysisUnit.MIXED,
    )
    other = _dataset((("other", "K00001", "accepted"),))
    detail = compare_ko_datasets(
        (
            ComparisonDatasetInput(label="pooled", dataset=pooled),
            ComparisonDatasetInput(label="other", dataset=other),
        )
    )

    assert detail.datasets[0].sample_labels == ("sample-b", "sample-a")
    assert ComparisonWarningCode.MULTI_SAMPLE_DATASET in _warning_codes(detail)
    assert ComparisonWarningCode.UNKNOWN_OR_MIXED_ANALYSIS_UNIT in _warning_codes(detail)


def test_public_schema_has_no_statistical_or_biological_status_fields() -> None:
    schema_text = str(KoSetComparisonSummary.model_json_schema()).lower()

    for forbidden in (
        "p_value",
        "fold_change",
        "enrichment",
        "pathway_present",
        "pathway_complete",
        "activity_change",
        "flux_change",
    ):
        assert forbidden not in schema_text
