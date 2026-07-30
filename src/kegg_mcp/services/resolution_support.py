"""Private shared support for bounded KEGG entity resolution."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, NoReturn

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg import (
    KeggLinkRelationship,
    KeggRequestOptions,
    KeggTaxonomyRank,
)
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggPairRow
from kegg_mcp.services.kegg_relations import (
    BoundedRelationResult,
    bounded_relation_batches,
)
from kegg_mcp.services.models import DETAIL_SECTION
from kegg_mcp.services.query_models import (
    MAX_RESOLUTION_CANDIDATE_PREVIEW,
    MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS,
    MAX_RESOLUTION_ENTITIES,
    MAX_RESOLUTION_ENTITY_PREVIEW,
    MAX_RESOLUTION_INPUT_PREVIEW,
    MAX_RESOLUTION_PATHWAY_DIRECT_PREVIEW,
    MAX_RESOLUTION_TAXONOMY_PREVIEW,
    EntityResolution,
    EntityResolutionPreview,
    GeneResolutionRequest,
    KeggRelationType,
    MappingStatus,
    OrganismPathwayDirectPreviewEntry,
    OrganismPathwayPreviewEntry,
    OrganismResolutionRequest,
    ResolutionOperation,
    ResolvedEntityCandidate,
    ResolvedEntityCandidatePreview,
    ResolveKeggEntitiesResult,
)
from kegg_mcp.services.query_support import (
    bounded_query_payload,
    entity_key,
    require_bounded_query_direct_result,
    require_provenance_bound,
    summarize_query_retrieval,
)
from kegg_mcp.services.reference_budget import KeggQueryClient
from kegg_mcp.services.result_builders import _artifact_metadata
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    SQLiteResultStore,
    compensate_created_result,
)

MAX_RESOLVER_KEGG_REQUESTS = 128
MAX_RESOLVER_ROWS = 10_000
MAX_RESOLVER_RESPONSE_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class ResolverBudget:
    request_limit: int = MAX_RESOLVER_KEGG_REQUESTS
    row_limit: int = MAX_RESOLVER_ROWS
    response_byte_limit: int = MAX_RESOLVER_RESPONSE_BYTES
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
            _resolver_budget_limit(
                "kegg_request_count",
                next_requests,
                self.request_limit,
            )
        if next_rows > self.row_limit:
            _resolver_budget_limit("query_row_count", next_rows, self.row_limit)
        if next_response_bytes > self.response_byte_limit:
            _resolver_budget_limit(
                "kegg_response_bytes",
                next_response_bytes,
                self.response_byte_limit,
            )
        self.requests = next_requests
        self.rows = next_rows
        self.response_bytes = next_response_bytes


def bounded_resolver_link(
    source_identifiers: tuple[str, ...],
    *,
    relationship: KeggLinkRelationship,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: ResolverBudget,
    taxonomy_rank: KeggTaxonomyRank = KeggTaxonomyRank.EXACT,
) -> BoundedRelationResult:
    if budget.remaining_rows <= 0:
        _resolver_budget_limit(
            "query_row_count",
            budget.rows + 1,
            budget.row_limit,
        )
    if budget.remaining_response_bytes <= 0:
        _resolver_budget_limit(
            "kegg_response_bytes",
            budget.response_bytes + 1,
            budget.response_byte_limit,
        )
    if budget.remaining_requests <= 0:
        _resolver_budget_limit(
            "kegg_request_count",
            budget.requests + 1,
            budget.request_limit,
        )
    return bounded_relation_batches(
        source_identifiers,
        relationship=relationship,
        client=client,
        options=options,
        taxonomy_rank=taxonomy_rank,
        max_total_requests=budget.remaining_requests,
        max_total_rows=budget.remaining_rows,
        max_total_response_bytes=budget.remaining_response_bytes,
        record_batch=lambda count, batches: budget.record(
            row_count=count,
            batches=batches,
        ),
    )


def retain_resolution(
    *,
    request: GeneResolutionRequest | OrganismResolutionRequest,
    kind: Literal["gene", "organism"],
    candidate_groups: tuple[tuple[ResolvedEntityCandidate, ...], ...],
    mismatch_counts: tuple[int, ...],
    operations_by_input: tuple[tuple[ResolutionOperation, ...], ...],
    before_deduplication: int,
    provenance: tuple[KeggBatchProvenance, ...],
    steps: list[dict[str, Any]],
    budget: ResolverBudget,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> ResolveKeggEntitiesResult:
    require_provenance_bound(provenance)
    unique_entities = {
        entity_key(entity)
        for groups in candidate_groups
        for candidate in groups
        for entity in candidate.entities
    }
    if len(unique_entities) > MAX_RESOLUTION_ENTITIES:
        resolution_limit("unique_mapped_entity_count")

    canonical_candidate_counts = Counter(
        entity_key(candidate.canonical_entity)
        for groups in candidate_groups
        for candidate in groups
    )
    resolutions: list[EntityResolution] = []
    for input_identifier, groups, mismatch_count, operations in zip(
        request.identifiers,
        candidate_groups,
        mismatch_counts,
        operations_by_input,
        strict=True,
    ):
        if not operations:
            raise AssertionError("every resolution path must record an operation")
        if not groups:
            status = MappingStatus.ORGANISM_MISMATCH if mismatch_count else MappingStatus.UNMAPPED
        elif len(groups) > 1:
            status = MappingStatus.ONE_TO_MANY
        elif canonical_candidate_counts[entity_key(next(iter(groups)).canonical_entity)] > 1:
            status = MappingStatus.MANY_TO_ONE
        else:
            status = MappingStatus.ONE_TO_ONE
        resolutions.append(
            EntityResolution(
                input_identifier=input_identifier,
                status=status,
                candidates=groups,
                discarded_organism_mismatch_count=mismatch_count,
                operations_used=operations,
            )
        )

    mapped_count = sum(
        resolution.status
        in {
            MappingStatus.ONE_TO_ONE,
            MappingStatus.ONE_TO_MANY,
            MappingStatus.MANY_TO_ONE,
        }
        for resolution in resolutions
    )
    resolution_previews = tuple(
        _resolution_preview(resolution) for resolution in resolutions[:MAX_RESOLUTION_INPUT_PREVIEW]
    )
    payload = bounded_query_payload(
        {
            "request": request.model_dump(mode="json"),
            "resolutions": [resolution.model_dump(mode="json") for resolution in resolutions],
            "steps": steps,
            "provenance": [batch.model_dump(mode="json") for batch in provenance],
            "budget": {
                "kegg_requests": budget.requests,
                "query_rows": budget.rows,
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
        result = ResolveKeggEntitiesResult(
            result=stored,
            artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
            kind=kind,
            input_count=len(request.identifiers),
            mapped_input_count=mapped_count,
            ambiguous_input_count=sum(
                resolution.status is MappingStatus.ONE_TO_MANY for resolution in resolutions
            ),
            many_to_one_input_count=sum(
                resolution.status is MappingStatus.MANY_TO_ONE for resolution in resolutions
            ),
            mismatch_input_count=sum(
                resolution.discarded_organism_mismatch_count > 0 for resolution in resolutions
            ),
            mapping_yield=mapped_count / len(request.identifiers),
            mapped_entity_count_before_deduplication=before_deduplication,
            unique_mapped_entity_count=len(unique_entities),
            resolution_previews=resolution_previews,
            resolutions_truncated=len(resolutions) > len(resolution_previews),
            retrieval=summarize_query_retrieval(provenance),
            interpretation_caveats=_resolution_caveats(request),
        )
        require_bounded_query_direct_result(result)
        return result
    except BaseException:
        compensate_created_result(
            result_store,
            scope_id,
            stored.result_id,
            stored.created_at,
        )
        raise


def _resolution_preview(resolution: EntityResolution) -> EntityResolutionPreview:
    candidate_preview = tuple(
        _candidate_preview(candidate)
        for candidate in resolution.candidates[:MAX_RESOLUTION_CANDIDATE_PREVIEW]
    )
    return EntityResolutionPreview(
        input_identifier=resolution.input_identifier,
        status=resolution.status,
        candidate_count=len(resolution.candidates),
        candidate_preview=candidate_preview,
        candidates_truncated=len(resolution.candidates) > len(candidate_preview),
        discarded_organism_mismatch_count=resolution.discarded_organism_mismatch_count,
        operations_used=resolution.operations_used,
    )


def _candidate_preview(
    candidate: ResolvedEntityCandidate,
) -> ResolvedEntityCandidatePreview:
    entity_preview = candidate.entities[:MAX_RESOLUTION_ENTITY_PREVIEW]
    lineage = tuple(
        _direct_text(label)
        for label in candidate.taxonomy_lineage[:MAX_RESOLUTION_TAXONOMY_PREVIEW]
    )
    if candidate.name is None:
        name = None
        name_truncated = False
    else:
        name, name_truncated = _direct_text(candidate.name)
    pathways = candidate.organism_pathways
    pathway_preview = (
        ()
        if pathways is None
        else tuple(
            _pathway_direct_preview(entry)
            for entry in pathways.preview[:MAX_RESOLUTION_PATHWAY_DIRECT_PREVIEW]
        )
    )
    return ResolvedEntityCandidatePreview(
        canonical_entity=candidate.canonical_entity,
        entity_count=len(candidate.entities),
        entity_preview=entity_preview,
        entities_truncated=len(candidate.entities) > len(entity_preview),
        name=name,
        name_truncated=name_truncated,
        taxonomy_lineage_count=len(candidate.taxonomy_lineage),
        taxonomy_lineage_preview=tuple(value for value, _ in lineage),
        taxonomy_lineage_truncated=len(candidate.taxonomy_lineage) > len(lineage),
        taxonomy_lineage_text_truncated=any(truncated for _, truncated in lineage),
        organism_pathway_count=(None if pathways is None else pathways.total_count),
        organism_pathway_preview=pathway_preview,
        organism_pathways_truncated=(
            None if pathways is None else pathways.total_count > len(pathway_preview)
        ),
    )


def _pathway_direct_preview(
    entry: OrganismPathwayPreviewEntry,
) -> OrganismPathwayDirectPreviewEntry:
    name, name_truncated = _direct_text(entry.name)
    return OrganismPathwayDirectPreviewEntry(
        pathway=entry.pathway,
        name=name,
        name_truncated=name_truncated,
    )


def _direct_text(value: str) -> tuple[str, bool]:
    return (
        value[:MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS],
        len(value) > MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS,
    )


def _resolution_caveats(
    request: GeneResolutionRequest | OrganismResolutionRequest,
) -> tuple[str, ...]:
    caveats = [
        "Unmapped identifiers are not evidence that the biological entity does not exist.",
        "Ambiguous candidates are reported without automatic selection.",
    ]
    if isinstance(request, OrganismResolutionRequest) and request.include_pathway_directory:
        caveats.append(
            "Organism-specific pathway directory entries are KEGG references and do not "
            "establish pathway presence, completeness, activity, flux, or phenotype."
        )
    return tuple(caveats)


def relation_step(
    relationship: KeggRelationType,
    rows: tuple[KeggPairRow, ...],
    batches: tuple[KeggBatchProvenance, ...],
) -> dict[str, Any]:
    return {
        "operation": ResolutionOperation.LINK.value,
        "relationship": relationship.value,
        "rows": [row.model_dump(mode="json") for row in rows],
        "provenance": [batch.model_dump(mode="json") for batch in batches],
    }


def _resolver_budget_limit(
    name: str,
    observed: int,
    limit: int,
) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "Entity resolution exceeded its aggregate KEGG query budget.",
        suggested_action="Request fewer identifiers or target entity kinds.",
        safe_details=(
            SafeDetail(name="limit_name", value=name),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit", value=str(limit)),
        ),
    )


def require_resolver_request_capacity(budget: ResolverBudget) -> None:
    if budget.remaining_requests <= 0:
        _resolver_budget_limit(
            "kegg_request_count",
            budget.requests + 1,
            budget.request_limit,
        )


def resolution_limit(limit_name: str) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "Entity resolution exceeded its bounded direct-result projection.",
        suggested_action="Request fewer identifiers or target entity kinds.",
        safe_details=(SafeDetail(name="limit_name", value=limit_name),),
    )


__all__: list[str] = []
