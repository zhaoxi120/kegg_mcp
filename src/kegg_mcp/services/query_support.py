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
    KeggTaxonomyRank,
    ResponseOrigin,
)
from kegg_mcp.kegg.contracts import (
    MAX_GET_ENTRIES_PER_BATCH,
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggFlatFileField,
)
from kegg_mcp.services.kegg_relations import (
    BoundedRelationResult,
    bounded_relation_batches,
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


@dataclass(slots=True)
class QueryBudget:
    """One shared aggregate request, row, and response-byte budget."""

    request_limit: int
    row_limit: int
    response_byte_limit: int
    error_message: str
    suggested_action: str
    row_limit_name: str
    request_capacity_first: bool = True
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

    def require_request_capacity(self) -> None:
        if self.remaining_requests <= 0:
            self._limit("kegg_request_count", self.requests + 1, self.request_limit)

    def require_relation_capacity(self) -> None:
        if self.request_capacity_first:
            self.require_request_capacity()
        if self.remaining_rows <= 0:
            self._limit(self.row_limit_name, self.rows + 1, self.row_limit)
        if self.remaining_response_bytes <= 0:
            self._limit(
                "kegg_response_bytes",
                self.response_bytes + 1,
                self.response_byte_limit,
            )
        if not self.request_capacity_first:
            self.require_request_capacity()

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
            self._limit("kegg_request_count", next_requests, self.request_limit)
        if next_rows > self.row_limit:
            self._limit(self.row_limit_name, next_rows, self.row_limit)
        if next_response_bytes > self.response_byte_limit:
            self._limit(
                "kegg_response_bytes",
                next_response_bytes,
                self.response_byte_limit,
            )
        self.requests = next_requests
        self.rows = next_rows
        self.response_bytes = next_response_bytes

    def _limit(self, name: str, observed: int, limit: int) -> NoReturn:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            self.error_message,
            suggested_action=self.suggested_action,
            safe_details=(
                SafeDetail(name="limit_name", value=name),
                SafeDetail(name="observed", value=str(observed)),
                SafeDetail(name="limit", value=str(limit)),
            ),
        )


def bounded_query_relation(
    source_identifiers: tuple[str, ...],
    *,
    relationship: KeggLinkRelationship,
    client: KeggQueryClient,
    options: KeggRequestOptions | None,
    budget: QueryBudget,
    taxonomy_rank: KeggTaxonomyRank = KeggTaxonomyRank.EXACT,
) -> BoundedRelationResult:
    """Run one LINK relation against the remaining aggregate query budget."""
    budget.require_relation_capacity()
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
        MAX_GET_ENTRIES_PER_BATCH,
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
            suggested_action=(
                "Request a smaller projection and retry; use the retained resource from a "
                "successful response for complete query details."
            ),
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
    "QueryBudget",
    "bounded_query_payload",
    "bounded_query_relation",
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
