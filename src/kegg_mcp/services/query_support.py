"""Private shared support for bounded KEGG query services."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, NoReturn

from pydantic import BaseModel

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg import (
    GetRequest,
    KeggEntryRef,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggFlatFileField,
)
from kegg_mcp.services.query_models import (
    MAX_QUERY_PROVENANCE_BATCHES,
    MAX_QUERY_RELEASE_PREVIEW,
    KeggEntityKind,
    KeggEntityRef,
    KeggRelationType,
    QueryRetrievalSummary,
    ResolutionOperation,
)
from kegg_mcp.services.reference_budget import KeggQueryClient
from kegg_mcp.services.result_builders import _json_bytes

MAX_QUERY_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_QUERY_DIRECT_BYTES = 64 * 1024
MAX_GENOME_GET_ENTRIES_PER_CALL = 10


@dataclass(frozen=True, slots=True)
class GenomeRecord:
    """Source-backed identity and taxonomy metadata from one KEGG GENOME entry."""

    t_number: str
    organism_code: str
    name: str | None
    taxonomy_lineage: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GenomeRecordLoad:
    """Bounded genome records with precise provenance indexes for every alias."""

    records: dict[str, GenomeRecord]
    batches: tuple[KeggBatchProvenance, ...]
    batch_index_by_alias: dict[str, int]
    step: dict[str, Any]


def load_genome_records(
    identifiers: tuple[str, ...],
    *,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    before_batch: Callable[[], None],
    record_batch: Callable[[int, tuple[KeggBatchProvenance, ...]], None],
) -> GenomeRecordLoad:
    """Load code/T aliases in endpoint-sized GET calls and record every batch immediately."""
    unique_identifiers = tuple(dict.fromkeys(identifiers))
    if not unique_identifiers:
        return GenomeRecordLoad(
            records={},
            batches=(),
            batch_index_by_alias={},
            step={
                "operation": ResolutionOperation.GET.value,
                "database": "genome",
                "identifiers": [],
                "results": [],
            },
        )

    records: dict[str, GenomeRecord] = {}
    batches: list[KeggBatchProvenance] = []
    batch_index_by_alias: dict[str, int] = {}
    retained_results: list[dict[str, Any]] = []
    batch_size = min(
        MAX_GENOME_GET_ENTRIES_PER_CALL,
        client.config.limits.max_identifiers,
    )
    for start in range(0, len(unique_identifiers), batch_size):
        chunk = unique_identifiers[start : start + batch_size]
        before_batch()
        fetched = client.get(
            GetRequest(
                entries=tuple(
                    KeggEntryRef(
                        database=KeggGetDatabase.GENOME,
                        identifier=identifier,
                    )
                    for identifier in chunk
                )
            ),
            options=options,
        )
        if len(fetched.documents) != len(fetched.batches):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A bounded KEGG GENOME call returned an ambiguous document-to-batch mapping.",
                suggested_action="Retry with the typed KEGG client and unchanged request limits.",
            )
        if any(not isinstance(document, KeggFlatFileDocument) for document in fetched.documents):
            fail_unexpected_genome_document()
        returned_entry_count = sum(
            len(document.entries)
            for document in fetched.documents
            if isinstance(document, KeggFlatFileDocument)
        )
        record_batch(returned_entry_count, fetched.batches)
        batch_offset = len(batches)
        batches.extend(fetched.batches)
        retained_results.append(fetched.model_dump(mode="json"))
        for local_batch_index, document in enumerate(fetched.documents):
            if not isinstance(document, KeggFlatFileDocument):
                raise AssertionError("GENOME documents were narrowed before record parsing")
            batch_index = batch_offset + local_batch_index
            for entry in document.entries:
                record = _genome_record(entry.identifier, entry.fields)
                records[record.t_number] = record
                records[record.organism_code] = record
                batch_index_by_alias[record.t_number] = batch_index
                batch_index_by_alias[record.organism_code] = batch_index
    return GenomeRecordLoad(
        records=records,
        batches=tuple(batches),
        batch_index_by_alias=batch_index_by_alias,
        step={
            "operation": ResolutionOperation.GET.value,
            "database": "genome",
            "identifiers": list(unique_identifiers),
            "results": retained_results,
        },
    )


def _genome_record(
    identifier: str,
    fields: tuple[KeggFlatFileField, ...],
) -> GenomeRecord:
    t_number = KeggEntityRef(
        kind=KeggEntityKind.GENOME,
        identifier=identifier,
    ).identifier
    organism_code: str | None = None
    name: str | None = None
    taxonomy_lineage: tuple[str, ...] = ()
    for field in fields:
        field_text = " ".join(field.value_lines).strip()
        if field.name == "ORG_CODE":
            value = field_text.split(maxsplit=1)[0]
            organism_code = KeggEntityRef(
                kind=KeggEntityKind.ORGANISM,
                identifier=value,
            ).identifier
        elif field.name == "NAME":
            name = field_text
        elif field.name == "LINEAGE":
            taxonomy_lineage = tuple(
                label.strip() for label in field_text.split(";") if label.strip()
            )
    if organism_code is None:
        fail_unexpected_genome_document()
    return GenomeRecord(
        t_number=t_number,
        organism_code=organism_code,
        name=name,
        taxonomy_lineage=taxonomy_lineage,
    )


def pair_entity(kind: KeggEntityKind, identifier: str) -> KeggEntityRef:
    """Normalize one typed LINK/FIND identifier to the public entity representation."""
    value = identifier
    if kind is KeggEntityKind.GENE:
        prefixes: tuple[str, ...] = ()
    elif kind is KeggEntityKind.KO:
        prefixes = ("ko:",)
    elif kind is KeggEntityKind.PATHWAY:
        prefixes = ("path:", "pathway:")
    elif kind is KeggEntityKind.MODULE:
        prefixes = ("md:", "module:")
    elif kind is KeggEntityKind.REACTION:
        prefixes = ("rn:", "reaction:")
    elif kind is KeggEntityKind.ENZYME:
        prefixes = ("ec:", "enzyme:")
    elif kind is KeggEntityKind.COMPOUND:
        prefixes = ("cpd:", "compound:")
    elif kind is KeggEntityKind.BRITE:
        prefixes = ("br:", "brite:")
    elif kind is KeggEntityKind.GENOME:
        prefixes = ("gn:", "genome:")
    elif kind is KeggEntityKind.TAXONOMY:
        prefixes = ("taxid:", "taxonomy:", "tax:")
    else:
        prefixes = ("organism:", "gn:", "genome:")
    for prefix in prefixes:
        if value.startswith(prefix):
            suffix = value.removeprefix(prefix)
            value = f"taxid:{suffix}" if kind is KeggEntityKind.TAXONOMY else suffix
            break
    try:
        return KeggEntityRef(kind=kind, identifier=value)
    except ValueError:
        fail_unexpected_relation_row()


def genome_lookup_from_pair(identifier: str) -> KeggEntityRef:
    """Accept a typed genome LINK target in either code or T-number form."""
    value = identifier
    for prefix in ("gn:", "genome:"):
        if value.startswith(prefix):
            value = value.removeprefix(prefix)
            break
    for kind in (KeggEntityKind.GENOME, KeggEntityKind.ORGANISM):
        try:
            return KeggEntityRef(kind=kind, identifier=value)
        except ValueError:
            continue
    fail_unexpected_relation_row()


def deduplicate_entities(
    entities: Iterable[KeggEntityRef],
) -> tuple[KeggEntityRef, ...]:
    unique: dict[tuple[str, str], KeggEntityRef] = {}
    for entity in entities:
        unique.setdefault(entity_key(entity), entity)
    return tuple(unique.values())


def entity_key(entity: KeggEntityRef) -> tuple[str, str]:
    return entity.kind.value, entity.identifier


def link_relationship(relationship: KeggRelationType) -> KeggLinkRelationship:
    try:
        return KeggLinkRelationship(relationship.value)
    except ValueError:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The requested relation is not available in the typed KEGG client.",
            suggested_action="Use a relationship exposed by this installed Core version.",
        )


def require_provenance_bound(
    provenance: Iterable[KeggBatchProvenance],
) -> None:
    if len(tuple(provenance)) > MAX_QUERY_PROVENANCE_BATCHES:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Query provenance exceeded the fixed retained-result batch bound.",
            suggested_action="Request fewer identifiers or relationship types.",
        )


def summarize_query_retrieval(
    provenance: Iterable[KeggBatchProvenance],
) -> QueryRetrievalSummary:
    """Return compact retrieval accounting while retaining full provenance elsewhere."""
    batches = tuple(provenance)
    require_provenance_bound(batches)
    database_releases = tuple(
        sorted({batch.database_release for batch in batches if batch.database_release})
    )
    return QueryRetrievalSummary(
        batch_count=len(batches),
        network_request_count=sum(
            batch.attempt_count for batch in batches if batch.origin is ResponseOrigin.NETWORK
        ),
        cache_hit_count=sum(batch.origin is ResponseOrigin.CACHE for batch in batches),
        stale_batch_count=sum(batch.is_stale for batch in batches),
        response_bytes=sum(batch.response_bytes for batch in batches),
        database_release_count=len(database_releases),
        database_releases=database_releases[:MAX_QUERY_RELEASE_PREVIEW],
        database_releases_truncated=len(database_releases) > MAX_QUERY_RELEASE_PREVIEW,
    )


def bounded_query_payload(value: object) -> bytes:
    payload = _json_bytes(value)
    if len(payload) > MAX_QUERY_ARTIFACT_BYTES:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "The retained query artifact exceeded its fixed output-size bound.",
            suggested_action="Request fewer identifiers, candidates, or relationship types.",
            safe_details=(
                SafeDetail(name="observed_bytes", value=str(len(payload))),
                SafeDetail(name="limit_bytes", value=str(MAX_QUERY_ARTIFACT_BYTES)),
            ),
        )
    return payload


def require_bounded_query_direct_result(value: BaseModel) -> None:
    """Fail closed when a supposedly compact direct projection exceeds 64 KiB."""
    payload = _json_bytes(value.model_dump(mode="json"))
    if len(payload) > MAX_QUERY_DIRECT_BYTES:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "The direct query projection exceeded its fixed output-size bound.",
            suggested_action="Use the retained resource for complete query details.",
            safe_details=(
                SafeDetail(name="observed_bytes", value=str(len(payload))),
                SafeDetail(name="limit_bytes", value=str(MAX_QUERY_DIRECT_BYTES)),
            ),
        )


def fail_unexpected_relation_row() -> NoReturn:
    fail(
        ErrorCode.KEGG_PARSE_FAILED,
        "A typed KEGG query returned an incompatible relationship row.",
        suggested_action="Refresh the bounded typed request and retry.",
    )


def fail_unexpected_genome_document() -> NoReturn:
    fail(
        ErrorCode.KEGG_PARSE_FAILED,
        "A typed KEGG GENOME response lacked one canonical ENTRY or ORG_CODE field.",
        suggested_action="Refresh the bounded typed GENOME request and retry.",
    )


__all__ = [
    "MAX_QUERY_ARTIFACT_BYTES",
    "MAX_QUERY_DIRECT_BYTES",
    "GenomeRecord",
    "GenomeRecordLoad",
    "bounded_query_payload",
    "deduplicate_entities",
    "entity_key",
    "fail_unexpected_genome_document",
    "fail_unexpected_relation_row",
    "genome_lookup_from_pair",
    "link_relationship",
    "load_genome_records",
    "pair_entity",
    "require_bounded_query_direct_result",
    "require_provenance_bound",
    "summarize_query_retrieval",
]
