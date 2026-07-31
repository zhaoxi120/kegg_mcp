"""Bounded KEGG GET and cache-read services."""

from __future__ import annotations

from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import (
    GetRequest,
    GetResult,
    KeggGetDatabase,
    KeggRequestOptions,
)
from kegg_mcp.kegg.client import KeggClient
from kegg_mcp.kegg.contracts import KeggFlatFileDocument
from kegg_mcp.kegg.operations import get_entry_matches
from kegg_mcp.services.entry_cards import (
    ENTRY_CARD_DATABASES,
    ENTRY_CARD_SNAPSHOT_SECTION,
    build_entry_cards,
    entry_card_previews,
    entry_card_reference_previews,
)
from kegg_mcp.services.models import (
    DETAIL_SECTION,
    MAX_ENTRY_PREVIEW_CHARACTERS,
    MAX_ENTRY_PREVIEW_FIELDS,
    MAX_GET_DIRECT_ENTRY_PREVIEWS,
    MAX_GET_PROVENANCE_BATCHES,
    CachedKeggEntryServiceResult,
    KeggEntriesServiceResult,
    KeggEntryPreview,
    KeggEntryProjection,
)
from kegg_mcp.services.query_support import (
    bounded_query_payload,
    require_bounded_query_direct_result,
)
from kegg_mcp.services.reference_budget import KeggPrimitiveClient, effective_query_options
from kegg_mcp.services.result_builders import _artifact_metadata
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    SQLiteResultStore,
    create_retained_result,
)


def retrieve_kegg_entries(
    request: GetRequest,
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    projection: KeggEntryProjection = KeggEntryProjection.PREVIEW,
    options: KeggRequestOptions | None = None,
) -> KeggEntriesServiceResult:
    """Retrieve approved entries and retain the complete parsed response locally."""
    if projection in {KeggEntryProjection.CARD, KeggEntryProjection.REFERENCES} and any(
        entry.database not in ENTRY_CARD_DATABASES for entry in request.entries
    ):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The typed entry projection contains an unsupported KEGG entry type.",
            suggested_action=(
                "Use preview projection or request only KO, MODULE, pathway, reaction, enzyme, "
                "compound, glycan, gene, or genome cards."
            ),
        )
    fetched = client.get(request, options=effective_query_options(options))
    payload = fetched.model_dump_json().encode("utf-8")
    all_previews = _entry_previews(fetched)
    direct_previews = (
        all_previews[:MAX_GET_DIRECT_ENTRY_PREVIEWS]
        if projection is KeggEntryProjection.PREVIEW
        else ()
    )
    snapshot_payload: bytes | None = None
    card_preview = None
    literature_preview = None
    if projection in {KeggEntryProjection.CARD, KeggEntryProjection.REFERENCES}:
        snapshot = build_entry_cards(fetched)
        snapshot_payload = bounded_query_payload(snapshot.model_dump(mode="json"))
        if projection is KeggEntryProjection.CARD:
            card_preview = entry_card_previews(snapshot)
        else:
            literature_preview = entry_card_reference_previews(snapshot)
    provenance = tuple(fetched.batches[:MAX_GET_PROVENANCE_BATCHES])
    artifact = _artifact_metadata(DETAIL_SECTION, "application/json", payload)
    snapshot_artifact = (
        _artifact_metadata(
            ENTRY_CARD_SNAPSHOT_SECTION,
            "application/json",
            snapshot_payload,
        )
        if snapshot_payload is not None
        else None
    )
    artifacts = [
        ResultArtifactInput(
            section=DETAIL_SECTION,
            mime_type="application/json",
            content=payload,
        )
    ]
    if snapshot_payload is not None:
        artifacts.append(
            ResultArtifactInput(
                section=ENTRY_CARD_SNAPSHOT_SECTION,
                mime_type="application/json",
                content=snapshot_payload,
            )
        )
    with create_retained_result(
        result_store,
        scope_id,
        tuple(artifacts),
    ) as stored:
        result = KeggEntriesServiceResult(
            result=stored,
            artifact=artifact,
            snapshot_artifact=snapshot_artifact,
            projection=projection,
            requested_count=len(request.entries),
            returned_count=len(all_previews),
            missing_identifiers=tuple(item.identifier for item in fetched.missing_entries),
            previews=direct_previews,
            previews_truncated=(
                len(direct_previews) < len(all_previews)
                if projection is KeggEntryProjection.PREVIEW
                else False
            ),
            card_preview=card_preview,
            literature_preview=literature_preview,
            provenance_batch_count=len(fetched.batches),
            provenance=provenance,
            provenance_truncated=len(provenance) < len(fetched.batches),
        )
        require_bounded_query_direct_result(result)
        return result


def _entry_previews(fetched: GetResult) -> tuple[KeggEntryPreview, ...]:
    flat_requests = tuple(
        item for item in fetched.request.entries if item.database is not KeggGetDatabase.BRITE
    )
    previews: list[KeggEntryPreview] = []
    for document in fetched.documents:
        if isinstance(document, KeggFlatFileDocument):
            for entry in document.entries:
                matches = tuple(
                    requested for requested in flat_requests if get_entry_matches(requested, entry)
                )
                if len(matches) != 1:
                    fail(
                        ErrorCode.KEGG_PARSE_FAILED,
                        "The GET result contains an unexpected flat-file entry.",
                        suggested_action="Refresh the exact database-qualified entries and retry.",
                    )
                database = matches[0].database
                text = "\n".join(
                    f"{field.name}: {' '.join(field.value_lines)}" for field in entry.fields
                )
                shown = text[:MAX_ENTRY_PREVIEW_CHARACTERS]
                field_names = tuple(dict.fromkeys(field.name for field in entry.fields))
                shown_field_names = field_names[:MAX_ENTRY_PREVIEW_FIELDS]
                previews.append(
                    KeggEntryPreview(
                        database=database,
                        identifier=entry.identifier,
                        format=document.format.value,
                        field_names=shown_field_names,
                        field_names_truncated=len(shown_field_names) < len(field_names),
                        text_preview=shown,
                        preview_truncated=len(shown) < len(text),
                    )
                )
        else:
            if not document.lines:
                continue
            text = "\n".join(document.lines)
            shown = text[:MAX_ENTRY_PREVIEW_CHARACTERS]
            previews.append(
                KeggEntryPreview(
                    database=KeggGetDatabase.BRITE,
                    identifier=document.identifier,
                    format=document.format.value,
                    text_preview=shown,
                    preview_truncated=len(shown) < len(text),
                )
            )
    return tuple(previews)


def read_cached_kegg_entry(
    request: GetRequest,
    *,
    client: KeggPrimitiveClient,
) -> CachedKeggEntryServiceResult:
    """Read one GET entry from the configured cache without network fallback."""
    cache_client = KeggClient(client.config)
    fetched = cache_client.get(
        request,
        options=KeggRequestOptions(refresh=False, allow_stale=True, cache_only=True),
    )
    previews = _entry_previews(fetched)
    return CachedKeggEntryServiceResult(
        requested_count=len(request.entries),
        returned_count=len(previews),
        missing_identifiers=tuple(item.identifier for item in fetched.missing_entries),
        previews=previews,
        provenance=fetched.batches,
    )


__all__ = ["read_cached_kegg_entry", "retrieve_kegg_entries"]
