"""Offline integration tests from imported evidence to accepted unique KO sets."""

from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS,
    AnnotationDataset,
    KOEvidenceView,
    NormalizedStatus,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    TableDialect,
    import_generic_table,
)


def test_generic_import_to_accepted_unique_view_is_stable_and_lossless() -> None:
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
        "p2,K00002,unclassified\n"
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
        policy=CANONICAL_SOURCE_STATUS,
        limits=limits,
    )
    before = dataset.model_dump_json()

    first = build_ko_evidence_view(dataset)
    second = build_ko_evidence_view(dataset)

    assert first == second
    assert first.accepted_kos == ("K00006",)
    assert first.rejected_kos == ("K00003", "K00006")
    assert select_ko_ids(first) == ("K00006",)
    assert next(
        item.count
        for item in first.status_counts
        if item.status is NormalizedStatus.UNCLASSIFIED
    ) == 1
    assert dataset.model_dump_json() == before
    assert AnnotationDataset.model_validate_json(before) == dataset
    assert KOEvidenceView.model_validate_json(first.model_dump_json()) == first
    assert first.records_by_sequence[0].record_ids == ("record-000001", "record-000002")
