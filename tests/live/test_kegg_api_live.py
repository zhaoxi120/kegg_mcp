"""Opt-in, bounded compatibility checks against an explicitly authorized KEGG endpoint.

The identifier catalog and external mappings were verified against the official KEGG API on
2026-07-15. Live assertions intentionally avoid snapshots and mutable database statistics.
"""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TypeAlias, TypeVar

import pytest

from kegg_mcp.kegg import (
    CacheLookupState,
    ConvRequest,
    GetRequest,
    InfoRequest,
    KeggBatchProvenance,
    KeggBriteEntryKind,
    KeggClient,
    KeggClientLimits,
    KeggConvDatabase,
    KeggEntryRef,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import (
    KeggBriteHtextDocument,
    KeggFlatFileDocument,
    KeggOperation,
)
from kegg_mcp.kegg.operations import (
    PreparedRequest,
    pair_target_matches,
    prepare_conv,
    prepare_get,
    prepare_info,
    prepare_link,
)

_T = TypeVar("_T")
LiveRequest: TypeAlias = InfoRequest | GetRequest | LinkRequest | ConvRequest


@dataclass(frozen=True, slots=True)
class LiveCase:
    case_id: str
    operation: KeggOperation
    request: LiveRequest
    expected_targets: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class _GeneIdentity:
    gene: str
    ncbi_gene_id: str
    ncbi_protein_id: str
    uniprot_accession: str


def _shuffled(values: Iterable[_T], *, seed: int) -> tuple[_T, ...]:
    result = list(values)
    random.Random(seed).shuffle(result)
    return tuple(result)


_INFO_DATABASES = _shuffled(tuple(KeggInfoDatabase) * 3, seed=2026071501)
INFO_CASES = tuple(
    LiveCase(
        case_id=f"info-{index:02d}-{database.value}",
        operation=KeggOperation.INFO,
        request=InfoRequest(database=database),
    )
    for index, database in enumerate(_INFO_DATABASES, start=1)
)

_GET_ENTRIES = _shuffled(
    (
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00844"),
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K01810"),
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K01623"),
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00134"),
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00927"),
        KeggEntryRef(database=KeggGetDatabase.MODULE, identifier="M00001"),
        KeggEntryRef(database=KeggGetDatabase.MODULE, identifier="M00002"),
        KeggEntryRef(database=KeggGetDatabase.MODULE, identifier="M00003"),
        KeggEntryRef(database=KeggGetDatabase.MODULE, identifier="M00165"),
        KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier="map00010"),
        KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier="ko00010"),
        KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier="map00020"),
        KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier="map00030"),
        KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier="map00190"),
        KeggEntryRef(database=KeggGetDatabase.REACTION, identifier="R00200"),
        KeggEntryRef(database=KeggGetDatabase.REACTION, identifier="R00209"),
        KeggEntryRef(database=KeggGetDatabase.REACTION, identifier="R01061"),
        KeggEntryRef(database=KeggGetDatabase.REACTION, identifier="R01512"),
        KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier="2.7.1.1"),
        KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier="5.3.1.9"),
        KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier="1.1.1.1"),
        KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier="2.7.1.40"),
        KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00031"),
        KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00022"),
        KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00002"),
        KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00024"),
        KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00001"),
        KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00003"),
        KeggEntryRef(database=KeggGetDatabase.COMPOUND, identifier="C00004"),
        KeggEntryRef(
            database=KeggGetDatabase.BRITE,
            identifier="br08901",
            brite_kind=KeggBriteEntryKind.HIERARCHY,
        ),
    ),
    seed=2026071502,
)
GET_CASES = tuple(
    LiveCase(
        case_id=f"get-{index:02d}-{entry.database.value}-{entry.identifier}",
        operation=KeggOperation.GET,
        request=GetRequest(entries=(entry,)),
    )
    for index, entry in enumerate(_GET_ENTRIES, start=1)
)

