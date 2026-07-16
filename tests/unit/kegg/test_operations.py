"""Tests for bounded KEGG request preparation."""

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg.contracts import (
    ConvRequest,
    GetRequest,
    InfoRequest,
    KeggBriteEntryKind,
    KeggClientLimits,
    KeggConvDatabase,
    KeggEntryRef,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    LinkRequest,
)
from kegg_mcp.kegg.operations import (
    PairTargetDatabase,
    ResponseParser,
    pair_target_matches,
    prepare_conv,
    prepare_get,
    prepare_info,
    prepare_link,
)


def _ko(number: int) -> KeggEntryRef:
    return KeggEntryRef(database=KeggGetDatabase.KO, identifier=f"K{number:05d}")


def test_info_preparation_uses_only_the_typed_database() -> None:
    batches = prepare_info(InfoRequest(database=KeggInfoDatabase.KO), KeggClientLimits())

    assert len(batches) == 1
    assert batches[0].path == "/info/ko"
    assert batches[0].parser is ResponseParser.INFO


def test_get_never_batches_more_than_ten_entries() -> None:
    batches = prepare_get(
        GetRequest(entries=tuple(_ko(index) for index in range(1, 24))), KeggClientLimits()
    )

    assert [len(batch.requested_entries) for batch in batches] == [10, 10, 3]
    assert all(batch.path.count("+") <= 9 for batch in batches)


def test_get_isolates_brite_htext_from_flat_file_batches() -> None:
    request = GetRequest(
        entries=(
            _ko(1),
            KeggEntryRef(
                database=KeggGetDatabase.BRITE,
                identifier="br08901",
                brite_kind=KeggBriteEntryKind.HIERARCHY,
            ),
            _ko(2),
        )
    )

    batches = prepare_get(request, KeggClientLimits())

    assert [batch.parser for batch in batches] == [
        ResponseParser.FLAT_FILE,
        ResponseParser.BRITE_HTEXT,
        ResponseParser.FLAT_FILE,
    ]
    assert batches[1].path == "/get/br:br08901"


def test_link_uses_bounded_selected_identifier_batches() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_MODULE,
        source_identifiers=tuple(f"K{index:05d}" for index in range(1, 6)),
    )

    batches = prepare_link(request, KeggClientLimits(link_batch_size=2))

    assert [batch.requested_identifiers for batch in batches] == [
        ("K00001", "K00002"),
        ("K00003", "K00004"),
        ("K00005",),
    ]
    assert batches[0].path == "/link/module/K00001+K00002"
    assert batches[0].expected_pair_source_ids == frozenset({"ko:K00001", "ko:K00002"})
    assert batches[0].pair_target_database is PairTargetDatabase.MODULE


def test_link_greedily_packs_seventy_three_kos_into_one_default_request() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=tuple(f"K{index:05d}" for index in range(1, 74)),
    )

    batches = prepare_link(request, KeggClientLimits())

    assert len(batches) == 1
    assert len(batches[0].requested_identifiers) == 73
    assert batches[0].normalized_request_key.startswith("v2:/link/pathway/")


def test_link_greedy_batches_are_canonical_and_respect_the_url_limit() -> None:
    identifiers = tuple(f"K{index:05d}" for index in range(1, 101))
    forward = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=identifiers,
    )
    reversed_request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=tuple(reversed(identifiers)),
    )
    limits = KeggClientLimits(max_url_bytes=256)
    endpoint_bytes = len("https://rest.kegg.jp".encode("ascii"))

    batches = prepare_link(forward, limits, url_prefix_bytes=endpoint_bytes)
    reversed_batches = prepare_link(
        reversed_request,
        limits,
        url_prefix_bytes=endpoint_bytes,
    )

    assert len(batches) > 1
    assert [batch.normalized_request_key for batch in batches] == [
        batch.normalized_request_key for batch in reversed_batches
    ]
    assert all(
        endpoint_bytes + len(batch.path.encode("ascii")) <= limits.max_url_bytes
        for batch in batches
    )
    assert (
        tuple(identifier for batch in batches for identifier in batch.requested_identifiers)
        == identifiers
    )


