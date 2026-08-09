"""Tests for the documented DeepKOALA detailed-output import contract."""

from pathlib import Path

import pytest

from kegg_mcp.domain import (
    DiagnosticCode,
    ErrorCode,
    KeggMcpError,
    NormalizedStatus,
    ScoreType,
    ThresholdRule,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.importers import (
    ImportLimits,
    SourceProvenanceInput,
    import_deepkoala_detailed,
)

LIMITS = ImportLimits(
    max_bytes=50_000,
    max_rows=100,
    max_columns=32,
    max_field_length=2_000,
)
FIXTURE = Path(__file__).parents[2] / "fixtures" / "deepkoala" / "detailed.csv"


def test_deepkoala_truth_table_and_multi_domain_contract() -> None:
    dataset = import_deepkoala_detailed(FIXTURE.read_bytes(), limits=LIMITS)
    view = build_ko_evidence_view(dataset)

    assert [record.normalized_status for record in dataset.records] == [
        NormalizedStatus.ACCEPTED,
        NormalizedStatus.ACCEPTED,
        NormalizedStatus.ACCEPTED,
        NormalizedStatus.REJECTED,
        NormalizedStatus.UNCLASSIFIED,
        NormalizedStatus.INVALID,
    ]
    assert dataset.records[0].status_reason == "source_acceptance_marker"
    assert dataset.records[1].status_reason == "meets_source_threshold"
    assert dataset.records[3].status_reason == "below_source_threshold"
    assert dataset.records[0].domain_start == 1
    assert dataset.records[0].domain_end == 100
    assert dataset.records[0].score_type is ScoreType.PROBABILITY
    assert dataset.records[0].threshold_rule is ThresholdRule.GTE
    assert dataset.records[0].evidence.get("note") == "marker wins"
    assert select_ko_ids(view) == ("K00001", "K00002", "K00003")
    assert "K00004" not in select_ko_ids(view)
    assert dataset.import_report.source_columns == (
        "name",
        "predict_label",
        "probability",
        "threshold",
        "annotate",
        "start",
        "end",
        "note",
    )


def test_deepkoala_marker_threshold_disagreement_is_reported_but_preserved() -> None:
    dataset = import_deepkoala_detailed(FIXTURE.read_bytes(), limits=LIMITS)

    assert dataset.records[0].normalized_status is NormalizedStatus.ACCEPTED
    assert any(
        issue.code is DiagnosticCode.SOURCE_DECISION_CONFLICT
        for issue in dataset.import_report.diagnostics
    )


def test_deepkoala_splits_exact_composite_ko_labels_into_independent_records() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate,note\n"
        'p1,"  K01784 + K01785+K01786  ",0.9,0.5,*,composite source row\n'
    )

    dataset = import_deepkoala_detailed(payload, limits=LIMITS)
    view = build_ko_evidence_view(dataset)

    assert [record.raw_ko for record in dataset.records] == ["K01784", "K01785", "K01786"]
    assert [record.ko_id for record in dataset.records] == ["K01784", "K01785", "K01786"]
    assert {record.normalized_status for record in dataset.records} == {NormalizedStatus.ACCEPTED}
    assert {record.status_reason for record in dataset.records} == {"source_acceptance_marker"}
    assert {record.score for record in dataset.records} == {0.9}
    assert {record.threshold for record in dataset.records} == {0.5}
    assert {record.raw_decision for record in dataset.records} == {"*"}
    assert all(record.source == dataset.sources[0] for record in dataset.records)
    assert dataset.sources[0].importer_version == "2"
    assert {record.evidence.get("predict_label") for record in dataset.records} == {
        "  K01784 + K01785+K01786  "
    }
    assert {record.evidence.get("note") for record in dataset.records} == {"composite source row"}
    assert dataset.import_report.input_rows == 1
    assert dataset.import_report.emitted_records == 3
    assert dataset.import_report.conflict_count == 0
    assert view.accepted_kos == ("K01784", "K01785", "K01786")


def test_deepkoala_composite_components_preserve_rejected_source_semantics() -> None:
    payload = "name,predict_label,probability,threshold,annotate\np1,K01784+K01785,0.4,0.5,\n"

    dataset = import_deepkoala_detailed(payload, limits=LIMITS)
    view = build_ko_evidence_view(dataset)

    assert [record.ko_id for record in dataset.records] == ["K01784", "K01785"]
    assert {record.normalized_status for record in dataset.records} == {NormalizedStatus.REJECTED}
    assert {record.status_reason for record in dataset.records} == {"below_source_threshold"}
    assert view.rejected_kos == ("K01784", "K01785")
    assert select_ko_ids(view) == ()


@pytest.mark.parametrize(
    "raw_label",
    [
        "K01784+not-a-ko",
        "K01784++K01785",
        "K01784+ko:K01785",
        "K01784,K01785",
        "k01784+K01785",
    ],
)
def test_deepkoala_does_not_guess_malformed_or_mixed_composite_labels(raw_label: str) -> None:
    payload = f'name,predict_label,probability,threshold,annotate\np1,"{raw_label}",0.9,0.5,*\n'

    dataset = import_deepkoala_detailed(payload, limits=LIMITS)

    assert len(dataset.records) == 1
    assert dataset.records[0].raw_ko == raw_label
    assert dataset.records[0].ko_id is None
    assert dataset.records[0].normalized_status is NormalizedStatus.INVALID
    assert (
        sum(
            diagnostic.code is DiagnosticCode.INVALID_KO_IDENTIFIER
            for diagnostic in dataset.import_report.diagnostics
        )
        == 1
    )


