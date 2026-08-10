"""Tests for immutable canonical annotation contracts and JSON Schemas."""

import math
from typing import cast

import pytest
from pydantic import ValidationError

from kegg_mcp.domain import (
    AnnotationDataset,
    AnnotationRecord,
    DecisionEvidence,
    DecisionOutcome,
    EvidenceField,
    ImportReport,
    KOEvidenceView,
    NormalizedStatus,
    ScoreType,
    SourceProvenance,
    build_ko_evidence_view,
)
from kegg_mcp.domain.annotations import MAX_EVIDENCE_STRING_CHARACTERS
from kegg_mcp.importers import ImportLimits, import_plain_ko

LIMITS = ImportLimits(
    max_bytes=10_000,
    max_rows=100,
    max_columns=32,
    max_field_length=1_000,
)


def _record_payload() -> tuple[AnnotationRecord, dict[str, object]]:
    record = import_plain_ko("K00001", limits=LIMITS).records[0]
    payload: dict[str, object] = record.model_dump()
    return record, payload


def test_dataset_json_round_trip_preserves_all_contract_fields() -> None:
    dataset = import_plain_ko("K00001\nBAD", limits=LIMITS)

    restored = AnnotationDataset.model_validate_json(dataset.model_dump_json())

    assert restored == dataset
    assert restored.sources == dataset.sources
    assert restored.records[1].raw_ko == "BAD"
    assert restored.records[1].ko_id is None


def test_contracts_are_frozen_at_top_level_and_inside_raw_evidence() -> None:
    dataset = import_plain_ko("K00001", limits=LIMITS)
    record = dataset.records[0]

    raw_ko_field = "raw_ko"
    value_field = "value"
    records_field = "records"
    with pytest.raises(ValidationError):
        setattr(record, raw_ko_field, "K99999")
    with pytest.raises(ValidationError):
        setattr(record.evidence.fields[0], value_field, "K99999")
    with pytest.raises(ValidationError):
        setattr(dataset, records_field, ())

    assert isinstance(dataset.records, tuple)
    assert isinstance(record.evidence.fields, tuple)


def test_annotation_record_rejects_status_without_ko() -> None:
    _, payload = _record_payload()
    payload["ko_id"] = None
    payload["raw_ko"] = ""

    with pytest.raises(ValidationError, match="accepted records require ko_id"):
        AnnotationRecord.model_validate(payload)


def test_annotation_record_rejects_invalid_status_with_ko() -> None:
    _, payload = _record_payload()
    payload["normalized_status"] = NormalizedStatus.INVALID

    with pytest.raises(ValidationError, match="invalid records must not contain"):
        AnnotationRecord.model_validate(payload)


def test_annotation_record_requires_raw_and_normalized_ko_consistency() -> None:
    _, payload = _record_payload()
    payload["ko_id"] = "K99999"

    with pytest.raises(ValidationError, match="exact normalization"):
        AnnotationRecord.model_validate(payload)


def test_annotation_record_rejects_non_utf8_raw_text() -> None:
    _, payload = _record_payload()
    payload["raw_decision"] = "\ud800"

    with pytest.raises(ValidationError):
        AnnotationRecord.model_validate(payload)


def test_machine_reasons_use_stable_lowercase_identifiers() -> None:
    _, payload = _record_payload()
    payload["status_reason"] = "Not machine readable"

    with pytest.raises(ValidationError):
        AnnotationRecord.model_validate(payload)
    with pytest.raises(ValidationError):
        DecisionOutcome(
            status=NormalizedStatus.ACCEPTED,
            reason="",
        )


@pytest.mark.parametrize(("field", "value"), [("sample_id", "  "), ("sequence_id", "\t")])
def test_annotation_record_rejects_blank_identifiers(field: str, value: str) -> None:
    _, payload = _record_payload()
    payload[field] = value

    with pytest.raises(ValidationError, match=f"{field} must not be blank"):
        AnnotationRecord.model_validate(payload)


@pytest.mark.parametrize(
    ("start", "end"),
    [(1, None), (None, 1), (2, 1), (0, 1), (True, 2)],
)
def test_annotation_record_rejects_invalid_domain_coordinates(start: object, end: object) -> None:
    _, payload = _record_payload()
    payload["domain_start"] = start
    payload["domain_end"] = end

    with pytest.raises(ValidationError):
        AnnotationRecord.model_validate(payload)


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, 1.1, -0.1])
def test_probability_score_must_be_finite_and_bounded(value: float) -> None:
    _, payload = _record_payload()
    payload["score"] = value
    payload["score_type"] = "probability"

    with pytest.raises(ValidationError):
        AnnotationRecord.model_validate(payload)


def test_decision_evidence_requires_exact_ko_normalization() -> None:
    with pytest.raises(ValidationError, match="exact normalization"):
        DecisionEvidence(
            raw_ko="K00001",
            ko_id="K00002",
            raw_decision=None,
            score=None,
            score_type=None,
            threshold=None,
            threshold_rule=None,
        )


def test_decision_evidence_rejects_nonfinite_probability() -> None:
    with pytest.raises(ValidationError):
        DecisionEvidence(
            raw_ko="K00001",
            ko_id="K00001",
            raw_decision=None,
            score=math.nan,
            score_type=ScoreType.PROBABILITY,
            threshold=None,
            threshold_rule=None,
        )


@pytest.mark.parametrize("value", [2**63, float(2**63), float(2**64)])
def test_evidence_field_rejects_number_outside_signed_64_bit_range(
    value: int | float,
) -> None:
    with pytest.raises(ValidationError):
        EvidenceField(name="count", value=value)


