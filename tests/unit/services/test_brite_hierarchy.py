"""Tests for bounded, source-backed BRITE hierarchy mapping."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggBriteHtextDocument,
    KeggOperation,
    KeggPairRow,
    KeggRequestOptions,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services import brite_hierarchy, query_support
from kegg_mcp.services.brite_hierarchy import (
    BRITE_DETAIL_SCHEMA_VERSION,
    BRITE_DETAIL_SECTION,
    BRITE_TABLE_SECTION,
    MapBriteHierarchyRequest,
    map_brite_hierarchy,
)
from kegg_mcp.services.kegg_relations import BoundedRelationResult
from kegg_mcp.services.query_models import KeggEntityKind, KeggEntityRef
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.result_store import SQLiteResultStore

_NOW = datetime(2026, 7, 30, 4, 0, tzinfo=UTC)


def _provenance(operation: KeggOperation, response_bytes: int = 100) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        response_bytes=response_bytes,
        parser_name="pair_table" if operation is KeggOperation.LINK else "brite_htext",
        parser_version=PARSER_VERSION,
        database_release="Release 119.0+/07-30",
        attempt_count=1,
        is_stale=False,
    )


class _GetClient:
    def __init__(self, documents: tuple[KeggBriteHtextDocument, ...]) -> None:
        self.documents = documents
        self.get_requests: list[GetRequest] = []
        self.options: list[KeggRequestOptions | None] = []

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        self.get_requests.append(request)
        self.options.append(options)
        requested = {entry.identifier for entry in request.entries}
        documents = tuple(
            document for document in self.documents if document.identifier in requested
        )
        returned = {document.identifier for document in documents if document.lines}
        return GetResult(
            request=request,
            documents=documents,
            missing_entries=tuple(
                entry for entry in request.entries if entry.identifier not in returned
            ),
            batches=(_provenance(KeggOperation.GET),),
        )

    def link(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("explicit BRITE mapping must not call LINK")


def _ko(identifier: str) -> KeggEntityRef:
    return KeggEntityRef(kind=KeggEntityKind.KO, identifier=identifier)


def _document(identifier: str, *lines: str) -> KeggBriteHtextDocument:
    return KeggBriteHtextDocument(identifier=identifier, lines=lines)


def _artifact_bytes(
    store: SQLiteResultStore,
    result_id: str,
    section: str,
) -> bytes:
    page = store.read_artifact(
        "scope",
        result_id,
        section,
        offset=0,
        limit=store.limits.max_range_bytes,
    )
    assert page.next_offset is None
    return page.content


def test_explicit_hierarchy_preserves_multi_parent_paths_and_source_ids(
    tmp_path: Path,
) -> None:
    document = _document(
        "ko00001",
        "+D\tKO hierarchy",
        "!",
        "A09100 Metabolism",
        "B  Carbohydrate metabolism",
        "C    00010 Glycolysis",
        "D      K00001 first enzyme",
        "B  Alternative functions",
        "C    Unnumbered category",
        "D      K00001 first enzyme",
        "D      K00002 second enzyme",
    )
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result = map_brite_hierarchy(
        MapBriteHierarchyRequest(
            entity_ids=(_ko("K00001"), _ko("K00002"), _ko("K00003")),
            brite_ids=("ko00001",),
            preview_limit=1,
        ),
        client=cast(KeggPrimitiveClient, _GetClient((document,))),
        result_store=store,
        scope_id="scope",
    )

    assert result.path_count == 3
    assert result.paths_truncated is True
    assert result.unmatched_count == 1
    assert result.unmatched_preview == (_ko("K00003"),)
    assert result.unmatched_truncated is False
    assert result.classifications_truncated is True
    assert result.artifacts[0].section == BRITE_DETAIL_SECTION
    assert result.artifacts[1].section == BRITE_TABLE_SECTION

    detail = json.loads(_artifact_bytes(store, result.result.result_id, BRITE_DETAIL_SECTION))
    assert detail["schema_version"] == BRITE_DETAIL_SCHEMA_VERSION
    k1_paths = [path for path in detail["paths"] if path["input_entity"]["identifier"] == "K00001"]
    assert len(k1_paths) == 2
    assert k1_paths[0]["nodes"][0] == {
        "depth": 0,
        "level": "A",
        "node_id": "09100",
        "name": "Metabolism",
        "is_input_entity": False,
    }
    assert k1_paths[0]["nodes"][1]["node_id"] is None
    assert k1_paths[0]["nodes"][-1]["node_id"] == "K00001"
    root_count = next(
        item
        for item in detail["classifications"]
        if len(item["path"]) == 1 and item["path"][0]["node_id"] == "09100"
    )
    assert root_count["unique_input_count"] == 2
    assert "unique-input counts" in detail["count_semantics"]

    table = _artifact_bytes(
        store,
        result.result.result_id,
        BRITE_TABLE_SECTION,
    ).decode("utf-8")
    assert table.startswith("record_type\tinput_kind\tinput_identifier\tbrite_id")
    assert "\nunmatched\tko\tK00003\t" in table


def test_selected_resolved_and_missing_brite_identifiers_are_distinct(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "missing-results.sqlite3")
    result = map_brite_hierarchy(
        MapBriteHierarchyRequest(
            entity_ids=(_ko("K00001"),),
            brite_ids=("ko00001", "br08901"),
        ),
        client=cast(
            KeggPrimitiveClient,
            _GetClient(
                (
                    _document(
                        "ko00001",
                        "A09100 Metabolism",
                        "B  K00001 first",
                    ),
                )
            ),
        ),
        result_store=store,
        scope_id="scope",
    )

    assert result.selected_brite_count == 2
    assert result.selected_brite_ids == ("ko00001", "br08901")
    assert result.resolved_brite_count == 1
    assert result.resolved_brite_ids == ("ko00001",)
    assert result.missing_brite_ids == ("br08901",)
    detail = json.loads(_artifact_bytes(store, result.result.result_id, BRITE_DETAIL_SECTION))
    assert detail["selected_brite_ids"] == ["ko00001", "br08901"]
    assert detail["resolved_brite_ids"] == ["ko00001"]
    assert detail["missing_brite_ids"] == ["br08901"]


def test_first_path_and_unmatched_exclusion_are_request_controlled(
    tmp_path: Path,
) -> None:
    client = _GetClient(
        (
            _document(
                "ko00001",
                "+C\tKO hierarchy",
                "A09100 Metabolism",
                "B  First branch",
                "C    K00001 first",
                "B  Second branch",
                "C    K00001 second",
            ),
        )
    )
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result = map_brite_hierarchy(
        MapBriteHierarchyRequest(
            entity_ids=(_ko("K00001"), _ko("K00002")),
            brite_ids=("ko00001",),
            include_all_paths=False,
            include_unmatched=False,
        ),
        client=cast(KeggPrimitiveClient, client),
        result_store=store,
        scope_id="scope",
    )

    assert result.path_count == 1
    assert result.path_preview[0].nodes[1].name == "First branch"
    assert result.unmatched_count == 1
    assert result.unmatched_preview == ()
    assert result.unmatched_truncated is True
    assert result.unmatched_included is False
    detail = json.loads(_artifact_bytes(store, result.result.result_id, BRITE_DETAIL_SECTION))
    assert detail["unmatched_entities"] == []
    table = _artifact_bytes(
        store,
        result.result.result_id,
        BRITE_TABLE_SECTION,
    ).decode("utf-8")
    assert "\nunmatched\t" not in table


def test_ko_only_automatic_discovery_uses_bounded_relation_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def discover(
        source_identifiers: tuple[str, ...],
        **kwargs: object,
    ) -> BoundedRelationResult:
        captured["source_identifiers"] = source_identifiers
        captured.update(kwargs)
        return BoundedRelationResult(
            rows=(
                KeggPairRow(
                    batch_index=0,
                    line_number=1,
                    source_id="ko:K00001",
                    target_id="br:ko00001",
                ),
                KeggPairRow(
                    batch_index=0,
                    line_number=2,
                    source_id="ko:K00002",
                    target_id="br:br08901",
                ),
            ),
            batches=(_provenance(KeggOperation.LINK),),
        )

    monkeypatch.setattr(brite_hierarchy, "bounded_relation_batches", discover)
    client = _GetClient(
        (
            _document(
                "ko00001",
                "+B\tKO",
                "A09100 Metabolism",
                "B  K00001 first",
            ),
            _document(
                "br08901",
                "+B\tKO",
                "A09180 Brite category",
                "B  K00002 second",
            ),
        )
    )
    options = KeggRequestOptions(refresh=False, allow_stale=True)
    result = map_brite_hierarchy(
        MapBriteHierarchyRequest(entity_ids=(_ko("K00001"), _ko("K00002"))),
        client=cast(KeggPrimitiveClient, client),
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id="scope",
        options=options,
    )

    assert captured["source_identifiers"] == ("K00001", "K00002")
    assert captured["relationship"] is brite_hierarchy.KeggLinkRelationship.KO_TO_BRITE
    assert captured["options"] == options
    assert result.resolved_brite_ids == ("ko00001", "br08901")
    assert result.path_count == 2
    assert [
        entry.identifier for get_request in client.get_requests for entry in get_request.entries
    ] == ["ko00001", "br08901"]
    assert client.options == [options, options]


def test_explicit_hierarchy_accepts_non_ko_entities_and_retains_source_identifier(
    tmp_path: Path,
) -> None:
    compound = KeggEntityRef(kind=KeggEntityKind.COMPOUND, identifier="C00001")
    result = map_brite_hierarchy(
        MapBriteHierarchyRequest(
            entity_ids=(compound,),
            brite_ids=("br08001",),
        ),
        client=cast(
            KeggPrimitiveClient,
            _GetClient(
                (
                    _document(
                        "br08001",
                        "+B\tCompounds",
                        "A09100 Compounds",
                        "B  C00001 Water",
                    ),
                )
            ),
        ),
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id="scope",
    )

    assert result.path_count == 1
    terminal = result.path_preview[0].nodes[-1]
    assert terminal.node_id == "C00001"
    assert terminal.is_input_entity is True

    gene = KeggEntityRef(kind=KeggEntityKind.GENE, identifier="hsa:1234")
    gene_result = map_brite_hierarchy(
        MapBriteHierarchyRequest(
            entity_ids=(gene,),
            brite_ids=("br08001",),
        ),
        client=cast(
            KeggPrimitiveClient,
            _GetClient(
                (
                    _document(
                        "br08001",
                        "+B\tGenes",
                        "A09100 Genes",
                        "B\thsa:1234 example gene",
                    ),
                )
            ),
        ),
        result_store=SQLiteResultStore(tmp_path / "gene-results.sqlite3"),
        scope_id="scope",
    )

    assert gene_result.path_preview[0].nodes[-1].node_id == "hsa:1234"


@pytest.mark.parametrize("formula_text", ["=1+1", "+1", "-1", "@cmd", "'quoted"])
def test_brite_tsv_escapes_spreadsheet_formulas_without_altering_retained_json(
    tmp_path: Path,
    formula_text: str,
) -> None:
    document = _document(
        "ko00001",
        "A09100 Metabolism",
        f"B  K00001 {formula_text}",
    )
    store = SQLiteResultStore(tmp_path / f"formula-{ord(formula_text[0])}.sqlite3")

    result = map_brite_hierarchy(
        MapBriteHierarchyRequest(
            entity_ids=(_ko("K00001"),),
            brite_ids=("ko00001",),
        ),
        client=cast(KeggPrimitiveClient, _GetClient((document,))),
        result_store=store,
        scope_id="scope",
    )

    detail = json.loads(_artifact_bytes(store, result.result.result_id, BRITE_DETAIL_SECTION))
    assert detail["paths"][0]["nodes"][-1]["name"] == formula_text
    table = _artifact_bytes(
        store,
        result.result.result_id,
        BRITE_TABLE_SECTION,
    ).decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(table), delimiter="\t"))
    assert rows[-1]["node_name"] == f"'{formula_text}"


def test_brite_direct_preview_truncates_node_text_and_stays_bounded(
    tmp_path: Path,
) -> None:
    lines = (
        *(f"{chr(ord('A') + depth)}  {'x' * 2_000}" for depth in range(25)),
        f"Z  K00001 {'y' * 1_993}",
    )
    store = SQLiteResultStore(tmp_path / "large-direct.sqlite3")

    result = map_brite_hierarchy(
        MapBriteHierarchyRequest(
            entity_ids=(_ko("K00001"),),
            brite_ids=("ko00001",),
        ),
        client=cast(
            KeggPrimitiveClient,
            _GetClient((_document("ko00001", *lines),)),
        ),
        result_store=store,
        scope_id="scope",
    )

    assert len(result.path_preview[0].nodes) == 26
    assert all(len(node.name) <= 128 for node in result.path_preview[0].nodes)
    assert all(node.name_truncated for node in result.path_preview[0].nodes)
    assert result.retrieval.batch_count == 1
    assert len(result.model_dump_json().encode("utf-8")) <= query_support.MAX_QUERY_DIRECT_BYTES
    detail = json.loads(_artifact_bytes(store, result.result.result_id, BRITE_DETAIL_SECTION))
    assert len(detail["paths"][0]["nodes"][0]["name"]) == 2_000


def test_request_rejects_unsafe_discovery_and_identifier_shapes() -> None:
    compound = KeggEntityRef(kind=KeggEntityKind.COMPOUND, identifier="C00001")
    with pytest.raises(ValidationError, match="only KO"):
        MapBriteHierarchyRequest(entity_ids=(compound,))
    with pytest.raises(ValidationError, match="supported BRITE"):
        MapBriteHierarchyRequest(
            entity_ids=(compound,),
            brite_ids=("K00001",),
        )
    with pytest.raises(ValidationError, match="unique"):
        MapBriteHierarchyRequest(
            entity_ids=(_ko("K00001"), _ko("K00001")),
            brite_ids=("ko00001",),
        )


def test_non_contiguous_htext_path_fails_without_retaining_output(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    with pytest.raises(KeggMcpError) as caught:
        map_brite_hierarchy(
            MapBriteHierarchyRequest(
                entity_ids=(_ko("K00001"),),
                brite_ids=("ko00001",),
            ),
            client=cast(
                KeggPrimitiveClient,
                _GetClient(
                    (
                        _document(
                            "ko00001",
                            "+C\tKO",
                            "A09100 Metabolism",
                            "C    K00001 missing parent",
                        ),
                    )
                ),
            ),
            result_store=store,
            scope_id="scope",
        )

    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert store.list_results("scope").total_items == 0


def test_path_and_response_caps_fail_closed_before_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(
        "ko00001",
        "+C\tKO",
        "A09100 Metabolism",
        "B  First",
        "C    K00001 first",
        "B  Second",
        "C    K00001 second",
    )
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    monkeypatch.setattr(brite_hierarchy, "MAX_BRITE_PATHS", 1)
    with pytest.raises(KeggMcpError) as path_error:
        map_brite_hierarchy(
            MapBriteHierarchyRequest(
                entity_ids=(_ko("K00001"),),
                brite_ids=("ko00001",),
            ),
            client=cast(KeggPrimitiveClient, _GetClient((document,))),
            result_store=store,
            scope_id="scope",
        )
    assert path_error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert store.list_results("scope").total_items == 0

    monkeypatch.setattr(brite_hierarchy, "MAX_BRITE_PATHS", 10_000)
    monkeypatch.setattr(brite_hierarchy, "MAX_BRITE_SOURCE_CHARACTERS", 1)
    with pytest.raises(KeggMcpError) as response_error:
        map_brite_hierarchy(
            MapBriteHierarchyRequest(
                entity_ids=(_ko("K00001"),),
                brite_ids=("ko00001",),
            ),
            client=cast(KeggPrimitiveClient, _GetClient((document,))),
            result_store=store,
            scope_id="scope",
        )
    assert response_error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert store.list_results("scope").total_items == 0


def test_hierarchy_response_budget_stops_after_the_failing_endpoint_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _GetClient(
        tuple(
            _document(
                identifier,
                "+B\tKO",
                "A09100 Metabolism",
                "B  K00001 first",
            )
            for identifier in ("ko00001", "ko00002", "ko00003")
        )
    )
    monkeypatch.setattr(brite_hierarchy, "MAX_BRITE_TOTAL_RESPONSE_BYTES", 150)

    with pytest.raises(KeggMcpError) as caught:
        map_brite_hierarchy(
            MapBriteHierarchyRequest(
                entity_ids=(_ko("K00001"),),
                brite_ids=("ko00001", "ko00002", "ko00003"),
            ),
            client=cast(KeggPrimitiveClient, client),
            result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
            scope_id="scope",
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert len(client.get_requests) == 2


def test_detail_construction_failure_leaves_no_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(
        "ko00001",
        "+B\tKO",
        "A09100 Metabolism",
        "B  K00001 first",
    )
    store = SQLiteResultStore(tmp_path / "results.sqlite3")

    def fail_table(*args: object, **kwargs: object) -> bytes:
        raise RuntimeError("table construction failed")

    monkeypatch.setattr(brite_hierarchy, "_tsv_bytes", fail_table)
    with pytest.raises(RuntimeError, match="table construction"):
        map_brite_hierarchy(
            MapBriteHierarchyRequest(
                entity_ids=(_ko("K00001"),),
                brite_ids=("ko00001",),
            ),
            client=cast(KeggPrimitiveClient, _GetClient((document,))),
            result_store=store,
            scope_id="scope",
        )
    assert store.list_results("scope").total_items == 0


def test_brite_direct_result_cap_compensates_the_retained_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _document(
        "ko00001",
        "A09100 Metabolism",
        "B  K00001 first",
    )
    store = SQLiteResultStore(tmp_path / "direct-cap.sqlite3")
    monkeypatch.setattr(query_support, "MAX_QUERY_DIRECT_BYTES", 1)

    with pytest.raises(KeggMcpError) as caught:
        map_brite_hierarchy(
            MapBriteHierarchyRequest(
                entity_ids=(_ko("K00001"),),
                brite_ids=("ko00001",),
            ),
            client=cast(KeggPrimitiveClient, _GetClient((document,))),
            result_store=store,
            scope_id="scope",
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert store.list_results("scope").total_items == 0
