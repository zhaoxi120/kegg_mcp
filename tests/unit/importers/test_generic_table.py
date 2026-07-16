"""Tests for generic CSV/TSV import with explicit semantics."""

import pytest
from pydantic import ValidationError

from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS,
    DEEPKOALA_DETAILED,
    USER_SUPPLIED_KO,
    DiagnosticCode,
    ErrorCode,
    EvidenceMode,
    KeggMcpError,
    NormalizedStatus,
    ScoreType,
    ThresholdRule,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    TableDialect,
    import_generic_table,
)

LIMITS = ImportLimits(
    max_bytes=50_000,
    max_rows=100,
    max_columns=32,
    max_field_length=2_000,
)


def _full_mapping() -> GenericColumnMapping:
    return GenericColumnMapping(
        sample_id="sample",
        sequence_id="sequence",
        ko_id="ko",
        raw_decision="decision",
        score="score",
        score_type=ScoreType.PROBABILITY,
        threshold="threshold",
        threshold_rule=ThresholdRule.GTE,
        rank="rank",
        domain_start="start",
        domain_end="end",
    )


def test_generic_csv_preserves_all_columns_and_policy_defined_uncertainty() -> None:
    payload = (
        "sample,sequence,ko,decision,score,threshold,rank,start,end,note\n"
        's1,p1,K00001,accepted,0.9,0.5,1,1,100,"alpha,beta"\n'
        "s1,p1,K00002,uncertain,0.4,0.5,2,101,200,second\n"
        "s1,p2,K00003,rejected,0.2,0.5,1,1,50,third\n"
        "s1,p3,K00004,mystery,0.8,0.5,1,5,60,fourth\n"
    )

    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=_full_mapping(),
        policy=CANONICAL_SOURCE_STATUS,
        limits=LIMITS,
    )
    view = build_ko_evidence_view(dataset)

    assert [record.sequence_id for record in dataset.records[:2]] == ["p1", "p1"]
    assert [record.rank for record in dataset.records[:2]] == [1, 2]
    assert dataset.records[0].evidence.get("note") == "alpha,beta"
    assert dataset.records[1].raw_decision == "uncertain"
    assert dataset.records[1].normalized_status is NormalizedStatus.UNCERTAIN
    assert dataset.records[3].normalized_status is NormalizedStatus.UNCLASSIFIED
    assert select_ko_ids(view, EvidenceMode.STRICT) == ("K00001",)
    assert select_ko_ids(view, EvidenceMode.LENIENT) == ("K00001", "K00002")
    assert any(
        issue.code is DiagnosticCode.UNRECOGNIZED_SOURCE_DECISION
        for issue in dataset.import_report.diagnostics
    )
    assert dataset.import_report.source_columns == (
        "sample",
        "sequence",
        "ko",
        "decision",
        "score",
        "threshold",
        "rank",
        "start",
        "end",
        "note",
    )


def test_generic_tsv_uses_explicit_dialect_and_user_supplied_policy() -> None:
    payload = "sequence\tko\textra\np1\tK00001\tkept\np1\tK00002\talso-kept\n"
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko")

    dataset = import_generic_table(
        payload,
        dialect=TableDialect.TSV,
        mapping=mapping,
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert len(dataset.records) == 2
    assert dataset.records[0].evidence.get("extra") == "kept"
    assert dataset.import_report.delimiter == "\t"
    assert dataset.import_report.conflict_count == 0


def test_generic_table_preserves_same_sequence_top_k_and_reports_explicit_slot_conflict() -> None:
    payload = "sequence,ko,rank\np1,K00001,1\np1,K00002,2\np2,K00003,1\np2,K00004,1\n"
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko", rank="rank")

    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=mapping,
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert len(dataset.records) == 4
    assert dataset.import_report.conflict_count == 1
    assert any(
        issue.code is DiagnosticCode.CONFLICTING_ASSIGNMENT
        for issue in dataset.import_report.diagnostics
    )


def test_generic_table_keeps_malformed_numeric_text_and_reports_it() -> None:
    payload = "sequence,ko,decision,score\np1,K00001,accepted,NaN\n"
    mapping = GenericColumnMapping(
        sequence_id="sequence",
        ko_id="ko",
        raw_decision="decision",
        score="score",
        score_type=ScoreType.PROBABILITY,
    )

    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=mapping,
        policy=CANONICAL_SOURCE_STATUS,
        limits=LIMITS,
    )

    assert dataset.records[0].score is None
    assert dataset.records[0].evidence.get("score") == "NaN"
    assert dataset.records[0].normalized_status is NormalizedStatus.ACCEPTED
    assert any(
        issue.code is DiagnosticCode.INVALID_FIELD_VALUE
        for issue in dataset.import_report.diagnostics
    )


