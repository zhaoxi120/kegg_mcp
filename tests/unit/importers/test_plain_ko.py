"""Tests for lossless plain K-number list import."""

from typing import cast

import pytest

from kegg_mcp.domain import (
    AnalysisUnit,
    DiagnosticCode,
    ErrorCode,
    EvidenceField,
    EvidenceMode,
    KeggMcpError,
    NormalizedStatus,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.importers import ImportLimits, SourceProvenanceInput, import_plain_ko

LIMITS = ImportLimits(
    max_bytes=10_000,
    max_rows=100,
    max_columns=32,
    max_field_length=1_000,
)


def test_plain_import_preserves_raw_rows_and_reports_invalid_and_duplicates() -> None:
    payload = " K00001\nko:K00002\nK00001\nBAD\n"

    dataset = import_plain_ko(
        payload,
        limits=LIMITS,
        analysis_unit=AnalysisUnit.ISOLATE_PROTEOME,
    )
    view = build_ko_evidence_view(dataset)

    assert [record.raw_ko for record in dataset.records] == [
        " K00001",
        "ko:K00002",
        "K00001",
        "BAD",
    ]
    assert [record.normalized_status for record in dataset.records] == [
        NormalizedStatus.ACCEPTED,
        NormalizedStatus.ACCEPTED,
        NormalizedStatus.ACCEPTED,
        NormalizedStatus.INVALID,
    ]
    assert dataset.records[3].ko_id is None
    assert dataset.import_report.duplicate_count == 1
    assert dataset.import_report.count_for(NormalizedStatus.INVALID) == 1
    assert view.accepted_kos == ("K00001", "K00002")
    assert select_ko_ids(view, EvidenceMode.STRICT) == ("K00001", "K00002")
    assert select_ko_ids(view, EvidenceMode.LENIENT) == ("K00001", "K00002")
    assert dataset.analysis_unit is AnalysisUnit.ISOLATE_PROTEOME


def test_plain_import_accepts_bom_and_crlf_without_digest_provenance() -> None:
    payload = b"\xef\xbb\xbfK00001\r\nK00002\r\n"

    dataset = import_plain_ko(payload, limits=LIMITS)

    assert dataset.records[0].raw_ko == "K00001"
    assert "input_sha256" not in dataset.records[0].source.model_dump()


def test_plain_import_empty_input_is_explicit() -> None:
    dataset = import_plain_ko(" \n\n", limits=LIMITS)

    assert dataset.records == ()
    assert dataset.sources[0].source_name == "manual"
    assert "input_sha256" not in dataset.sources[0].model_dump()
    assert dataset.import_report.input_rows == 0
    assert dataset.import_report.diagnostics[0].code is DiagnosticCode.EMPTY_INPUT


@pytest.mark.parametrize(
    ("limits", "payload"),
    [
        (
            ImportLimits(max_bytes=2, max_rows=10, max_columns=2, max_field_length=10),
            "K00001",
        ),
        (
            ImportLimits(max_bytes=100, max_rows=1, max_columns=2, max_field_length=10),
            "K00001\nK00002",
        ),
        (
            ImportLimits(max_bytes=100, max_rows=10, max_columns=2, max_field_length=3),
            "K00001",
        ),
    ],
)
def test_plain_import_enforces_caller_selected_limits(limits: ImportLimits, payload: str) -> None:
    with pytest.raises(KeggMcpError) as error:
        import_plain_ko(payload, limits=limits)

    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_plain_import_rejects_unpaired_surrogate_as_structured_error() -> None:
    with pytest.raises(KeggMcpError) as error:
        import_plain_ko("\ud800", limits=LIMITS)

    assert error.value.detail.code is ErrorCode.UNSUPPORTED_INPUT_FORMAT


def test_plain_import_rejects_unsupported_runtime_payload_type() -> None:
    with pytest.raises(KeggMcpError) as error:
        import_plain_ko(cast(str | bytes, object()), limits=LIMITS)

    assert error.value.detail.code is ErrorCode.UNSUPPORTED_INPUT_FORMAT


def test_plain_import_enforces_metadata_field_count_before_model_validation() -> None:
    metadata = tuple(EvidenceField(name=f"field_{index}", value="value") for index in range(129))

    with pytest.raises(KeggMcpError) as error:
        import_plain_ko("K00001", limits=LIMITS, metadata=metadata)

    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_plain_import_applies_field_limit_to_source_provenance() -> None:
    limits = ImportLimits(max_bytes=100, max_rows=10, max_columns=2, max_field_length=3)
    source = SourceProvenanceInput(source_name="src", source_version="1234")

    with pytest.raises(KeggMcpError) as error:
        import_plain_ko("K00001", limits=limits, source=source)

    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_plain_import_rejects_invalid_sample_label_as_structured_error() -> None:
    with pytest.raises(KeggMcpError) as error:
        import_plain_ko("K00001", limits=LIMITS, sample_id="\ud800")

    assert error.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
