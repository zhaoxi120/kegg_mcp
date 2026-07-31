"""Opt-in compatibility campaign for an authorized KEGG endpoint."""

from __future__ import annotations

import os
from itertools import cycle, islice
from typing import TypeVar

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
    KeggFindMode,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    KeggTaxonomyRank,
    LinkRequest,
    OrganismPathwayListRequest,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import KeggBriteHtextDocument, KeggFlatFileDocument
from kegg_mcp.kegg.operations import get_entry_matches
from kegg_mcp.services.entry_cards import ENTRY_CARD_DATABASES, build_entry_cards

_RequestT = TypeVar("_RequestT")
_REFRESH = KeggRequestOptions(refresh=True)
_INFO_REQUESTS = tuple(
    InfoRequest(database=database)
    for database in (
        KeggInfoDatabase.KO,
        KeggInfoDatabase.COMPOUND,
        KeggInfoDatabase.GENOME,
        KeggInfoDatabase.BRITE,
        KeggInfoDatabase.GLYCAN,
        KeggInfoDatabase.RCLASS,
        KeggInfoDatabase.DRUG,
    )
)
_LIST_REQUESTS = tuple(OrganismPathwayListRequest(organism=organism) for organism in ("hsa", "eco"))
_FIND_REQUESTS = (
    FindRequest(database=KeggFindDatabase.KO, query="hexokinase"),
    FindRequest(database=KeggFindDatabase.COMPOUND, query="glucose"),
    FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query="C7H10O5",
        mode=KeggFindMode.FORMULA,
    ),
    FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query="174.05",
        mode=KeggFindMode.EXACT_MASS,
    ),
    FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query="300-310",
        mode=KeggFindMode.MOL_WEIGHT,
    ),
    FindRequest(database=KeggFindDatabase.GLYCAN, query="mannose"),
    FindRequest(database=KeggFindDatabase.DRUG, query="aspirin"),
    FindRequest(
        database=KeggFindDatabase.DRUG,
        query="180.063",
        mode=KeggFindMode.EXACT_MASS,
    ),
)
_GET_REQUESTS = (
    GetRequest(
        entries=(
            KeggEntryRef(
                database=KeggGetDatabase.BRITE,
                identifier="br08901",
                brite_kind=KeggBriteEntryKind.HIERARCHY,
            ),
        )
    ),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00844"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.MODULE, identifier="M00001"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier="map00010"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.REACTION, identifier="R01786"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier="2.7.1.1"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00031"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.GLYCAN, identifier="G00001"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.GENE, identifier="hsa:7157"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.GENOME, identifier="T01001"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.DRUG, identifier="D00109"),)),
    GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.RCLASS, identifier="RC00002"),)),
)
_LINK_REQUESTS = (
    LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=("K00844",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_BRITE,
        source_identifiers=("K00844",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.GENE_TO_PATHWAY,
        source_identifiers=("hsa:10458",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.COMPOUND_TO_REACTION,
        source_identifiers=("C00031",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
        source_identifiers=("taxid:562",),
        taxonomy_rank=KeggTaxonomyRank.SPECIES,
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_GENE,
        organism_scope="eco",
        source_identifiers=("K01810",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.PATHWAY_TO_GENE,
        organism_scope="hsa",
        source_identifiers=("hsa00010",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.MODULE_TO_KO,
        source_identifiers=("M00001",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.MODULE_TO_REACTION,
        source_identifiers=("M00001",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.GLYCAN_TO_REACTION,
        source_identifiers=("G00001",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.PATHWAY_TO_GLYCAN,
        source_identifiers=("map00510",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.PATHWAY_TO_MODULE,
        source_identifiers=("map01200",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.REACTION_TO_GLYCAN,
        source_identifiers=("R05969",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.GLYCAN_TO_PATHWAY,
        source_identifiers=("G00001",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.DRUG_TO_PATHWAY,
        source_identifiers=("D00109",),
    ),
    LinkRequest(
        relationship=KeggLinkRelationship.MODULE_TO_PATHWAY,
        source_identifiers=("M00001",),
    ),
)
_CONV_REQUESTS = (
    ConvRequest(
        target_database=KeggConvDatabase.UNIPROT,
        source_database=KeggConvDatabase.GENES,
        source_identifiers=("hsa:7157",),
    ),
    ConvRequest(
        target_database=KeggConvDatabase.NCBI_PROTEINID,
        source_database=KeggConvDatabase.GENES,
        source_identifiers=("hsa:10458",),
    ),
    ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.NCBI_GENEID,
        source_identifiers=("ncbi-geneid:948364",),
    ),
    ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.UNIPROT,
        source_identifiers=("uniprot:P04637",),
    ),
    ConvRequest(
        target_database=KeggConvDatabase.COMPOUND,
        source_database=KeggConvDatabase.CHEBI,
        source_identifiers=("chebi:4167",),
    ),
    ConvRequest(
        target_database=KeggConvDatabase.COMPOUND,
        source_database=KeggConvDatabase.PUBCHEM,
        source_identifiers=("pubchem:3333",),
    ),
    ConvRequest(
        target_database=KeggConvDatabase.PUBCHEM,
        source_database=KeggConvDatabase.DRUG,
        source_identifiers=("D00109",),
    ),
)

pytestmark = [
    pytest.mark.live_kegg,
    pytest.mark.skipif(
        os.environ.get("KEGG_MCP_RUN_LIVE_TESTS", "").lower() != "true",
        reason="set KEGG_MCP_RUN_LIVE_TESTS=true to run live KEGG tests",
    ),
]


def _rotated_requests(
    requests: tuple[_RequestT, ...],
    count: int,
) -> tuple[_RequestT, ...]:
    return tuple(islice(cycle(requests), count))


def _assert_single_network_request(batch: KeggBatchProvenance) -> None:
    assert batch.origin is ResponseOrigin.NETWORK
    assert batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert batch.attempt_count == 1
    assert batch.response_bytes > 0
    assert batch.parser_version == "4"
    assert not batch.is_stale


def test_live_info_rotates_stable_cases(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for request in _rotated_requests(_INFO_REQUESTS, live_requests_per_operation):
        result = live_kegg_client.info(request, options=_REFRESH)

        _assert_single_network_request(result.batch)
        assert result.document.database is request.database
        assert result.document.lines


def test_live_organism_pathway_list_rotates_stable_cases(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for request in _rotated_requests(_LIST_REQUESTS, live_requests_per_operation):
        result = live_kegg_client.list_organism_pathways(
            request,
            options=_REFRESH,
        )

        _assert_single_network_request(result.batch)
        assert result.request == request
        assert result.document.organism == request.organism
        assert result.document.rows
        assert all(
            row.pathway_id.startswith(f"path:{request.organism}") for row in result.document.rows
        )
        assert all(row.name.strip() for row in result.document.rows)


def test_live_get_rotates_stable_text_entry_cases(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for request in _rotated_requests(_GET_REQUESTS, live_requests_per_operation):
        result = live_kegg_client.get(request, options=_REFRESH)

        assert result.missing_entries == ()
        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert len(result.documents) == 1
        document = result.documents[0]
        if request.entries[0].database is KeggGetDatabase.BRITE:
            assert isinstance(document, KeggBriteHtextDocument)
            assert document.identifier == request.entries[0].identifier
            assert document.lines
        else:
            assert isinstance(document, KeggFlatFileDocument)
            assert get_entry_matches(request.entries[0], document.entries[0])
            if request.entries[0].database in ENTRY_CARD_DATABASES:
                snapshot = build_entry_cards(result)
                assert len(snapshot.entries) == 1
                assert snapshot.entries[0].entity.identifier == request.entries[0].identifier


def test_live_find_rotates_all_public_search_modes(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for request in _rotated_requests(_FIND_REQUESTS, live_requests_per_operation):
        result = live_kegg_client.find(request, options=_REFRESH)

        _assert_single_network_request(result.batch)
        assert result.request == request
        assert result.document.rows


def test_live_link_rotates_typed_relation_cases(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for request in _rotated_requests(_LINK_REQUESTS, live_requests_per_operation):
        result = live_kegg_client.link(request, options=_REFRESH)

        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert result.request == request
        assert result.rows


def test_live_conv_rotates_selected_identifier_directions(
    live_kegg_client: KeggClient,
    live_requests_per_operation: int,
) -> None:
    for request in _rotated_requests(_CONV_REQUESTS, live_requests_per_operation):
        result = live_kegg_client.conv(request, options=_REFRESH)

        assert len(result.batches) == 1
        _assert_single_network_request(result.batches[0])
        assert result.request == request
        assert result.rows