def test_deepkoala_bounds_records_created_by_composite_expansion() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\np1,K01784+K01785+K01786,0.9,0.5,*\n"
    )
    limits = ImportLimits(
        max_bytes=50_000,
        max_rows=2,
        max_columns=32,
        max_field_length=2_000,
    )

    with pytest.raises(KeggMcpError) as error:
        import_deepkoala_detailed(payload, limits=limits)

    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert error.value.detail.safe_details[0].name == "max_records"
    assert error.value.detail.safe_details[0].value == "2"


def test_deepkoala_marker_is_exact_and_unknown_values_are_unclassified() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.1,0.5, * \n"
        "p2,K00002,0.9,0.5,yes\n"
    )

    dataset = import_deepkoala_detailed(payload, limits=LIMITS)

    assert [record.normalized_status for record in dataset.records] == [
        NormalizedStatus.UNCLASSIFIED,
        NormalizedStatus.UNCLASSIFIED,
    ]
    assert (
        sum(
            issue.code is DiagnosticCode.UNRECOGNIZED_SOURCE_DECISION
            for issue in dataset.import_report.diagnostics
        )
        == 2
    )
    assert not any(
        issue.code is DiagnosticCode.SOURCE_DECISION_CONFLICT
        for issue in dataset.import_report.diagnostics
    )


def test_deepkoala_conflicts_at_sequence_or_explicit_domain_slot() -> None:
    sequence_payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.9,0.5,*\n"
        "p1,K00002,0.9,0.5,*\n"
    )
    domain_payload = (
        "name,predict_label,probability,threshold,annotate,start,end\n"
        "p1,K00001,0.9,0.5,*,1,100\n"
        "p1,K00002,0.9,0.5,*,101,200\n"
    )

    sequence_dataset = import_deepkoala_detailed(sequence_payload, limits=LIMITS)
    domain_dataset = import_deepkoala_detailed(domain_payload, limits=LIMITS)

    assert sequence_dataset.import_report.conflict_count == 1
    assert domain_dataset.import_report.conflict_count == 0


def test_deepkoala_reports_invalid_threshold_and_coordinates() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate,start,end\np1,K00001,0.9,1.2,,20,10\n"
    )

    dataset = import_deepkoala_detailed(payload, limits=LIMITS)

    record = dataset.records[0]
    assert record.normalized_status is NormalizedStatus.UNCLASSIFIED
    assert record.threshold is None
    assert record.domain_start is None
    assert record.domain_end is None
    assert (
        sum(
            issue.code is DiagnosticCode.INVALID_FIELD_VALUE
            for issue in dataset.import_report.diagnostics
        )
        == 2
    )


def test_deepkoala_reports_exact_duplicate_without_dropping_evidence() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.9,0.5,*\n"
        "p1,K00001,0.9,0.5,*\n"
    )

    dataset = import_deepkoala_detailed(payload, limits=LIMITS)

    assert len(dataset.records) == 2
    assert dataset.import_report.duplicate_count == 1
    assert dataset.import_report.conflict_count == 0


def test_deepkoala_nonfinite_or_out_of_range_probability_is_unclassified() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,NaN,0.5,\n"
        "p2,K00002,1.2,0.5,\n"
        "p3,K00003,NaN,0.5,*\n"
    )

    dataset = import_deepkoala_detailed(payload, limits=LIMITS)

    assert dataset.records[0].normalized_status is NormalizedStatus.UNCLASSIFIED
    assert dataset.records[1].normalized_status is NormalizedStatus.UNCLASSIFIED
    assert dataset.records[2].normalized_status is NormalizedStatus.ACCEPTED
    assert dataset.records[0].evidence.get("probability") == "NaN"
    assert (
        sum(
            issue.code is DiagnosticCode.INVALID_FIELD_VALUE
            for issue in dataset.import_report.diagnostics
        )
        == 3
    )


@pytest.mark.parametrize(
    "payload",
    [
        "name,predict_label,probability,threshold\np1,K00001,0.9,0.5\n",
        "name,predict_label\np1,K00001\n",
    ],
)
def test_simple_or_incomplete_deepkoala_output_is_not_misrepresented(payload: str) -> None:
    with pytest.raises(KeggMcpError) as error:
        import_deepkoala_detailed(payload, limits=LIMITS)

    assert error.value.detail.code is ErrorCode.MISSING_REQUIRED_COLUMN


def test_deepkoala_requires_both_domain_columns() -> None:
    payload = "name,predict_label,probability,threshold,annotate,start\np1,K00001,0.9,0.5,*,1\n"

    with pytest.raises(KeggMcpError) as error:
        import_deepkoala_detailed(payload, limits=LIMITS)

    assert error.value.detail.code is ErrorCode.MISSING_REQUIRED_COLUMN


def test_deepkoala_provenance_is_preserved_without_inference() -> None:
    source = SourceProvenanceInput(
        source_name="deepkoala",
        source_version="1.2.3",
        model_name="frag",
        model_version=None,
        input_uri="inline://deepkoala-result",
    )

    dataset = import_deepkoala_detailed(FIXTURE.read_bytes(), limits=LIMITS, source=source)
    provenance = dataset.records[0].source

    assert provenance.source_name == "deepkoala"
    assert provenance.source_version == "1.2.3"
    assert provenance.model_name == "frag"
    assert provenance.model_version is None
    assert provenance.input_uri == "inline://deepkoala-result"


def test_deepkoala_rejects_incompatible_declared_source() -> None:
    with pytest.raises(KeggMcpError) as error:
        import_deepkoala_detailed(
            FIXTURE.read_bytes(),
            limits=LIMITS,
            source=SourceProvenanceInput(source_name="kofamscan"),
        )

    assert error.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
