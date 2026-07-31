"""Deterministic KEGG mapping preparation for external enrichment software."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from kegg_mcp import __version__
from kegg_mcp._serialization import escape_spreadsheet_formula
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.kegg import (
    ConvRequest,
    KeggConvDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
)
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    is_kegg_gene_identifier,
)
from kegg_mcp.services._atomic_bundle import write_text_bundle
from kegg_mcp.services.brite_hierarchy import (
    BriteHierarchyNode,
    iter_brite_htext_nodes,
    load_brite_htext_documents,
)
from kegg_mcp.services.enrichment_handoff_models import (
    ENRICHMENT_HANDOFF_MANIFEST,
    ENRICHMENT_HANDOFF_SCHEMA_VERSION,
    ENRICHMENT_NO_STATISTICS_CAVEAT,
    MAX_ENRICHMENT_ARTIFACT_BYTES,
    MAX_ENRICHMENT_BUNDLE_BYTES,
    MAX_ENRICHMENT_EXPANDED_MAPPINGS,
    MAX_ENRICHMENT_GENE_SETS,
    MAX_ENRICHMENT_KEGG_REQUESTS,
    MAX_ENRICHMENT_MEMBERSHIPS,
    MAX_ENRICHMENT_RELATIONSHIP_ROWS,
    MAX_ENRICHMENT_RESPONSE_BYTES,
    EnrichmentExpandedMapping,
    EnrichmentGeneSet,
    EnrichmentGeneSetSummary,
    EnrichmentGeneSetType,
    EnrichmentHandoffArtifact,
    EnrichmentHandoffBundle,
    EnrichmentHandoffDetail,
    EnrichmentHandoffRequest,
    EnrichmentHandoffResult,
    EnrichmentIdentifierNamespace,
    EnrichmentIdentifierSet,
    EnrichmentInputMapping,
    EnrichmentMappingAudit,
    EnrichmentMappingStatus,
    EnrichmentMappingSummary,
)
from kegg_mcp.services.kegg_relations import (
    BoundedRelationResult,
    bounded_relation_batches,
    planned_relation_request_count,
)
from kegg_mcp.services.query_models import KeggEntityKind, KeggEntityRef
from kegg_mcp.services.query_support import QueryBudget, summarize_query_retrieval
from kegg_mcp.services.reference_budget import KeggQueryClient, effective_query_options

_GENE_NAMESPACE_PREFIX = {
    "ncbi_geneid": "ncbi-geneid",
    "ncbi_proteinid": "ncbi-proteinid",
    "uniprot": "uniprot",
}
_CONV_DATABASE = {
    "ncbi_geneid": KeggConvDatabase.NCBI_GENEID,
    "ncbi_proteinid": KeggConvDatabase.NCBI_PROTEINID,
    "uniprot": KeggConvDatabase.UNIPROT,
}
_GENE_SET_RELATION = {
    "pathway": KeggLinkRelationship.KO_TO_PATHWAY,
    "module": KeggLinkRelationship.KO_TO_MODULE,
}
_GENE_SET_TARGET_PREFIXES = {
    "pathway": frozenset({"path", "pathway"}),
    "module": frozenset({"md", "module"}),
}


@dataclass(frozen=True, slots=True)
class _ResolvedMappings:
    mappings_by_input: dict[str, EnrichmentInputMapping]
    expanded: tuple[EnrichmentExpandedMapping, ...]


def build_enrichment_handoff(
    request: EnrichmentHandoffRequest,
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None = None,
) -> EnrichmentHandoffDetail:
    """Resolve one explicit universe and build KEGG memberships without statistics."""
    options = effective_query_options(options)
    budget = QueryBudget(
        request_limit=MAX_ENRICHMENT_KEGG_REQUESTS,
        row_limit=MAX_ENRICHMENT_RELATIONSHIP_ROWS,
        response_byte_limit=MAX_ENRICHMENT_RESPONSE_BYTES,
        error_message="The enrichment handoff exceeded its aggregate KEGG query bound.",
        suggested_action="Use a smaller explicit universe or fewer KEGG reference classes.",
        row_limit_name="enrichment_relationship_rows",
    )
    provenance: list[KeggBatchProvenance] = []
    _preflight_initial_request_budget(request, client=client, budget=budget)
    resolved = _resolve_universe(
        request,
        client=client,
        options=options,
        budget=budget,
        provenance=provenance,
    )
    foreground_mappings = tuple(
        resolved.mappings_by_input[identifier] for identifier in request.foreground.identifiers
    )
    universe_mappings = tuple(
        resolved.mappings_by_input[identifier] for identifier in request.universe.identifiers
    )
    universe_kos = tuple(
        dict.fromkeys(ko_id for item in universe_mappings for ko_id in item.ko_ids)
    )
    _preflight_gene_set_request_budget(
        request,
        universe_kos=universe_kos,
        client=client,
        budget=budget,
    )
    gene_sets, brite_resolved, brite_missing, brite_unmatched = _build_gene_sets(
        request,
        universe_mappings=universe_mappings,
        universe_kos=universe_kos,
        client=client,
        options=options,
        budget=budget,
        provenance=provenance,
    )
    summaries = tuple(
        EnrichmentGeneSetSummary(
            target=target,
            term_count=sum(item.target is target for item in gene_sets),
            membership_count=sum(
                len(item.universe_identifiers) for item in gene_sets if item.target is target
            ),
        )
        for target in request.gene_sets
    )
    audit = EnrichmentMappingAudit(
        request=request,
        foreground=_mapping_summary("foreground", foreground_mappings),
        universe=_mapping_summary("universe", universe_mappings),
        mappings=universe_mappings,
        expanded_mappings=resolved.expanded,
        gene_sets=summaries,
        brite_resolved_ids=brite_resolved,
        brite_missing_ids=brite_missing,
        brite_unmatched_ko_ids=brite_unmatched,
        provenance=tuple(provenance),
        retrieval=summarize_query_retrieval(tuple(provenance)),
    )
    return EnrichmentHandoffDetail(audit=audit, gene_sets=gene_sets)


def write_enrichment_handoff(
    detail: EnrichmentHandoffDetail,
    output_directory: Path,
    *,
    remove_created_directory_on_failure: bool = False,
) -> EnrichmentHandoffBundle:
    """Write one immutable, manifest-committed enrichment handoff bundle."""
    files = _serialize_handoff_files(detail)
    write_text_bundle(
        output_directory,
        files,
        manifest_name=ENRICHMENT_HANDOFF_MANIFEST,
        remove_created_directory_on_failure=remove_created_directory_on_failure,
        max_artifact_bytes=MAX_ENRICHMENT_ARTIFACT_BYTES,
        max_total_bytes=MAX_ENRICHMENT_BUNDLE_BYTES,
    )
    artifacts = tuple(_artifact(output_directory, name, content) for name, content in files.items())

    def path(name: str) -> str:
        return str((output_directory / name).absolute())

    return EnrichmentHandoffBundle(
        output_directory=str(output_directory.absolute()),
        mapped_foreground=path("mapped_foreground.tsv"),
        mapped_universe=path("mapped_universe.tsv"),
        unmapped=path("unmapped_identifiers.tsv"),
        gene_sets=path("gene_sets.gmt"),
        mapping_audit=path("mapping_audit.json"),
        manifest=path(ENRICHMENT_HANDOFF_MANIFEST),
        artifacts=artifacts,
    )


def prepare_enrichment_handoff(
    request: EnrichmentHandoffRequest,
    *,
    client: KeggQueryClient,
    output_directory: Path,
    options: KeggRequestOptions | None = None,
    remove_created_directory_on_failure: bool = False,
) -> EnrichmentHandoffResult:
    """Build and durably commit an enrichment handoff in one service call."""
    detail = build_enrichment_handoff(request, client=client, options=options)
    bundle = write_enrichment_handoff(
        detail,
        output_directory,
        remove_created_directory_on_failure=remove_created_directory_on_failure,
    )
    return EnrichmentHandoffResult(
        bundle=bundle,
        foreground=detail.audit.foreground,
        universe=detail.audit.universe,
        gene_sets=detail.audit.gene_sets,
        retrieval=detail.audit.retrieval,
    )


def _resolve_universe(
    request: EnrichmentHandoffRequest,
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions,
    budget: QueryBudget,
    provenance: list[KeggBatchProvenance],
) -> _ResolvedMappings:
    identifiers = request.universe.identifiers
    if request.universe.namespace is EnrichmentIdentifierNamespace.KO:
        mappings = {
            identifier: EnrichmentInputMapping(
                input_identifier=identifier,
                status=EnrichmentMappingStatus.MAPPED,
                kegg_genes=(),
                organism_mismatch_genes=(),
                ko_ids=(identifier,),
                organism_mismatch_count=0,
                expanded_mapping_count=1,
                ambiguous=False,
            )
            for identifier in identifiers
        }
        return _ResolvedMappings(
            mappings_by_input=mappings,
            expanded=tuple(
                EnrichmentExpandedMapping(
                    input_identifier=identifier,
                    kegg_gene=None,
                    ko_id=identifier,
                )
                for identifier in identifiers
            ),
        )

    assert request.organism is not None
    genes_by_input: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
    mismatch_genes_by_input: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
    if request.universe.namespace is EnrichmentIdentifierNamespace.KEGG_GENE:
        for identifier in identifiers:
            genes_by_input[identifier].append(identifier)
    else:
        _convert_external_genes(
            request,
            genes_by_input=genes_by_input,
            mismatch_genes_by_input=mismatch_genes_by_input,
            client=client,
            options=options,
            budget=budget,
            provenance=provenance,
        )
    unique_genes = tuple(
        dict.fromkeys(gene for identifier in identifiers for gene in genes_by_input[identifier])
    )
    _preflight_gene_relation_budget(
        request,
        unique_genes=unique_genes,
        client=client,
        budget=budget,
    )
    ko_rows = _bounded_link(
        unique_genes,
        relationship=KeggLinkRelationship.GENE_TO_KO,
        client=client,
        options=options,
        budget=budget,
    )
    provenance.extend(ko_rows.batches)
    kos_by_gene: dict[str, list[str]] = {gene: [] for gene in unique_genes}
    for row in ko_rows.rows:
        gene = row.source_id
        if gene not in kos_by_gene:
            _unexpected_mapping_response("unexpected_gene_to_ko_source")
        ko_id = _prefixed_ko(row.target_id)
        if ko_id not in kos_by_gene[gene]:
            kos_by_gene[gene].append(ko_id)

    mappings: dict[str, EnrichmentInputMapping] = {}
    expanded: list[EnrichmentExpandedMapping] = []
    for identifier in identifiers:
        genes = tuple(dict.fromkeys(genes_by_input[identifier]))
        ko_ids = tuple(
            dict.fromkeys(ko_id for gene in genes for ko_id in kos_by_gene.get(gene, ()))
        )
        input_expanded = tuple(
            EnrichmentExpandedMapping(
                input_identifier=identifier,
                kegg_gene=gene,
                ko_id=ko_id,
            )
            for gene in genes
            for ko_id in kos_by_gene.get(gene, ())
        )
        expanded.extend(input_expanded)
        if len(expanded) > MAX_ENRICHMENT_EXPANDED_MAPPINGS:
            _handoff_limit(
                "expanded_identifier_mappings",
                len(expanded),
                MAX_ENRICHMENT_EXPANDED_MAPPINGS,
            )
        status = (
            EnrichmentMappingStatus.MAPPED
            if ko_ids
            else (
                EnrichmentMappingStatus.ORGANISM_MISMATCH
                if not genes and mismatch_genes_by_input[identifier]
                else EnrichmentMappingStatus.UNMAPPED
            )
        )
        mappings[identifier] = EnrichmentInputMapping(
            input_identifier=identifier,
            status=status,
            kegg_genes=genes,
            organism_mismatch_genes=tuple(mismatch_genes_by_input[identifier]),
            ko_ids=ko_ids,
            organism_mismatch_count=len(mismatch_genes_by_input[identifier]),
            expanded_mapping_count=len(input_expanded),
            ambiguous=len(genes) > 1 or len(ko_ids) > 1,
        )
    return _ResolvedMappings(mappings_by_input=mappings, expanded=tuple(expanded))


def _convert_external_genes(
    request: EnrichmentHandoffRequest,
    *,
    genes_by_input: dict[str, list[str]],
    mismatch_genes_by_input: dict[str, list[str]],
    client: KeggQueryClient,
    options: KeggRequestOptions,
    budget: QueryBudget,
    provenance: list[KeggBatchProvenance],
) -> None:
    namespace = request.universe.namespace
    prefix = _GENE_NAMESPACE_PREFIX[namespace.value]
    source_database = _CONV_DATABASE[namespace.value]
    source_by_alias: dict[str, str] = {}
    wire_identifiers: list[str] = []
    for identifier in request.universe.identifiers:
        wire = f"{prefix}:{identifier}"
        wire_identifiers.append(wire)
        source_by_alias[wire] = identifier
        if namespace is EnrichmentIdentifierNamespace.UNIPROT:
            source_by_alias[f"up:{identifier}"] = identifier
    batch_size = _conversion_batch_size(client)
    for start in range(0, len(wire_identifiers), batch_size):
        budget.require_request_capacity()
        result = client.conv(
            ConvRequest(
                target_database=KeggConvDatabase.GENES,
                source_database=source_database,
                source_identifiers=tuple(wire_identifiers[start : start + batch_size]),
            ),
            options=options,
        )
        if len(result.batches) != 1:
            _unexpected_mapping_response("unexpected_conversion_batch_count")
        budget.record(row_count=len(result.rows), batches=result.batches)
        provenance.extend(result.batches)
        for row in result.rows:
            input_identifier = source_by_alias.get(row.source_id)
            if input_identifier is None or not is_kegg_gene_identifier(row.target_id):
                _unexpected_mapping_response("unexpected_conversion_identifier")
            if row.target_id.partition(":")[0] != request.organism:
                if row.target_id not in mismatch_genes_by_input[input_identifier]:
                    mismatch_genes_by_input[input_identifier].append(row.target_id)
            elif row.target_id not in genes_by_input[input_identifier]:
                genes_by_input[input_identifier].append(row.target_id)


def _preflight_initial_request_budget(
    request: EnrichmentHandoffRequest,
    *,
    client: KeggQueryClient,
    budget: QueryBudget,
) -> None:
    """Reject request plans that cannot fit before the first KEGG operation."""
    namespace = request.universe.namespace
    if namespace is EnrichmentIdentifierNamespace.KO:
        _preflight_gene_set_request_budget(
            request,
            universe_kos=request.universe.identifiers,
            client=client,
            budget=budget,
        )
        return
    minimum_downstream = _minimum_gene_set_request_count(request)
    if namespace is EnrichmentIdentifierNamespace.KEGG_GENE:
        gene_requests = planned_relation_request_count(
            request.universe.identifiers,
            relationship=KeggLinkRelationship.GENE_TO_KO,
            client=client,
        )
        planned = gene_requests + minimum_downstream
    else:
        batch_size = _conversion_batch_size(client)
        conversion_requests = (len(request.universe.identifiers) + batch_size - 1) // batch_size
        planned = conversion_requests + 1 + minimum_downstream
    if planned:
        budget.require_request_capacity(planned)


def _preflight_gene_relation_budget(
    request: EnrichmentHandoffRequest,
    *,
    unique_genes: tuple[str, ...],
    client: KeggQueryClient,
    budget: QueryBudget,
) -> None:
    """Reserve exact gene LINK calls plus the minimum possible downstream calls."""
    if not unique_genes:
        return
    planned = planned_relation_request_count(
        unique_genes,
        relationship=KeggLinkRelationship.GENE_TO_KO,
        client=client,
    ) + _minimum_gene_set_request_count(request)
    budget.require_request_capacity(planned)


def _preflight_gene_set_request_budget(
    request: EnrichmentHandoffRequest,
    *,
    universe_kos: tuple[str, ...],
    client: KeggQueryClient,
    budget: QueryBudget,
) -> None:
    """Reserve the exact known LINK and explicit BRITE GET request plan."""
    if not universe_kos:
        return
    planned = sum(
        planned_relation_request_count(
            universe_kos,
            relationship=_GENE_SET_RELATION[target.value],
            client=client,
        )
        for target in request.gene_sets
        if target is not EnrichmentGeneSetType.BRITE
    )
    if EnrichmentGeneSetType.BRITE in request.gene_sets:
        planned += len(request.brite_ids)
    if planned:
        budget.require_request_capacity(planned)


def _minimum_gene_set_request_count(request: EnrichmentHandoffRequest) -> int:
    relation_requests = sum(
        target is not EnrichmentGeneSetType.BRITE for target in request.gene_sets
    )
    return relation_requests + (
        len(request.brite_ids) if EnrichmentGeneSetType.BRITE in request.gene_sets else 0
    )


def _conversion_batch_size(client: KeggQueryClient) -> int:
    return min(
        client.config.limits.relation_batch_size,
        client.config.limits.max_identifiers,
    )


def _build_gene_sets(
    request: EnrichmentHandoffRequest,
    *,
    universe_mappings: tuple[EnrichmentInputMapping, ...],
    universe_kos: tuple[str, ...],
    client: KeggQueryClient,
    options: KeggRequestOptions,
    budget: QueryBudget,
    provenance: list[KeggBatchProvenance],
) -> tuple[
    tuple[EnrichmentGeneSet, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    universe_ko_set = frozenset(universe_kos)
    inputs_by_ko, input_order = _input_membership_indexes(universe_mappings)
    sets: list[EnrichmentGeneSet] = []
    brite_resolved: tuple[str, ...] = ()
    brite_missing: tuple[str, ...] = ()
    brite_unmatched: tuple[str, ...] = ()
    for target in request.gene_sets:
        if target is EnrichmentGeneSetType.BRITE:
            (
                brite_sets,
                brite_resolved,
                brite_missing,
                brite_unmatched,
                brite_batches,
            ) = _build_brite_sets(
                request,
                universe_mappings=universe_mappings,
                universe_kos=universe_kos,
                client=client,
                options=options,
                budget=budget,
            )
            sets.extend(brite_sets)
            provenance.extend(brite_batches)
            continue
        relation = _bounded_link(
            universe_kos,
            relationship=_GENE_SET_RELATION[target.value],
            client=client,
            options=options,
            budget=budget,
        )
        provenance.extend(relation.batches)
        kos_by_term: dict[str, set[str]] = defaultdict(set)
        for row in relation.rows:
            ko_id = _prefixed_ko(row.source_id)
            if ko_id not in universe_ko_set:
                _unexpected_mapping_response("unexpected_gene_set_source")
            term_id = _target_identifier(target, row.target_id)
            kos_by_term[term_id].add(ko_id)
        for term_id in sorted(kos_by_term):
            member_kos = frozenset(kos_by_term[term_id])
            members = _members_for_kos(member_kos, inputs_by_ko, input_order)
            if members:
                sets.append(
                    EnrichmentGeneSet(
                        target=target,
                        term_id=term_id,
                        description="na",
                        ko_ids=tuple(sorted(member_kos)),
                        universe_identifiers=members,
                    )
                )
        _check_gene_set_limits(sets)
    return tuple(sets), brite_resolved, brite_missing, brite_unmatched


def _build_brite_sets(
    request: EnrichmentHandoffRequest,
    *,
    universe_mappings: tuple[EnrichmentInputMapping, ...],
    universe_kos: tuple[str, ...],
    client: KeggQueryClient,
    options: KeggRequestOptions,
    budget: QueryBudget,
) -> tuple[
    tuple[EnrichmentGeneSet, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[KeggBatchProvenance, ...],
]:
    if not universe_kos:
        return (), (), (), (), ()
    budget.require_request_capacity(len(request.brite_ids))
    loaded = load_brite_htext_documents(
        request.brite_ids,
        client=client,
        options=options,
    )
    batches = loaded.hierarchy_provenance
    budget.record(row_count=0, batches=batches)
    inputs_by_ko, input_order = _input_membership_indexes(universe_mappings)
    universe_ko_set = frozenset(universe_kos)
    matched_kos: set[str] = set()
    path_members: dict[
        tuple[str, tuple[tuple[str, str | None, str], ...]],
        set[str],
    ] = defaultdict(set)
    nodes_by_path: dict[
        tuple[str, tuple[tuple[str, str | None, str], ...]],
        tuple[BriteHierarchyNode, ...],
    ] = {}
    observed_memberships = 0
    for document in loaded.documents:
        for nodes, candidate in iter_brite_htext_nodes(document):
            if candidate not in universe_ko_set:
                continue
            matched_kos.add(candidate)
            # Every non-leaf prefix is a category. The terminal KO is not a category set.
            for depth in range(1, len(nodes)):
                category_nodes = nodes[:depth]
                key = (
                    document.identifier,
                    tuple((node.level, node.node_id, node.name) for node in category_nodes),
                )
                if key not in path_members and len(path_members) >= MAX_ENRICHMENT_GENE_SETS:
                    _handoff_limit(
                        "gene_set_count",
                        len(path_members) + 1,
                        MAX_ENRICHMENT_GENE_SETS,
                    )
                if candidate not in path_members[key]:
                    observed_memberships += 1
                    if observed_memberships > MAX_ENRICHMENT_MEMBERSHIPS:
                        _handoff_limit(
                            "gene_set_membership_count",
                            observed_memberships,
                            MAX_ENRICHMENT_MEMBERSHIPS,
                        )
                    path_members[key].add(candidate)
                nodes_by_path[key] = category_nodes
    sets: list[EnrichmentGeneSet] = []
    for key in sorted(path_members, key=_brite_path_sort_key):
        brite_id, canonical_path = key
        member_kos = frozenset(path_members[key])
        members = _members_for_kos(member_kos, inputs_by_ko, input_order)
        if not members:
            continue
        digest = hashlib.sha256(
            json.dumps(
                (brite_id, canonical_path),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        nodes = nodes_by_path[key]
        sets.append(
            EnrichmentGeneSet(
                target=EnrichmentGeneSetType.BRITE,
                term_id=f"brite:{brite_id}:{digest}",
                description=" / ".join(_one_line(node.name) for node in nodes) or brite_id,
                ko_ids=tuple(sorted(member_kos)),
                universe_identifiers=members,
            )
        )
    _check_gene_set_limits(sets)
    unmatched = tuple(ko_id for ko_id in universe_kos if ko_id not in matched_kos)
    return (
        tuple(sets),
        loaded.resolved_brite_ids,
        loaded.missing_brite_ids,
        unmatched,
        batches,
    )


def _bounded_link(
    source_identifiers: tuple[str, ...],
    *,
    relationship: KeggLinkRelationship,
    client: KeggQueryClient,
    options: KeggRequestOptions,
    budget: QueryBudget,
) -> BoundedRelationResult:
    result = bounded_relation_batches(
        source_identifiers,
        relationship=relationship,
        client=client,
        options=options,
        max_total_requests=budget.remaining_requests,
        max_total_rows=budget.remaining_rows,
        max_total_response_bytes=budget.remaining_response_bytes,
    )
    budget.record(row_count=len(result.rows), batches=result.batches)
    return result


def _mapping_summary(
    role: Literal["foreground", "universe"],
    mappings: tuple[EnrichmentInputMapping, ...],
) -> EnrichmentMappingSummary:
    mapped = sum(item.status is EnrichmentMappingStatus.MAPPED for item in mappings)
    return EnrichmentMappingSummary(
        role=role,
        input_count=len(mappings),
        mapped_input_count=mapped,
        unmapped_input_count=sum(
            item.status is EnrichmentMappingStatus.UNMAPPED for item in mappings
        ),
        organism_mismatch_input_count=sum(
            item.status is EnrichmentMappingStatus.ORGANISM_MISMATCH for item in mappings
        ),
        ambiguous_input_count=sum(item.ambiguous for item in mappings),
        unique_kegg_gene_count=len({gene for mapping in mappings for gene in mapping.kegg_genes}),
        unique_ko_count=len({ko_id for mapping in mappings for ko_id in mapping.ko_ids}),
        expanded_mapping_count=sum(item.expanded_mapping_count for item in mappings),
        mapping_yield=mapped / len(mappings),
        organism_mismatch_candidate_count=sum(item.organism_mismatch_count for item in mappings),
    )


def _input_membership_indexes(
    mappings: tuple[EnrichmentInputMapping, ...],
) -> tuple[dict[str, tuple[str, ...]], dict[str, int]]:
    inputs_by_ko: dict[str, list[str]] = defaultdict(list)
    for mapping in mappings:
        for ko_id in mapping.ko_ids:
            inputs_by_ko[ko_id].append(mapping.input_identifier)
    return (
        {ko_id: tuple(identifiers) for ko_id, identifiers in inputs_by_ko.items()},
        {mapping.input_identifier: index for index, mapping in enumerate(mappings)},
    )


def _members_for_kos(
    ko_ids: frozenset[str],
    inputs_by_ko: dict[str, tuple[str, ...]],
    input_order: dict[str, int],
) -> tuple[str, ...]:
    members = {identifier for ko_id in ko_ids for identifier in inputs_by_ko.get(ko_id, ())}
    return tuple(sorted(members, key=input_order.__getitem__))


def _serialize_handoff_files(detail: EnrichmentHandoffDetail) -> dict[str, str]:
    mapping_by_identifier = {mapping.input_identifier: mapping for mapping in detail.audit.mappings}
    foreground = tuple(
        mapping_by_identifier[identifier]
        for identifier in detail.audit.request.foreground.identifiers
    )
    universe = tuple(
        mapping_by_identifier[identifier]
        for identifier in detail.audit.request.universe.identifiers
    )
    files = {
        "mapped_foreground.tsv": _mapped_tsv(
            detail.audit.expanded_mappings,
            foreground,
        ),
        "mapped_universe.tsv": _mapped_tsv(
            detail.audit.expanded_mappings,
            universe,
        ),
        "unmapped_identifiers.tsv": _unmapped_tsv(
            detail.audit.request.foreground.identifiers,
            detail.audit.request.universe.identifiers,
            mapping_by_identifier,
        ),
        "gene_sets.gmt": _gene_sets_gmt(detail.gene_sets),
        "mapping_audit.json": _json_text(detail.audit.model_dump(mode="json")),
    }
    manifest_files = {
        name: {
            "byte_size": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for name, content in files.items()
    }
    provenance = detail.audit.provenance
    manifest = {
        "schema_version": ENRICHMENT_HANDOFF_SCHEMA_VERSION,
        "bundle_kind": "kegg_enrichment_input",
        "target": "enrichment",
        "producer": {"name": "kegg-mcp", "version": __version__},
        "request_contract": detail.audit.request.model_dump(mode="json"),
        "files": manifest_files,
        "retrieval": detail.audit.retrieval.model_dump(mode="json"),
        "retrieved_at": sorted({batch.retrieved_at.isoformat() for batch in provenance}),
        "database_releases": sorted(
            {batch.database_release for batch in provenance if batch.database_release is not None}
        ),
        "endpoint_classes": sorted({batch.retrieval_endpoint_class.value for batch in provenance}),
        "parser_versions": sorted(
            {f"{batch.parser_name}:{batch.parser_version}" for batch in provenance}
        ),
        "ambiguity_policy": "expand_all_candidates",
        "statistical_tests_performed": False,
        "interpretation_caveat": ENRICHMENT_NO_STATISTICS_CAVEAT,
    }
    files[ENRICHMENT_HANDOFF_MANIFEST] = _json_text(manifest)
    return files


def _mapped_tsv(
    expanded: tuple[EnrichmentExpandedMapping, ...],
    mappings: tuple[EnrichmentInputMapping, ...],
) -> str:
    requested = {mapping.input_identifier for mapping in mappings}
    mapping_by_identifier = {mapping.input_identifier: mapping for mapping in mappings}
    rows = [
        (
            item.input_identifier,
            item.kegg_gene or "",
            item.ko_id,
            str(mapping_by_identifier[item.input_identifier].ambiguous).lower(),
        )
        for item in expanded
        if item.input_identifier in requested
    ]
    return _tabular(
        ("input_identifier", "kegg_gene", "ko_id", "ambiguous"),
        rows,
    )


def _unmapped_tsv(
    foreground_identifiers: tuple[str, ...],
    universe_identifiers: tuple[str, ...],
    mappings: dict[str, EnrichmentInputMapping],
) -> str:
    rows: list[tuple[object, ...]] = []
    for role, identifiers in (
        ("foreground", foreground_identifiers),
        ("universe", universe_identifiers),
    ):
        rows.extend(
            (
                role,
                identifier,
                mappings[identifier].status.value,
                mappings[identifier].organism_mismatch_count,
                ",".join(mappings[identifier].organism_mismatch_genes),
            )
            for identifier in identifiers
            if mappings[identifier].status is not EnrichmentMappingStatus.MAPPED
        )
    return _tabular(
        (
            "role",
            "input_identifier",
            "mapping_status",
            "organism_mismatch_count",
            "organism_mismatch_gene_candidates",
        ),
        rows,
    )


def _gene_sets_gmt(gene_sets: tuple[EnrichmentGeneSet, ...]) -> str:
    return _tabular(
        (),
        (
            (
                gene_set.term_id,
                gene_set.description,
                *gene_set.universe_identifiers,
            )
            for gene_set in gene_sets
        ),
        include_header=False,
    )


def _tabular(
    header: tuple[str, ...],
    rows: Iterable[tuple[object, ...]],
    *,
    include_header: bool = True,
) -> str:
    target = io.StringIO(newline="")
    writer = csv.writer(target, delimiter="\t", lineterminator="\n")
    if include_header:
        writer.writerow(header)
    for row in rows:
        writer.writerow(
            escape_spreadsheet_formula("" if value is None else str(value)) for value in row
        )
    return target.getvalue()


def _artifact(
    output_directory: Path,
    name: str,
    content: str,
) -> EnrichmentHandoffArtifact:
    encoded = content.encode("utf-8")
    mime_type = (
        "application/json" if name.endswith(".json") else "text/tab-separated-values; charset=utf-8"
    )
    return EnrichmentHandoffArtifact(
        name=name,
        mime_type=mime_type,
        byte_size=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        path=str((output_directory / name).absolute()),
    )


def _prefixed_ko(value: str) -> str:
    prefix, separator, identifier = value.partition(":")
    ko_id, _ = try_normalize_ko_id(identifier if separator and prefix == "ko" else value)
    if ko_id is None:
        _unexpected_mapping_response("invalid_ko_identifier")
    return ko_id


def _target_identifier(target: EnrichmentGeneSetType, value: str) -> str:
    prefix, separator, identifier = value.partition(":")
    if separator != ":" or prefix not in _GENE_SET_TARGET_PREFIXES[target.value]:
        _unexpected_mapping_response("invalid_gene_set_target")
    if target is EnrichmentGeneSetType.PATHWAY:
        KeggEntityRef(kind=KeggEntityKind.PATHWAY, identifier=identifier)
    else:
        KeggEntityRef(kind=KeggEntityKind.MODULE, identifier=identifier)
    return identifier


def _brite_path_sort_key(
    item: tuple[str, tuple[tuple[str, str | None, str], ...]],
) -> tuple[str, tuple[tuple[str, str, str], ...]]:
    brite_id, path = item
    return brite_id, tuple((level, node_id or "", name) for level, node_id, name in path)


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _check_gene_set_limits(gene_sets: list[EnrichmentGeneSet]) -> None:
    if len(gene_sets) > MAX_ENRICHMENT_GENE_SETS:
        _handoff_limit("gene_set_count", len(gene_sets), MAX_ENRICHMENT_GENE_SETS)
    memberships = sum(len(item.universe_identifiers) for item in gene_sets)
    if memberships > MAX_ENRICHMENT_MEMBERSHIPS:
        _handoff_limit(
            "gene_set_membership_count",
            memberships,
            MAX_ENRICHMENT_MEMBERSHIPS,
        )


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _unexpected_mapping_response(reason: str) -> NoReturn:
    fail(
        ErrorCode.KEGG_PARSE_FAILED,
        "The KEGG mapping response was incompatible with the enrichment handoff request.",
        suggested_action="Refresh the exact typed KEGG mapping request and retry.",
        safe_details=(SafeDetail(name="reason", value=reason),),
    )


def _handoff_limit(name: str, observed: int, limit: int) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "The enrichment handoff exceeded its fixed service bound.",
        suggested_action="Use a smaller explicit universe or fewer KEGG reference classes.",
        safe_details=(
            SafeDetail(name="limit_name", value=name),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit", value=str(limit)),
        ),
    )


__all__ = [
    "ENRICHMENT_HANDOFF_MANIFEST",
    "ENRICHMENT_HANDOFF_SCHEMA_VERSION",
    "EnrichmentExpandedMapping",
    "EnrichmentGeneSet",
    "EnrichmentGeneSetSummary",
    "EnrichmentGeneSetType",
    "EnrichmentHandoffBundle",
    "EnrichmentHandoffDetail",
    "EnrichmentHandoffRequest",
    "EnrichmentHandoffResult",
    "EnrichmentIdentifierNamespace",
    "EnrichmentIdentifierSet",
    "EnrichmentInputMapping",
    "EnrichmentMappingAudit",
    "EnrichmentMappingStatus",
    "EnrichmentMappingSummary",
    "build_enrichment_handoff",
    "prepare_enrichment_handoff",
    "write_enrichment_handoff",
]
