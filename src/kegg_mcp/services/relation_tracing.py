"""Bounded traversal over the typed KEGG LINK allowlist."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, NoReturn

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggPairRow
from kegg_mcp.services.kegg_relations import (
    DEFAULT_MAX_RELATION_RESPONSE_BYTES,
    BoundedRelationResult,
    bounded_relation_batches,
)
from kegg_mcp.services.models import DETAIL_SECTION
from kegg_mcp.services.query_models import (
    KeggEntityKind,
    KeggEntityRef,
    KeggRelationEdge,
    KeggRelationType,
    ResolutionOperation,
    TraceKeggRelationsRequest,
    TraceKeggRelationsResult,
    relation_entity_kinds,
)
from kegg_mcp.services.query_support import (
    bounded_query_payload,
    entity_key,
    fail_unexpected_relation_row,
    genome_lookup_from_pair,
    link_relationship,
    load_genome_records,
    pair_entity,
    require_provenance_bound,
)
from kegg_mcp.services.reference_budget import KeggQueryClient, effective_query_options
from kegg_mcp.services.result_builders import _artifact_metadata
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    SQLiteResultStore,
    compensate_created_result,
)

MAX_TRACE_KEGG_REQUESTS = 128
_MAX_TRACE_RAW_ROW_FACTOR = 4


@dataclass(frozen=True, slots=True)
class _TracedPair:
    source: KeggEntityRef
    target: KeggEntityRef
    provenance_batch_indexes: tuple[int, ...]


@dataclass(slots=True)
class _TraceBudget:
    row_limit: int
    request_limit: int = MAX_TRACE_KEGG_REQUESTS
    response_byte_limit: int = DEFAULT_MAX_RELATION_RESPONSE_BYTES
    requests: int = 0
    rows: int = 0
    response_bytes: int = 0

    @property
    def remaining_requests(self) -> int:
        return self.request_limit - self.requests

    @property
    def remaining_rows(self) -> int:
        return self.row_limit - self.rows

    @property
    def remaining_response_bytes(self) -> int:
        return self.response_byte_limit - self.response_bytes

    def record(
        self,
        *,
        row_count: int,
        batches: Iterable[KeggBatchProvenance],
    ) -> None:
        batch_tuple = tuple(batches)
        next_requests = self.requests + len(batch_tuple)
        next_rows = self.rows + row_count
        next_response_bytes = self.response_bytes + sum(
            batch.response_bytes for batch in batch_tuple
        )
        if next_requests > self.request_limit:
            _trace_limit(
                "kegg_request_count",
                next_requests,
                self.request_limit,
            )
        if next_rows > self.row_limit:
            _trace_limit("raw_relation_row_count", next_rows, self.row_limit)
        if next_response_bytes > self.response_byte_limit:
            _trace_limit(
                "kegg_response_bytes",
                next_response_bytes,
                self.response_byte_limit,
            )
        self.requests = next_requests
        self.rows = next_rows
        self.response_bytes = next_response_bytes


def trace_kegg_relations(
    request: TraceKeggRelationsRequest,
    *,
    client: KeggQueryClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
) -> TraceKeggRelationsResult:
    """Traverse one or two bounded levels over the fixed typed LINK allowlist."""
    options = effective_query_options(options)
    nodes = list(request.seeds)
    node_keys = {entity_key(node) for node in nodes}
    edges: list[KeggRelationEdge] = []
    edge_indexes: dict[
        tuple[KeggRelationType, tuple[str, str], tuple[str, str]],
        int,
    ] = {}
    queried: set[tuple[KeggRelationType, tuple[str, str]]] = set()
    provenance: list[KeggBatchProvenance] = []
    steps: list[dict[str, Any]] = []
    frontier = list(request.seeds)
    budget = _TraceBudget(
        row_limit=min(
            request.max_edges * _MAX_TRACE_RAW_ROW_FACTOR,
            10_000,
        )
    )

    for depth in range(1, request.max_depth + 1):
        next_frontier: list[KeggEntityRef] = []
        next_frontier_keys: set[tuple[str, str]] = set()
        for relationship in request.edge_types:
            source_kind, _ = relation_entity_kinds(relationship)
            sources = [
                entity
                for entity in frontier
                if entity.kind is source_kind and (relationship, entity_key(entity)) not in queried
            ]
            if not sources:
                continue
            for source in sources:
                queried.add((relationship, entity_key(source)))
            pairs, relation_provenance, relation_steps = _trace_relation(
                relationship,
                tuple(sources),
                client=client,
                options=options,
                budget=budget,
                depth=depth,
            )
            provenance_offset = len(provenance)
            provenance.extend(relation_provenance)
            steps.extend(relation_steps)
            for pair in pairs:
                source = pair.source
                target = pair.target
                edge_key = (
                    relationship,
                    entity_key(source),
                    entity_key(target),
                )
                pair_provenance_indexes = tuple(
                    provenance_offset + index for index in pair.provenance_batch_indexes
                )
                if edge_key in edge_indexes:
                    edge_index = edge_indexes[edge_key]
                    existing = edges[edge_index]
                    merged_indexes = tuple(
                        sorted(
                            {
                                *existing.provenance_batch_indexes,
                                *pair_provenance_indexes,
                            }
                        )
                    )
                    if merged_indexes != existing.provenance_batch_indexes:
                        edges[edge_index] = existing.model_copy(
                            update={"provenance_batch_indexes": merged_indexes}
                        )
                    continue
                if len(edges) >= request.max_edges:
                    _trace_limit(
                        "edge_count",
                        len(edges) + 1,
                        request.max_edges,
                    )
                edge_indexes[edge_key] = len(edges)
                edges.append(
                    KeggRelationEdge(
                        relationship=relationship,
                        source=source,
                        target=target,
                        depth=depth,
                        provenance_batch_indexes=pair_provenance_indexes,
                    )
                )
                target_key = entity_key(target)
                if target_key not in node_keys:
                    if len(nodes) >= request.max_nodes:
                        _trace_limit(
                            "node_count",
                            len(nodes) + 1,
                            request.max_nodes,
                        )
                    node_keys.add(target_key)
                    nodes.append(target)
                    if target_key not in next_frontier_keys:
                        next_frontier_keys.add(target_key)
                        next_frontier.append(target)
        frontier = next_frontier
        if not frontier:
            break

    require_provenance_bound(provenance)
    payload = bounded_query_payload(
        {
            "request": request.model_dump(mode="json"),
            "nodes": [node.model_dump(mode="json") for node in nodes],
            "edges": [edge.model_dump(mode="json") for edge in edges],
            "steps": steps,
            "budget": {
                "kegg_requests": budget.requests,
                "raw_relation_rows": budget.rows,
                "kegg_response_bytes": budget.response_bytes,
            },
        }
    )
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION,
                mime_type="application/json",
                content=payload,
            ),
        ),
    )
    try:
        return TraceKeggRelationsResult(
            result=stored,
            artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
            seed_count=len(request.seeds),
            node_count=len(nodes),
            edge_count=len(edges),
            nodes=tuple(nodes),
            edges=tuple(edges),
            provenance=tuple(provenance),
        )
    except BaseException:
        compensate_created_result(
            result_store,
            scope_id,
            stored.result_id,
            stored.created_at,
        )
        raise


def _trace_relation(
    relationship: KeggRelationType,
    sources: tuple[KeggEntityRef, ...],
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: _TraceBudget,
    depth: int,
) -> tuple[
    tuple[_TracedPair, ...],
    tuple[KeggBatchProvenance, ...],
    tuple[dict[str, Any], ...],
]:
    if relationship is KeggRelationType.GENOME_TO_TAXONOMY:
        return _trace_genome_to_taxonomy(
            sources,
            client=client,
            options=options,
            budget=budget,
            depth=depth,
        )
    if relationship is KeggRelationType.TAXONOMY_TO_GENOME:
        return _trace_taxonomy_to_genome(
            sources,
            client=client,
            options=options,
            budget=budget,
            depth=depth,
        )

    source_kind, target_kind = relation_entity_kinds(relationship)
    linked = _bounded_trace_link(
        tuple(source.identifier for source in sources),
        relationship=relationship,
        client=client,
        options=options,
        budget=budget,
    )
    source_by_key = {entity_key(source): source for source in sources}
    pairs: list[_TracedPair] = []
    for row in linked.rows:
        source = pair_entity(source_kind, row.source_id)
        if entity_key(source) not in source_by_key:
            fail_unexpected_relation_row()
        pairs.append(
            _TracedPair(
                source=source_by_key[entity_key(source)],
                target=pair_entity(target_kind, row.target_id),
                provenance_batch_indexes=(row.batch_index,),
            )
        )
    return (
        tuple(pairs),
        linked.batches,
        (
            _trace_link_step(
                relationship,
                depth,
                tuple(source.identifier for source in sources),
                linked.rows,
                linked.batches,
            ),
        ),
    )


def _trace_genome_to_taxonomy(
    sources: tuple[KeggEntityRef, ...],
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: _TraceBudget,
    depth: int,
) -> tuple[
    tuple[_TracedPair, ...],
    tuple[KeggBatchProvenance, ...],
    tuple[dict[str, Any], ...],
]:
    loaded_genomes = load_genome_records(
        tuple(source.identifier for source in sources),
        client=client,
        options=options,
        before_batch=lambda: _require_trace_request_capacity(budget),
        record_batch=lambda _count, batches: budget.record(
            row_count=0,
            batches=batches,
        ),
    )
    records = loaded_genomes.records
    get_batches = loaded_genomes.batches
    get_step = loaded_genomes.step
    source_by_t_number = {source.identifier: source for source in sources}
    records_by_code = {
        record.organism_code: record
        for source in sources
        if (record := records.get(source.identifier)) is not None
    }
    if not records_by_code:
        return (), get_batches, (get_step,)
    linked = _bounded_trace_link(
        tuple(records_by_code),
        relationship=KeggRelationType.GENOME_TO_TAXONOMY,
        client=client,
        options=options,
        budget=budget,
    )
    pairs: list[_TracedPair] = []
    for row in linked.rows:
        organism = pair_entity(KeggEntityKind.ORGANISM, row.source_id)
        record = records_by_code.get(organism.identifier)
        if record is None or record.t_number not in source_by_t_number:
            fail_unexpected_relation_row()
        pairs.append(
            _TracedPair(
                source=source_by_t_number[record.t_number],
                target=pair_entity(KeggEntityKind.TAXONOMY, row.target_id),
                provenance_batch_indexes=(
                    loaded_genomes.batch_index_by_alias[record.t_number],
                    len(get_batches) + row.batch_index,
                ),
            )
        )
    return (
        tuple(pairs),
        (*get_batches, *linked.batches),
        (
            get_step,
            _trace_link_step(
                KeggRelationType.GENOME_TO_TAXONOMY,
                depth,
                tuple(records_by_code),
                linked.rows,
                linked.batches,
            ),
        ),
    )


def _trace_taxonomy_to_genome(
    sources: tuple[KeggEntityRef, ...],
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: _TraceBudget,
    depth: int,
) -> tuple[
    tuple[_TracedPair, ...],
    tuple[KeggBatchProvenance, ...],
    tuple[dict[str, Any], ...],
]:
    linked = _bounded_trace_link(
        tuple(source.identifier for source in sources),
        relationship=KeggRelationType.TAXONOMY_TO_GENOME,
        client=client,
        options=options,
        budget=budget,
    )
    source_by_key = {entity_key(source): source for source in sources}
    parsed_rows: list[tuple[KeggEntityRef, str, int]] = []
    genome_lookups: list[str] = []
    for row in linked.rows:
        source = pair_entity(KeggEntityKind.TAXONOMY, row.source_id)
        if entity_key(source) not in source_by_key:
            fail_unexpected_relation_row()
        lookup = genome_lookup_from_pair(row.target_id)
        parsed_rows.append(
            (
                source_by_key[entity_key(source)],
                lookup.identifier,
                row.batch_index,
            )
        )
        genome_lookups.append(lookup.identifier)
    unique_codes = tuple(dict.fromkeys(genome_lookups))
    loaded_genomes = load_genome_records(
        unique_codes,
        client=client,
        options=options,
        before_batch=lambda: _require_trace_request_capacity(budget),
        record_batch=lambda _count, batches: budget.record(
            row_count=0,
            batches=batches,
        ),
    )
    records = loaded_genomes.records
    get_batches = loaded_genomes.batches
    get_step = loaded_genomes.step
    pairs = tuple(
        _TracedPair(
            source=source,
            target=KeggEntityRef(
                kind=KeggEntityKind.GENOME,
                identifier=record.t_number,
            ),
            provenance_batch_indexes=(
                link_batch_index,
                len(linked.batches) + loaded_genomes.batch_index_by_alias[code],
            ),
        )
        for source, code, link_batch_index in parsed_rows
        if (record := records.get(code)) is not None
    )
    steps: tuple[dict[str, Any], ...] = (
        _trace_link_step(
            KeggRelationType.TAXONOMY_TO_GENOME,
            depth,
            tuple(source.identifier for source in sources),
            linked.rows,
            linked.batches,
        ),
    )
    if unique_codes:
        steps = (*steps, get_step)
    return pairs, (*linked.batches, *get_batches), steps


def _bounded_trace_link(
    source_identifiers: tuple[str, ...],
    *,
    relationship: KeggRelationType,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: _TraceBudget,
) -> BoundedRelationResult:
    if budget.remaining_requests <= 0:
        _trace_limit(
            "kegg_request_count",
            budget.requests + 1,
            budget.request_limit,
        )
    if budget.remaining_rows <= 0:
        _trace_limit(
            "raw_relation_row_count",
            budget.rows + 1,
            budget.row_limit,
        )
    if budget.remaining_response_bytes <= 0:
        _trace_limit(
            "kegg_response_bytes",
            budget.response_bytes + 1,
            budget.response_byte_limit,
        )
    return bounded_relation_batches(
        source_identifiers,
        relationship=link_relationship(relationship),
        client=client,
        options=options,
        max_total_requests=budget.remaining_requests,
        max_total_rows=budget.remaining_rows,
        max_total_response_bytes=budget.remaining_response_bytes,
        record_batch=lambda count, batches: budget.record(
            row_count=count,
            batches=batches,
        ),
    )


def _trace_link_step(
    relationship: KeggRelationType,
    depth: int,
    source_identifiers: tuple[str, ...],
    rows: tuple[KeggPairRow, ...],
    batches: tuple[KeggBatchProvenance, ...],
) -> dict[str, Any]:
    return {
        "operation": ResolutionOperation.LINK.value,
        "relationship": relationship.value,
        "depth": depth,
        "source_identifiers": list(source_identifiers),
        "rows": [row.model_dump(mode="json") for row in rows],
        "provenance": [batch.model_dump(mode="json") for batch in batches],
    }


def _require_trace_request_capacity(budget: _TraceBudget) -> None:
    if budget.remaining_requests <= 0:
        _trace_limit(
            "kegg_request_count",
            budget.requests + 1,
            budget.request_limit,
        )


def _trace_limit(
    limit_name: str,
    observed: int,
    limit: int,
) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "Relation tracing exceeded a caller-selected traversal bound.",
        suggested_action="Use fewer seeds, edge types, or a smaller traversal depth.",
        safe_details=(
            SafeDetail(name="limit_name", value=limit_name),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit", value=str(limit)),
        ),
    )


__all__ = ["trace_kegg_relations"]
