"""Private bounded KEGG chemical-substance resolution."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import (
    ConvRequest,
    GetRequest,
    KeggConvDatabase,
    KeggEntryRef,
    KeggGetDatabase,
    KeggRequestOptions,
)
from kegg_mcp.kegg.contracts import (
    MAX_GET_ENTRIES_PER_BATCH,
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggPairRow,
)
from kegg_mcp.services.kegg_relations import BoundedRelationResult
from kegg_mcp.services.query_models import (
    MAX_RESOLUTION_ENTITIES,
    KeggEntityKind,
    KeggEntityRef,
    KeggRelationType,
    ResolutionOperation,
    ResolvedEntityCandidate,
    ResolveKeggEntitiesResult,
    SubstanceIdentifierNamespace,
    SubstanceResolutionRequest,
    SubstanceResolutionTarget,
)
from kegg_mcp.services.query_support import (
    QueryBudget,
    bounded_query_relation,
    deduplicate_entities,
    entity_key,
    fail_unexpected_relation_row,
    link_relationship,
    pair_entity,
)
from kegg_mcp.services.reference_budget import KeggQueryClient
from kegg_mcp.services.resolution_support import (
    new_resolver_budget,
    relation_step,
    resolution_limit,
    retain_resolution,
)
from kegg_mcp.services.result_store import SQLiteResultStore

_NAMESPACE_CONV_DATABASE = {
    SubstanceIdentifierNamespace.CHEBI: KeggConvDatabase.CHEBI,
    SubstanceIdentifierNamespace.PUBCHEM_SID: KeggConvDatabase.PUBCHEM,
}
_KIND_CONV_DATABASE = {
    KeggEntityKind.COMPOUND: KeggConvDatabase.COMPOUND,
    KeggEntityKind.GLYCAN: KeggConvDatabase.GLYCAN,
    KeggEntityKind.DRUG: KeggConvDatabase.DRUG,
}
_KIND_GET_DATABASE = {
    KeggEntityKind.COMPOUND: KeggGetDatabase.COMPOUND,
    KeggEntityKind.GLYCAN: KeggGetDatabase.GLYCAN,
    KeggEntityKind.DRUG: KeggGetDatabase.DRUG,
}
_NAMESPACE_KIND = {
    SubstanceIdentifierNamespace.KEGG_COMPOUND: KeggEntityKind.COMPOUND,
    SubstanceIdentifierNamespace.KEGG_GLYCAN: KeggEntityKind.GLYCAN,
    SubstanceIdentifierNamespace.KEGG_DRUG: KeggEntityKind.DRUG,
}
_PROJECTION_RELATION = {
    (KeggEntityKind.COMPOUND, SubstanceResolutionTarget.REACTION): (
        KeggRelationType.COMPOUND_TO_REACTION
    ),
    (KeggEntityKind.COMPOUND, SubstanceResolutionTarget.PATHWAY): (
        KeggRelationType.COMPOUND_TO_PATHWAY
    ),
    (KeggEntityKind.GLYCAN, SubstanceResolutionTarget.REACTION): (
        KeggRelationType.GLYCAN_TO_REACTION
    ),
    (KeggEntityKind.GLYCAN, SubstanceResolutionTarget.PATHWAY): (
        KeggRelationType.GLYCAN_TO_PATHWAY
    ),
    (KeggEntityKind.DRUG, SubstanceResolutionTarget.PATHWAY): (KeggRelationType.DRUG_TO_PATHWAY),
}
_PROJECTION_KIND = {
    SubstanceResolutionTarget.REACTION: KeggEntityKind.REACTION,
    SubstanceResolutionTarget.PATHWAY: KeggEntityKind.PATHWAY,
}


def resolve_substance_request(
    request: SubstanceResolutionRequest,
    *,
    client: KeggQueryClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None,
) -> ResolveKeggEntitiesResult:
    """Resolve selected chemical crosswalks and validated one-hop references."""
    candidates_by_input: list[list[KeggEntityRef]] = [[] for _ in request.identifiers]
    operations_by_input: list[list[ResolutionOperation]] = [[] for _ in request.identifiers]
    provenance: list[KeggBatchProvenance] = []
    steps: list[dict[str, Any]] = []
    budget = new_resolver_budget()
    source_kind = _NAMESPACE_KIND.get(request.source_namespace)

    if source_kind is not None:
        direct = tuple(
            KeggEntityRef(kind=source_kind, identifier=identifier)
            for identifier in request.identifiers
        )
        existing, batches, step = _load_existing_substances(
            direct,
            client=client,
            options=options,
            budget=budget,
        )
        provenance.extend(batches)
        steps.append(step)
        existing_keys = {entity_key(entity) for entity in existing}
        for index, entity in enumerate(direct):
            operations_by_input[index].extend((ResolutionOperation.DIRECT, ResolutionOperation.GET))
            if entity_key(entity) in existing_keys:
                candidates_by_input[index].append(entity)
    else:
        source_database = _NAMESPACE_CONV_DATABASE[request.source_namespace]
        wire_identifiers = tuple(
            request.conversion_identifier(identifier) for identifier in request.identifiers
        )
        input_index_by_source = {
            wire_identifier: index for index, wire_identifier in enumerate(wire_identifiers)
        }
        for target_kind in sorted(request.target_kinds, key=lambda kind: kind.value):
            converted, retained_results = _bounded_substance_conv(
                target_database=_KIND_CONV_DATABASE[target_kind],
                source_database=source_database,
                source_identifiers=wire_identifiers,
                client=client,
                options=options,
                budget=budget,
            )
            provenance.extend(converted.batches)
            steps.append(
                {
                    "operation": ResolutionOperation.CONV.value,
                    "target_kind": target_kind.value,
                    "results": retained_results,
                }
            )
            for row in converted.rows:
                index = input_index_by_source.get(row.source_id)
                if index is None:
                    fail_unexpected_relation_row()
                candidates_by_input[index].append(pair_entity(target_kind, row.target_id))
        for operations in operations_by_input:
            operations.append(ResolutionOperation.CONV)
        candidates_by_input = [
            list(deduplicate_entities(candidates)) for candidates in candidates_by_input
        ]

    if sum(len(candidates) for candidates in candidates_by_input) > MAX_RESOLUTION_ENTITIES:
        resolution_limit("canonical_candidate_count")

    projection_rows: dict[
        tuple[str, str],
        dict[SubstanceResolutionTarget, list[KeggEntityRef]],
    ] = defaultdict(lambda: defaultdict(list))
    all_candidates = deduplicate_entities(
        entity for candidates in candidates_by_input for entity in candidates
    )
    for target in (
        SubstanceResolutionTarget.REACTION,
        SubstanceResolutionTarget.PATHWAY,
    ):
        if target not in request.targets:
            continue
        for candidate_kind in (
            KeggEntityKind.COMPOUND,
            KeggEntityKind.GLYCAN,
            KeggEntityKind.DRUG,
        ):
            relationship = _PROJECTION_RELATION.get((candidate_kind, target))
            sources = tuple(
                candidate for candidate in all_candidates if candidate.kind is candidate_kind
            )
            if relationship is None or not sources:
                continue
            linked = bounded_query_relation(
                tuple(source.identifier for source in sources),
                relationship=link_relationship(relationship),
                client=client,
                options=options,
                budget=budget,
            )
            provenance.extend(linked.batches)
            steps.append(relation_step(relationship, linked.rows, linked.batches))
            expected_sources = {entity_key(source) for source in sources}
            for row in linked.rows:
                source = pair_entity(candidate_kind, row.source_id)
                if entity_key(source) not in expected_sources:
                    fail_unexpected_relation_row()
                projection_rows[entity_key(source)][target].append(
                    pair_entity(_PROJECTION_KIND[target], row.target_id)
                )

    if any(
        target in request.targets
        for target in (
            SubstanceResolutionTarget.REACTION,
            SubstanceResolutionTarget.PATHWAY,
        )
    ):
        for operations, candidates in zip(
            operations_by_input,
            candidates_by_input,
            strict=True,
        ):
            if candidates:
                operations.append(ResolutionOperation.LINK)

    before_deduplication = 0
    candidate_groups: list[tuple[ResolvedEntityCandidate, ...]] = []
    for candidates in candidates_by_input:
        groups: list[ResolvedEntityCandidate] = []
        for candidate in candidates:
            projected = [candidate]
            for target in request.targets:
                projected.extend(projection_rows[entity_key(candidate)].get(target, ()))
            before_deduplication += len(projected)
            unique_projected = deduplicate_entities(projected)
            if len(unique_projected) > MAX_RESOLUTION_ENTITIES:
                resolution_limit("substance_candidate_projection_count")
            groups.append(
                ResolvedEntityCandidate(
                    canonical_entity=candidate,
                    entities=unique_projected,
                )
            )
        candidate_groups.append(tuple(groups))

    return retain_resolution(
        request=request,
        kind="substance",
        candidate_groups=tuple(candidate_groups),
        mismatch_counts=tuple(0 for _ in request.identifiers),
        operations_by_input=tuple(
            tuple(dict.fromkeys(operations)) for operations in operations_by_input
        ),
        before_deduplication=before_deduplication,
        provenance=tuple(provenance),
        steps=steps,
        budget=budget,
        result_store=result_store,
        scope_id=scope_id,
    )


def _load_existing_substances(
    entities: tuple[KeggEntityRef, ...],
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: QueryBudget,
) -> tuple[
    tuple[KeggEntityRef, ...],
    tuple[KeggBatchProvenance, ...],
    dict[str, Any],
]:
    existing: list[KeggEntityRef] = []
    batches: list[KeggBatchProvenance] = []
    retained_results: list[dict[str, Any]] = []
    batch_size = min(
        MAX_GET_ENTRIES_PER_BATCH,
        client.config.limits.max_identifiers,
    )
    for start in range(0, len(entities), batch_size):
        chunk = entities[start : start + batch_size]
        budget.require_request_capacity()
        fetched = client.get(
            GetRequest(
                entries=tuple(
                    KeggEntryRef(
                        database=_KIND_GET_DATABASE[entity.kind],
                        identifier=entity.identifier,
                    )
                    for entity in chunk
                )
            ),
            options=options,
        )
        if any(not isinstance(document, KeggFlatFileDocument) for document in fetched.documents):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A bounded KEGG substance call returned an unexpected document type.",
                suggested_action="Retry with a supported compound, glycan, or drug entry.",
            )
        missing = {(entry.database, entry.identifier) for entry in fetched.missing_entries}
        returned = tuple(
            entity
            for entity in chunk
            if (_KIND_GET_DATABASE[entity.kind], entity.identifier) not in missing
        )
        budget.record(row_count=len(returned), batches=fetched.batches)
        existing.extend(returned)
        batches.extend(fetched.batches)
        retained_results.append(fetched.model_dump(mode="json"))
    return (
        tuple(existing),
        tuple(batches),
        {
            "operation": ResolutionOperation.GET.value,
            "database": "substance",
            "identifiers": [entity.identifier for entity in entities],
            "results": retained_results,
        },
    )


def _bounded_substance_conv(
    *,
    target_database: KeggConvDatabase,
    source_database: KeggConvDatabase,
    source_identifiers: tuple[str, ...],
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: QueryBudget,
) -> tuple[BoundedRelationResult, tuple[dict[str, Any], ...]]:
    rows: list[KeggPairRow] = []
    batches: list[KeggBatchProvenance] = []
    retained_results: list[dict[str, Any]] = []
    batch_size = min(
        client.config.limits.relation_batch_size,
        client.config.limits.max_identifiers,
    )
    for start in range(0, len(source_identifiers), batch_size):
        budget.require_request_capacity()
        result = client.conv(
            ConvRequest(
                target_database=target_database,
                source_database=source_database,
                source_identifiers=source_identifiers[start : start + batch_size],
            ),
            options=options,
        )
        if len(result.batches) != 1:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A selected-substance conversion returned an unexpected batch count.",
                suggested_action="Retry with the typed KEGG client and unchanged limits.",
            )
        budget.record(row_count=len(result.rows), batches=result.batches)
        batch_offset = len(batches)
        rows.extend(
            row.model_copy(update={"batch_index": row.batch_index + batch_offset})
            for row in result.rows
        )
        batches.extend(result.batches)
        retained_results.append(result.model_dump(mode="json"))
    return (
        BoundedRelationResult(rows=tuple(rows), batches=tuple(batches)),
        tuple(retained_results),
    )


__all__: list[str] = []