def test_generic_table_retains_ragged_row_as_unparsed_evidence() -> None:
    payload = "sequence,ko\np1,K00001\np2,K00002,extra\n"
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko")

    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=mapping,
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert len(dataset.records) == 1
    assert dataset.import_report.input_rows == 2
    assert dataset.import_report.skipped_rows == 1
    assert dataset.import_report.unparsed_rows[0].get("_extra_1") == "extra"


@pytest.mark.parametrize(
    "payload",
    [
        "sequence,ko\np1,K00001,extra\np1,K00001,extra\n",
        "sequence,ko\n,K00001\n,K00001\n",
    ],
)
def test_generic_table_reports_duplicate_skipped_logical_rows(payload: str) -> None:
    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko"),
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert dataset.records == ()
    assert dataset.import_report.skipped_rows == 2
    assert dataset.import_report.duplicate_count == 1
    assert any(
        issue.code is DiagnosticCode.DUPLICATE_ROW for issue in dataset.import_report.diagnostics
    )


def test_ragged_extra_field_names_do_not_collide_with_source_headers() -> None:
    payload = "sequence,ko,_extra_1\np1,K00001,kept,overflow\n"
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko")

    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=mapping,
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    row = dataset.import_report.unparsed_rows[0]
    assert row.get("_extra_1") == "kept"
    assert row.get("_extra_1_1") == "overflow"


def test_ragged_extra_field_collision_suffix_stays_bounded() -> None:
    payload = "sequence,ko,_extra_1,_extra_1_1\np1,K00001,first,second,overflow\n"

    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko"),
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert dataset.import_report.unparsed_rows[0].get("_extra_1_2") == "overflow"


