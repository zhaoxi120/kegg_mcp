"""Offline integration tests from imported evidence to deterministic KO sets."""

from typing import cast

import pytest

from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS_V1,
    AnnotationDataset,
    ErrorCode,
    EvidenceMode,
    KeggMcpError,
    KOEvidenceView,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    TableDialect,
    import_generic_table,
)


def test_generic_import_to_strict_and_lenient_view_is_stable_and_lossless() -> None:
    limits = ImportLimits(
        max_bytes=10_000,
        max_rows=100,
        max_columns=20,
        max_field_length=1_000,
    )
    payload = (
        "sequence,ko,decision\n"
        "p1,K00006,rejected\n"
        "p1,K00006,accepted\n"
        "p2,K00002,uncertain\n"
        "p3,K00003,rejected\n"
    )
    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="decision",
        ),
        policy=CANONICAL_SOURCE_STATUS_V1,
        limits=limits,
    )
    before = dataset.model_dump_json()

    first = build_ko_evidence_view(dataset)
    second = build_ko_evidence_view(dataset)

    assert first == second
    assert first.accepted_kos == ("K00006",)
    assert first.rejected_kos == ("K00003", "K00006")
    assert select_ko_ids(first, EvidenceMode.STRICT) == ("K00006",)
    assert select_ko_ids(first, EvidenceMode.LENIENT) == ("K00002", "K00006")
    assert dataset.model_dump_json() == before
    assert AnnotationDataset.model_validate_json(before) == dataset
    assert KOEvidenceView.model_validate_json(first.model_dump_json()) == first
    assert first.records_by_sequence[0].record_ids == ("record-000001", "record-000002")

    with pytest.raises(KeggMcpError) as error:
        select_ko_ids(first, cast(EvidenceMode, "strict"))

    assert error.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
