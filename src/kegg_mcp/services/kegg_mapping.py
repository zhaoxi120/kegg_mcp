"""Bounded KEGG GET, cache-read, and KO mapping services."""

from __future__ import annotations

from typing import Literal

from kegg_mcp.domain.annotations import try_normalize_ko_id
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import (
    GetRequest,
    GetResult,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
)
from kegg_mcp.kegg.client import KeggClient
from kegg_mcp.kegg.contracts import (
    KeggFlatFileDocument,
    KeggPairRow,
    is_kegg_pathway_identifier,
)
from kegg_mcp.services.models import (
    DETAIL_SECTION,
    MAX_ENTRY_PREVIEW_CHARACTERS,
    MAX_ENTRY_PREVIEW_FIELDS,
    MAX_MAPPING_PREVIEW_ROWS,
    CachedKeggEntryServiceResult,
    KeggEntriesServiceResult,
    KeggEntryPreview,
    KoMappingServiceResult,
    PathwayMappingRow,
)
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.result_builders import _artifact_metadata, _json_bytes
from kegg_mcp.services.result_store import ResultArtifactInput, SQLiteResultStore


def retrieve_kegg_entries(
    request: GetRequest,
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
) -> KeggEntriesServiceResult:
    """Retrieve approved entries and retain the complete parsed response locally."""
    fetched = client.get(request, options=options)
    payload = fetched.model_dump_json().encode("utf-8")
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    previews = _entry_previews(fetched, request)
    returned = len(previews)
    return KeggEntriesServiceResult(
        result=stored,
        artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
        requested_count=len(request.entries),
        returned_count=returned,
        missing_identifiers=tuple(item.identifier for item in fetched.missing_entries),
        previews=previews,
        provenance=tuple(fetched.batches),
    )


def _entry_previews(fetched: GetResult, request: GetRequest) -> tuple[KeggEntryPreview, ...]:
    database_by_identifier = {item.identifier: item.database for item in request.entries}
    previews: list[KeggEntryPreview] = []
    for document in fetched.documents:
        if isinstance(document, KeggFlatFileDocument):
            for entry in document.entries:
                text = "\n".join(
                    f"{field.name}: {' '.join(field.value_lines)}" for field in entry.fields
                )
                shown = text[:MAX_ENTRY_PREVIEW_CHARACTERS]
                field_names = tuple(dict.fromkeys(field.name for field in entry.fields))
                shown_field_names = field_names[:MAX_ENTRY_PREVIEW_FIELDS]
                previews.append(
                    KeggEntryPreview(
                        database=database_by_identifier[entry.identifier],
                        identifier=entry.identifier,
                        format=document.format.value,
                        field_names=shown_field_names,
                        field_names_truncated=len(shown_field_names) < len(field_names),
                        text_preview=shown,
                        preview_truncated=len(shown) < len(text),
                    )
                )
        else:
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
    previews = _entry_previews(fetched, request)
    return CachedKeggEntryServiceResult(
        requested_count=len(request.entries),
        returned_count=len(previews),
        missing_identifiers=tuple(item.identifier for item in fetched.missing_entries),
        previews=previews,
        provenance=fetched.batches,
    )


def map_ko_identifiers(
    request: LinkRequest,
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
    preview_limit: int = 100,
) -> KoMappingServiceResult:
    """Map selected K numbers to one explicitly approved KEGG relationship."""
    if request.relationship is KeggLinkRelationship.PATHWAY_TO_KO:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "map_ko_ids accepts K numbers as sources and does not expose pathway-to-KO expansion.",
            suggested_action="Choose a KO-to-pathway, module, reaction, enzyme, or BRITE target.",
        )
    if not 0 <= preview_limit <= MAX_MAPPING_PREVIEW_ROWS:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The mapping preview limit is outside the MCP service bound.",
            suggested_action=f"Choose preview_limit between 0 and {MAX_MAPPING_PREVIEW_ROWS}.",
        )
    mapped = client.link(request, options=options)
    pathway_rows = (
        tuple(_pathway_mapping_row(row) for row in mapped.rows)
        if request.relationship is KeggLinkRelationship.KO_TO_PATHWAY
        else ()
    )
    payload = (
        _json_bytes(
            {
                "relationship": request.relationship.value,
                "rows": [row.model_dump(mode="json") for row in pathway_rows],
                "provenance": [batch.model_dump(mode="json") for batch in mapped.batches],
            }
        )
        if pathway_rows
        else mapped.model_dump_json().encode("utf-8")
    )
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    rows = (pathway_rows or mapped.rows)[:preview_limit]
    pathway_numbers = {row.pathway_number for row in pathway_rows}
    return KoMappingServiceResult(
        result=stored,
        artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
        relationship=request.relationship,
        source_identifier_count=len(request.source_identifiers),
        row_count=len(mapped.rows),
        raw_relationship_row_count=len(mapped.rows),
        unique_reference_pathway_number_count=len(pathway_numbers),
        available_ko_reference_view_count=len(pathway_numbers),
        available_map_reference_view_count=len(pathway_numbers),
        row_preview=tuple(rows),
        preview_truncated=len(rows) < len(mapped.rows),
        provenance=tuple(mapped.batches),
    )


def _pathway_mapping_row(row: KeggPairRow) -> PathwayMappingRow:
    source_value = row.source_id.rsplit(":", 1)[-1]
    target_value = row.target_id.rsplit(":", 1)[-1]
    ko_id, _ = try_normalize_ko_id(source_value)
    if ko_id is None or not is_kegg_pathway_identifier(target_value):
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "A KO-to-pathway relationship row has incompatible identifiers.",
            suggested_action="Refresh the typed KEGG LINK response and retry.",
        )
    prefix = target_value[:-5]
    if prefix == "ko":
        namespace: Literal["ko", "map"] = "ko"
    elif prefix == "map":
        namespace = "map"
    else:
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "A KO-to-pathway relationship row uses an unsupported namespace.",
            suggested_action="Use reference ko/map pathway relationships for KO evidence.",
        )
    paired_prefix = "map" if namespace == "ko" else "ko"
    return PathwayMappingRow(
        source_ko_id=ko_id,
        target_id=target_value,
        pathway_number=target_value[-5:],
        namespace=namespace,
        paired_reference_id=f"{paired_prefix}{target_value[-5:]}",
    )


__all__ = ["map_ko_identifiers", "read_cached_kegg_entry", "retrieve_kegg_entries"]
