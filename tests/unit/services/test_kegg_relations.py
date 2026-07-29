"""Tests for shared bounded selected-entry relation batching."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.execution import ReferenceLoadingLimits
from kegg_mcp.kegg import (
    GetRequest,
    GetResult,
    KeggClientConfig,
    KeggEntryRef,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    KeggTaxonomyRank,
    LinkRequest,
    LinkResult,
)
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    KeggPairRow,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services.kegg_relations import bounded_relation_batches
from kegg_mcp.services.reference_budget import SharedReferenceBudgetClient

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


def _provenance(
    marker: int,
    *,
    operation: KeggOperation = KeggOperation.LINK,
    response_bytes: int = 100,
) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        request_key=f"synthetic:{marker}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        response_bytes=response_bytes,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="synthetic",
        attempt_count=1,
        is_stale=False,
    )


class _RelationClient:
    def __init__(
        self,
        *,
        max_identifiers: int = 100,
        link_batch_size: int = 100,
        response_bytes: int = 100,
        provenance_batches_per_call: int = 1,
    ) -> None:
        self._config = KeggClientConfig.model_validate(
            {
                "limits": {
                    "max_identifiers": max_identifiers,
                    "link_batch_size": link_batch_size,
                }
            }
        )
        self.response_bytes = response_bytes
        self.provenance_batches_per_call = provenance_batches_per_call
        self.requests: list[LinkRequest] = []
        self.get_requests: list[GetRequest] = []

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        del options
        self.requests.append(request)
        call_index = len(self.requests)
        rows = tuple(
            KeggPairRow(
                line_number=index,
                source_id=f"ko:{identifier}",
                target_id=f"path:ko{index:05d}",
            )
            for index, identifier in enumerate(request.source_identifiers, start=1)
        )
        return LinkResult(
            request=request,
            rows=rows,
            batches=tuple(
                _provenance(
                    call_index * 10 + batch_index,
                    response_bytes=self.response_bytes,
                )
                for batch_index in range(self.provenance_batches_per_call)
            ),
        )

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del options
        self.get_requests.append(request)
        call_index = len(self.get_requests)
        return GetResult(
            request=request,
            documents=(),
            missing_entries=request.entries,
            batches=tuple(
                _provenance(
                    call_index * 10 + batch_index,
                    operation=KeggOperation.GET,
                    response_bytes=self.response_bytes,
                )
                for batch_index in range(self.provenance_batches_per_call)
            ),
        )


def test_empty_relation_input_performs_no_client_call() -> None:
    client = _RelationClient()

    result = bounded_relation_batches(
        (),
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        client=client,
    )

    assert result.rows == ()
    assert result.batches == ()
    assert client.requests == []


def test_relation_batches_honor_client_limit_and_rebase_batch_indexes() -> None:
    client = _RelationClient(link_batch_size=2)
    identifiers = tuple(f"K{index:05d}" for index in range(1, 6))

    result = bounded_relation_batches(
        identifiers,
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        client=client,
    )

    assert [request.source_identifiers for request in client.requests] == [
        ("K00001", "K00002"),
        ("K00003", "K00004"),
        ("K00005",),
    ]
    assert [row.batch_index for row in result.rows] == [0, 0, 1, 1, 2]
    assert len(result.batches) == 3


def test_relation_batches_check_the_actual_endpoint_request_limit_before_each_call() -> None:
    client = _RelationClient(link_batch_size=2)

    with pytest.raises(KeggMcpError) as captured:
        bounded_relation_batches(
            tuple(f"K{index:05d}" for index in range(1, 6)),
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            client=client,
            max_total_requests=2,
        )

    assert captured.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert {detail.name: detail.value for detail in captured.value.detail.safe_details}[
        "limit_name"
    ] == "relationship_request_count"
    assert len(client.requests) == 2


@pytest.mark.parametrize(
    ("limit_name", "max_total_rows", "max_total_response_bytes"),
    [
        ("relationship_row_count", 3, 5_000_000),
        ("relationship_response_bytes", 10_000, 150),
    ],
)
def test_relation_batches_check_rows_and_bytes_after_each_endpoint_batch(
    limit_name: str,
    max_total_rows: int,
    max_total_response_bytes: int,
) -> None:
    client = _RelationClient(link_batch_size=2, response_bytes=100)

    with pytest.raises(KeggMcpError) as captured:
        bounded_relation_batches(
            tuple(f"K{index:05d}" for index in range(1, 6)),
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            client=client,
            max_total_rows=max_total_rows,
            max_total_response_bytes=max_total_response_bytes,
        )

    assert captured.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert {detail.name: detail.value for detail in captured.value.detail.safe_details}[
        "limit_name"
    ] == limit_name
    assert len(client.requests) == 2


def test_relation_batches_invoke_the_budget_hook_before_starting_the_next_batch() -> None:
    client = _RelationClient(link_batch_size=2)
    recorded: list[tuple[int, int]] = []

    def record_batch(row_count: int, batches: tuple[KeggBatchProvenance, ...]) -> None:
        recorded.append((row_count, len(batches)))
        if len(recorded) == 2:
            raise RuntimeError("synthetic shared budget stop")

    with pytest.raises(RuntimeError, match="shared budget stop"):
        bounded_relation_batches(
            tuple(f"K{index:05d}" for index in range(1, 6)),
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            client=client,
            record_batch=record_batch,
        )

    assert recorded == [(2, 1), (2, 1)]
    assert len(client.requests) == 2


def test_relation_batches_forward_the_typed_taxonomy_rank_to_every_call() -> None:
    client = _RelationClient(max_identifiers=1)

    bounded_relation_batches(
        ("taxid:561", "taxid:562"),
        relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
        taxonomy_rank=KeggTaxonomyRank.SPECIES,
        client=client,
    )

    assert len(client.requests) == 2
    assert all(request.taxonomy_rank is KeggTaxonomyRank.SPECIES for request in client.requests)


def test_relation_batches_reject_duplicate_sources_before_client_use() -> None:
    client = _RelationClient()

    with pytest.raises(ValueError, match="must be unique"):
        bounded_relation_batches(
            ("K00001", "K00001"),
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            client=client,
        )

    assert client.requests == []


@pytest.mark.parametrize(
    ("max_total_requests", "max_total_rows", "max_total_response_bytes"),
    [
        (-1, 10_000, 5_000_000),
        (100, -1, 5_000_000),
        (100, 10_000, -1),
    ],
)
def test_relation_batches_reject_negative_aggregate_limits(
    max_total_requests: int,
    max_total_rows: int,
    max_total_response_bytes: int,
) -> None:
    with pytest.raises(ValueError, match="must be non-negative"):
        bounded_relation_batches(
            ("K00001",),
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            client=_RelationClient(),
            max_total_requests=max_total_requests,
            max_total_rows=max_total_rows,
            max_total_response_bytes=max_total_response_bytes,
        )


def test_shared_reference_budget_counts_actual_provenance_batches() -> None:
    underlying = _RelationClient(provenance_batches_per_call=2)
    client = SharedReferenceBudgetClient(
        underlying,
        ReferenceLoadingLimits(max_total_kegg_requests=2),
    )
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=("K00001",),
    )

    client.link(request)
    with pytest.raises(KeggMcpError) as captured:
        client.link(request)

    assert captured.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert len(underlying.requests) == 1


def test_shared_reference_budget_rejects_an_actual_multi_batch_overrun() -> None:
    underlying = _RelationClient(provenance_batches_per_call=2)
    client = SharedReferenceBudgetClient(
        underlying,
        ReferenceLoadingLimits(max_total_kegg_requests=1),
    )

    with pytest.raises(KeggMcpError) as captured:
        client.link(
            LinkRequest(
                relationship=KeggLinkRelationship.KO_TO_PATHWAY,
                source_identifiers=("K00001",),
            )
        )

    assert captured.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert len(underlying.requests) == 1


def test_shared_reference_budget_counts_actual_get_provenance_batches() -> None:
    underlying = _RelationClient(provenance_batches_per_call=2)
    client = SharedReferenceBudgetClient(
        underlying,
        ReferenceLoadingLimits(max_total_kegg_requests=2),
    )
    request = GetRequest(
        entries=(
            KeggEntryRef(
                database=KeggGetDatabase.MODULE,
                identifier="M00001",
            ),
        )
    )

    client.get(request)
    with pytest.raises(KeggMcpError) as captured:
        client.get(request)

    assert captured.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert len(underlying.get_requests) == 1