_LINK_INPUTS = _shuffled(
    (
        (KeggLinkRelationship.KO_TO_PATHWAY, "K00134"),
        (KeggLinkRelationship.KO_TO_PATHWAY, "K01834"),
        (KeggLinkRelationship.KO_TO_PATHWAY, "K01810"),
        (KeggLinkRelationship.KO_TO_PATHWAY, "K00927"),
        (KeggLinkRelationship.KO_TO_PATHWAY, "K00873"),
        (KeggLinkRelationship.KO_TO_MODULE, "K01623"),
        (KeggLinkRelationship.KO_TO_MODULE, "K01803"),
        (KeggLinkRelationship.KO_TO_MODULE, "K00844"),
        (KeggLinkRelationship.KO_TO_MODULE, "K00850"),
        (KeggLinkRelationship.KO_TO_MODULE, "K00134"),
        (KeggLinkRelationship.KO_TO_REACTION, "K00927"),
        (KeggLinkRelationship.KO_TO_REACTION, "K00850"),
        (KeggLinkRelationship.KO_TO_REACTION, "K01623"),
        (KeggLinkRelationship.KO_TO_REACTION, "K01834"),
        (KeggLinkRelationship.KO_TO_REACTION, "K00844"),
        (KeggLinkRelationship.KO_TO_ENZYME, "K00844"),
        (KeggLinkRelationship.KO_TO_ENZYME, "K01689"),
        (KeggLinkRelationship.KO_TO_ENZYME, "K00134"),
        (KeggLinkRelationship.KO_TO_ENZYME, "K00873"),
        (KeggLinkRelationship.KO_TO_ENZYME, "K01810"),
        (KeggLinkRelationship.KO_TO_BRITE, "K01810"),
        (KeggLinkRelationship.KO_TO_BRITE, "K00873"),
        (KeggLinkRelationship.KO_TO_BRITE, "K00927"),
        (KeggLinkRelationship.KO_TO_BRITE, "K01689"),
        (KeggLinkRelationship.KO_TO_BRITE, "K01623"),
        (KeggLinkRelationship.PATHWAY_TO_KO, "map00190"),
        (KeggLinkRelationship.PATHWAY_TO_KO, "map00030"),
        (KeggLinkRelationship.PATHWAY_TO_KO, "ko00010"),
        (KeggLinkRelationship.PATHWAY_TO_KO, "map00020"),
        (KeggLinkRelationship.PATHWAY_TO_KO, "map00010"),
    ),
    seed=2026071503,
)
LINK_CASES = tuple(
    LiveCase(
        case_id=f"link-{index:02d}-{relationship.value}-{identifier}",
        operation=KeggOperation.LINK,
        request=LinkRequest(
            relationship=relationship,
            source_identifiers=(identifier,),
        ),
    )
    for index, (relationship, identifier) in enumerate(_LINK_INPUTS, start=1)
)

_GENE_IDENTITIES = (
    _GeneIdentity(
        gene="hsa:7157",
        ncbi_gene_id="ncbi-geneid:7157",
        ncbi_protein_id="ncbi-proteinid:NP_000537",
        uniprot_accession="P04637",
    ),
    _GeneIdentity(
        gene="hsa:1956",
        ncbi_gene_id="ncbi-geneid:1956",
        ncbi_protein_id="ncbi-proteinid:NP_005219",
        uniprot_accession="P00533",
    ),
    _GeneIdentity(
        gene="hsa:672",
        ncbi_gene_id="ncbi-geneid:672",
        ncbi_protein_id="ncbi-proteinid:NP_001394522",
        uniprot_accession="P38398",
    ),
    _GeneIdentity(
        gene="hsa:3845",
        ncbi_gene_id="ncbi-geneid:3845",
        ncbi_protein_id="ncbi-proteinid:NP_001356715",
        uniprot_accession="P01116",
    ),
    _GeneIdentity(
        gene="hsa:5290",
        ncbi_gene_id="ncbi-geneid:5290",
        ncbi_protein_id="ncbi-proteinid:NP_006209",
        uniprot_accession="P42336",
    ),
)


