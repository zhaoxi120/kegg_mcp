"""Shared bounded batching for selected-entry KEGG LINK relationships."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg import (
    KeggLinkRelationship,
    KeggRequestOptions,
    KeggTaxonomyRank,
    LinkRequest,
)
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggPairRow
from kegg_mcp.kegg.operations import prepare_link
from kegg_mcp.services.reference_budget import KeggRelationClient

DEFAULT_MAX_RELATION_REQUESTS = 100
DEFAULT_MAX_RELATION_ROWS = 10_000
DEFAULT_MAX_RELATION_RESPONSE_BYTES = 5_000_000
MAX_RELATION_IDENTIFIERS_PER_CALL = 100


@dataclass(frozen=True, slots=True)
class BoundedRelationResult:
    """Merged LINK rows and provenance with globally rebased batch indexes."""

    rows: tuple[KeggPairRow, ...]
    batches: tuple[KeggBatchProvenance, ...]


def bounded_relation_batches(
    source_identifiers: tuple[str, ...],
    *,
    relationship: KeggLinkRelationship,
    client: KeggRelationClient,
    options: KeggRequestOptions | None = None,
    taxonomy_rank: KeggTaxonomyRank = KeggTaxonomyRank.EXACT,
    max_total_requests: int = DEFAULT_MAX_RELATION_REQUESTS,
    max_total_rows: int = DEFAULT_MAX_RELATION_ROWS,
    max_total_response_bytes: int = DEFAULT_MAX_RELATION_RESPONSE_BYTES,
    record_batch: Callable[[int, tuple[KeggBatchProvenance, ...]], None] | None = None,
) -> BoundedRelationResult:
    """Fetch one selected-entry relation in bounded calls without changing row semantics."""
    if max_total_requests < 0 or max_total_rows < 0 or max_total_response_bytes < 0:
        raise ValueError("aggregate relationship limits must be non-negative")
    if len(source_identifiers) != len(set(source_identifiers)):
        raise ValueError("source_identifiers must be unique")
    if not source_identifiers:
        return BoundedRelationResult(rows=(), batches=())

    maximum_per_call = min(
        MAX_RELATION_IDENTIFIERS_PER_CALL,
        client.config.limits.max_identifiers,
    )
    rows: list[KeggPairRow] = []
    batches: list[KeggBatchProvenance] = []
    response_bytes = 0
    request_count = 0
    endpoint_bytes = len(client.config.access.endpoint.encode("ascii"))
    for start in range(0, len(source_identifiers), maximum_per_call):
        coarse_request = LinkRequest(
            relationship=relationship,
            taxonomy_rank=taxonomy_rank,
            source_identifiers=source_identifiers[start : start + maximum_per_call],
        )
        prepared_batches = prepare_link(
            coarse_request,
            client.config.limits,
            url_prefix_bytes=endpoint_bytes,
        )
        for prepared in prepared_batches:
            next_request_count = request_count + 1
            if next_request_count > max_total_requests:
                _limit_exceeded(
                    "relationship_request_count",
                    next_request_count,
                    max_total_requests,
                )
            result = client.link(
                LinkRequest(
                    relationship=relationship,
                    taxonomy_rank=taxonomy_rank,
                    source_identifiers=prepared.requested_identifiers,
                ),
                options=options,
            )
            if len(result.batches) != 1:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "A selected-entry relationship call returned an unexpected batch count.",
                    suggested_action=(
                        "Retry with the typed KEGG client and unchanged request limits."
                    ),
                )
            next_row_count = len(rows) + len(result.rows)
            next_response_bytes = response_bytes + result.batches[0].response_bytes
            if next_row_count > max_total_rows:
                _limit_exceeded(
                    "relationship_row_count",
                    next_row_count,
                    max_total_rows,
                )
            if next_response_bytes > max_total_response_bytes:
                _limit_exceeded(
                    "relationship_response_bytes",
                    next_response_bytes,
                    max_total_response_bytes,
                )
            if record_batch is not None:
                record_batch(len(result.rows), result.batches)
            batch_offset = len(batches)
            rows.extend(
                KeggPairRow(
                    batch_index=row.batch_index + batch_offset,
                    line_number=row.line_number,
                    source_id=row.source_id,
                    target_id=row.target_id,
                )
                for row in result.rows
            )
            batches.extend(result.batches)
            request_count = next_request_count
            response_bytes = next_response_bytes
    return BoundedRelationResult(rows=tuple(rows), batches=tuple(batches))


def _limit_exceeded(name: str, observed: int, limit: int) -> None:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "The selected KEGG relationship exceeded its aggregate service bound.",
        suggested_action="Use fewer source identifiers or a narrower relationship request.",
        safe_details=(
            SafeDetail(name="limit_name", value=name),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit", value=str(limit)),
        ),
    )


__all__ = [
    "DEFAULT_MAX_RELATION_REQUESTS",
    "DEFAULT_MAX_RELATION_RESPONSE_BYTES",
    "DEFAULT_MAX_RELATION_ROWS",
    "BoundedRelationResult",
    "bounded_relation_batches",
]
