"""Tests for the stable Milestone 2 public import surface."""

from kegg_mcp.kegg import (
    GetRequest,
    KeggClient,
    KeggClientConfig,
    KeggEntryRef,
    KeggGetDatabase,
    PublicAcademicAccess,
)


def test_public_client_contracts_are_importable_from_kegg_package() -> None:
    config = KeggClientConfig()
    request = GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),))

    assert KeggClient
    assert isinstance(config.access, PublicAcademicAccess)
    assert request.entries[0].identifier == "K00001"
