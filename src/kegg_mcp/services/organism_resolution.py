"""Private bounded KEGG organism resolution."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg import (
    FindRequest,
    KeggFindDatabase,
    KeggRequestOptions,
    KeggTaxonomyRank,
    OrganismPathwayListRequest,
)
from kegg_mcp.kegg.contracts import KeggBatchProvenance
from kegg_mcp.services.query_models import (
    MAX_ORGANISM_PATHWAY_PREVIEW,
    MAX_RESOLUTION_ENTITIES,
    KeggEntityKind,
    KeggEntityRef,
    KeggRelationType,
    OrganismIdentifierNamespace,
    OrganismPathwayPreviewEntry,
    OrganismPathwaySummary,
    OrganismResolutionRequest,
    ResolutionOperation,
    ResolvedEntityCandidate,
    ResolveKeggEntitiesResult,
)
from kegg_mcp.services.query_support import (
    GenomeRecord,
    bounded_query_relation,
    deduplicate_entities,
    entity_key,
    fail_unexpected_relation_row,
    genome_lookup_from_pair,
    link_relationship,
    load_genome_records,
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


def resolve_organism_request(
    request: OrganismResolutionRequest,
    *,
    client: KeggQueryClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None,
) -> ResolveKeggEntitiesResult:
    candidate_identities: list[dict[tuple[str, str], list[KeggEntityRef]]] = [
        {} for _ in request.identifiers
    ]
    operations_by_input: list[list[ResolutionOperation]] = [[] for _ in request.identifiers]
    provenance: list[KeggBatchProvenance] = []
    steps: list[dict[str, Any]] = []
    budget = new_resolver_budget()

    if request.source_namespace is OrganismIdentifierNamespace.CODE:
        for index, identifier in enumerate(request.identifiers):
            code = KeggEntityRef(
                kind=KeggEntityKind.ORGANISM,
                identifier=identifier,
            )
            operations_by_input[index].append(ResolutionOperation.DIRECT)
            # A syntax-valid code is only a GET probe until KEGG returns its record.
            candidate_identities[index][entity_key(code)] = [code]
    elif request.source_namespace is OrganismIdentifierNamespace.GENOME:
        for index, identifier in enumerate(request.identifiers):
            genome = KeggEntityRef(
                kind=KeggEntityKind.GENOME,
                identifier=identifier,
            )
            operations_by_input[index].append(ResolutionOperation.DIRECT)
            candidate_identities[index][entity_key(genome)] = [genome]
    elif request.source_namespace is OrganismIdentifierNamespace.TAXONOMY:
        taxonomies = tuple(_taxonomy_entity(identifier) for identifier in request.identifiers)
        linked = bounded_query_relation(
            tuple(taxonomy.identifier for taxonomy in taxonomies),
            relationship=link_relationship(KeggRelationType.TAXONOMY_TO_GENOME),
            client=client,
            options=options,
            budget=budget,
            taxonomy_rank=KeggTaxonomyRank(request.taxonomy_rank.value),
        )
        provenance.extend(linked.batches)
        steps.append(
            relation_step(
                KeggRelationType.TAXONOMY_TO_GENOME,
                linked.rows,
                linked.batches,
            )
        )
        input_index_by_taxonomy = {
            entity_key(taxonomy): index for index, taxonomy in enumerate(taxonomies)
        }
        for index in range(len(request.identifiers)):
            operations_by_input[index].append(ResolutionOperation.LINK)
        for row in linked.rows:
            taxonomy = pair_entity(KeggEntityKind.TAXONOMY, row.source_id)
            index = input_index_by_taxonomy.get(entity_key(taxonomy))
            if index is None:
                fail_unexpected_relation_row()
            lookup = genome_lookup_from_pair(row.target_id)
            candidate_identities[index][entity_key(lookup)] = [taxonomy, lookup]
    else:
        for index, identifier in enumerate(request.identifiers):
            budget.require_request_capacity()
            found = client.find(
                FindRequest(
                    database=KeggFindDatabase.ORGANISM,
                    query=identifier,
                ),
                options=options,
            )
            operations_by_input[index].append(ResolutionOperation.FIND)
            budget.record(row_count=len(found.document.rows), batches=(found.batch,))
            provenance.append(found.batch)
            steps.append(
                {
                    "input_identifier": identifier,
                    "operation": ResolutionOperation.FIND.value,
                    "result": found.model_dump(mode="json"),
                }
            )
            for row in found.document.rows:
                genome = _find_genome(row.identifier)
                identities = [genome]
                identities.extend(
                    KeggEntityRef(
                        kind=KeggEntityKind.ORGANISM,
                        identifier=code,
                    )
                    for code in _organism_codes(row.matched_text)
                )
                existing = candidate_identities[index].setdefault(
                    entity_key(genome),
                    [],
                )
                existing.extend(identities)

    if sum(len(groups) for groups in candidate_identities) > MAX_RESOLUTION_ENTITIES:
        resolution_limit("canonical_candidate_count")

    lookup_entities = deduplicate_entities(
        next(entity for entity in identities if entity_key(entity) == canonical_key)
        for groups in candidate_identities
        for canonical_key, identities in groups.items()
    )
    genome_records: dict[str, GenomeRecord] = {}
    if lookup_entities:
        loaded_genomes = load_genome_records(
            tuple(entity.identifier for entity in lookup_entities),
            client=client,
            options=options,
            before_batch=budget.require_request_capacity,
            record_batch=lambda count, batches: budget.record(
                row_count=count,
                batches=batches,
            ),
        )
        genome_records = loaded_genomes.records
        provenance.extend(loaded_genomes.batches)
        steps.append(loaded_genomes.step)
        for index, groups in enumerate(candidate_identities):
            if groups:
                operations_by_input[index].append(ResolutionOperation.GET)

    normalized_groups: list[dict[tuple[str, str], list[KeggEntityRef]]] = [
        {} for _ in request.identifiers
    ]
    for index, groups in enumerate(candidate_identities):
        for canonical_key, identities in groups.items():
            lookup = next(entity for entity in identities if entity_key(entity) == canonical_key)
            record = genome_records.get(lookup.identifier)
            if record is None:
                # Syntax-valid but missing code/T-number probes are not mappings.
                continue
            supplied_codes = {
                identity.identifier
                for identity in identities
                if identity.kind is KeggEntityKind.ORGANISM
            }
            if supplied_codes and supplied_codes != {record.organism_code}:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "The KEGG organism candidate did not match its genome record.",
                    suggested_action="Refresh the typed FIND and GENOME responses and retry.",
                    safe_details=(SafeDetail(name="reason", value="organism_code_mismatch"),),
                )
            genome = KeggEntityRef(
                kind=KeggEntityKind.GENOME,
                identifier=record.t_number,
            )
            organism = KeggEntityRef(
                kind=KeggEntityKind.ORGANISM,
                identifier=record.organism_code,
            )
            normalized = normalized_groups[index].setdefault(
                entity_key(organism),
                [],
            )
            normalized.extend((*identities, genome, organism))
    candidate_identities = normalized_groups

    all_organism_codes = deduplicate_entities(
        entity
        for groups in candidate_identities
        for identities in groups.values()
        for entity in identities
        if entity.kind is KeggEntityKind.ORGANISM
    )
    pathway_summaries: dict[str, OrganismPathwaySummary] = {}
    if request.include_pathway_directory:
        for organism in all_organism_codes:
            budget.require_request_capacity()
            listed = client.list_organism_pathways(
                OrganismPathwayListRequest(organism=organism.identifier),
                options=options,
            )
            budget.record(
                row_count=len(listed.document.rows),
                batches=(listed.batch,),
            )
            provenance.append(listed.batch)
            steps.append(
                {
                    "operation": ResolutionOperation.LIST.value,
                    "database": "pathway",
                    "organism": organism.identifier,
                    "result": listed.model_dump(mode="json"),
                }
            )
            preview = tuple(
                OrganismPathwayPreviewEntry(
                    pathway=pair_entity(KeggEntityKind.PATHWAY, row.pathway_id),
                    name=row.name,
                )
                for row in listed.document.rows[:MAX_ORGANISM_PATHWAY_PREVIEW]
            )
            pathway_summaries[organism.identifier] = OrganismPathwaySummary(
                total_count=len(listed.document.rows),
                preview=preview,
                truncated=len(preview) < len(listed.document.rows),
            )
        for index, groups in enumerate(candidate_identities):
            if groups:
                operations_by_input[index].append(ResolutionOperation.LIST)

    if all_organism_codes:
        linked_taxonomies = bounded_query_relation(
            tuple(organism.identifier for organism in all_organism_codes),
            relationship=link_relationship(KeggRelationType.GENOME_TO_TAXONOMY),
            client=client,
            options=options,
            budget=budget,
        )
        provenance.extend(linked_taxonomies.batches)
        steps.append(
            relation_step(
                KeggRelationType.GENOME_TO_TAXONOMY,
                linked_taxonomies.rows,
                linked_taxonomies.batches,
            )
        )
        expected_codes = {entity_key(code) for code in all_organism_codes}
        identities_by_code: dict[
            tuple[str, str],
            list[KeggEntityRef],
        ] = defaultdict(list)
        for row in linked_taxonomies.rows:
            organism = pair_entity(KeggEntityKind.ORGANISM, row.source_id)
            if entity_key(organism) not in expected_codes:
                fail_unexpected_relation_row()
            identities_by_code[entity_key(organism)].append(
                pair_entity(KeggEntityKind.TAXONOMY, row.target_id)
            )
        for index, groups in enumerate(candidate_identities):
            if any(
                entity.kind is KeggEntityKind.ORGANISM
                for identities in groups.values()
                for entity in identities
            ):
                operations_by_input[index].append(ResolutionOperation.LINK)
            for identities in groups.values():
                for identity in tuple(identities):
                    if identity.kind is KeggEntityKind.ORGANISM:
                        identities.extend(identities_by_code.get(entity_key(identity), ()))

    candidate_groups: list[tuple[ResolvedEntityCandidate, ...]] = []
    before_deduplication = 0
    for groups in candidate_identities:
        resolved_groups: list[ResolvedEntityCandidate] = []
        for canonical_key, identities in groups.items():
            unique_identities = deduplicate_entities(identities)
            before_deduplication += len(identities)
            canonical = next(
                identity for identity in unique_identities if entity_key(identity) == canonical_key
            )
            record = next(
                (
                    genome_records.get(identity.identifier)
                    for identity in unique_identities
                    if genome_records.get(identity.identifier) is not None
                ),
                None,
            )
            resolved_groups.append(
                ResolvedEntityCandidate(
                    canonical_entity=canonical,
                    entities=unique_identities,
                    name=record.name if record is not None else None,
                    taxonomy_lineage=(record.taxonomy_lineage if record is not None else ()),
                    organism_pathways=pathway_summaries.get(canonical.identifier),
                )
            )
        candidate_groups.append(tuple(resolved_groups))

    return retain_resolution(
        request=request,
        kind="organism",
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


def _find_genome(identifier: str) -> KeggEntityRef:
    return pair_entity(KeggEntityKind.GENOME, identifier)


def _taxonomy_entity(identifier: str) -> KeggEntityRef:
    value = identifier.removeprefix("taxid:")
    return KeggEntityRef(
        kind=KeggEntityKind.TAXONOMY,
        identifier=f"taxid:{value}",
    )


def _organism_codes(matched_text: str) -> tuple[str, ...]:
    first_token = matched_text.lstrip().split(maxsplit=1)[0].rstrip(";,|")
    try:
        entity = KeggEntityRef(
            kind=KeggEntityKind.ORGANISM,
            identifier=first_token,
        )
    except ValueError:
        return ()
    return (entity.identifier,)


__all__: list[str] = []
