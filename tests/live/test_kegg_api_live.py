"""Opt-in compatibility campaign for an authorized KEGG endpoint."""

from __future__ import annotations

import os

import pytest

from kegg_mcp.kegg import (
    CacheLookupState,
    ConvRequest,
    FindRequest,
    GetRequest,
    InfoRequest,
    KeggBatchProvenance,
    KeggBriteEntryKind,
    KeggClient,
    KeggConvDatabase,
    KeggEntryRef,
    KeggFindDatabase,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
    OrganismPathwayListRequest,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import KeggBriteHtextDocument

_REFRESH = KeggRequestOptions(refresh=True)
_INFO_REQUEST = InfoRequest(database=KeggInfoDatabase.KO)
_LIST_REQUEST = OrganismPathwayListRequest(organism="hsa")
_FIND_REQUEST = FindRequest(database=KeggFindDatabase.KO, query="hexokinase")
_GET_REQUEST = GetRequest(
    entries=(
        KeggEntryRef(
            database=KeggGetDatabase.BRITE,
            identifier="br08901",
            brite_kind=KeggBriteEntryKind.HIERARCHY,
        ),
    )
)
_LINK_REQUEST = LinkRequest(
    relationship=KeggLinkRelationship.KO_TO_PATHWAY,
    source_identifiers=("K00844",),
)
_CONV_REQUEST = ConvRequest(
    target_database=KeggConvDatabase.UNIPROT,
    source_database=KeggConvDatabase.GENES,
    source_identifiers=("hsa:7157",),
)

pytestmark = [
    pytest.mark.live_kegg,
    pytest.mark.skipif(
        os.environ.get("KEGG_MCP_RUN_LIVE_TESTS", "").lower() != "true",
        reason="set KEGG_MCP_RUN_LIVE_TESTS=true to run live KEGG tests",
    ),
]


def _assert_single_network_request(batch: KeggBatchProvenance) -> None:
    assert batch.origin is ResponseOrigin.NETWORK
    assert batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert batch.attempt_count == 1
    assert batch.response_bytes > 0
    assert batch.parser_version == "4"
    assert not batch.is_stale


def test_live_info_repeatedly(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for _ in range(live_requests_per_operation):
        result = live_kegg_client.info(_INFO_REQUEST, options=_REFRESH)

        _assert_single_network_request(result.batch)
        assert result.document.database is KeggInfoDatabase.KO
        assert result.document.lines


def test_live_organism_pathway_list_repeatedly(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for _ in range(live_requests_per_operation):
        result = live_kegg_client.list_organism_pathways(
            _LIST_REQUEST,
            options=_REFRESH,
        )

        _assert_single_network_request(result.batch)
        assert result.request.organism == "hsa"
        assert result.document.organism == "hsa"
        assert result.document.rows
        assert all(row.pathway_id.startswith("path:hsa") for row in result.document.rows)
        assert all(row.name.strip() for row in result.document.rows)


def test_live_get_repeatedly(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for _ in range(live_requests_per_operation):
        result = live_kegg_client.get(_GET_REQUEST, options=_REFRESH)

        assert result.missing_entries == ()
        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert len(result.documents) == 1
        document = result.documents[0]
        assert isinstance(document, KeggBriteHtextDocument)
        assert document.identifier == "br08901"
        assert document.lines


def test_live_find_repeatedly(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for _ in range(live_requests_per_operation):
        result = live_kegg_client.find(_FIND_REQUEST, options=_REFRESH)

        _assert_single_network_request(result.batch)
        assert result.request.database is KeggFindDatabase.KO
        assert result.document.rows


def test_live_link_repeatedly(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for _ in range(live_requests_per_operation):
        result = live_kegg_client.link(_LINK_REQUEST, options=_REFRESH)

        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert result.rows


def test_live_conv_repeatedly(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for _ in range(live_requests_per_operation):
        result = live_kegg_client.conv(_CONV_REQUEST, options=_REFRESH)

        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert result.rows
