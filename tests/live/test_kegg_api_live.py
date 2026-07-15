"""Opt-in 120-request compatibility campaign for an authorized KEGG endpoint."""

from __future__ import annotations

import pytest

from kegg_mcp.kegg import (
    CacheLookupState,
    ConvRequest,
    GetRequest,
    InfoRequest,
    KeggBatchProvenance,
    KeggBriteEntryKind,
    KeggClient,
    KeggConvDatabase,
    KeggEntryRef,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import KeggBriteHtextDocument

_REQUESTS_PER_OPERATION = 30
_REFRESH = KeggRequestOptions(refresh=True)
_INFO_REQUEST = InfoRequest(database=KeggInfoDatabase.KO)
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


def _assert_single_network_request(batch: KeggBatchProvenance) -> None:
    assert batch.origin is ResponseOrigin.NETWORK
    assert batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert batch.attempt_count == 1
    assert batch.response_bytes > 0
    assert batch.parser_version == "4"
    assert not batch.is_stale


@pytest.mark.live_kegg
def test_live_info_thirty_times(live_kegg_client: KeggClient) -> None:
    for _ in range(_REQUESTS_PER_OPERATION):
        result = live_kegg_client.info(_INFO_REQUEST, options=_REFRESH)

        _assert_single_network_request(result.batch)
        assert result.document.database is KeggInfoDatabase.KO
        assert result.document.lines


@pytest.mark.live_kegg
def test_live_get_thirty_times(live_kegg_client: KeggClient) -> None:
    for _ in range(_REQUESTS_PER_OPERATION):
        result = live_kegg_client.get(_GET_REQUEST, options=_REFRESH)

        assert result.missing_entries == ()
        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert len(result.documents) == 1
        document = result.documents[0]
        assert isinstance(document, KeggBriteHtextDocument)
        assert document.identifier == "br08901"
        assert document.lines


@pytest.mark.live_kegg
def test_live_link_thirty_times(live_kegg_client: KeggClient) -> None:
    for _ in range(_REQUESTS_PER_OPERATION):
        result = live_kegg_client.link(_LINK_REQUEST, options=_REFRESH)

        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert result.rows


@pytest.mark.live_kegg
def test_live_conv_thirty_times(live_kegg_client: KeggClient) -> None:
    for _ in range(_REQUESTS_PER_OPERATION):
        result = live_kegg_client.conv(_CONV_REQUEST, options=_REFRESH)

        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert result.rows