def _conversion_cases() -> tuple[LiveCase, ...]:
    cases: list[LiveCase] = []
    for identity in _GENE_IDENTITIES:
        uniprot = f"uniprot:{identity.uniprot_accession}"
        cases.extend(
            (
                LiveCase(
                    case_id=f"conv-genes-to-geneid-{identity.gene}",
                    operation=KeggOperation.CONV,
                    request=ConvRequest(
                        target_database=KeggConvDatabase.NCBI_GENEID,
                        source_database=KeggConvDatabase.GENES,
                        source_identifiers=(identity.gene,),
                    ),
                    expected_targets=frozenset({identity.ncbi_gene_id}),
                ),
                LiveCase(
                    case_id=f"conv-genes-to-proteinid-{identity.gene}",
                    operation=KeggOperation.CONV,
                    request=ConvRequest(
                        target_database=KeggConvDatabase.NCBI_PROTEINID,
                        source_database=KeggConvDatabase.GENES,
                        source_identifiers=(identity.gene,),
                    ),
                    expected_targets=frozenset({identity.ncbi_protein_id}),
                ),
                LiveCase(
                    case_id=f"conv-genes-to-uniprot-{identity.gene}",
                    operation=KeggOperation.CONV,
                    request=ConvRequest(
                        target_database=KeggConvDatabase.UNIPROT,
                        source_database=KeggConvDatabase.GENES,
                        source_identifiers=(identity.gene,),
                    ),
                    expected_targets=frozenset({uniprot, f"up:{identity.uniprot_accession}"}),
                ),
                LiveCase(
                    case_id=f"conv-geneid-to-genes-{identity.gene}",
                    operation=KeggOperation.CONV,
                    request=ConvRequest(
                        target_database=KeggConvDatabase.GENES,
                        source_database=KeggConvDatabase.NCBI_GENEID,
                        source_identifiers=(identity.ncbi_gene_id,),
                    ),
                    expected_targets=frozenset({identity.gene}),
                ),
                LiveCase(
                    case_id=f"conv-proteinid-to-genes-{identity.gene}",
                    operation=KeggOperation.CONV,
                    request=ConvRequest(
                        target_database=KeggConvDatabase.GENES,
                        source_database=KeggConvDatabase.NCBI_PROTEINID,
                        source_identifiers=(identity.ncbi_protein_id,),
                    ),
                    expected_targets=frozenset({identity.gene}),
                ),
                LiveCase(
                    case_id=f"conv-uniprot-to-genes-{identity.gene}",
                    operation=KeggOperation.CONV,
                    request=ConvRequest(
                        target_database=KeggConvDatabase.GENES,
                        source_database=KeggConvDatabase.UNIPROT,
                        source_identifiers=(uniprot,),
                    ),
                    expected_targets=frozenset({identity.gene}),
                ),
            )
        )
    return _shuffled(cases, seed=2026071504)


CONV_CASES = _conversion_cases()
ALL_LIVE_CASES = _shuffled(
    (*INFO_CASES, *GET_CASES, *LINK_CASES, *CONV_CASES),
    seed=2026071505,
)


def _prepared(case: LiveCase, client: KeggClient) -> tuple[PreparedRequest, ...]:
    request = case.request
    if isinstance(request, InfoRequest):
        return prepare_info(request, client.config.limits)
    if isinstance(request, GetRequest):
        return prepare_get(request, client.config.limits)
    if isinstance(request, LinkRequest):
        return prepare_link(request, client.config.limits)
    return prepare_conv(request, client.config.limits)


def _info_request(case: LiveCase) -> InfoRequest:
    request = case.request
    assert isinstance(request, InfoRequest)
    return request


def _get_request(case: LiveCase) -> GetRequest:
    request = case.request
    assert isinstance(request, GetRequest)
    return request


def _link_request(case: LiveCase) -> LinkRequest:
    request = case.request
    assert isinstance(request, LinkRequest)
    return request


def _conv_request(case: LiveCase) -> ConvRequest:
    request = case.request
    assert isinstance(request, ConvRequest)
    return request


def test_live_case_catalog_is_bounded_diverse_and_one_request_per_case() -> None:
    """Validate the campaign plan without enabling or contacting a live endpoint."""
    groups = (INFO_CASES, GET_CASES, LINK_CASES, CONV_CASES)
    info_requests = tuple(_info_request(case) for case in INFO_CASES)
    get_requests = tuple(_get_request(case) for case in GET_CASES)
    link_requests = tuple(_link_request(case) for case in LINK_CASES)
    conv_requests = tuple(_conv_request(case) for case in CONV_CASES)
    assert all(len(group) == 30 for group in groups)
    assert all(
        len(requests) == 30
        for requests in (info_requests, get_requests, link_requests, conv_requests)
    )
    assert len(ALL_LIVE_CASES) == 120
    assert len({case.case_id for case in ALL_LIVE_CASES}) == 120
    assert Counter(case.operation for case in ALL_LIVE_CASES) == {
        operation: 30 for operation in KeggOperation
    }
    assert Counter(request.database for request in info_requests) == {
        database: 3 for database in KeggInfoDatabase
    }
    assert Counter(request.relationship for request in link_requests) == {
        relationship: 5 for relationship in KeggLinkRelationship
    }
    assert Counter(
        (request.target_database, request.source_database) for request in conv_requests
    ) == {
        (target, source): 5
        for target, source in (
            (KeggConvDatabase.NCBI_GENEID, KeggConvDatabase.GENES),
            (KeggConvDatabase.NCBI_PROTEINID, KeggConvDatabase.GENES),
            (KeggConvDatabase.UNIPROT, KeggConvDatabase.GENES),
            (KeggConvDatabase.GENES, KeggConvDatabase.NCBI_GENEID),
            (KeggConvDatabase.GENES, KeggConvDatabase.NCBI_PROTEINID),
            (KeggConvDatabase.GENES, KeggConvDatabase.UNIPROT),
        )
    }

    limits = KeggClientLimits(max_identifiers=1, relation_batch_size=1)
    for case in ALL_LIVE_CASES:
        request = case.request
        if isinstance(request, InfoRequest):
            prepared = prepare_info(request, limits)
        elif isinstance(request, GetRequest):
            prepared = prepare_get(request, limits)
        elif isinstance(request, LinkRequest):
            prepared = prepare_link(request, limits)
        else:
            prepared = prepare_conv(request, limits)
        assert len(prepared) == 1
        assert prepared[0].operation is case.operation

    operations = tuple(case.operation for case in ALL_LIVE_CASES)
    assert operations != tuple(sorted(operations, key=lambda operation: operation.value))
    ko_numbers = tuple(
        int(request.entries[0].identifier[1:])
        for request in get_requests
        if request.entries[0].database is KeggGetDatabase.KO
    )
    assert ko_numbers != tuple(sorted(ko_numbers))


