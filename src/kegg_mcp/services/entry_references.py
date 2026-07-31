"""Deterministic PubMed-reference projections from selected KEGG flat files.

The projection reports only identifiers explicitly present in KEGG ``REFERENCE``
fields. It does not retrieve papers, summarize literature, or treat a citation as
evidence for a causal or mechanistic claim.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from kegg_mcp.domain.annotations import FrozenModel
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import GetResult, KeggEntryRef, KeggGetDatabase
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION as KEGG_RESPONSE_PARSER_VERSION,
)
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggFlatFileDocument
from kegg_mcp.kegg.operations import get_entry_matches
from kegg_mcp.services.entry_cards import MAX_ENTRY_CARD_ITEMS, pubmed_ids_from_fields

ENTRY_REFERENCE_SCHEMA_VERSION = "1"
ENTRY_REFERENCE_PARSER_NAME = "kegg_flat_file_pubmed_references"
ENTRY_REFERENCE_PARSER_VERSION = "1"
ENTRY_REFERENCE_SNAPSHOT_SECTION = "literature_references"
MAX_ENTRY_REFERENCE_PREVIEWS = 10
MAX_PUBMED_REFERENCE_PREVIEW = 10

PubMedIdentifier = Annotated[str, Field(pattern=r"^[0-9]+$", max_length=20)]


class KeggEntryLiteratureReferences(FrozenModel):
    """PubMed identifiers explicitly listed on one returned KEGG entry."""

    entity: KeggEntryRef
    pubmed_ids: Annotated[
        tuple[PubMedIdentifier, ...],
        Field(max_length=MAX_ENTRY_CARD_ITEMS),
    ] = ()


class KeggEntryLiteratureReferenceSnapshot(FrozenModel):
    """Complete current-scope reference projection for one bounded GET request."""

    schema_version: Literal["1"] = ENTRY_REFERENCE_SCHEMA_VERSION
    parser_name: Literal["kegg_flat_file_pubmed_references"] = ENTRY_REFERENCE_PARSER_NAME
    parser_version: Literal["1"] = ENTRY_REFERENCE_PARSER_VERSION
    response_parser_version: str = Field(
        default=KEGG_RESPONSE_PARSER_VERSION,
        pattern=r"^[0-9]+(?:\.[0-9]+)*$",
        max_length=32,
    )
    requested_entries: Annotated[tuple[KeggEntryRef, ...], Field(min_length=1, max_length=50)]
    entries: Annotated[
        tuple[KeggEntryLiteratureReferences, ...],
        Field(max_length=50),
    ]
    missing_entries: Annotated[tuple[KeggEntryRef, ...], Field(max_length=50)]
    provenance: Annotated[tuple[KeggBatchProvenance, ...], Field(min_length=1, max_length=50)]

    @model_validator(mode="after")
    def validate_request_partition(self) -> KeggEntryLiteratureReferenceSnapshot:
        requested = tuple(
            (entry.database, entry.identifier, entry.brite_kind) for entry in self.requested_entries
        )
        returned = tuple(
            (item.entity.database, item.entity.identifier, item.entity.brite_kind)
            for item in self.entries
        )
        missing = tuple(
            (entry.database, entry.identifier, entry.brite_kind) for entry in self.missing_entries
        )
        if len(requested) != len(set(requested)):
            raise ValueError("requested literature-reference entries must be unique")
        if len(returned) != len(set(returned)) or len(missing) != len(set(missing)):
            raise ValueError("returned and missing literature-reference entries must be unique")
        if set(returned) & set(missing) or set(returned) | set(missing) != set(requested):
            raise ValueError("returned and missing entries must partition the request")
        return self


class KeggEntryLiteratureReferencePreview(FrozenModel):
    """One compact citation preview for direct MCP output."""

    entity: KeggEntryRef
    pubmed_id_count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARD_ITEMS)
    pubmed_ids: Annotated[
        tuple[PubMedIdentifier, ...],
        Field(max_length=MAX_PUBMED_REFERENCE_PREVIEW),
    ]
    pubmed_ids_truncated: bool

    @model_validator(mode="after")
    def validate_preview(self) -> KeggEntryLiteratureReferencePreview:
        if self.pubmed_id_count < len(self.pubmed_ids):
            raise ValueError("pubmed_id_count cannot be smaller than its preview")
        if self.pubmed_ids_truncated != (self.pubmed_id_count > len(self.pubmed_ids)):
            raise ValueError("pubmed_ids_truncated must match the PMID preview")
        return self


class KeggEntryLiteratureReferencePreviewSet(FrozenModel):
    """Bounded direct projection of a complete retained reference snapshot."""

    entry_count: int = Field(strict=True, ge=0, le=50)
    referenced_entry_count: int = Field(strict=True, ge=0, le=50)
    pubmed_id_count: int = Field(strict=True, ge=0, le=50 * MAX_ENTRY_CARD_ITEMS)
    previews: Annotated[
        tuple[KeggEntryLiteratureReferencePreview, ...],
        Field(max_length=MAX_ENTRY_REFERENCE_PREVIEWS),
    ]
    previews_truncated: bool

    @model_validator(mode="after")
    def validate_counts(self) -> KeggEntryLiteratureReferencePreviewSet:
        if self.referenced_entry_count > self.entry_count:
            raise ValueError("referenced_entry_count cannot exceed entry_count")
        if self.entry_count < len(self.previews):
            raise ValueError("entry_count cannot be smaller than previews")
        if self.previews_truncated != (self.entry_count > len(self.previews)):
            raise ValueError("previews_truncated must match the entry preview")
        return self


def build_entry_literature_references(
    result: GetResult,
) -> KeggEntryLiteratureReferenceSnapshot:
    """Project KEGG-supplied PubMed identifiers without another network request."""
    if any(item.database is KeggGetDatabase.BRITE for item in result.request.entries):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The literature-reference projection does not support BRITE htext documents.",
            suggested_action="Use preview projection for BRITE entries.",
        )

    returned: dict[tuple[KeggGetDatabase, str], KeggEntryLiteratureReferences] = {}
    for document in result.documents:
        if not isinstance(document, KeggFlatFileDocument):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The literature-reference projection requires KEGG flat-file documents.",
                suggested_action="Use preview projection for non-flat-file content.",
            )
        for entry in document.entries:
            matches = tuple(
                requested
                for requested in result.request.entries
                if get_entry_matches(requested, entry)
            )
            if len(matches) != 1:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "The GET result contains an unexpected literature-reference entry.",
                    suggested_action="Refresh the exact database-qualified entries and retry.",
                )
            requested = matches[0]
            key = (requested.database, requested.identifier)
            if key in returned:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "The GET result contains a duplicate literature-reference entry.",
                    suggested_action="Refresh the exact database-qualified entries and retry.",
                )
            returned[key] = KeggEntryLiteratureReferences(
                entity=requested,
                pubmed_ids=pubmed_ids_from_fields(entry.fields),
            )

    missing_keys = {(item.database, item.identifier) for item in result.missing_entries}
    entries: list[KeggEntryLiteratureReferences] = []
    missing: list[KeggEntryRef] = []
    for requested in result.request.entries:
        key = (requested.database, requested.identifier)
        item = returned.get(key)
        if item is not None:
            entries.append(item)
        elif key in missing_keys:
            missing.append(requested)
        else:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The GET result did not account for every literature-reference request.",
                suggested_action="Refresh the exact database-qualified entries and retry.",
            )
    if len(missing) != len(missing_keys):
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "The GET result reported an unexpected missing literature-reference entry.",
            suggested_action="Refresh the exact database-qualified entries and retry.",
        )
    return KeggEntryLiteratureReferenceSnapshot(
        requested_entries=result.request.entries,
        entries=tuple(entries),
        missing_entries=tuple(missing),
        provenance=result.batches,
    )


def entry_literature_reference_previews(
    snapshot: KeggEntryLiteratureReferenceSnapshot,
    *,
    limit: int = MAX_ENTRY_REFERENCE_PREVIEWS,
) -> KeggEntryLiteratureReferencePreviewSet:
    """Build the compact direct preview for a retained citation projection."""
    if not 0 <= limit <= MAX_ENTRY_REFERENCE_PREVIEWS:
        raise ValueError(f"limit must be between zero and {MAX_ENTRY_REFERENCE_PREVIEWS}")
    previews = tuple(
        KeggEntryLiteratureReferencePreview(
            entity=item.entity,
            pubmed_id_count=len(item.pubmed_ids),
            pubmed_ids=item.pubmed_ids[:MAX_PUBMED_REFERENCE_PREVIEW],
            pubmed_ids_truncated=len(item.pubmed_ids) > MAX_PUBMED_REFERENCE_PREVIEW,
        )
        for item in snapshot.entries[:limit]
    )
    all_ids = {pubmed_id for item in snapshot.entries for pubmed_id in item.pubmed_ids}
    return KeggEntryLiteratureReferencePreviewSet(
        entry_count=len(snapshot.entries),
        referenced_entry_count=sum(bool(item.pubmed_ids) for item in snapshot.entries),
        pubmed_id_count=len(all_ids),
        previews=previews,
        previews_truncated=len(previews) < len(snapshot.entries),
    )


__all__ = [
    "ENTRY_REFERENCE_PARSER_NAME",
    "ENTRY_REFERENCE_PARSER_VERSION",
    "ENTRY_REFERENCE_SCHEMA_VERSION",
    "ENTRY_REFERENCE_SNAPSHOT_SECTION",
    "KeggEntryLiteratureReferencePreview",
    "KeggEntryLiteratureReferencePreviewSet",
    "KeggEntryLiteratureReferenceSnapshot",
    "KeggEntryLiteratureReferences",
    "build_entry_literature_references",
    "entry_literature_reference_previews",
]