def test_conv_never_prepares_a_whole_database_conversion() -> None:
    request = ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.UNIPROT,
        source_identifiers=("uniprot:P12345", "uniprot:Q00001"),
    )

    batches = prepare_conv(request, KeggClientLimits(relation_batch_size=1))

    assert [batch.path for batch in batches] == [
        "/conv/genes/uniprot:P12345",
        "/conv/genes/uniprot:Q00001",
    ]
    assert batches[0].expected_pair_source_ids == frozenset({"uniprot:P12345", "up:P12345"})
    assert batches[0].pair_target_database is PairTargetDatabase.GENES


@pytest.mark.parametrize(
    ("database", "identifier"),
    [
        (PairTargetDatabase.PATHWAY, "path:map00010"),
        (PairTargetDatabase.PATHWAY, "path:hsa00010"),
        (PairTargetDatabase.PATHWAY, "path:ddi00230"),
        (PairTargetDatabase.PATHWAY, "path:vg00541"),
        (PairTargetDatabase.PATHWAY, "path:vx00541"),
        (PairTargetDatabase.MODULE, "md:M00001"),
        (PairTargetDatabase.REACTION, "rn:R00001"),
        (PairTargetDatabase.ENZYME, "ec:1.1.1.1"),
        (PairTargetDatabase.ENZYME, "ec:1.1.1.-"),
        (PairTargetDatabase.ENZYME, "enzyme:1.-.-.-"),
        (PairTargetDatabase.BRITE, "br:ko00001"),
        (PairTargetDatabase.BRITE, "br:hsa00001"),
        (PairTargetDatabase.BRITE, "br:ddi00001"),
        (PairTargetDatabase.KO, "ko:K00001"),
        (PairTargetDatabase.GENES, "T01001:10458"),
        (PairTargetDatabase.GENES, "hsa:10458"),
        (PairTargetDatabase.GENES, "ddi:DDB_G0291764"),
        (PairTargetDatabase.GENES, "ag:ENTRY1"),
        (PairTargetDatabase.NCBI_GENEID, "ncbi-geneid:948364"),
        (PairTargetDatabase.UNIPROT, "up:P12345"),
    ],
)
def test_pair_target_contract_accepts_expected_namespaces(
    database: PairTargetDatabase, identifier: str
) -> None:
    assert pair_target_matches(database, identifier)


def test_pair_target_contract_rejects_a_wrong_relationship_namespace() -> None:
    assert not pair_target_matches(PairTargetDatabase.MODULE, "path:map00010")


@pytest.mark.parametrize(
    ("database", "identifier"),
    [
        (PairTargetDatabase.PATHWAY, "path:module00010"),
        (PairTargetDatabase.PATHWAY, "path:path00010"),
        (PairTargetDatabase.BRITE, "br:module00010"),
        (PairTargetDatabase.BRITE, "br:map00010"),
        (PairTargetDatabase.ENZYME, "ec:0.1.1.1"),
        (PairTargetDatabase.ENZYME, "ec:8.1.1.1"),
        (PairTargetDatabase.ENZYME, "ec:1.--.1.1"),
        (PairTargetDatabase.ENZYME, "ec:1.1-2.3.4"),
        (PairTargetDatabase.ENZYME, "ec:1.1.-.1"),
        (PairTargetDatabase.GENES, "uniprot:P12345"),
        (PairTargetDatabase.GENES, "ko:K00001"),
        (PairTargetDatabase.GENES, "vtax:1234"),
        (PairTargetDatabase.NCBI_GENEID, "ncbi-geneid:not-a-number"),
        (PairTargetDatabase.NCBI_GENEID, "ncbi-geneid:0"),
    ],
)
def test_pair_target_contract_rejects_invalid_internal_namespaces(
    database: PairTargetDatabase, identifier: str
) -> None:
    assert not pair_target_matches(database, identifier)


def test_service_limit_is_enforced_before_batching() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=("K00001", "K00002"),
    )

    with pytest.raises(KeggMcpError) as caught:
        prepare_link(request, KeggClientLimits(max_identifiers=1))

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_preparation_rejects_a_url_above_the_configured_bound() -> None:
    request = ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.UNIPROT,
        source_identifiers=("uniprot:" + "A" * 240,),
    )

    with pytest.raises(KeggMcpError) as caught:
        prepare_conv(request, KeggClientLimits(max_url_bytes=256))

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
