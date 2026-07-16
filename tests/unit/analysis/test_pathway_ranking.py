"""Tests for deterministic server-side pathway ranking."""

from kegg_mcp.analysis import PathwaySelection, PathwaySelectionMode, rank_pathways
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS, EvidenceMode
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    TableDialect,
    import_generic_table,
    import_plain_ko,
)
from kegg_mcp.kegg.contracts import KeggPairRow

_LIMITS = ImportLimits(
    max_bytes=50_000,
    max_rows=100,
    max_columns=16,
    max_field_length=1_000,
)


def _row(source: str, target: str, line: int) -> KeggPairRow:
    return KeggPairRow(
        batch_index=0,
        line_number=line,
        source_id=f"ko:{source}",
        target_id=f"path:{target}",
    )


def test_duplicate_annotation_records_do_not_inflate_detected_nodes() -> None:
    dataset = import_plain_ko("K00001\nK00001\n", limits=_LIMITS)

    ranked = rank_pathways(
        dataset,
        (_row("K00001", "ko00020", 1),),
        EvidenceMode.STRICT,
    )

    assert ranked.selected_ko_ids == ("K00001",)
    assert ranked.rows[0].detected_unique_ko_count == 1
    assert ranked.rows[0].detected_ko_ids == ("K00001",)


def test_duplicate_link_rows_change_relationship_count_but_not_node_count() -> None:
    dataset = import_plain_ko("K00001\n", limits=_LIMITS)

    ranked = rank_pathways(
        dataset,
        (
            _row("K00001", "ko00020", 1),
            _row("K00001", "map00020", 2),
            _row("K00001", "ko00020", 3),
        ),
        EvidenceMode.STRICT,
    )

    assert len(ranked.rows) == 1
    assert ranked.rows[0].detected_unique_ko_count == 1
    assert ranked.rows[0].relationship_row_count == 3


def test_strict_and_lenient_ranking_use_only_their_selected_evidence() -> None:
    dataset = import_generic_table(
        ("sequence,ko,decision\np1,K00001,accepted\np2,K00002,uncertain\np3,K00003,rejected\n"),
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="decision",
        ),
        policy=CANONICAL_SOURCE_STATUS,
        limits=_LIMITS,
    )
    rows = (
        _row("K00001", "ko00030", 1),
        _row("K00002", "ko00020", 2),
        _row("K00003", "ko00010", 3),
    )

    strict = rank_pathways(dataset, rows, EvidenceMode.STRICT)
    lenient = rank_pathways(dataset, rows, EvidenceMode.LENIENT)

    assert [item.pathway_id for item in strict.rows] == ["ko00030"]
    assert [item.pathway_id for item in lenient.rows] == ["ko00020", "ko00030"]
    assert all(item.pathway_id != "ko00010" for item in (*strict.rows, *lenient.rows))


def test_ties_use_canonical_pathway_id_and_contracts_round_trip() -> None:
    dataset = import_plain_ko("K00001\nK00002\n", limits=_LIMITS)

    ranked = rank_pathways(
        dataset,
        (
            _row("K00001", "ko00020", 1),
            _row("K00002", "map00010", 2),
        ),
        EvidenceMode.STRICT,
    )

    assert [item.pathway_id for item in ranked.rows] == ["ko00010", "ko00020"]
    assert [item.rank for item in ranked.rows] == [1, 2]
    assert type(ranked).model_validate_json(ranked.model_dump_json()) == ranked
    selection = PathwaySelection(mode=PathwaySelectionMode.TOP_DETECTED, top_n=1)
    assert PathwaySelection.model_validate_json(selection.model_dump_json()) == selection
