"""Bounded traversal over the typed KEGG LINK allowlist."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggPairRow
from kegg_mcp.services.kegg_relations import DEFAULT_MAX_RELATION_RESPONSE_BYTES
from kegg_mcp.services.models import DETAIL_SECTION
from kegg_mcp.services.query_models import (
    MAX_TRACE_EDGE_PREVIEW,
    MAX_TRACE_NODE_PREVIEW,
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
    QueryBudget,
    bounded_query_payload,
    bounded_query_relation,
    entity_key,
    fail_unexpected_relation_row,
    genome_lookup_from_pair,
    link_relationship,
    load_genome_records,
    pair_entity,
    planned_genome_get_batch_count,
    require_bounded_query_direct_result,
    require_provenance_bound,
    summarize_query_retrieval,
)
from kegg_mcp.services.reference_budget import KeggQueryClient, effective_query_options
from kegg_mcp.services.result_builders import _artifact_metadata
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    SQLiteResultStore,
    create_retained_result,
)

MAX_TRACE_KEGG_REQUESTS = 128
_MAX_TRACE_RAW_ROW_FACTOR = 4


@dataclass(frozen=True, slots=True)
class _TracedPair:
    source: KeggEntityRef
    target: KeggEntityRef
    provenance_batch_indexes: tuple[int, ...]


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
    budget = QueryBudget(
        request_limit=MAX_TRACE_KEGG_REQUESTS,
        row_limit=min(
            request.max_edges * _MAX_TRACE_RAW_ROW_FACTOR,
            10_000,
        ),
        response_byte_limit=DEFAULT_MAX_RELATION_RESPONSE_BYTES,
        error_message="Relation tracing exceeded a caller-selected traversal bound.",
        suggested_action="Use fewer seeds, edge types, or a smaller traversal depth.",
        row_limit_name="raw_relation_row_count",
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
            if relationship is KeggRelationType.PATHWAY_TO_GENE:
                if request.organism_scope is None:
                    raise AssertionError("scoped relation validation requires organism_scope")
                sources = [
                    source for source in sources if source.identifier[:-5] == request.organism_scope
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
                organism_scope=request.organism_scope,
                current_node_count=len(nodes),
                current_node_keys=frozenset(node_keys),
                current_edge_count=len(edges),
                current_edge_keys=frozenset(edge_indexes),
                node_limit=request.max_nodes,
                edge_limit=request.max_edges,
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
            "provenance": [batch.model_dump(mode="json") for batch in provenance],
            "budget": {
                "kegg_requests": budget.requests,
                "raw_relation_rows": budget.rows,
                "kegg_response_bytes": budget.response_bytes,
            },
        }
    )
    with create_retained_result(
        result_store,
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION,
                mime_type="application/json",
                content=payload,
            ),
        ),
    ) as stored:
        result = TraceKeggRelationsResult(
            result=stored,
            artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
            seed_count=len(request.seeds),
            node_count=len(nodes),
            edge_count=len(edges),
            node_preview=tuple(nodes[:MAX_TRACE_NODE_PREVIEW]),
            nodes_truncated=len(nodes) > MAX_TRACE_NODE_PREVIEW,
            edge_preview=tuple(edges[:MAX_TRACE_EDGE_PREVIEW]),
            edges_truncated=len(edges) > MAX_TRACE_EDGE_PREVIEW,
            retrieval=summarize_query_retrieval(provenance),
        )
        require_bounded_query_direct_result(result)
        return result


def _trace_relation(
    relationship: KeggRelationType,
    sources: tuple[KeggEntityRef, ...],
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: QueryBudget,
    depth: int,
    organism_scope: str | None,
    current_node_count: int,
    current_node_keys: frozenset[tuple[str, str]],
    current_edge_count: int,
    current_edge_keys: frozenset[tuple[KeggRelationType, tuple[str, str], tuple[str, str]]],
    node_limit: int,
    edge_limit: int,
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
            current_node_count=current_node_count,
            current_node_keys=current_node_keys,
            current_edge_count=current_edge_count,
            current_edge_keys=current_edge_keys,
            node_limit=node_limit,
            edge_limit=edge_limit,
        )

    source_kind, target_kind = relation_entity_kinds(relationship)
    linked = bounded_query_relation(
        tuple(source.identifier for source in sources),
        relationship=link_relationship(relationship),
        client=client,
        options=options,
        budget=budget,
        organism_scope=(
            organism_scope
            if relationship in {KeggRelationType.KO_TO_GENE, KeggRelationType.PATHWAY_TO_GENE}
            else None
        ),
    )
    source_by_key = {entity_key(source): source for source in sources}
    pairs: list[_TracedPair] = []
    for row in linked.rows:
        source = pair_entity(source_kind, row.source_id)
        if entity_key(source) not in source_by_key:
            fail_unexpected_relation_row()
        target = pair_entity(target_kind, row.target_id)
        if (
            target.kind is KeggEntityKind.GENE
            and organism_scope is not None
            and target.identifier.partition(":")[0] != organism_scope
        ):
            fail_unexpected_relation_row()
        pairs.append(
            _TracedPair(
                source=source_by_key[entity_key(source)],
                target=target,
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
    budget: QueryBudget,
    depth: int,
) -> tuple[
    tuple[_TracedPair, ...],
    tuple[KeggBatchProvenance, ...],
    tuple[dict[str, Any], ...],
]:
    budget.require_request_capacity(
        planned_genome_get_batch_count(
            tuple(source.identifier for source in sources),
            client=client,
        )
    )
    loaded_genomes = load_genome_records(
        tuple(source.identifier for source in sources),
        client=client,
        options=options,
        before_batch=budget.require_request_capacity,
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
    linked = bounded_query_relation(
        tuple(records_by_code),
        relationship=link_relationship(KeggRelationType.GENOME_TO_TAXONOMY),
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
    budget: QueryBudget,
    depth: int,
    current_node_count: int,
    current_node_keys: frozenset[tuple[str, str]],
    current_edge_count: int,
    current_edge_keys: frozenset[tuple[KeggRelationType, tuple[str, str], tuple[str, str]]],
    node_limit: int,
    edge_limit: int,
) -> tuple[
    tuple[_TracedPair, ...],
    tuple[KeggBatchProvenance, ...],
    tuple[dict[str, Any], ...],
]:
    linked = bounded_query_relation(
        tuple(source.identifier for source in sources),
        relationship=link_relationship(KeggRelationType.TAXONOMY_TO_GENOME),
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
    explicit_t_number_edge_keys = {
        (
            KeggRelationType.TAXONOMY_TO_GENOME,
            entity_key(source),
            (KeggEntityKind.GENOME.value, code),
        )
        for source, code, _batch_index in parsed_rows
        if code.startswith("T")
    }
    potential_edge_count = len(explicit_t_number_edge_keys - current_edge_keys)
    if current_edge_count + potential_edge_count > edge_limit:
        _trace_limit(
            "edge_count",
            current_edge_count + potential_edge_count,
            edge_limit,
        )
    t_number_lookups = {
        code
        for code in unique_codes
        if code.startswith("T") and (KeggEntityKind.GENOME.value, code) not in current_node_keys
    }
    if current_node_count + len(t_number_lookups) > node_limit:
        _trace_limit(
            "node_count",
            current_node_count + len(t_number_lookups),
            node_limit,
        )
    if unique_codes:
        budget.require_request_capacity(planned_genome_get_batch_count(unique_codes, client=client))
    loaded_genomes = load_genome_records(
        unique_codes,
        client=client,
        options=options,
        before_batch=budget.require_request_capacity,
        record_batch=lambda _count, batches: budget.record(
            row_count=0,
            batches=batches,
        ),
    )
    records = loaded_genomes.records
    get_batches = loaded_genomes.batches
    get_step = loaded_genomes.step
    potential_target_keys = {
        (KeggEntityKind.GENOME.value, record.t_number)
        for code in unique_codes
        if (record := records.get(code)) is not None
    }
    potential_new_node_count = len(potential_target_keys - current_node_keys)
    if current_node_count + potential_new_node_count > node_limit:
        _trace_limit(
            "node_count",
            current_node_count + potential_new_node_count,
            node_limit,
        )
    canonical_edge_keys = {
        (
            KeggRelationType.TAXONOMY_TO_GENOME,
            entity_key(source),
            (KeggEntityKind.GENOME.value, record.t_number),
        )
        for source, code, _batch_index in parsed_rows
        if (record := records.get(code)) is not None
    }
    potential_edge_count = len(canonical_edge_keys - current_edge_keys)
    if current_edge_count + potential_edge_count > edge_limit:
        _trace_limit(
            "edge_count",
            current_edge_count + potential_edge_count,
            edge_limit,
        )
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
