"""Tests for deterministic local KEGG entry-card snapshot comparison."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis import MODULE_PARSER_NAME, MODULE_PARSER_VERSION
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services.entry_cards import (
    ENTRY_CARD_PARSER_NAME,
    ENTRY_CARD_PARSER_VERSION,
    ENTRY_CARD_SCHEMA_VERSION,
    CompoundEntryCard,
    GenomeEntryCard,
    KeggEntryCard,
    KeggEntryCardEntity,
    KeggEntryCardKind,
    KeggEntryCardReference,
    KeggEntryCardSnapshot,
    KeggModuleDefinitionCard,
    KoEntryCard,
    ModuleEntryCard,
    PathwayEntryCard,
)
from kegg_mcp.services.query_support import MAX_QUERY_ARTIFACT_BYTES
from kegg_mcp.services.reference_snapshots import (
    ENTRY_SNAPSHOT_SECTION,
    CompareKeggReferenceSnapshotsRequest,
    ReferenceReleaseComparisonStatus,
    ReferenceSnapshotChangeType,
    ReferenceSnapshotComparisonDimension,
    ReferenceSnapshotSource,
    compare_kegg_reference_snapshots,
)
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    SQLiteResultStore,
)

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _provenance(
    marker: str,
    *,
    release: str | None,
    endpoint_label: str = PUBLIC_KEGG_ENDPOINT_LABEL,
) -> KeggBatchProvenance:
    endpoint_class = (
        RetrievalEndpointClass.PUBLIC_ACADEMIC
        if endpoint_label == PUBLIC_KEGG_ENDPOINT_LABEL
        else RetrievalEndpointClass.LICENSED
    )
    access_mode = (
        AccessMode.PUBLIC_ACADEMIC
        if endpoint_class is RetrievalEndpointClass.PUBLIC_ACADEMIC
        else AccessMode.LICENSED
    )
    return KeggBatchProvenance(
        operation=KeggOperation.GET,
        request_key=f"synthetic:{marker}",
        access_mode=access_mode,
        retrieval_endpoint_class=endpoint_class,
        endpoint_label=endpoint_label,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        response_bytes=100,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release=release,
        attempt_count=1,
        is_stale=False,
    )


def _cached_provenance(marker: str, *, stale: bool = False) -> KeggBatchProvenance:
    expires_at = _NOW + timedelta(hours=1)
    served_at = expires_at + timedelta(hours=1) if stale else _NOW + timedelta(minutes=1)
    return KeggBatchProvenance(
        operation=KeggOperation.GET,
        request_key=f"synthetic:{marker}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.CACHE,
        cache_lookup_state=(CacheLookupState.STALE_HIT if stale else CacheLookupState.FRESH_HIT),
        retrieved_at=_NOW,
        served_at=served_at,
        expires_at=expires_at,
        response_bytes=100,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release same",
        attempt_count=0,
        is_stale=stale,
    )


def _entity(kind: KeggEntryCardKind, identifier: str) -> KeggEntryCardEntity:
    return KeggEntryCardEntity(database=kind, identifier=identifier)


def _snapshot(
    *,
    changed: bool,
    release: str | None,
    endpoint_label: str = PUBLIC_KEGG_ENDPOINT_LABEL,
) -> KeggEntryCardSnapshot:
    ko = KoEntryCard(
        entity=_entity(KeggEntryCardKind.KO, "K00001"),
        names=("Synthetic KO",),
        definition="new definition" if changed else "old definition",
        pathways=(
            KeggEntryCardReference(identifier="ko00010", label="Synthetic pathway"),
            *((KeggEntryCardReference(identifier="ko00020"),) if changed else ()),
        ),
    )
    pathway = PathwayEntryCard(
        entity=_entity(KeggEntryCardKind.PATHWAY, "ko00010"),
        names=("Synthetic pathway",),
        ko_identifiers=("K00001", "K00002") if changed else ("K00001",),
    )
    module_definition = KeggModuleDefinitionCard(
        raw_definition="K00001 K00002" if changed else "K00001",
        parser_name=MODULE_PARSER_NAME,
        parser_version=MODULE_PARSER_VERSION,
        is_valid=True,
        required_blocks=("K00001", "K00002") if changed else ("K00001",),
        optional_components=(),
        referenced_modules=(),
        ko_components=("K00001", "K00002") if changed else ("K00001",),
        diagnostic_codes=(),
    )
    module = ModuleEntryCard(
        entity=_entity(KeggEntryCardKind.MODULE, "M00001"),
        names=("Synthetic module",),
        definition=module_definition.raw_definition,
        module_definition=module_definition,
    )
    entries: list[KeggEntryCard] = [ko, pathway, module]
    compound_entity = _entity(KeggEntryCardKind.COMPOUND, "C00031")
    if changed:
        entries.append(
            CompoundEntryCard(
                entity=compound_entity,
                names=("Synthetic compound",),
                formula="C6H12O6",
            )
        )
    return KeggEntryCardSnapshot(
        schema_version=ENTRY_CARD_SCHEMA_VERSION,
        parser_name=ENTRY_CARD_PARSER_NAME,
        parser_version=ENTRY_CARD_PARSER_VERSION,
        response_parser_version=PARSER_VERSION,
        requested_entries=(
            _entity(KeggEntryCardKind.KO, "K00001"),
            _entity(KeggEntryCardKind.PATHWAY, "ko00010"),
            _entity(KeggEntryCardKind.MODULE, "M00001"),
            compound_entity,
        ),
        entries=tuple(entries),
        missing_entries=() if changed else (compound_entity,),
        provenance=(
            _provenance(
                "right" if changed else "left",
                release=release,
                endpoint_label=endpoint_label,
            ),
        ),
    )


def _retain(
    store: SQLiteResultStore,
    scope_id: str,
    snapshot: KeggEntryCardSnapshot | bytes,
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
                section=ENTRY_SNAPSHOT_SECTION,
                mime_type="application/json",
                content=content,
            ),
        ),
    )
    return result.result_id


def test_snapshot_comparison_classifies_changes_and_retains_values(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    scope_id = "snapshot-scope"
    left_id = _retain(store, scope_id, _snapshot(changed=False, release="Release left"))
    right_id = _retain(store, scope_id, _snapshot(changed=True, release="Release right"))

    result = compare_kegg_reference_snapshots(
        CompareKeggReferenceSnapshotsRequest(
            left=ReferenceSnapshotSource(result_id=left_id),
            right=ReferenceSnapshotSource(result_id=right_id),
        ),
        result_store=store,
        scope_id=scope_id,
    )

    assert result.release_status is ReferenceReleaseComparisonStatus.DIFFERENT
    assert result.endpoint_context_compatible is True
    assert result.parser_compatible is True
    assert result.shared_entry_count == 3
    assert result.added_entry_count == 1
    assert result.removed_entry_count == 0
    assert result.changed_entry_count == 3
    counts = {item.dimension: item.change_count for item in result.dimension_counts}
    assert counts[ReferenceSnapshotComparisonDimension.ENTRY_FIELDS] == 1
    assert counts[ReferenceSnapshotComparisonDimension.RELATIONSHIPS] == 1
    assert counts[ReferenceSnapshotComparisonDimension.MODULE_DEFINITIONS] == 1
    assert counts[ReferenceSnapshotComparisonDimension.PATHWAY_DENOMINATORS] == 1
    assert result.field_change_count == 4
    assert any(
        item.change_type is ReferenceSnapshotChangeType.ENTRY_ADDED
        and item.entity.identifier == "C00031"
        for item in result.change_preview
    )

    retained = json.loads(
        store.read_artifact(
            scope_id,
            result.result.result_id,
            "reference_diff",
            limit=store.limits.max_range_bytes,
        ).content
    )
    definition_change = next(
        item
        for item in retained["changes"]
        if item["entity"]["identifier"] == "K00001" and item["field_name"] == "definition"
    )
    assert definition_change["left_value"] == "old definition"
    assert definition_change["right_value"] == "new definition"
    pathway_change = next(
        item for item in retained["changes"] if item["field_name"] == "ko_identifiers"
    )
    assert pathway_change["added_values"] == ["K00002"]


def test_genome_alias_snapshots_compare_the_same_original_request(
    tmp_path: Path,
) -> None:
    requested = _entity(KeggEntryCardKind.GENOME, "hsa")
    returned = KeggEntryCardSnapshot(
        schema_version=ENTRY_CARD_SCHEMA_VERSION,
        parser_name=ENTRY_CARD_PARSER_NAME,
        parser_version=ENTRY_CARD_PARSER_VERSION,
        response_parser_version=PARSER_VERSION,
        requested_entries=(requested,),
        entries=(
            GenomeEntryCard(
                entity=_entity(KeggEntryCardKind.GENOME, "T01001"),
                organism_code="hsa",
            ),
        ),
        provenance=(_provenance("returned-genome", release="Release left"),),
    )
    missing = KeggEntryCardSnapshot(
        schema_version=ENTRY_CARD_SCHEMA_VERSION,
        parser_name=ENTRY_CARD_PARSER_NAME,
        parser_version=ENTRY_CARD_PARSER_VERSION,
        response_parser_version=PARSER_VERSION,
        requested_entries=(requested,),
        entries=(),
        missing_entries=(requested,),
        provenance=(_provenance("missing-genome", release="Release right"),),
    )
    store = SQLiteResultStore(tmp_path / "genome-alias.sqlite3")
    left_id = _retain(store, "scope", returned)
    right_id = _retain(store, "scope", missing)

    result = compare_kegg_reference_snapshots(
        CompareKeggReferenceSnapshotsRequest(
            left=ReferenceSnapshotSource(result_id=left_id),
            right=ReferenceSnapshotSource(result_id=right_id),
        ),
        result_store=store,
        scope_id="scope",
    )

    assert result.shared_entry_count == 0
    assert result.removed_entry_count == 1
    assert result.added_entry_count == 0
    assert result.change_preview[0].entity.identifier == "T01001"


def test_dimension_selection_filters_semantic_fields_but_keeps_entry_membership(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    left_id = _retain(store, "scope", _snapshot(changed=False, release=None))
    right_id = _retain(store, "scope", _snapshot(changed=True, release=None))

    result = compare_kegg_reference_snapshots(
        CompareKeggReferenceSnapshotsRequest(
            left=ReferenceSnapshotSource(result_id=left_id),
            right=ReferenceSnapshotSource(result_id=right_id),
            compare=(ReferenceSnapshotComparisonDimension.MODULE_DEFINITIONS,),
        ),
        result_store=store,
        scope_id="scope",
    )

    assert result.release_status is ReferenceReleaseComparisonStatus.UNAVAILABLE
    assert result.added_entry_count == 1
    assert result.changed_entry_count == 1
    assert result.field_change_count == 1
    assert {item.dimension: item.change_count for item in result.dimension_counts}[
        ReferenceSnapshotComparisonDimension.MODULE_DEFINITIONS
    ] == 1


def test_endpoint_mismatch_is_explicit_and_not_attributed_to_release(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    left_id = _retain(store, "scope", _snapshot(changed=False, release="Release same"))
    right_id = _retain(
        store,
        "scope",
        _snapshot(
            changed=True,
            release="Release same",
            endpoint_label="licensed-test",
        ),
    )

    result = compare_kegg_reference_snapshots(
        CompareKeggReferenceSnapshotsRequest(
            left=ReferenceSnapshotSource(result_id=left_id),
            right=ReferenceSnapshotSource(result_id=right_id),
        ),
        result_store=store,
        scope_id="scope",
    )

    assert result.endpoint_context_compatible is False
    assert result.release_status is ReferenceReleaseComparisonStatus.SAME
    assert any("Endpoint contexts differ" in caveat for caveat in result.interpretation_caveats)


def test_network_cache_and_stale_retrieval_context_is_explicit(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    network = _snapshot(changed=False, release="Release same")
    stale = network.model_copy(update={"provenance": (_cached_provenance("stale", stale=True),)})
    left_id = _retain(store, "scope", network)
    right_id = _retain(store, "scope", stale)

    result = compare_kegg_reference_snapshots(
        CompareKeggReferenceSnapshotsRequest(
            left=ReferenceSnapshotSource(result_id=left_id),
            right=ReferenceSnapshotSource(result_id=right_id),
        ),
        result_store=store,
        scope_id="scope",
    )

    assert result.retrieval_context_compatible is False
    assert result.left.network_request_count == 1
    assert result.right.cache_hit_count == 1
    assert result.right.stale_batch_count == 1
    assert any("Retrieval contexts differ" in item for item in result.interpretation_caveats)


def test_comparison_requires_a_current_scope_entry_snapshot(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    valid_id = _retain(store, "scope", _snapshot(changed=False, release=None))
    other_scope_id = _retain(store, "other", _snapshot(changed=True, release=None))

    with pytest.raises(KeggMcpError) as captured:
        compare_kegg_reference_snapshots(
            CompareKeggReferenceSnapshotsRequest(
                left=ReferenceSnapshotSource(result_id=valid_id),
                right=ReferenceSnapshotSource(result_id=other_scope_id),
            ),
            result_store=store,
            scope_id="scope",
        )

    assert captured.value.detail.code is ErrorCode.RESULT_NOT_FOUND


@pytest.mark.parametrize(
    "missing_field",
    (
        "schema_version",
        "parser_name",
        "parser_version",
        "response_parser_version",
    ),
)
def test_comparison_rejects_snapshot_missing_wire_identity(
    tmp_path: Path,
    missing_field: str,
) -> None:
    store = SQLiteResultStore(tmp_path / f"missing-{missing_field}.sqlite3")
    valid = _snapshot(changed=False, release=None)
    invalid = valid.model_dump(mode="json")
    del invalid[missing_field]
    valid_id = _retain(store, "scope", valid)
    invalid_id = _retain(store, "scope", json.dumps(invalid).encode("utf-8"))

    with pytest.raises(KeggMcpError) as captured:
        compare_kegg_reference_snapshots(
            CompareKeggReferenceSnapshotsRequest(
                left=ReferenceSnapshotSource(result_id=valid_id),
                right=ReferenceSnapshotSource(result_id=invalid_id),
            ),
            result_store=store,
            scope_id="scope",
        )

    assert captured.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    details = {item.name: item.value for item in captured.value.detail.safe_details}
    assert details["required_snapshot_schema_version"] == ENTRY_CARD_SCHEMA_VERSION


def test_snapshot_request_rejects_duplicates_and_canonicalizes_dimension_order() -> None:
    source = ReferenceSnapshotSource(result_id="res_" + "a" * 32)
    with pytest.raises(ValidationError, match="must be unique"):
        CompareKeggReferenceSnapshotsRequest(
            left=source,
            right=source,
            compare=(
                ReferenceSnapshotComparisonDimension.ENTRY_FIELDS,
                ReferenceSnapshotComparisonDimension.ENTRY_FIELDS,
            ),
        )
    request = CompareKeggReferenceSnapshotsRequest(
        left=source,
        right=source,
        compare=(
            ReferenceSnapshotComparisonDimension.RELATIONSHIPS,
            ReferenceSnapshotComparisonDimension.ENTRY_FIELDS,
        ),
    )
    assert request.compare == (
        ReferenceSnapshotComparisonDimension.ENTRY_FIELDS,
        ReferenceSnapshotComparisonDimension.RELATIONSHIPS,
    )


def test_comparison_rejects_different_requested_entry_scopes(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    left = _snapshot(changed=False, release=None)
    right = left.model_copy(
        update={
            "requested_entries": left.requested_entries[:-1],
            "missing_entries": (),
        }
    )
    left_id = _retain(store, "scope", left)
    right_id = _retain(store, "scope", right)

    with pytest.raises(KeggMcpError) as captured:
        compare_kegg_reference_snapshots(
            CompareKeggReferenceSnapshotsRequest(
                left=ReferenceSnapshotSource(result_id=left_id),
                right=ReferenceSnapshotSource(result_id=right_id),
            ),
            result_store=store,
            scope_id="scope",
        )

    assert captured.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    details = {item.name: item.value for item in captured.value.detail.safe_details}
    assert details == {
        "left_requested_entry_count": "4",
        "right_requested_entry_count": "3",
    }


def test_partial_release_labels_are_not_reported_as_same(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    base = _snapshot(changed=False, release="Release same")
    partial = base.model_copy(
        update={
            "provenance": (
                _provenance("partial-labeled", release="Release same"),
                _provenance("partial-unavailable", release=None),
            )
        }
    )
    left_id = _retain(store, "scope", partial)
    right_id = _retain(store, "scope", base)

    result = compare_kegg_reference_snapshots(
        CompareKeggReferenceSnapshotsRequest(
            left=ReferenceSnapshotSource(result_id=left_id),
            right=ReferenceSnapshotSource(result_id=right_id),
        ),
        result_store=store,
        scope_id="scope",
    )

    assert result.release_status is ReferenceReleaseComparisonStatus.PARTIAL
    assert result.left.release_labeled_batch_count == 1
    assert result.left.release_unavailable_batch_count == 1


def test_snapshot_source_media_type_and_size_are_bounded(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    valid_id = _retain(store, "scope", _snapshot(changed=False, release=None))
    wrong_type = store.create(
        "scope",
        (
            ResultArtifactInput(
                section=ENTRY_SNAPSHOT_SECTION,
                mime_type="text/plain",
                content=b"not-json",
            ),
        ),
    )
    too_large = store.create(
        "scope",
        (
            ResultArtifactInput(
                section=ENTRY_SNAPSHOT_SECTION,
                mime_type="application/json",
                content=b"x" * (MAX_QUERY_ARTIFACT_BYTES + 1),
            ),
        ),
    )

    for result_id, code in (
        (wrong_type.result_id, ErrorCode.ANALYSIS_CONFIGURATION_INVALID),
        (too_large.result_id, ErrorCode.OUTPUT_LIMIT_EXCEEDED),
    ):
        with pytest.raises(KeggMcpError) as captured:
            compare_kegg_reference_snapshots(
                CompareKeggReferenceSnapshotsRequest(
                    left=ReferenceSnapshotSource(result_id=valid_id),
                    right=ReferenceSnapshotSource(result_id=result_id),
                ),
                result_store=store,
                scope_id="scope",
            )
        assert captured.value.detail.code is code
