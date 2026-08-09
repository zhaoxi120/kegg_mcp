"""Tests for deterministic server-side pathway and MODULE ranking."""

import json

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis import (
    MODULE_RANKING_METHOD,
    MODULE_RANKING_VERSION,
    PATHWAY_RANKING_METHOD,
    PATHWAY_RANKING_VERSION,
    ModuleRankingResult,
    ModuleSelection,
    PathwayRankingResult,
    PathwaySelection,
    rank_modules,
    rank_pathways,
)
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS
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


def _module_row(
    source: str,
    target: str,
    line: int,
    *,
    namespace: str = "md",
) -> KeggPairRow:
    return KeggPairRow(
        batch_index=0,
        line_number=line,
        source_id=f"ko:{source}",
        target_id=f"{namespace}:{target}",
    )


def _assert_current_ranking_identity(
    ranked: PathwayRankingResult | ModuleRankingResult,
    *,
    method: str,
    method_version: str,
) -> None:
    model = type(ranked)
    schema = model.model_json_schema()
    assert {"method", "method_version"}.issubset(schema["required"])
    assert ranked.method == method
    assert ranked.method_version == method_version
    for field in ("method", "method_version"):
        payload = ranked.model_dump(mode="json")
        del payload[field]
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(payload))

        payload = ranked.model_dump(mode="json")
        payload[field] = "unsupported"
        with pytest.raises(ValidationError):
            model.model_validate_json(json.dumps(payload))


def test_duplicate_annotation_records_do_not_inflate_detected_nodes() -> None:
    dataset = import_plain_ko("K00001\nK00001\n", limits=_LIMITS)

    ranked = rank_pathways(
        dataset,
        (_row("K00001", "ko00020", 1),),
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
    )

    assert len(ranked.rows) == 1
    assert ranked.rows[0].detected_unique_ko_count == 1
    assert ranked.rows[0].relationship_row_count == 3


def test_pathway_ranking_uses_only_accepted_evidence() -> None:
    dataset = import_generic_table(
        (
            "sequence,ko,decision\n"
            "p1,K00001,accepted\n"
            "p2,K00002,unclassified\n"
            "p3,K00003,rejected\n"
        ),
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

    ranked = rank_pathways(dataset, rows)

    assert [item.pathway_id for item in ranked.rows] == ["ko00030"]
    assert all(item.pathway_id != "ko00010" for item in ranked.rows)


def test_ties_use_canonical_pathway_id_and_contracts_round_trip() -> None:
    dataset = import_plain_ko("K00001\nK00002\n", limits=_LIMITS)

    ranked = rank_pathways(
        dataset,
        (
            _row("K00001", "ko00020", 1),
            _row("K00002", "map00010", 2),
        ),
    )

    assert [item.pathway_id for item in ranked.rows] == ["ko00010", "ko00020"]
    assert [item.rank for item in ranked.rows] == [1, 2]
    assert type(ranked).model_validate_json(ranked.model_dump_json()) == ranked
    _assert_current_ranking_identity(
        ranked,
        method=PATHWAY_RANKING_METHOD,
        method_version=PATHWAY_RANKING_VERSION,
    )
    selection = PathwaySelection(top_n=1)
    assert PathwaySelection.model_validate_json(selection.model_dump_json()) == selection


def test_automatic_selection_defaults_to_top_five_for_both_target_types() -> None:
    assert PathwaySelection().top_n == 5
    assert ModuleSelection().top_n == 5


def test_module_ranking_reuses_unique_selected_ko_semantics() -> None:
    dataset = import_plain_ko("K00001\nK00001\nK00002\n", limits=_LIMITS)

    ranked = rank_modules(
        dataset,
        (
            _module_row("K00001", "M00020", 1),
            _module_row("K00001", "M00020", 2, namespace="module"),
            _module_row("K00002", "M00010", 3),
        ),
    )

    assert [item.module_id for item in ranked.rows] == ["M00010", "M00020"]
    assert ranked.rows[1].detected_unique_ko_count == 1
    assert ranked.rows[1].relationship_row_count == 2
    assert {item.target_namespace for item in ranked.relationships} == {"md", "module"}


def test_module_ranking_excludes_rejected_and_unclassified_evidence() -> None:
    dataset = import_generic_table(
        (
            "sequence,ko,decision\n"
            "p1,K00001,accepted\n"
            "p2,K00002,unclassified\n"
            "p3,K00003,rejected\n"
        ),
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
        _module_row("K00001", "M00030", 1),
        _module_row("K00002", "M00020", 2),
        _module_row("K00003", "M00010", 3),
    )

    ranked = rank_modules(dataset, rows)

    assert [item.module_id for item in ranked.rows] == ["M00030"]
    assert all(item.module_id != "M00010" for item in ranked.rows)
    assert type(ranked).model_validate_json(ranked.model_dump_json()) == ranked
    _assert_current_ranking_identity(
        ranked,
        method=MODULE_RANKING_METHOD,
        method_version=MODULE_RANKING_VERSION,
    )
    selection = ModuleSelection(top_n=5)
    assert ModuleSelection.model_validate_json(selection.model_dump_json()) == selection
