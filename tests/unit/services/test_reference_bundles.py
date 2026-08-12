"""Tests for durable, bounded KEGG reference bundles."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services import reference_bundles
from kegg_mcp.services.brite_hierarchy import (
    BRITE_DETAIL_MIME_TYPE,
    BRITE_DETAIL_SCHEMA_VERSION,
    BRITE_DETAIL_SECTION,
    BriteClassificationCount,
    BriteHierarchyDetail,
    BriteHierarchyNode,
    BriteHierarchyPath,
    MapBriteHierarchyRequest,
)
from kegg_mcp.services.entry_cards import (
    ENTRY_CARD_PARSER_NAME,
    ENTRY_CARD_PARSER_VERSION,
    ENTRY_CARD_SCHEMA_VERSION,
    ENTRY_CARD_SNAPSHOT_SECTION,
    KeggEntryCardDbLink,
    KeggEntryCardEntity,
    KeggEntryCardKind,
    KeggEntryCardReference,
    KeggEntryCardSnapshot,
    KoEntryCard,
)
from kegg_mcp.services.query_models import KeggEntityKind, KeggEntityRef
from kegg_mcp.services.reference_bundles import (
    REFERENCE_BRITE_PATHS_NAME,
    REFERENCE_BUNDLE_SCHEMA_VERSION,
    REFERENCE_MANIFEST_NAME,
    REFERENCE_RELATIONSHIPS_NAME,
    REFERENCE_SNAPSHOT_NAME,
    KeggReferenceBundle,
    ReferenceBundleManifest,
    ReferenceBundleSource,
    WriteKeggReferenceBundleRequest,
    write_kegg_reference_bundle,
)
from kegg_mcp.services.result_store import ResultArtifactInput, SQLiteResultStore

_NOW = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
_SECRET_REQUEST = "https://licensed.invalid/private/get?token=do-not-export"
_SECRET_ENDPOINT_LABEL = "private-license-endpoint"


def _entity(kind: KeggEntryCardKind, identifier: str) -> KeggEntryCardEntity:
    return KeggEntryCardEntity(database=kind, identifier=identifier)


def _provenance() -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=KeggOperation.GET,
        request_key=_SECRET_REQUEST,
        access_mode=AccessMode.LICENSED,
        retrieval_endpoint_class=RetrievalEndpointClass.LICENSED,
        endpoint_label=_SECRET_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        response_bytes=321,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release test",
        attempt_count=1,
        is_stale=False,
    )


def _snapshot() -> KeggEntryCardSnapshot:
    ko = KoEntryCard(
        entity=_entity(KeggEntryCardKind.KO, "K00001"),
        names=("Synthetic KO",),
        definition="Synthetic definition",
        dblinks=(
            KeggEntryCardDbLink(
                database="NCBI-GeneID",
                identifiers=("123",),
            ),
        ),
        pubmed_ids=("12345678",),
        modules=(
            KeggEntryCardReference(
                identifier="M00001",
                label="=untrusted spreadsheet formula",
            ),
        ),
        pathways=(
            KeggEntryCardReference(
                identifier="ko00010",
                label="Synthetic pathway",
            ),
        ),
    )
    pathway = _entity(KeggEntryCardKind.PATHWAY, "ko99999")
    return KeggEntryCardSnapshot(
        schema_version=ENTRY_CARD_SCHEMA_VERSION,
        parser_name=ENTRY_CARD_PARSER_NAME,
        parser_version=ENTRY_CARD_PARSER_VERSION,
        response_parser_version=PARSER_VERSION,
        requested_entries=(ko.entity, pathway),
        entries=(ko,),
        missing_entries=(pathway,),
        provenance=(_provenance(),),
    )


def _retain(
    store: SQLiteResultStore,
    snapshot: KeggEntryCardSnapshot | bytes,
    *,
    scope_id: str = "reference-scope",
    mime_type: str = "application/json",
) -> str:
    content = (
        snapshot.model_dump_json().encode("utf-8")
        if isinstance(snapshot, KeggEntryCardSnapshot)
        else snapshot
    )
    result = store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=ENTRY_CARD_SNAPSHOT_SECTION,
                mime_type=mime_type,
                content=content,
            ),
        ),
    )
    return result.result_id


def _request(result_id: str) -> WriteKeggReferenceBundleRequest:
    return WriteKeggReferenceBundleRequest(
        source=ReferenceBundleSource(result_id=result_id),
    )


def _brite_detail() -> BriteHierarchyDetail:
    entity = KeggEntityRef(kind=KeggEntityKind.KO, identifier="K00001")
    root = BriteHierarchyNode(
        depth=0,
        level="A",
        node_id="09100",
        name="Metabolism",
    )
    leaf = BriteHierarchyNode(
        depth=1,
        level="B",
        node_id="K00001",
        name="=untrusted BRITE node",
        is_input_entity=True,
    )
    return BriteHierarchyDetail(
        schema_version=BRITE_DETAIL_SCHEMA_VERSION,
        request=MapBriteHierarchyRequest(
            entity_ids=(entity,),
            brite_ids=("ko00001",),
        ),
        selected_brite_ids=("ko00001",),
        resolved_brite_ids=("ko00001",),
        missing_brite_ids=(),
        paths=(
            BriteHierarchyPath(
                input_entity=entity,
                brite_id="ko00001",
                nodes=(root, leaf),
            ),
        ),
        classifications=(
            BriteClassificationCount(
                brite_id="ko00001",
                path=(root,),
                unique_input_count=1,
            ),
            BriteClassificationCount(
                brite_id="ko00001",
                path=(root, leaf),
                unique_input_count=1,
            ),
        ),
        unmatched_entities=(),
        hierarchy_provenance=(_provenance(),),
    )


def _retain_brite(
    store: SQLiteResultStore,
    detail: BriteHierarchyDetail | bytes,
    *,
    scope_id: str = "reference-scope",
    mime_type: str = BRITE_DETAIL_MIME_TYPE,
) -> str:
    content = (
        detail.model_dump_json().encode("utf-8")
        if isinstance(detail, BriteHierarchyDetail)
        else detail
    )
    result = store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=BRITE_DETAIL_SECTION,
                mime_type=mime_type,
                content=content,
            ),
        ),
    )
    return result.result_id


def test_writes_committed_reference_bundle_with_portable_artifact_metadata(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "reference-bundle"

    result = write_kegg_reference_bundle(
        _request(result_id),
        output_directory=output,
        result_store=store,
        scope_id="reference-scope",
    )

    expected_names = (
        REFERENCE_SNAPSHOT_NAME,
        REFERENCE_RELATIONSHIPS_NAME,
        REFERENCE_MANIFEST_NAME,
    )
    assert tuple(item.name for item in result.artifacts) == expected_names
    assert {path.name for path in output.iterdir()} == set(expected_names)
    assert result.requested_entry_count == 2
    assert result.returned_entry_count == 1
    assert result.missing_entry_count == 1
    assert result.relationship_count == 4
    assert result.total_bytes == sum(path.stat().st_size for path in output.iterdir())
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir())
    assert result.schema_version == REFERENCE_BUNDLE_SCHEMA_VERSION

    manifest = json.loads((output / REFERENCE_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2"
    assert manifest["bundle_type"] == "kegg_reference"
    assert manifest["brite"] == {
        "missing_brite_count": 0,
        "path_count": 0,
        "resolved_brite_count": 0,
        "row_count": 0,
        "selected_brite_count": 0,
        "status": "not_requested",
        "unmatched_entity_count": 0,
    }
    assert manifest["selection"] == {
        "entity_types": ["ko", "pathway"],
        "missing_entry_count": 1,
        "relationship_count": 4,
        "requested_entry_count": 2,
        "returned_entry_count": 1,
        "source_requested_entry_count": 2,
        "truncated": False,
    }
    for record in manifest["artifacts"]:
        content = (output / record["name"]).read_bytes()
        assert record["byte_size"] == len(content)
        assert set(record) == {"byte_size", "mime_type", "name"}
        assert record["mime_type"] in {
            "application/json",
            "text/tab-separated-values; charset=utf-8",
        }

    reference_snapshot = json.loads((output / REFERENCE_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert set(reference_snapshot) == {
        "schema_version",
        "source_schema",
        "request",
        "entries",
        "missing_entries",
        "brite",
        "retrieval",
    }
    assert reference_snapshot["schema_version"] == "1"
    assert reference_snapshot["source_schema"] == {
        "card_parser_name": "kegg_flat_file_entry_card",
        "card_parser_version": "1",
        "card_schema_version": "1",
        "response_parser_version": PARSER_VERSION,
    }
    assert reference_snapshot["request"] == {
        "operation": "get",
        "projection": "card",
        "entries": [
            {"database": "ko", "identifier": "K00001"},
            {"database": "pathway", "identifier": "ko99999"},
        ],
    }
    assert reference_snapshot["entries"][0]["entity"]["identifier"] == "K00001"
    assert reference_snapshot["missing_entries"] == [
        {"database": "pathway", "identifier": "ko99999"}
    ]
    assert reference_snapshot["brite"] is None
    relationships = (output / REFERENCE_RELATIONSHIPS_NAME).read_text(encoding="utf-8")
    assert "module\tM00001\t'=untrusted spreadsheet formula" in relationships

    retrieval = reference_snapshot["retrieval"]
    assert set(retrieval) == {
        "entry_batches",
        "brite_relation_batches",
        "brite_hierarchy_batches",
    }
    assert retrieval["brite_relation_batches"] == []
    assert retrieval["brite_hierarchy_batches"] == []
    assert set(retrieval["entry_batches"][0]) == {
        "access_mode",
        "attempt_count",
        "batch_index",
        "cache_lookup_state",
        "database_release",
        "expires_at",
        "is_stale",
        "operation",
        "origin",
        "parser_name",
        "parser_version",
        "response_bytes",
        "retrieval_endpoint_class",
        "retrieved_at",
        "served_at",
    }

    combined = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    assert _SECRET_REQUEST not in combined
    assert _SECRET_ENDPOINT_LABEL not in combined
    assert result_id not in combined
    assert str(output) not in combined


def test_reference_bundle_outputs_require_current_wire_identity(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "required-schema.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "required-schema"
    result = write_kegg_reference_bundle(
        _request(result_id),
        output_directory=output,
        result_store=store,
        scope_id="reference-scope",
    )

    bundle_payload = result.model_dump(mode="json")
    del bundle_payload["schema_version"]
    with pytest.raises(ValidationError):
        KeggReferenceBundle.model_validate(bundle_payload)

    for path in (
        ("schema_version",),
        ("bundle_type",),
        ("producer", "name"),
    ):
        manifest_payload = json.loads(
            (output / REFERENCE_MANIFEST_NAME).read_text(encoding="utf-8")
        )
        parent = manifest_payload
        for component in path[:-1]:
            parent = parent[component]
        del parent[path[-1]]
        with pytest.raises(ValidationError):
            ReferenceBundleManifest.model_validate(manifest_payload)


def test_exports_complete_validated_brite_paths_without_network_access(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    brite_result_id = _retain_brite(store, _brite_detail())
    output = tmp_path / "with-brite"

    result = write_kegg_reference_bundle(
        WriteKeggReferenceBundleRequest(
            source=ReferenceBundleSource(result_id=result_id),
            brite_source=ReferenceBundleSource(result_id=brite_result_id),
        ),
        output_directory=output,
        result_store=store,
        scope_id="reference-scope",
    )

    assert tuple(item.name for item in result.artifacts) == (
        REFERENCE_SNAPSHOT_NAME,
        REFERENCE_RELATIONSHIPS_NAME,
        REFERENCE_BRITE_PATHS_NAME,
        REFERENCE_MANIFEST_NAME,
    )
    brite_table = (output / REFERENCE_BRITE_PATHS_NAME).read_text(encoding="utf-8")
    assert "\npath_node\tko\tK00001\tko00001\t1\t0\tA\t09100\tMetabolism\t1\n" in brite_table
    assert (
        "\npath_node\tko\tK00001\tko00001\t1\t1\tB\tK00001\t'=untrusted BRITE node\t1\n"
    ) in brite_table
    manifest = json.loads((output / REFERENCE_MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["brite"] == {
        "missing_brite_count": 0,
        "path_count": 1,
        "resolved_brite_count": 1,
        "row_count": 2,
        "selected_brite_count": 1,
        "status": "completed",
        "unmatched_entity_count": 0,
    }
    reference_snapshot = json.loads((output / REFERENCE_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert reference_snapshot["brite"]["schema_version"] == "1"
    assert reference_snapshot["brite"]["request"]["brite_ids"] == ["ko00001"]
    assert reference_snapshot["retrieval"]["brite_relation_batches"] == []
    assert len(reference_snapshot["retrieval"]["brite_hierarchy_batches"]) == 1
    combined = "\n".join(path.read_text(encoding="utf-8") for path in output.iterdir())
    assert _SECRET_REQUEST not in combined
    assert _SECRET_ENDPOINT_LABEL not in combined


def test_brite_source_may_exactly_match_an_explicit_entry_selection(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    brite_result_id = _retain_brite(store, _brite_detail())
    output = tmp_path / "exact-brite-selection"

    result = write_kegg_reference_bundle(
        WriteKeggReferenceBundleRequest(
            source=ReferenceBundleSource(result_id=result_id),
            brite_source=ReferenceBundleSource(result_id=brite_result_id),
            entries=(_entity(KeggEntryCardKind.KO, "K00001"),),
        ),
        output_directory=output,
        result_store=store,
        scope_id="reference-scope",
    )

    assert result.requested_entry_count == 1
    assert (output / REFERENCE_BRITE_PATHS_NAME).is_file()


def test_brite_source_may_omit_unmatched_rows_when_the_request_excludes_them(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    detail = _brite_detail()
    detail = detail.model_copy(
        update={
            "request": detail.request.model_copy(
                update={
                    "entity_ids": (
                        detail.request.entity_ids[0],
                        KeggEntityRef(
                            kind=KeggEntityKind.PATHWAY,
                            identifier="ko99999",
                        ),
                    ),
                    "include_unmatched": False,
                }
            )
        }
    )
    brite_result_id = _retain_brite(store, detail)
    output = tmp_path / "excluded-unmatched"

    result = write_kegg_reference_bundle(
        WriteKeggReferenceBundleRequest(
            source=ReferenceBundleSource(result_id=result_id),
            brite_source=ReferenceBundleSource(result_id=brite_result_id),
        ),
        output_directory=output,
        result_store=store,
        scope_id="reference-scope",
    )

    assert result.requested_entry_count == 2
    assert (output / REFERENCE_BRITE_PATHS_NAME).is_file()


@pytest.mark.parametrize(
    "identifiers",
    [
        pytest.param(("K99999",), id="unrelated"),
        pytest.param(("K00001", "K99999"), id="partially-outside"),
    ],
)
def test_brite_source_entities_outside_the_selection_are_rejected(
    tmp_path: Path,
    identifiers: tuple[str, ...],
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    entities = tuple(
        KeggEntityRef(kind=KeggEntityKind.KO, identifier=identifier) for identifier in identifiers
    )
    detail = _brite_detail().model_copy(
        update={
            "request": MapBriteHierarchyRequest(
                entity_ids=entities,
                brite_ids=("ko00001",),
            ),
            "paths": (),
            "classifications": (),
            "unmatched_entities": entities,
        }
    )
    brite_result_id = _retain_brite(store, detail)
    output = tmp_path / "invalid-brite-selection"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                brite_source=ReferenceBundleSource(result_id=brite_result_id),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert not output.exists()


@pytest.mark.parametrize(
    "tamper",
    [
        "missing-entity-accounting",
        "matched-and-unmatched",
        "duplicate-path",
        "forged-classification-count",
        "duplicate-classification",
        "unresolved-path",
        "selected-request-mismatch",
    ],
)
def test_inconsistent_brite_detail_is_rejected_before_bundle_creation(
    tmp_path: Path,
    tamper: str,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    detail = _brite_detail()
    entity = detail.request.entity_ids[0]
    if tamper == "missing-entity-accounting":
        detail = detail.model_copy(
            update={
                "request": detail.request.model_copy(
                    update={
                        "entity_ids": (
                            entity,
                            KeggEntityRef(
                                kind=KeggEntityKind.PATHWAY,
                                identifier="ko99999",
                            ),
                        )
                    }
                )
            }
        )
    elif tamper == "matched-and-unmatched":
        detail = detail.model_copy(update={"unmatched_entities": (entity,)})
    elif tamper == "duplicate-path":
        detail = detail.model_copy(update={"paths": (*detail.paths, detail.paths[0])})
    elif tamper == "forged-classification-count":
        forged = detail.classifications[0].model_copy(update={"unique_input_count": 100})
        detail = detail.model_copy(
            update={"classifications": (forged, *detail.classifications[1:])}
        )
    elif tamper == "duplicate-classification":
        detail = detail.model_copy(
            update={"classifications": (*detail.classifications, detail.classifications[0])}
        )
    else:
        unrelated_id = "ko99999"
        unrelated_path = detail.paths[0].model_copy(update={"brite_id": unrelated_id})
        unrelated_classifications = tuple(
            item.model_copy(update={"brite_id": unrelated_id}) for item in detail.classifications
        )
        update: dict[str, object] = {
            "paths": (unrelated_path,),
            "classifications": unrelated_classifications,
        }
        if tamper == "selected-request-mismatch":
            update.update(
                selected_brite_ids=(unrelated_id,),
                resolved_brite_ids=(unrelated_id,),
            )
        detail = detail.model_copy(update=update)
    brite_result_id = _retain_brite(store, detail)
    output = tmp_path / f"invalid-{tamper}"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                brite_source=ReferenceBundleSource(result_id=brite_result_id),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert not output.exists()


def test_explicit_selection_exports_only_selected_reference(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    selected = _entity(KeggEntryCardKind.KO, "K00001")
    output = tmp_path / "selected"

    result = write_kegg_reference_bundle(
        WriteKeggReferenceBundleRequest(
            source=ReferenceBundleSource(result_id=result_id),
            entries=(selected,),
        ),
        output_directory=output,
        result_store=store,
        scope_id="reference-scope",
    )

    assert result.requested_entry_count == 1
    assert result.returned_entry_count == 1
    assert result.missing_entry_count == 0
    reference_snapshot = json.loads((output / REFERENCE_SNAPSHOT_NAME).read_text(encoding="utf-8"))
    assert reference_snapshot["request"]["entries"] == [
        {"database": "ko", "identifier": "K00001"},
    ]
    assert reference_snapshot["missing_entries"] == []


def test_selection_outside_snapshot_fails_before_filesystem_write(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "not-created"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                entries=(_entity(KeggEntryCardKind.KO, "K99999"),),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert not output.exists()


@pytest.mark.parametrize(
    ("payload", "mime_type"),
    [
        (b"{malformed", "application/json"),
        (_snapshot().model_dump_json().encode("utf-8"), "text/plain"),
    ],
)
def test_tampered_or_incompatible_snapshot_is_rejected_without_output(
    tmp_path: Path,
    payload: bytes,
    mime_type: str,
) -> None:
    store = SQLiteResultStore(tmp_path / f"results-{mime_type.replace('/', '-')}.sqlite3")
    result_id = _retain(store, payload, mime_type=mime_type)
    output = tmp_path / f"output-{mime_type.replace('/', '-')}"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert caught.value.detail.suggested_action is not None
    assert "card or references" in caught.value.detail.suggested_action
    assert not output.exists()


@pytest.mark.parametrize(
    "missing_field",
    (
        "schema_version",
        "parser_name",
        "parser_version",
        "response_parser_version",
    ),
)
def test_snapshot_missing_identity_is_rejected_before_reference_bundle_write(
    tmp_path: Path,
    missing_field: str,
) -> None:
    snapshot = _snapshot().model_dump(mode="json")
    del snapshot[missing_field]
    store = SQLiteResultStore(tmp_path / f"missing-{missing_field}.sqlite3")
    result_id = _retain(store, json.dumps(snapshot).encode("utf-8"))
    output = tmp_path / f"missing-{missing_field}"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    details = {item.name: item.value for item in caught.value.detail.safe_details}
    assert details["required_snapshot_schema_version"] == ENTRY_CARD_SCHEMA_VERSION
    assert not output.exists()


def test_snapshot_from_another_scope_is_not_exported(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot(), scope_id="owner-scope")
    output = tmp_path / "cross-scope"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="different-scope",
        )

    assert caught.value.detail.code is ErrorCode.RESULT_NOT_FOUND
    assert not output.exists()


def test_active_non_snapshot_result_reports_artifact_kind_mismatch(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result = store.create(
        "reference-scope",
        (
            ResultArtifactInput(
                section="detail",
                mime_type="application/json",
                content=b"{}",
            ),
        ),
    )
    output = tmp_path / "wrong-artifact-kind"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result.result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    details = {item.name: item.value for item in caught.value.detail.safe_details}
    assert details == {
        "expected_artifact_kind": ENTRY_CARD_SNAPSHOT_SECTION,
        "actual_artifact_kind": "detail",
    }
    assert not output.exists()


@pytest.mark.parametrize("source_state", ("unknown", "expired"))
def test_unavailable_reference_source_remains_not_found(
    tmp_path: Path,
    source_state: str,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    if source_state == "unknown":
        result_id = "res_" + "a" * 32
    else:
        result_id = store.create(
            "reference-scope",
            (
                ResultArtifactInput(
                    section=ENTRY_CARD_SNAPSHOT_SECTION,
                    mime_type="application/json",
                    content=_snapshot().model_dump_json().encode("utf-8"),
                ),
            ),
            now=datetime.now(UTC) - timedelta(days=2),
        ).result_id
    output = tmp_path / source_state

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.RESULT_NOT_FOUND
    assert caught.value.detail.safe_details == ()
    assert not output.exists()


def test_non_brite_result_is_rejected_as_brite_source(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "wrong-kind"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                brite_source=ReferenceBundleSource(result_id=result_id),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert not output.exists()


def test_brite_source_from_another_scope_is_not_exported(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    brite_result_id = _retain_brite(
        store,
        _brite_detail(),
        scope_id="other-scope",
    )
    output = tmp_path / "cross-scope-brite"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                brite_source=ReferenceBundleSource(result_id=brite_result_id),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.RESULT_NOT_FOUND
    assert not output.exists()


def test_tampered_brite_detail_is_rejected_without_output(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    brite_result_id = _retain_brite(store, b"{malformed")
    output = tmp_path / "tampered-brite"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                brite_source=ReferenceBundleSource(result_id=brite_result_id),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert not output.exists()


def test_brite_detail_missing_schema_is_rejected_before_reference_bundle_write(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "missing-brite-schema.sqlite3")
    result_id = _retain(store, _snapshot())
    detail = _brite_detail().model_dump(mode="json")
    del detail["schema_version"]
    brite_result_id = _retain_brite(store, json.dumps(detail).encode("utf-8"))
    output = tmp_path / "missing-brite-schema"

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                brite_source=ReferenceBundleSource(result_id=brite_result_id),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    details = {item.name: item.value for item in caught.value.detail.safe_details}
    assert details["required_brite_detail_schema_version"] == BRITE_DETAIL_SCHEMA_VERSION
    assert not output.exists()


def test_brite_source_size_limit_is_checked_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    brite_result_id = _retain_brite(store, _brite_detail())
    output = tmp_path / "oversize-brite"
    monkeypatch.setattr(reference_bundles, "MAX_BRITE_ARTIFACT_BYTES", 16)

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            WriteKeggReferenceBundleRequest(
                source=ReferenceBundleSource(result_id=result_id),
                brite_source=ReferenceBundleSource(result_id=brite_result_id),
            ),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert not output.exists()


def test_nonempty_output_is_not_modified(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "occupied"
    output.mkdir()
    existing = output / "caller.txt"
    existing.write_text("unchanged", encoding="utf-8")

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    assert tuple(output.iterdir()) == (existing,)
    assert existing.read_text(encoding="utf-8") == "unchanged"


def test_symlink_output_component_is_rejected_without_touching_target(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=alias / "bundle",
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert not tuple(target.iterdir())


def test_install_failure_rolls_back_files_and_created_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "transaction"
    real_link = os.link
    destinations: list[str] = []

    def fail_second_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        destinations.append(destination)
        if len(destinations) == 2:
            raise OSError("synthetic transaction failure")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", fail_second_link)
    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert not output.exists()
    assert REFERENCE_MANIFEST_NAME not in destinations


def test_manifest_is_published_after_every_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "manifest-last"
    real_link = os.link
    destinations: list[str] = []

    def record_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        destinations.append(destination)
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", record_link)
    write_kegg_reference_bundle(
        _request(result_id),
        output_directory=output,
        result_store=store,
        scope_id="reference-scope",
    )

    assert destinations[-1] == REFERENCE_MANIFEST_NAME
    assert set(destinations[:-1]) == {
        REFERENCE_SNAPSHOT_NAME,
        REFERENCE_RELATIONSHIPS_NAME,
    }


def test_artifact_byte_limit_is_checked_before_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "too-large"
    monkeypatch.setattr(reference_bundles, "MAX_REFERENCE_BUNDLE_ARTIFACT_BYTES", 16)

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert not output.exists()


def test_relationship_row_limit_is_checked_before_directory_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    result_id = _retain(store, _snapshot())
    output = tmp_path / "too-many-relationships"
    monkeypatch.setattr(reference_bundles, "MAX_REFERENCE_RELATIONSHIPS", 1)

    with pytest.raises(KeggMcpError) as caught:
        write_kegg_reference_bundle(
            _request(result_id),
            output_directory=output,
            result_store=store,
            scope_id="reference-scope",
        )

    assert caught.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert not output.exists()
