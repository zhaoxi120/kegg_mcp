"""Bounded retained KEGG entry search."""

from __future__ import annotations

from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.models import DETAIL_SECTION
from kegg_mcp.services.query_models import (
    MAX_SEARCH_PREVIEW_MATCH_CHARACTERS,
    MAX_SEARCH_PREVIEW_RESULTS,
    KeggEntityKind,
    KeggEntityRef,
    KeggSearchCandidatePreview,
    KeggSearchDatabase,
    KeggSearchMode,
    SearchKeggEntriesRequest,
    SearchKeggEntriesResult,
)
from kegg_mcp.services.query_support import (
    bounded_query_payload,
    pair_entity,
    require_bounded_query_direct_result,
    summarize_query_retrieval,
)
from kegg_mcp.services.reference_budget import KeggQueryClient, effective_query_options
from kegg_mcp.services.result_builders import _artifact_metadata
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    SQLiteResultStore,
    compensate_created_result,
)

_SEARCH_ENTITY_KINDS = {
    KeggSearchDatabase.KO: KeggEntityKind.KO,
    KeggSearchDatabase.PATHWAY: KeggEntityKind.PATHWAY,
    KeggSearchDatabase.MODULE: KeggEntityKind.MODULE,
    KeggSearchDatabase.REACTION: KeggEntityKind.REACTION,
    KeggSearchDatabase.ENZYME: KeggEntityKind.ENZYME,
    KeggSearchDatabase.COMPOUND: KeggEntityKind.COMPOUND,
    KeggSearchDatabase.GENOME: KeggEntityKind.GENOME,
    KeggSearchDatabase.ORGANISM: KeggEntityKind.GENOME,
}


def search_kegg_entries(
    request: SearchKeggEntriesRequest,
    *,
    client: KeggQueryClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
) -> SearchKeggEntriesResult:
    """Return bounded FIND candidates and retain the complete typed endpoint result."""
    fetched = client.find(
        request.to_find_request(),
        options=effective_query_options(options),
    )
    candidates = fetched.document.rows[: request.max_results]
    candidate_preview = tuple(
        _candidate_preview(request.database, row.identifier, row.matched_text)
        for row in candidates[:MAX_SEARCH_PREVIEW_RESULTS]
    )
    payload = bounded_query_payload(
        {
            "request": request.model_dump(mode="json"),
            "find_result": fetched.model_dump(mode="json"),
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
        result = SearchKeggEntriesResult(
            result=stored,
            artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
            database=request.database,
            mode=request.mode,
            observed_count=len(fetched.document.rows),
            candidate_count=len(candidates),
            candidate_preview=candidate_preview,
            candidates_truncated=len(candidates) > len(candidate_preview),
            endpoint_candidates_truncated=len(candidates) < len(fetched.document.rows),
            retrieval=summarize_query_retrieval((fetched.batch,)),
            interpretation_caveats=_search_caveats(request.mode),
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


def _candidate_preview(
    database: KeggSearchDatabase,
    identifier: str,
    raw_match: str,
) -> KeggSearchCandidatePreview:
    match_preview = raw_match[:MAX_SEARCH_PREVIEW_MATCH_CHARACTERS]
    return KeggSearchCandidatePreview(
        entity=_find_entity(database, identifier),
        raw_match=match_preview,
        raw_match_truncated=len(raw_match) > len(match_preview),
    )


def _find_entity(
    database: KeggSearchDatabase,
    identifier: str,
) -> KeggEntityRef:
    return pair_entity(_SEARCH_ENTITY_KINDS[database], identifier)


def _search_caveats(mode: KeggSearchMode) -> tuple[str, ...]:
    caveats = ["Candidates are endpoint matches, not relevance-ranked or selected best matches."]
    if mode is KeggSearchMode.EXACT_MASS:
        caveats.append("Exact-mass matches are compound candidates, not compound identifications.")
    return tuple(caveats)


__all__ = ["search_kegg_entries"]
