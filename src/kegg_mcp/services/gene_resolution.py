"""Private bounded KEGG gene resolution."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, NoReturn

from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import (
    ConvRequest,
    FindRequest,
    GetRequest,
    KeggConvDatabase,
    KeggEntryRef,
    KeggFindDatabase,
    KeggFindMode,
    KeggGetDatabase,
    KeggRequestOptions,
)
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggPairRow,
)
from kegg_mcp.services.kegg_relations import BoundedRelationResult
from kegg_mcp.services.query_models import (
    MAX_RESOLUTION_ENTITIES,
    GeneIdentifierNamespace,
    GeneResolutionRequest,
    GeneResolutionTarget,
    KeggEntityKind,
    KeggEntityRef,
    KeggRelationType,
    ResolutionOperation,
    ResolvedEntityCandidate,
    ResolveKeggEntitiesResult,
)
from kegg_mcp.services.query_support import (
    deduplicate_entities,
    entity_key,
    fail_unexpected_relation_row,
    link_relationship,
    load_genome_records,
    pair_entity,
)
from kegg_mcp.services.reference_budget import KeggQueryClient
from kegg_mcp.services.resolution_support import (
    ResolverBudget,
    bounded_resolver_link,
    relation_step,
    require_resolver_request_capacity,
    resolution_limit,
    retain_resolution,
)
from kegg_mcp.services.result_store import SQLiteResultStore

_MAX_GENE_GET_ENTRIES_PER_CALL = 10

_GENE_CONV_DATABASES = {
    GeneIdentifierNamespace.NCBI_GENEID: KeggConvDatabase.NCBI_GENEID,
    GeneIdentifierNamespace.NCBI_PROTEINID: KeggConvDatabase.NCBI_PROTEINID,
    GeneIdentifierNamespace.UNIPROT: KeggConvDatabase.UNIPROT,
}
_GENE_TARGET_RELATIONS = {
    GeneResolutionTarget.MODULE: KeggRelationType.KO_TO_MODULE,
    GeneResolutionTarget.REACTION: KeggRelationType.KO_TO_REACTION,
    GeneResolutionTarget.ENZYME: KeggRelationType.KO_TO_ENZYME,
}
_GENE_TARGET_KINDS = {
    GeneResolutionTarget.GENE: KeggEntityKind.GENE,
    GeneResolutionTarget.KO: KeggEntityKind.KO,
    GeneResolutionTarget.PATHWAY: KeggEntityKind.PATHWAY,
    GeneResolutionTarget.MODULE: KeggEntityKind.MODULE,
    GeneResolutionTarget.REACTION: KeggEntityKind.REACTION,
    GeneResolutionTarget.ENZYME: KeggEntityKind.ENZYME,
}


def resolve_gene_request(
    request: GeneResolutionRequest,
    *,
    client: KeggQueryClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None,
) -> ResolveKeggEntitiesResult:
    gene_candidates: list[list[KeggEntityRef]] = [[] for _ in request.identifiers]
    mismatch_counts = [0 for _ in request.identifiers]
    operations_by_input: list[list[ResolutionOperation]] = [[] for _ in request.identifiers]
    provenance: list[KeggBatchProvenance] = []
    steps: list[dict[str, Any]] = []
    budget = ResolverBudget()
    allowed_organism_prefixes: frozenset[str] | None = None

    if request.organism is not None:
        organism_context = load_genome_records(
            (request.organism,),
            client=client,
            options=options,
            before_batch=lambda: require_resolver_request_capacity(budget),
            record_batch=lambda count, batches: budget.record(
                row_count=count,
                batches=batches,
            ),
        )
        provenance.extend(organism_context.batches)
        steps.append({**organism_context.step, "purpose": "organism_identity"})
        record = organism_context.records.get(request.organism)
        allowed_organism_prefixes = (
            frozenset() if record is None else frozenset((record.organism_code, record.t_number))
        )

    if request.source_namespace is GeneIdentifierNamespace.KEGG_GENE:
        direct_probes: list[tuple[int, KeggEntityRef]] = []
        for index, identifier in enumerate(request.identifiers):
            operations_by_input[index].append(ResolutionOperation.DIRECT)
            gene = KeggEntityRef(kind=KeggEntityKind.GENE, identifier=identifier)
            if not _gene_matches_organism(gene, allowed_organism_prefixes):
                mismatch_counts[index] = 1
            else:
                direct_probes.append((index, gene))
                operations_by_input[index].append(ResolutionOperation.GET)
        existing_genes, get_batches, get_step = _load_existing_genes(
            tuple(gene for _, gene in direct_probes),
            client=client,
            options=options,
            budget=budget,
        )
        provenance.extend(get_batches)
        if direct_probes:
            steps.append(get_step)
        existing_keys = {entity_key(gene) for gene in existing_genes}
        for index, gene in direct_probes:
            if entity_key(gene) in existing_keys:
                gene_candidates[index].append(gene)
    elif request.source_namespace is GeneIdentifierNamespace.GENE_SYMBOL:
        if request.organism is None:
            raise AssertionError("gene_symbol request validation requires organism")
        for index, identifier in enumerate(request.identifiers):
            require_resolver_request_capacity(budget)
            found = client.find(
                FindRequest(
                    database=KeggFindDatabase.GENES,
                    query=identifier,
                    mode=KeggFindMode.KEYWORD,
                    organism=request.organism,
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
                gene = _pairless_gene(row.identifier)
                if not _gene_matches_organism(gene, allowed_organism_prefixes):
                    mismatch_counts[index] += 1
                else:
                    gene_candidates[index].append(gene)
            gene_candidates[index] = list(deduplicate_entities(gene_candidates[index]))
    else:
        source_database = _GENE_CONV_DATABASES[request.source_namespace]
        wire_identifiers = tuple(
            request.conversion_identifier(identifier) for identifier in request.identifiers
        )
        converted, converted_results = _bounded_resolver_conv(
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
                "results": converted_results,
            }
        )
        input_index_by_source: dict[str, int] = {}
        for index, wire_identifier in enumerate(wire_identifiers):
            operations_by_input[index].append(ResolutionOperation.CONV)
            input_index_by_source[wire_identifier] = index
            if request.source_namespace is GeneIdentifierNamespace.UNIPROT:
                input_index_by_source[f"up:{wire_identifier.partition(':')[2]}"] = index
        for row in converted.rows:
            index = input_index_by_source.get(row.source_id)
            if index is None:
                fail_unexpected_relation_row()
            gene = _pairless_gene(row.target_id)
            if not _gene_matches_organism(gene, allowed_organism_prefixes):
                mismatch_counts[index] += 1
            else:
                gene_candidates[index].append(gene)
        gene_candidates = [list(deduplicate_entities(candidates)) for candidates in gene_candidates]

    if request.organism is not None:
        for operations in operations_by_input:
            operations.append(ResolutionOperation.GET)

    all_genes = deduplicate_entities(gene for candidates in gene_candidates for gene in candidates)
    if sum(len(candidates) for candidates in gene_candidates) > MAX_RESOLUTION_ENTITIES:
        resolution_limit("canonical_candidate_count")

    ko_rows_by_gene: dict[tuple[str, str], list[KeggEntityRef]] = defaultdict(list)
    pathway_rows_by_gene: dict[tuple[str, str], list[KeggEntityRef]] = defaultdict(list)
    projected_rows_by_ko: dict[
        GeneResolutionTarget,
        dict[tuple[str, str], list[KeggEntityRef]],
    ] = {}
    if all_genes and GeneResolutionTarget.PATHWAY in request.targets:
        linked_pathways = bounded_resolver_link(
            tuple(gene.identifier for gene in all_genes),
            relationship=link_relationship(KeggRelationType.GENE_TO_PATHWAY),
            client=client,
            options=options,
            budget=budget,
        )
        provenance.extend(linked_pathways.batches)
        steps.append(
            relation_step(
                KeggRelationType.GENE_TO_PATHWAY,
                linked_pathways.rows,
                linked_pathways.batches,
            )
        )
        expected_genes = {entity_key(gene) for gene in all_genes}
        for row in linked_pathways.rows:
            source = pair_entity(KeggEntityKind.GENE, row.source_id)
            if entity_key(source) not in expected_genes:
                fail_unexpected_relation_row()
            pathway_rows_by_gene[entity_key(source)].append(
                pair_entity(KeggEntityKind.PATHWAY, row.target_id)
            )
        for operations, candidates in zip(
            operations_by_input,
            gene_candidates,
            strict=True,
        ):
            if candidates:
                operations.append(ResolutionOperation.LINK)

    requires_ko = any(
        target
        in {
            GeneResolutionTarget.KO,
            GeneResolutionTarget.MODULE,
            GeneResolutionTarget.REACTION,
            GeneResolutionTarget.ENZYME,
        }
        for target in request.targets
    )
    if all_genes and requires_ko:
        linked_kos = bounded_resolver_link(
            tuple(gene.identifier for gene in all_genes),
            relationship=link_relationship(KeggRelationType.GENE_TO_KO),
            client=client,
            options=options,
            budget=budget,
        )
        provenance.extend(linked_kos.batches)
        steps.append(
            relation_step(
                KeggRelationType.GENE_TO_KO,
                linked_kos.rows,
                linked_kos.batches,
            )
        )
        expected_genes = {entity_key(gene) for gene in all_genes}
        for row in linked_kos.rows:
            source = pair_entity(KeggEntityKind.GENE, row.source_id)
            if entity_key(source) not in expected_genes:
                fail_unexpected_relation_row()
            ko_rows_by_gene[entity_key(source)].append(
                pair_entity(KeggEntityKind.KO, row.target_id)
            )
        for operations, candidates in zip(
            operations_by_input,
            gene_candidates,
            strict=True,
        ):
            if candidates:
                operations.append(ResolutionOperation.LINK)

        unique_kos = deduplicate_entities(ko for rows in ko_rows_by_gene.values() for ko in rows)
        if len(unique_kos) > MAX_RESOLUTION_ENTITIES:
            resolution_limit("unique_ko_projection_count")
        for target in request.targets:
            relationship = _GENE_TARGET_RELATIONS.get(target)
            if relationship is None or not unique_kos:
                continue
            linked_targets = bounded_resolver_link(
                tuple(ko.identifier for ko in unique_kos),
                relationship=link_relationship(relationship),
                client=client,
                options=options,
                budget=budget,
            )
            provenance.extend(linked_targets.batches)
            steps.append(
                relation_step(
                    relationship,
                    linked_targets.rows,
                    linked_targets.batches,
                )
            )
            expected_kos = {entity_key(ko) for ko in unique_kos}
            rows_by_ko: dict[
                tuple[str, str],
                list[KeggEntityRef],
            ] = defaultdict(list)
            target_kind = _GENE_TARGET_KINDS[target]
            for row in linked_targets.rows:
                source = pair_entity(KeggEntityKind.KO, row.source_id)
                if entity_key(source) not in expected_kos:
                    fail_unexpected_relation_row()
                rows_by_ko[entity_key(source)].append(pair_entity(target_kind, row.target_id))
            projected_rows_by_ko[target] = rows_by_ko

    before_deduplication = 0
    candidate_groups: list[tuple[ResolvedEntityCandidate, ...]] = []
    for candidates in gene_candidates:
        groups: list[ResolvedEntityCandidate] = []
        for gene in candidates:
            projected: list[KeggEntityRef] = []
            for target in request.targets:
                if target is GeneResolutionTarget.GENE:
                    projected.append(gene)
                    continue
                if target is GeneResolutionTarget.PATHWAY:
                    projected.extend(pathway_rows_by_gene.get(entity_key(gene), ()))
                    continue
                kos = ko_rows_by_gene.get(entity_key(gene), ())
                if target is GeneResolutionTarget.KO:
                    projected.extend(kos)
                    continue
                rows_by_ko = projected_rows_by_ko.get(target, {})
                for ko in kos:
                    projected.extend(rows_by_ko.get(entity_key(ko), ()))
            before_deduplication += len(projected)
            groups.append(
                ResolvedEntityCandidate(
                    canonical_entity=gene,
                    entities=deduplicate_entities(projected),
                )
            )
        candidate_groups.append(tuple(groups))

    return retain_resolution(
        request=request,
        kind="gene",
        candidate_groups=tuple(candidate_groups),
        mismatch_counts=tuple(mismatch_counts),
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


def _load_existing_genes(
    genes: tuple[KeggEntityRef, ...],
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: ResolverBudget,
) -> tuple[
    tuple[KeggEntityRef, ...],
    tuple[KeggBatchProvenance, ...],
    dict[str, Any],
]:
    unique_genes = deduplicate_entities(genes)
    if not unique_genes:
        return (
            (),
            (),
            {
                "operation": ResolutionOperation.GET.value,
                "database": "gene",
                "identifiers": [],
                "results": [],
            },
        )
    batch_size = min(
        _MAX_GENE_GET_ENTRIES_PER_CALL,
        client.config.limits.max_identifiers,
    )
    existing: list[KeggEntityRef] = []
    batches: list[KeggBatchProvenance] = []
    retained_results: list[dict[str, Any]] = []
    for start in range(0, len(unique_genes), batch_size):
        chunk = unique_genes[start : start + batch_size]
        require_resolver_request_capacity(budget)
        fetched = client.get(
            GetRequest(
                entries=tuple(
                    KeggEntryRef(
                        database=KeggGetDatabase.GENE,
                        identifier=gene.identifier,
                    )
                    for gene in chunk
                )
            ),
            options=options,
        )
        if any(not isinstance(document, KeggFlatFileDocument) for document in fetched.documents):
            _fail_unexpected_gene_document()
        missing = {entry.identifier for entry in fetched.missing_entries}
        returned = tuple(gene for gene in chunk if gene.identifier not in missing)
        budget.record(row_count=len(returned), batches=fetched.batches)
        existing.extend(returned)
        batches.extend(fetched.batches)
        retained_results.append(fetched.model_dump(mode="json"))
    return (
        tuple(existing),
        tuple(batches),
        {
            "operation": ResolutionOperation.GET.value,
            "database": "gene",
            "identifiers": [gene.identifier for gene in unique_genes],
            "results": retained_results,
        },
    )


def _bounded_resolver_conv(
    *,
    source_database: KeggConvDatabase,
    source_identifiers: tuple[str, ...],
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: ResolverBudget,
) -> tuple[BoundedRelationResult, tuple[dict[str, Any], ...]]:
    rows: list[KeggPairRow] = []
    batches: list[KeggBatchProvenance] = []
    retained_results: list[dict[str, Any]] = []
    batch_size = min(
        client.config.limits.relation_batch_size,
        client.config.limits.max_identifiers,
    )
    for start in range(0, len(source_identifiers), batch_size):
        require_resolver_request_capacity(budget)
        result = client.conv(
            ConvRequest(
                target_database=KeggConvDatabase.GENES,
                source_database=source_database,
                source_identifiers=source_identifiers[start : start + batch_size],
            ),
            options=options,
        )
        if len(result.batches) != 1:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A selected-gene conversion call returned an unexpected batch count.",
                suggested_action=("Retry with the typed KEGG client and unchanged request limits."),
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


def _pairless_gene(identifier: str) -> KeggEntityRef:
    return pair_entity(KeggEntityKind.GENE, identifier)


def _gene_organism(gene: KeggEntityRef) -> str:
    return gene.identifier.partition(":")[0]


def _gene_matches_organism(
    gene: KeggEntityRef,
    allowed_organism_prefixes: frozenset[str] | None,
) -> bool:
    return allowed_organism_prefixes is None or _gene_organism(gene) in allowed_organism_prefixes


def _fail_unexpected_gene_document() -> NoReturn:
    fail(
        ErrorCode.KEGG_PARSE_FAILED,
        "A bounded KEGG GENE call returned an unexpected document type.",
        suggested_action="Retry with the typed KEGG client and a gene entry request.",
    )


__all__: list[str] = []