@pytest.mark.parametrize(
    ("name", "value"),
    [("\ud800", "value"), ("note", "\ud800")],
)
def test_evidence_field_rejects_non_utf8_strings(name: str, value: str) -> None:
    with pytest.raises(ValidationError):
        EvidenceField(name=name, value=value)


def test_evidence_field_string_value_enforces_the_retained_evidence_hard_bound() -> None:
    maximum_value = "x" * MAX_EVIDENCE_STRING_CHARACTERS

    assert EvidenceField(name="note", value=maximum_value).value == maximum_value
    with pytest.raises(ValidationError):
        EvidenceField(name="note", value=f"{maximum_value}x")


def test_sequence_id_can_be_null_only_for_plain_ko_records() -> None:
    _, payload = _record_payload()
    source_value = payload["source"]
    assert isinstance(source_value, dict)
    source = cast(dict[str, object], source_value)
    source["importer_name"] = "generic_table"
    payload["source"] = source

    with pytest.raises(ValidationError, match="sequence_id may be null"):
        AnnotationRecord.model_validate(payload)


def test_dataset_schema_is_versioned_draft_2020_12_and_forbids_extra_fields() -> None:
    schema = AnnotationDataset.model_json_schema(mode="serialization")

    assert schema["$id"] == "urn:kegg-mcp:schema:annotation-dataset:2"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert "analysis_unit" in schema["required"]
    assert "import_report" in schema["required"]


@pytest.mark.parametrize(
    ("model", "schema_id"),
    [
        (SourceProvenance, "urn:kegg-mcp:schema:source-provenance:1"),
        (AnnotationRecord, "urn:kegg-mcp:schema:annotation-record:2"),
        (ImportReport, "urn:kegg-mcp:schema:import-report:2"),
        (KOEvidenceView, "urn:kegg-mcp:schema:ko-evidence-view:2"),
    ],
)
def test_top_level_contract_schemas_have_stable_ids(
    model: type[SourceProvenance | AnnotationRecord | ImportReport | KOEvidenceView],
    schema_id: str,
) -> None:
    schema = model.model_json_schema(mode="serialization")

    assert schema["$id"] == schema_id
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False


def test_evidence_field_numeric_schema_matches_runtime_range() -> None:
    schema = EvidenceField.model_json_schema(mode="validation")
    branches = schema["properties"]["value"]["anyOf"]
    numeric_branches = [
        branch for branch in branches if branch.get("type") in {"integer", "number"}
    ]

    assert len(numeric_branches) == 2
    assert all(branch["minimum"] == -(2**63) for branch in numeric_branches)
    assert all(branch["maximum"] == 2**63 - 1 for branch in numeric_branches)


def test_dataset_rejects_report_status_counts_that_do_not_match_records() -> None:
    dataset = import_plain_ko("K00001", limits=LIMITS)
    payload = dataset.model_dump()
    report = cast(dict[str, object], payload["import_report"])
    status_counts = cast(list[dict[str, object]], report["status_counts"])
    for item in status_counts:
        if item["status"] is NormalizedStatus.ACCEPTED:
            item["count"] = 0
        elif item["status"] is NormalizedStatus.REJECTED:
            item["count"] = 1

    with pytest.raises(ValidationError, match="status counts do not match"):
        AnnotationDataset.model_validate(payload)


def test_import_report_rejects_mapping_to_unretained_source_column() -> None:
    dataset = import_plain_ko("K00001", limits=LIMITS)
    payload = dataset.model_dump()
    report = cast(dict[str, object], payload["import_report"])
    report["column_mapping"] = ({"logical_field": "ko_id", "source_column": "absent"},)

    with pytest.raises(ValidationError, match="retained source columns"):
        AnnotationDataset.model_validate(payload)


def test_import_report_requires_retained_evidence_for_every_skipped_row() -> None:
    dataset = import_plain_ko("K00001", limits=LIMITS)
    payload = dataset.model_dump()
    report = cast(dict[str, object], payload["import_report"])
    report["input_rows"] = 2
    report["skipped_rows"] = 1

    with pytest.raises(ValidationError, match="retained unparsed rows"):
        AnnotationDataset.model_validate(payload)


def test_import_report_allows_multiple_records_from_one_parsed_source_row() -> None:
    dataset = import_plain_ko("K00001\nK00002", limits=LIMITS)
    payload = dataset.model_dump()
    report = cast(dict[str, object], payload["import_report"])
    report["input_rows"] = 1

    validated = AnnotationDataset.model_validate(payload)

    assert validated.import_report.input_rows == 1
    assert validated.import_report.emitted_records == 2


def test_import_report_rejects_records_without_a_parsed_source_row() -> None:
    dataset = import_plain_ko("K00001", limits=LIMITS)
    payload = dataset.model_dump()
    report = cast(dict[str, object], payload["import_report"])
    report["input_rows"] = 0

    with pytest.raises(ValidationError, match="require at least one parsed input row"):
        AnnotationDataset.model_validate(payload)


def test_ko_evidence_view_rejects_noncanonical_ko_sets() -> None:
    view = build_ko_evidence_view(import_plain_ko("K00002\nK00001", limits=LIMITS))
    payload = view.model_dump()
    payload["accepted_kos"] = ("K00002", "K00001", "K00001")

    with pytest.raises(ValidationError, match="sorted tuple of unique"):
        KOEvidenceView.model_validate(payload)
