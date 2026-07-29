"""Bounded retained KEGG entry search."""

from __future__ import annotations

from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.models import DETAIL_SECTION
from kegg_mcp.services.query_models import (
    KeggEntityKind,
    KeggEntityRef,
    KeggSearchCandidate,
    KeggSearchDatabase,
    KeggSearchMode,
    SearchKeggEntriesRequest,
    SearchKeggEntriesResult,
)
from kegg_mcp.services.query_support import bounded_query_payload, pair_entity
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
    candidates = tuple(
        KeggSearchCandidate(
            entity=_find_entity(request.database, row.identifier),
            raw_match=row.matched_text,
            name=row.matched_text if request.mode is KeggSearchMode.KEYWORD else None,
        )
        for row in fetched.document.rows[: request.max_results]
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
        return SearchKeggEntriesResult(
            result=stored,
            artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
            database=request.database,
            mode=request.mode,
            observed_count=len(fetched.document.rows),
            returned_count=len(candidates),
            candidates=candidates,
            truncated=len(candidates) < len(fetched.document.rows),
            provenance=(fetched.batch,),
        )
    except BaseException:
        compensate_created_result(
            result_store,
            scope_id,
            stored.result_id,
            stored.created_at,
        )
        raise


def _find_entity(
    database: KeggSearchDatabase,
    identifier: str,
) -> KeggEntityRef:
    return pair_entity(_SEARCH_ENTITY_KINDS[database], identifier)


__all__ = ["search_kegg_entries"]
