"""Four opt-in compatibility requests against an explicitly authorized KEGG endpoint."""

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


def _assert_single_network_request(batch: KeggBatchProvenance) -> None:
    assert batch.origin is ResponseOrigin.NETWORK
    assert batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert batch.attempt_count == 1
    assert batch.response_bytes > 0
    assert batch.parser_version == "4"
    assert not batch.is_stale


@pytest.mark.live_kegg
def test_live_info(live_kegg_client: KeggClient) -> None:
    result = live_kegg_client.info(
        InfoRequest(database=KeggInfoDatabase.KO),
        options=KeggRequestOptions(refresh=True),
    )

    _assert_single_network_request(result.batch)
    assert result.document.database is KeggInfoDatabase.KO
    assert result.document.lines


@pytest.mark.live_kegg
def test_live_brite_get(live_kegg_client: KeggClient) -> None:
    result = live_kegg_client.get(
        GetRequest(
            entries=(
                KeggEntryRef(
                    database=KeggGetDatabase.BRITE,
                    identifier="br08901",
                    brite_kind=KeggBriteEntryKind.HIERARCHY,
                ),
            )
        ),
        options=KeggRequestOptions(refresh=True),
    )

    assert result.missing_entries == ()
    assert len(result.batches) == 1
    _assert_single_network_request(result.batches[0])
    assert len(result.documents) == 1
    document = result.documents[0]
    assert isinstance(document, KeggBriteHtextDocument)
    assert document.identifier == "br08901"
    assert document.lines


@pytest.mark.live_kegg
def test_live_link(live_kegg_client: KeggClient) -> None:
    result = live_kegg_client.link(
        LinkRequest(
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            source_identifiers=("K00844",),
        ),
        options=KeggRequestOptions(refresh=True),
    )

    assert len(result.batches) == 1
    _assert_single_network_request(result.batches[0])
    assert result.rows


@pytest.mark.live_kegg
def test_live_conv(live_kegg_client: KeggClient) -> None:
    result = live_kegg_client.conv(
        ConvRequest(
            target_database=KeggConvDatabase.UNIPROT,
            source_database=KeggConvDatabase.GENES,
            source_identifiers=("hsa:7157",),
        ),
        options=KeggRequestOptions(refresh=True),
    )

    assert len(result.batches) == 1
    _assert_single_network_request(result.batches[0])
    assert result.rows