def test_generic_table_reports_explicit_all_empty_logical_row() -> None:
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko")

    dataset = import_generic_table(
        "sequence,ko\n,\n",
        dialect=TableDialect.CSV,
        mapping=mapping,
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert dataset.records == ()
    assert dataset.sources[0].source_name == "unknown"
    assert dataset.import_report.input_rows == 1
    assert dataset.import_report.skipped_rows == 1
    assert dataset.import_report.unparsed_rows[0].get("sequence") == ""


def test_generic_header_only_input_retains_every_source_column() -> None:
    dataset = import_generic_table(
        "sequence,ko,unmapped_note\n",
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko"),
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert dataset.records == ()
    assert dataset.import_report.source_columns == ("sequence", "ko", "unmapped_note")


def test_generic_table_missing_mapped_column_is_repairable() -> None:
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko")

    with pytest.raises(KeggMcpError) as error:
        import_generic_table(
            "sequence,other\np1,K00001\n",
            dialect=TableDialect.CSV,
            mapping=mapping,
            policy=USER_SUPPLIED_KO,
            limits=LIMITS,
        )

    assert error.value.detail.code is ErrorCode.MISSING_REQUIRED_COLUMN
    assert error.value.detail.recoverable


def test_generic_table_rejects_duplicate_headers() -> None:
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko")

    with pytest.raises(KeggMcpError) as error:
        import_generic_table(
            "sequence,ko,ko\np1,K00001,K00002\n",
            dialect=TableDialect.CSV,
            mapping=mapping,
            policy=USER_SUPPLIED_KO,
            limits=LIMITS,
        )

    assert error.value.detail.code is ErrorCode.INVALID_ANNOTATION_TABLE


def test_generic_table_rejects_oversized_header_with_structured_error() -> None:
    mapping = GenericColumnMapping(sequence_id="sequence", ko_id="ko")
    oversized_header = "x" * 257

    with pytest.raises(KeggMcpError) as error:
        import_generic_table(
            f"sequence,ko,{oversized_header}\np1,K00001,value\n",
            dialect=TableDialect.CSV,
            mapping=mapping,
            policy=USER_SUPPLIED_KO,
            limits=LIMITS,
        )

    assert error.value.detail.code is ErrorCode.INVALID_ANNOTATION_TABLE


def test_generic_table_supports_explicit_field_limit_above_csv_default() -> None:
    long_value = "x" * 150_000
    limits = ImportLimits(
        max_bytes=200_000,
        max_rows=10,
        max_columns=10,
        max_field_length=160_000,
    )

    dataset = import_generic_table(
        f"sequence,ko,note\np1,K00001,{long_value}\n",
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko"),
        policy=USER_SUPPLIED_KO,
        limits=limits,
    )

    assert dataset.records[0].evidence.get("note") == long_value


def test_generic_table_reports_huge_integer_without_python_conversion_error() -> None:
    huge_rank = "9" * 5_000
    limits = ImportLimits(
        max_bytes=20_000,
        max_rows=10,
        max_columns=10,
        max_field_length=6_000,
    )

    dataset = import_generic_table(
        f"sequence,ko,rank\np1,K00001,{huge_rank}\n",
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko", rank="rank"),
        policy=USER_SUPPLIED_KO,
        limits=limits,
    )

    assert dataset.records[0].rank is None
    assert any(
        issue.code is DiagnosticCode.INVALID_FIELD_VALUE
        for issue in dataset.import_report.diagnostics
    )


def test_generic_table_skips_oversized_mapped_sample_identifier() -> None:
    sample_id = "s" * 257
    mapping = GenericColumnMapping(sample_id="sample", sequence_id="sequence", ko_id="ko")

    dataset = import_generic_table(
        f"sample,sequence,ko\n{sample_id},p1,K00001\n",
        dialect=TableDialect.CSV,
        mapping=mapping,
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert dataset.records == ()
    assert dataset.import_report.skipped_rows == 1
    assert dataset.import_report.unparsed_rows[0].get("sample") == sample_id


def test_generic_table_rejects_format_specific_policy() -> None:
    with pytest.raises(KeggMcpError) as error:
        import_generic_table(
            "sequence,ko\np1,K00001\n",
            dialect=TableDialect.CSV,
            mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko"),
            policy=DEEPKOALA_DETAILED,
            limits=LIMITS,
        )

    assert error.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID


def test_missing_column_preview_is_bounded_for_full_mapping() -> None:
    def long_name(prefix: str) -> str:
        return f"{prefix}{'x' * (256 - len(prefix))}"

    mapping = GenericColumnMapping(
        sequence_id=long_name("sequence_"),
        ko_id=long_name("ko_"),
        sample_id=long_name("sample_"),
        raw_decision=long_name("decision_"),
        score=long_name("score_"),
        score_type=ScoreType.PROBABILITY,
        threshold=long_name("threshold_"),
        threshold_rule=ThresholdRule.GTE,
        rank=long_name("rank_"),
        domain_start=long_name("start_"),
        domain_end=long_name("end_"),
    )

    with pytest.raises(KeggMcpError) as error:
        import_generic_table(
            "present\nvalue\n",
            dialect=TableDialect.CSV,
            mapping=mapping,
            policy=CANONICAL_SOURCE_STATUS,
            limits=LIMITS,
        )

    assert error.value.detail.code is ErrorCode.MISSING_REQUIRED_COLUMN
    assert all(len(detail.value) <= 1_000 for detail in error.value.detail.safe_details)


def test_generic_table_retains_invalid_ko_and_exact_duplicate_rows() -> None:
    dataset = import_generic_table(
        "sequence,ko\np1,BAD\np2,K00001\np2,K00001\n",
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko"),
        policy=USER_SUPPLIED_KO,
        limits=LIMITS,
    )

    assert dataset.records[0].normalized_status is NormalizedStatus.INVALID
    assert dataset.records[0].raw_ko == "BAD"
    assert dataset.import_report.duplicate_count == 1


def test_generic_mapping_rejects_reused_columns_and_incomplete_semantics() -> None:
    with pytest.raises(ValidationError):
        GenericColumnMapping(sequence_id="id", ko_id="id")
    with pytest.raises(ValidationError):
        GenericColumnMapping(sequence_id="id", ko_id="ko", score="score")
    with pytest.raises(ValidationError):
        GenericColumnMapping(sequence_id="id", ko_id="ko", domain_start="start")