def _assert_network_batch(batch: KeggBatchProvenance, operation: KeggOperation) -> None:
    assert batch.operation is operation
    assert batch.origin is ResponseOrigin.NETWORK
    assert batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert batch.attempt_count == 1
    assert batch.response_bytes > 0
    assert not batch.is_stale


def _run_info(case: LiveCase, client: KeggClient, request: InfoRequest) -> None:
    result = client.info(request, options=KeggRequestOptions(refresh=True))
    _assert_network_batch(result.batch, case.operation)
    assert result.document.database is request.database
    assert result.document.lines


def _run_get(case: LiveCase, client: KeggClient, request: GetRequest) -> None:
    result = client.get(request, options=KeggRequestOptions(refresh=True))
    assert len(result.batches) == 1
    _assert_network_batch(result.batches[0], case.operation)
    assert result.missing_entries == ()
    assert len(result.documents) == 1

    expected = request.entries[0]
    document = result.documents[0]
    if expected.database is KeggGetDatabase.BRITE:
        assert isinstance(document, KeggBriteHtextDocument)
        assert document.identifier == expected.identifier
        assert document.lines
    else:
        assert isinstance(document, KeggFlatFileDocument)
        assert len(document.entries) == 1
        entry = document.entries[0]
        assert entry.identifier == expected.identifier
        assert any(field.name == "ENTRY" for field in entry.fields)


def _run_link(case: LiveCase, client: KeggClient, request: LinkRequest) -> None:
    result = client.link(request, options=KeggRequestOptions(refresh=True))
    assert len(result.batches) == 1
    _assert_network_batch(result.batches[0], case.operation)
    assert result.rows

    prepared = _prepared(case, client)[0]
    assert prepared.pair_target_database is not None
    assert all(row.source_id in prepared.expected_pair_source_ids for row in result.rows)
    assert all(
        pair_target_matches(prepared.pair_target_database, row.target_id) for row in result.rows
    )


def _run_conv(case: LiveCase, client: KeggClient, request: ConvRequest) -> None:
    result = client.conv(request, options=KeggRequestOptions(refresh=True))
    assert len(result.batches) == 1
    _assert_network_batch(result.batches[0], case.operation)
    assert result.rows

    prepared = _prepared(case, client)[0]
    assert prepared.pair_target_database is not None
    assert all(row.source_id in prepared.expected_pair_source_ids for row in result.rows)
    assert all(
        pair_target_matches(prepared.pair_target_database, row.target_id) for row in result.rows
    )
    assert case.expected_targets.intersection(row.target_id for row in result.rows)


@pytest.mark.live_kegg
@pytest.mark.parametrize(
    "case",
    ALL_LIVE_CASES,
    ids=tuple(case.case_id for case in ALL_LIVE_CASES),
)
def test_live_kegg_api_case(case: LiveCase, live_kegg_client: KeggClient) -> None:
    """Execute one shuffled case, exactly one real HTTP request, and bounded assertions."""
    request = case.request
    if isinstance(request, InfoRequest):
        _run_info(case, live_kegg_client, request)
    elif isinstance(request, GetRequest):
        _run_get(case, live_kegg_client, request)
    elif isinstance(request, LinkRequest):
        _run_link(case, live_kegg_client, request)
    else:
        _run_conv(case, live_kegg_client, request)
