"""Tests for deterministic KEGG-to-PubMed reference projections."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg import KeggClientConfig, KeggRequestOptions, PublicAcademicAccess
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggEntryRef,
    KeggGetDatabase,
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.parsers import parse_flat_file_response
from kegg_mcp.services.entry_cards import (
    ENTRY_CARD_PARSER_VERSION,
    ENTRY_CARD_SCHEMA_VERSION,
    ENTRY_CARD_SNAPSHOT_SECTION,
    build_entry_cards,
    entry_card_reference_previews,
)
from kegg_mcp.services.kegg_entries import retrieve_kegg_entries
from kegg_mcp.services.models import KeggEntryProjection
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.result_store import SQLiteResultStore

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _provenance() -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=KeggOperation.GET,
        request_key="get:synthetic-literature-references",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        response_bytes=200,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release synthetic",
        attempt_count=1,
        is_stale=False,
    )


class _StaticGetClient:
    def __init__(self, result: GetResult) -> None:
        self._config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))
        self.result = result
        self.calls = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del options
        assert request == self.result.request
        self.calls += 1
        return self.result


class _UnexpectedGetClient:
    def __init__(self) -> None:
        self._config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))
        self.calls = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del request, options
        self.calls += 1
        raise AssertionError("unsupported reference projection must fail before KEGG access")


def _ko_result() -> GetResult:
    request = GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),))
    return GetResult(
        request=request,
        documents=(
            parse_flat_file_response(
                b"ENTRY       K00001                      KO\n"
                b"NAME        Synthetic ortholog\n"
                b"REFERENCE   PMID:123456\n"
                b"            PMID:234567 PMID:123456\n"
                b"///\n"
            ),
        ),
        missing_entries=(),
        batches=(_provenance(),),
    )


def test_reference_projection_extracts_only_ordered_unique_kegg_pmids() -> None:
    snapshot = build_entry_cards(_ko_result())

    assert snapshot.schema_version == ENTRY_CARD_SCHEMA_VERSION
    assert snapshot.parser_version == ENTRY_CARD_PARSER_VERSION
    assert snapshot.response_parser_version == PARSER_VERSION
    assert snapshot.entries[0].entity.database.value == KeggGetDatabase.KO.value
    assert snapshot.entries[0].pubmed_ids == ("123456", "234567")
    preview = entry_card_reference_previews(snapshot)
    assert preview.entry_count == 1
    assert preview.referenced_entry_count == 1
    assert preview.pubmed_id_count == 2
    assert preview.previews[0].pubmed_ids == ("123456", "234567")


def test_reference_projection_accepts_returned_entry_without_pubmed_ids() -> None:
    fetched = _ko_result().model_copy(
        update={
            "documents": (
                parse_flat_file_response(
                    b"ENTRY       K00001                      KO\n"
                    b"NAME        Synthetic ortholog without references\n"
                    b"///\n"
                ),
            ),
        }
    )

    snapshot = build_entry_cards(fetched)
    preview = entry_card_reference_previews(snapshot)

    assert snapshot.entries[0].pubmed_ids == ()
    assert preview.entry_count == 1
    assert preview.referenced_entry_count == 0
    assert preview.pubmed_id_count == 0


def test_reference_service_reuses_complete_card_snapshot(
    tmp_path: Path,
) -> None:
    fetched = _ko_result()
    client = _StaticGetClient(fetched)
    store = SQLiteResultStore(tmp_path / "literature.sqlite3")

    result = retrieve_kegg_entries(
        fetched.request,
        client=cast(KeggPrimitiveClient, client),
        result_store=store,
        scope_id="literature-scope",
        projection=KeggEntryProjection.REFERENCES,
    )

    assert client.calls == 1
    assert result.projection is KeggEntryProjection.REFERENCES
    assert result.snapshot_artifact is not None
    assert result.card_preview is None
    assert result.literature_preview is not None
    assert result.literature_preview.pubmed_id_count == 2
    retained = store.read_artifact(
        "literature-scope",
        result.result.result_id,
        ENTRY_CARD_SNAPSHOT_SECTION,
        now=_NOW,
    )
    payload = json.loads(retained.content)
    assert payload["entries"][0]["pubmed_ids"] == ["123456", "234567"]


def test_reference_projection_rejects_unsupported_card_type_before_kegg_access(
    tmp_path: Path,
) -> None:
    client = _UnexpectedGetClient()
    store = SQLiteResultStore(tmp_path / "brite-references.sqlite3")

    with pytest.raises(KeggMcpError) as caught:
        retrieve_kegg_entries(
            GetRequest(
                entries=(
                    KeggEntryRef(
                        database=KeggGetDatabase.DRUG,
                        identifier="D00001",
                    ),
                )
            ),
            client=cast(KeggPrimitiveClient, client),
            result_store=store,
            scope_id="brite-references",
            projection=KeggEntryProjection.REFERENCES,
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert client.calls == 0
    assert store.list_results("brite-references").total_items == 0
