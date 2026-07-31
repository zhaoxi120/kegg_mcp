"""Tests for the stable Milestone 2 public import surface."""

from kegg_mcp.kegg import (
    FindRequest,
    FindResult,
    GetRequest,
    KeggClient,
    KeggClientConfig,
    KeggEntryRef,
    KeggFindDatabase,
    KeggFindDocument,
    KeggFindMode,
    KeggFindRow,
    KeggGetDatabase,
    KeggTaxonomyRank,
    OfflineCacheAccess,
    OrganismPathwayListRequest,
    OrganismPathwayListResult,
    PathwayAssetKind,
    PathwayAssetRequest,
)


def test_public_client_contracts_are_importable_from_kegg_package() -> None:
    config = KeggClientConfig()
    request = GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),))
    find_request = FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query="C7H10O5",
        mode=KeggFindMode.FORMULA,
    )
    asset_request = PathwayAssetRequest(
        pathway_id="ko00010",
        kind=PathwayAssetKind.KGML,
    )

    assert KeggClient
    assert isinstance(config.access, OfflineCacheAccess)
    assert request.entries[0].identifier == "K00001"
    assert find_request.database is KeggFindDatabase.COMPOUND
    assert FindResult
    assert KeggFindDocument
    assert KeggFindRow
    assert KeggTaxonomyRank.EXACT.value == "exact"
    assert OrganismPathwayListRequest(organism="hsa").organism == "hsa"
    assert OrganismPathwayListResult
    assert asset_request.pathway_id == "ko00010"
