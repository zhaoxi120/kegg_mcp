"""Durable, bounded bundles of selected current-scope KEGG entry cards."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from kegg_mcp import __version__
from kegg_mcp._serialization import escape_spreadsheet_formula
from kegg_mcp.domain.annotations import FrozenModel
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import (
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services._atomic_bundle import write_text_bundle
from kegg_mcp.services._text_artifact import TextArtifactSpec
from kegg_mcp.services.brite_hierarchy import (
    BRITE_DETAIL_MIME_TYPE,
    BRITE_DETAIL_SECTION,
    MAX_BRITE_ARTIFACT_BYTES,
    MAX_BRITE_ENTITY_IDS,
    MAX_BRITE_PATH_DEPTH,
    MAX_BRITE_PATHS,
    BriteHierarchyDetail,
    BriteHierarchyNode,
)
from kegg_mcp.services.entry_cards import (
    MAX_ENTRY_CARDS,
    CompoundEntryCard,
    EnzymeEntryCard,
    GeneEntryCard,
    GenomeEntryCard,
    GlycanEntryCard,
    KeggEntryCard,
    KeggEntryCardEntity,
    KeggEntryCardKind,
    KeggEntryCardReference,
    KeggEntryCardSnapshot,
    KoEntryCard,
    ModuleEntryCard,
    PathwayEntryCard,
    ReactionEntryCard,
)
from kegg_mcp.services.entry_snapshot_io import read_entry_card_snapshot
from kegg_mcp.services.query_models import MAX_QUERY_PROVENANCE_BATCHES
from kegg_mcp.services.result_store import RESULT_ID_SCHEMA_PATTERN, SQLiteResultStore

REFERENCE_BUNDLE_SCHEMA_VERSION = "1"
REFERENCE_SNAPSHOT_SCHEMA_VERSION = "1"
REFERENCE_MANIFEST_NAME = "reference_manifest.json"
REFERENCE_SNAPSHOT_NAME = "reference_snapshot.json"
REFERENCE_RELATIONSHIPS_NAME = "relationships.tsv"
REFERENCE_BRITE_PATHS_NAME = "brite_paths.tsv"
MAX_REFERENCE_RELATIONSHIPS = 50_000
MAX_REFERENCE_BRITE_ROWS = MAX_BRITE_PATHS * MAX_BRITE_PATH_DEPTH + MAX_BRITE_ENTITY_IDS
MAX_REFERENCE_PROVENANCE_BATCHES = MAX_ENTRY_CARDS + 2 * MAX_QUERY_PROVENANCE_BATCHES
MAX_REFERENCE_BUNDLE_ARTIFACT_BYTES = 16 * 1024 * 1024
MAX_REFERENCE_BUNDLE_BYTES = 32 * 1024 * 1024

_JSON_MIME_TYPE = "application/json"
_TSV_MIME_TYPE = "text/tab-separated-values; charset=utf-8"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_BUNDLE_FILE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class ReferenceBundleSource(FrozenModel):
    """One retained current-scope result selected as input to a durable bundle."""

    result_id: str = Field(pattern=RESULT_ID_SCHEMA_PATTERN)


class WriteKeggReferenceBundleRequest(FrozenModel):
    """Select a bounded subset of one retained entry-card snapshot."""

    source: ReferenceBundleSource
    brite_source: ReferenceBundleSource | None = None
    entries: Annotated[
        tuple[KeggEntryCardEntity, ...] | None,
        Field(min_length=1, max_length=MAX_ENTRY_CARDS),
    ] = None

    @field_validator("entries")
    @classmethod
    def require_unique_entries(
        cls,
        value: tuple[KeggEntryCardEntity, ...] | None,
    ) -> tuple[KeggEntryCardEntity, ...] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("reference bundle entries must be unique")
        return value


class ReferenceBundleFileRecord(FrozenModel):
    """Portable integrity metadata without a local filesystem path."""

    name: str = Field(pattern=_BUNDLE_FILE_PATTERN)
    mime_type: str = Field(min_length=1, max_length=100)
    byte_size: int = Field(strict=True, ge=0, le=MAX_REFERENCE_BUNDLE_ARTIFACT_BYTES)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class ReferenceBundleArtifact(ReferenceBundleFileRecord):
    """One locally written bundle artifact."""

    path: str = Field(min_length=1, max_length=4_096)


class ReferenceBundleProducer(FrozenModel):
    name: Literal["kegg-mcp"] = "kegg-mcp"
    version: str = Field(min_length=1, max_length=100)


class ReferenceBundleSelectionSummary(FrozenModel):
    requested_entry_count: int = Field(strict=True, ge=1, le=MAX_ENTRY_CARDS)
    returned_entry_count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARDS)
    missing_entry_count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARDS)
    relationship_count: int = Field(strict=True, ge=0, le=MAX_REFERENCE_RELATIONSHIPS)
    entity_types: Annotated[
        tuple[KeggEntryCardKind, ...],
        Field(min_length=1, max_length=len(KeggEntryCardKind)),
    ]
    source_requested_entry_count: int = Field(strict=True, ge=1, le=MAX_ENTRY_CARDS)
    truncated: Literal[False] = False

    @model_validator(mode="after")
    def validate_entry_accounting(self) -> Self:
        if self.returned_entry_count + self.missing_entry_count != self.requested_entry_count:
            raise ValueError("selected returned and missing entries must cover the request")
        if self.requested_entry_count > self.source_requested_entry_count:
            raise ValueError("selection cannot exceed the source snapshot")
        return self


class ReferenceBundleBriteStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    COMPLETED = "completed"


class ReferenceBundleBriteSummary(FrozenModel):
    status: ReferenceBundleBriteStatus
    path_count: int = Field(strict=True, ge=0, le=MAX_BRITE_PATHS)
    row_count: int = Field(strict=True, ge=0, le=MAX_REFERENCE_BRITE_ROWS)
    unmatched_entity_count: int = Field(strict=True, ge=0, le=MAX_BRITE_ENTITY_IDS)
    selected_brite_count: int = Field(strict=True, ge=0)
    resolved_brite_count: int = Field(strict=True, ge=0)
    missing_brite_count: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_status(self) -> Self:
        counts = (
            self.path_count,
            self.row_count,
            self.unmatched_entity_count,
            self.selected_brite_count,
            self.resolved_brite_count,
            self.missing_brite_count,
        )
        if self.status is ReferenceBundleBriteStatus.NOT_REQUESTED and any(counts):
            raise ValueError("a BRITE source is required for nonzero BRITE counts")
        if self.resolved_brite_count + self.missing_brite_count != self.selected_brite_count:
            raise ValueError("resolved and missing BRITE counts must cover the selection")
        return self


class ReferenceBundleRetrievalSummary(FrozenModel):
    batch_count: int = Field(
        strict=True,
        ge=1,
        le=MAX_REFERENCE_PROVENANCE_BATCHES,
    )
    endpoint_classes: Annotated[
        tuple[RetrievalEndpointClass, ...],
        Field(min_length=1, max_length=len(RetrievalEndpointClass)),
    ]
    origins: Annotated[
        tuple[ResponseOrigin, ...],
        Field(min_length=1, max_length=len(ResponseOrigin)),
    ]
    cache_states: Annotated[
        tuple[CacheLookupState, ...],
        Field(min_length=1, max_length=len(CacheLookupState)),
    ]
    database_releases: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(max_length=MAX_REFERENCE_PROVENANCE_BATCHES),
    ]
    release_unavailable_batch_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_REFERENCE_PROVENANCE_BATCHES,
    )
    stale_batch_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_REFERENCE_PROVENANCE_BATCHES,
    )
    network_attempt_count: int = Field(strict=True, ge=0)
    response_bytes: int = Field(strict=True, ge=0)
    earliest_retrieved_at: datetime
    latest_retrieved_at: datetime

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.release_unavailable_batch_count > self.batch_count:
            raise ValueError("missing release count cannot exceed retrieval batches")
        if self.stale_batch_count > self.batch_count:
            raise ValueError("stale batch count cannot exceed retrieval batches")
        if self.latest_retrieved_at < self.earliest_retrieved_at:
            raise ValueError("latest retrieval cannot precede earliest retrieval")
        return self


class ReferenceBundleManifest(FrozenModel):
    """Committed portable manifest; local paths and endpoint labels are excluded."""

    schema_version: Literal["1"] = REFERENCE_BUNDLE_SCHEMA_VERSION
    bundle_type: Literal["kegg_reference"] = "kegg_reference"
    producer: ReferenceBundleProducer
    selection: ReferenceBundleSelectionSummary
    brite: ReferenceBundleBriteSummary
    retrieval: ReferenceBundleRetrievalSummary
    artifacts: Annotated[
        tuple[ReferenceBundleFileRecord, ...],
        Field(min_length=2, max_length=3),
    ]

    @model_validator(mode="after")
    def validate_artifacts(self) -> Self:
        expected = [
            REFERENCE_SNAPSHOT_NAME,
            REFERENCE_RELATIONSHIPS_NAME,
        ]
        if self.brite.status is ReferenceBundleBriteStatus.COMPLETED:
            expected.append(REFERENCE_BRITE_PATHS_NAME)
        if tuple(item.name for item in self.artifacts) != tuple(expected):
            raise ValueError(
                "reference manifest artifacts must match the selected stable bundle shape"
            )
        return self


class ReferenceBundleRetrievalBatch(FrozenModel):
    """Sanitized retrieval evidence without request keys, labels, headers, or URLs."""

    batch_index: int = Field(
        strict=True,
        ge=0,
        le=MAX_REFERENCE_PROVENANCE_BATCHES - 1,
    )
    operation: KeggOperation
    access_mode: AccessMode
    retrieval_endpoint_class: RetrievalEndpointClass
    origin: ResponseOrigin
    cache_lookup_state: CacheLookupState
    retrieved_at: datetime
    served_at: datetime
    expires_at: datetime
    response_bytes: int = Field(strict=True, ge=0)
    parser_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    parser_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)
    database_release: str | None = Field(default=None, max_length=256)
    attempt_count: int = Field(strict=True, ge=0)
    is_stale: bool


class KeggReferenceBundle(FrozenModel):
    """Paths and compact counts for one committed durable reference bundle."""

    schema_version: Literal["1"] = REFERENCE_BUNDLE_SCHEMA_VERSION
    output_directory: str = Field(min_length=1, max_length=4_096)
    manifest: str = Field(min_length=1, max_length=4_096)
    requested_entry_count: int = Field(strict=True, ge=1, le=MAX_ENTRY_CARDS)
    returned_entry_count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARDS)
    missing_entry_count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARDS)
    relationship_count: int = Field(strict=True, ge=0, le=MAX_REFERENCE_RELATIONSHIPS)
    total_bytes: int = Field(strict=True, ge=1, le=MAX_REFERENCE_BUNDLE_BYTES)
    artifacts: Annotated[
        tuple[ReferenceBundleArtifact, ...],
        Field(min_length=3, max_length=4),
    ]

    @model_validator(mode="after")
    def validate_artifacts(self) -> Self:
        names = tuple(item.name for item in self.artifacts)
        if names[:2] != (REFERENCE_SNAPSHOT_NAME, REFERENCE_RELATIONSHIPS_NAME):
            raise ValueError("reference bundle must start with snapshot and relationships")
        if names[-1] != REFERENCE_MANIFEST_NAME:
            raise ValueError("reference manifest must be the final bundle artifact")
        if len(names) == 4 and names[2] != REFERENCE_BRITE_PATHS_NAME:
            raise ValueError("the optional third reference artifact must contain BRITE paths")
        if sum(item.byte_size for item in self.artifacts) != self.total_bytes:
            raise ValueError("reference bundle total must match artifact sizes")
        return self


class _ReferenceRelationship(FrozenModel):
    source_database: str = Field(min_length=1, max_length=128)
    source_identifier: str = Field(min_length=1, max_length=256)
    relationship: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    target_database: str = Field(min_length=1, max_length=128)
    target_identifier: str = Field(min_length=1, max_length=65_536)
    target_label: str | None = Field(default=None, max_length=65_536)


def write_kegg_reference_bundle(
    request: WriteKeggReferenceBundleRequest,
    *,
    output_directory: Path,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> KeggReferenceBundle:
    """Write selected typed cards from one successful current-scope result."""
    snapshot = read_entry_card_snapshot(
        result_store,
        scope_id,
        request.source.result_id,
    )
    selected_requests = request.entries or snapshot.requested_entries
    _require_selection_in_snapshot(selected_requests, snapshot)
    selected_entries = tuple(
        card
        for card in snapshot.entries
        if any(_card_matches_request(card, requested) for requested in selected_requests)
    )
    selected_missing = tuple(item for item in snapshot.missing_entries if item in selected_requests)
    _require_selected_accounting(
        selected_requests,
        selected_entries,
        selected_missing,
    )
    relationships = _relationships(selected_entries)
    brite_detail = (
        None
        if request.brite_source is None
        else _read_brite_detail(
            result_store,
            scope_id,
            request.brite_source.result_id,
        )
    )
    if brite_detail is not None:
        _require_brite_source_selection(brite_detail, selected_requests)
    brite_table, brite_summary = _brite_paths_tsv(brite_detail)
    selection = ReferenceBundleSelectionSummary(
        requested_entry_count=len(selected_requests),
        returned_entry_count=len(selected_entries),
        missing_entry_count=len(selected_missing),
        relationship_count=len(relationships),
        entity_types=tuple(
            kind
            for kind in KeggEntryCardKind
            if any(item.database is kind for item in selected_requests)
        ),
        source_requested_entry_count=len(snapshot.requested_entries),
    )
    brite_relation_provenance = () if brite_detail is None else brite_detail.relation_provenance
    brite_hierarchy_provenance = () if brite_detail is None else brite_detail.hierarchy_provenance
    retrieval = _retrieval_summary(
        (
            *snapshot.provenance,
            *brite_relation_provenance,
            *brite_hierarchy_provenance,
        )
    )
    payloads = _payload_files(
        snapshot,
        selected_requests=selected_requests,
        selected_entries=selected_entries,
        selected_missing=selected_missing,
        relationships=relationships,
        brite_detail=brite_detail,
        brite_table=brite_table,
    )
    payload_specs = tuple(
        TextArtifactSpec(name=name, mime_type=_mime_type(name), content=content)
        for name, content in payloads.items()
    )
    artifact_records = tuple(
        ReferenceBundleFileRecord.model_validate(spec.integrity_record()) for spec in payload_specs
    )
    manifest = ReferenceBundleManifest(
        producer=ReferenceBundleProducer(version=__version__),
        selection=selection,
        brite=brite_summary,
        retrieval=retrieval,
        artifacts=artifact_records,
    )
    manifest_spec = TextArtifactSpec(
        name=REFERENCE_MANIFEST_NAME,
        mime_type=_JSON_MIME_TYPE,
        content=_json_text(manifest.model_dump(mode="json")),
    )
    all_specs = (*payload_specs, manifest_spec)
    files = {spec.name: spec.content for spec in all_specs}
    write_text_bundle(
        output_directory,
        files,
        manifest_name=REFERENCE_MANIFEST_NAME,
        remove_created_directory_on_failure=True,
        max_artifact_bytes=MAX_REFERENCE_BUNDLE_ARTIFACT_BYTES,
        max_total_bytes=MAX_REFERENCE_BUNDLE_BYTES,
    )
    artifacts = tuple(
        ReferenceBundleArtifact(
            name=spec.name,
            mime_type=spec.mime_type,
            byte_size=spec.byte_size,
            sha256=spec.sha256,
            path=str(output_directory / spec.name),
        )
        for spec in all_specs
    )
    return KeggReferenceBundle(
        output_directory=str(output_directory),
        manifest=str(output_directory / REFERENCE_MANIFEST_NAME),
        requested_entry_count=selection.requested_entry_count,
        returned_entry_count=selection.returned_entry_count,
        missing_entry_count=selection.missing_entry_count,
        relationship_count=selection.relationship_count,
        total_bytes=sum(item.byte_size for item in artifacts),
        artifacts=artifacts,
    )


def _require_selection_in_snapshot(
    selected: tuple[KeggEntryCardEntity, ...],
    snapshot: KeggEntryCardSnapshot,
) -> None:
    unavailable = tuple(item for item in selected if item not in snapshot.requested_entries)
    if unavailable:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Reference bundle entries must come from the retained card request.",
            suggested_action="Select only entries requested by the source card snapshot.",
            safe_details=(
                SafeDetail(name="selected_entry_count", value=str(len(selected))),
                SafeDetail(name="unavailable_entry_count", value=str(len(unavailable))),
            ),
        )


def _require_selected_accounting(
    selected: tuple[KeggEntryCardEntity, ...],
    entries: tuple[KeggEntryCard, ...],
    missing: tuple[KeggEntryCardEntity, ...],
) -> None:
    accounted = len(entries) + len(missing)
    if accounted != len(selected):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The retained card snapshot cannot account for the selected entries unambiguously.",
            suggested_action="Create a new card snapshot for the exact selected entries.",
            safe_details=(
                SafeDetail(name="selected_entry_count", value=str(len(selected))),
                SafeDetail(name="accounted_entry_count", value=str(accounted)),
            ),
        )
    for requested in selected:
        matches = sum(_card_matches_request(card, requested) for card in entries)
        matches += requested in missing
        if matches != 1:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "The retained card snapshot contains ambiguous selected-entry accounting.",
                suggested_action="Create a new card snapshot for the exact selected entries.",
            )


def _card_matches_request(card: KeggEntryCard, request: KeggEntryCardEntity) -> bool:
    if card.entity == request:
        return True
    return (
        isinstance(card, GenomeEntryCard)
        and request.database is KeggEntryCardKind.GENOME
        and card.organism_code == request.identifier
    )


def _require_brite_source_selection(
    detail: BriteHierarchyDetail,
    selected: tuple[KeggEntryCardEntity, ...],
) -> None:
    selected_keys = {(item.database.value, item.identifier) for item in selected}
    source_keys = {(item.kind.value, item.identifier) for item in detail.request.entity_ids}
    unavailable = source_keys - selected_keys
    if unavailable:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "BRITE source entities must be an exact match or subset of the selected entries.",
            suggested_action=(
                "Create a BRITE result from only the entries selected for this reference bundle."
            ),
            safe_details=(
                SafeDetail(name="selected_entry_count", value=str(len(selected_keys))),
                SafeDetail(name="brite_source_entity_count", value=str(len(source_keys))),
                SafeDetail(name="unavailable_entity_count", value=str(len(unavailable))),
            ),
        )
    path_keys = {
        (path.input_entity.kind.value, path.input_entity.identifier) for path in detail.paths
    }
    unmatched_sequence = tuple(
        (item.kind.value, item.identifier) for item in detail.unmatched_entities
    )
    unmatched_keys = set(unmatched_sequence)
    path_identities = tuple(
        (
            path.input_entity.kind.value,
            path.input_entity.identifier,
            path.brite_id,
            tuple(_brite_node_key(node) for node in path.nodes),
        )
        for path in detail.paths
    )
    resolved_brite_ids = set(detail.resolved_brite_ids)
    invalid_entity_accounting = (
        not path_keys.issubset(source_keys)
        or not unmatched_keys.issubset(source_keys)
        or bool(path_keys & unmatched_keys)
        or len(unmatched_sequence) != len(unmatched_keys)
        or len(path_identities) != len(set(path_identities))
        or any(path.brite_id not in resolved_brite_ids for path in detail.paths)
        or (
            bool(detail.request.brite_ids) and detail.selected_brite_ids != detail.request.brite_ids
        )
        or (detail.request.include_unmatched and path_keys | unmatched_keys != source_keys)
    )
    if invalid_entity_accounting:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The retained BRITE detail is inconsistent with its source request.",
            suggested_action="Create a new BRITE hierarchy result and retry.",
        )

    entities_by_classification: dict[
        tuple[str, tuple[tuple[str, str | None, str], ...]],
        set[tuple[str, str]],
    ] = {}
    for path in detail.paths:
        entity_key = (path.input_entity.kind.value, path.input_entity.identifier)
        for length in range(1, len(path.nodes) + 1):
            key = (
                path.brite_id,
                tuple(_brite_node_key(node) for node in path.nodes[:length]),
            )
            entities_by_classification.setdefault(key, set()).add(entity_key)
    expected_classifications = {
        key: len(entities) for key, entities in entities_by_classification.items()
    }
    actual_classifications = {
        (
            item.brite_id,
            tuple(_brite_node_key(node) for node in item.path),
        ): item.unique_input_count
        for item in detail.classifications
    }
    if (
        len(actual_classifications) != len(detail.classifications)
        or actual_classifications != expected_classifications
    ):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The retained BRITE classifications do not match the retained hierarchy paths.",
            suggested_action="Create a new BRITE hierarchy result and retry.",
        )


def _read_brite_detail(
    result_store: SQLiteResultStore,
    scope_id: str,
    result_id: str,
) -> BriteHierarchyDetail:
    artifact_page = result_store.list_artifacts(
        scope_id,
        result_id,
        limit=result_store.limits.max_page_size,
    )
    metadata = next(
        (item for item in artifact_page.items if item.section == BRITE_DETAIL_SECTION),
        None,
    )
    if metadata is None:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The selected result does not contain retained BRITE hierarchy detail.",
            suggested_action="Use the result_id returned by map_brite_hierarchy.",
        )
    if metadata.mime_type != BRITE_DETAIL_MIME_TYPE:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The retained BRITE hierarchy detail has an incompatible media type.",
            suggested_action="Use the result_id returned by map_brite_hierarchy.",
        )
    if metadata.byte_size > MAX_BRITE_ARTIFACT_BYTES:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "The retained BRITE hierarchy detail exceeds its fixed artifact bound.",
            suggested_action="Create a BRITE result from fewer entities or hierarchies.",
            safe_details=(
                SafeDetail(name="observed_bytes", value=str(metadata.byte_size)),
                SafeDetail(name="limit_bytes", value=str(MAX_BRITE_ARTIFACT_BYTES)),
            ),
        )
    chunks: list[bytes] = []
    offset = 0
    while True:
        page = result_store.read_artifact(
            scope_id,
            result_id,
            BRITE_DETAIL_SECTION,
            offset=offset,
            limit=result_store.limits.max_range_bytes,
        )
        if page.mime_type != BRITE_DETAIL_MIME_TYPE or page.total_bytes != metadata.byte_size:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "The retained BRITE hierarchy detail changed during bounded loading.",
                suggested_action="Create a new BRITE hierarchy result and retry.",
            )
        chunks.append(page.content)
        if page.next_offset is None:
            break
        offset = page.next_offset
    try:
        return BriteHierarchyDetail.model_validate_json(b"".join(chunks))
    except ValueError:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The selected result does not contain compatible BRITE hierarchy detail.",
            suggested_action="Use the result_id returned by map_brite_hierarchy.",
            safe_details=(SafeDetail(name="required_brite_detail_schema_version", value="1"),),
        )


def _brite_paths_tsv(
    detail: BriteHierarchyDetail | None,
) -> tuple[str, ReferenceBundleBriteSummary]:
    target = io.StringIO(newline="")
    writer = csv.writer(target, delimiter="\t", lineterminator="\n")
    writer.writerow(
        (
            "record_type",
            "input_kind",
            "input_identifier",
            "brite_id",
            "path_index",
            "depth",
            "level",
            "node_id",
            "node_name",
            "unique_input_count",
        )
    )
    if detail is None:
        return (
            target.getvalue(),
            ReferenceBundleBriteSummary(
                status=ReferenceBundleBriteStatus.NOT_REQUESTED,
                path_count=0,
                row_count=0,
                unmatched_entity_count=0,
                selected_brite_count=0,
                resolved_brite_count=0,
                missing_brite_count=0,
            ),
        )
    classification_lookup = {
        (
            item.brite_id,
            tuple(_brite_node_key(node) for node in item.path),
        ): item.unique_input_count
        for item in detail.classifications
    }
    row_count = 0
    for path_index, path in enumerate(detail.paths, start=1):
        for length, node in enumerate(path.nodes, start=1):
            classification_key = (
                path.brite_id,
                tuple(_brite_node_key(item) for item in path.nodes[:length]),
            )
            unique_input_count = classification_lookup.get(classification_key)
            if unique_input_count is None:
                fail(
                    ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                    "The retained BRITE hierarchy classifications do not cover every path.",
                    suggested_action="Create a new BRITE hierarchy result and retry.",
                )
            writer.writerow(
                _safe_tsv_row(
                    (
                        "path_node",
                        path.input_entity.kind.value,
                        path.input_entity.identifier,
                        path.brite_id,
                        path_index,
                        node.depth,
                        node.level,
                        node.node_id or "",
                        node.name,
                        unique_input_count,
                    )
                )
            )
            row_count += 1
    for entity in detail.unmatched_entities:
        writer.writerow(
            _safe_tsv_row(
                (
                    "unmatched",
                    entity.kind.value,
                    entity.identifier,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                )
            )
        )
        row_count += 1
    if row_count > MAX_REFERENCE_BRITE_ROWS:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "The retained BRITE hierarchy exceeds the reference-bundle row bound.",
            suggested_action="Create a BRITE result from fewer entities or hierarchies.",
            safe_details=(
                SafeDetail(
                    name="brite_path_row_limit",
                    value=str(MAX_REFERENCE_BRITE_ROWS),
                ),
            ),
        )
    return (
        target.getvalue(),
        ReferenceBundleBriteSummary(
            status=ReferenceBundleBriteStatus.COMPLETED,
            path_count=len(detail.paths),
            row_count=row_count,
            unmatched_entity_count=len(detail.unmatched_entities),
            selected_brite_count=len(detail.selected_brite_ids),
            resolved_brite_count=len(detail.resolved_brite_ids),
            missing_brite_count=len(detail.missing_brite_ids),
        ),
    )


def _brite_node_key(node: BriteHierarchyNode) -> tuple[str, str | None, str]:
    return node.level, node.node_id, node.name


def _safe_tsv_row(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        escape_spreadsheet_formula("" if value is None else str(value)) for value in values
    )


def _payload_files(
    snapshot: KeggEntryCardSnapshot,
    *,
    selected_requests: tuple[KeggEntryCardEntity, ...],
    selected_entries: tuple[KeggEntryCard, ...],
    selected_missing: tuple[KeggEntryCardEntity, ...],
    relationships: tuple[_ReferenceRelationship, ...],
    brite_detail: BriteHierarchyDetail | None,
    brite_table: str,
) -> dict[str, str]:
    reference_snapshot = {
        "schema_version": REFERENCE_SNAPSHOT_SCHEMA_VERSION,
        "source_schema": {
            "card_schema_version": snapshot.schema_version,
            "card_parser_name": snapshot.parser_name,
            "card_parser_version": snapshot.parser_version,
            "response_parser_version": snapshot.response_parser_version,
        },
        "request": {
            "operation": KeggOperation.GET.value,
            "projection": "card",
            "entries": [entry.model_dump(mode="json") for entry in selected_requests],
        },
        "entries": [entry.model_dump(mode="json") for entry in selected_entries],
        "missing_entries": [entry.model_dump(mode="json") for entry in selected_missing],
        "brite": (
            None
            if brite_detail is None
            else {
                "schema_version": brite_detail.schema_version,
                "request": brite_detail.request.model_dump(mode="json"),
            }
        ),
        "retrieval": {
            "entry_batches": [
                _sanitized_batch(index, batch).model_dump(mode="json")
                for index, batch in enumerate(snapshot.provenance)
            ],
            "brite_relation_batches": [
                _sanitized_batch(index, batch).model_dump(mode="json")
                for index, batch in enumerate(
                    () if brite_detail is None else brite_detail.relation_provenance
                )
            ],
            "brite_hierarchy_batches": [
                _sanitized_batch(index, batch).model_dump(mode="json")
                for index, batch in enumerate(
                    () if brite_detail is None else brite_detail.hierarchy_provenance
                )
            ],
        },
    }
    payloads = {
        REFERENCE_SNAPSHOT_NAME: _json_text(reference_snapshot),
        REFERENCE_RELATIONSHIPS_NAME: _relationships_tsv(relationships),
    }
    if brite_detail is not None:
        payloads[REFERENCE_BRITE_PATHS_NAME] = brite_table
    return payloads


def _sanitized_batch(
    index: int,
    batch: KeggBatchProvenance,
) -> ReferenceBundleRetrievalBatch:
    return ReferenceBundleRetrievalBatch(
        batch_index=index,
        operation=batch.operation,
        access_mode=batch.access_mode,
        retrieval_endpoint_class=batch.retrieval_endpoint_class,
        origin=batch.origin,
        cache_lookup_state=batch.cache_lookup_state,
        retrieved_at=batch.retrieved_at,
        served_at=batch.served_at,
        expires_at=batch.expires_at,
        response_bytes=batch.response_bytes,
        parser_name=batch.parser_name,
        parser_version=batch.parser_version,
        database_release=batch.database_release,
        attempt_count=batch.attempt_count,
        is_stale=batch.is_stale,
    )


def _retrieval_summary(
    batches: tuple[KeggBatchProvenance, ...],
) -> ReferenceBundleRetrievalSummary:
    retrieved = tuple(batch.retrieved_at for batch in batches)
    return ReferenceBundleRetrievalSummary(
        batch_count=len(batches),
        endpoint_classes=tuple(
            value
            for value in RetrievalEndpointClass
            if any(batch.retrieval_endpoint_class is value for batch in batches)
        ),
        origins=tuple(
            value for value in ResponseOrigin if any(batch.origin is value for batch in batches)
        ),
        cache_states=tuple(
            value
            for value in CacheLookupState
            if any(batch.cache_lookup_state is value for batch in batches)
        ),
        database_releases=tuple(
            dict.fromkeys(
                batch.database_release for batch in batches if batch.database_release is not None
            )
        ),
        release_unavailable_batch_count=sum(batch.database_release is None for batch in batches),
        stale_batch_count=sum(batch.is_stale for batch in batches),
        network_attempt_count=sum(batch.attempt_count for batch in batches),
        response_bytes=sum(batch.response_bytes for batch in batches),
        earliest_retrieved_at=min(retrieved),
        latest_retrieved_at=max(retrieved),
    )


def _relationships(
    cards: tuple[KeggEntryCard, ...],
) -> tuple[_ReferenceRelationship, ...]:
    rows: list[_ReferenceRelationship] = []
    observed: set[tuple[str, str, str, str, str]] = set()
    for card in cards:
        pairs: list[tuple[str, str, Iterable[str | KeggEntryCardReference]]] = [
            ("pubmed", "pubmed", card.pubmed_ids),
        ]
        pairs.extend(("dblink", item.database, item.identifiers) for item in card.dblinks)
        if isinstance(card, KoEntryCard):
            pairs.extend(
                (
                    ("ec_number", "enzyme", card.ec_numbers),
                    ("module", "module", card.modules),
                    ("pathway", "pathway", card.pathways),
                )
            )
        elif isinstance(card, ModuleEntryCard):
            pairs.extend(
                (
                    ("pathway", "pathway", card.pathways),
                    ("reaction", "reaction", card.reactions),
                )
            )
            if card.module_definition is not None:
                pairs.extend(
                    (
                        (
                            "referenced_module",
                            "module",
                            card.module_definition.referenced_modules,
                        ),
                        ("ko_component", "ko", card.module_definition.ko_components),
                    )
                )
        elif isinstance(card, PathwayEntryCard):
            pairs.extend(
                (
                    ("ko", "ko", card.ko_identifiers),
                    ("module", "module", card.modules),
                    ("reaction", "reaction", card.reactions),
                    ("compound", "compound", card.compounds),
                    ("glycan", "glycan", card.glycans),
                )
            )
        elif isinstance(card, ReactionEntryCard):
            pairs.extend(
                (
                    ("enzyme", "enzyme", card.enzyme_ids),
                    ("ko", "ko", card.ko_identifiers),
                    ("rclass", "rclass", card.rclass_ids),
                    ("compound", "compound", card.compound_ids),
                    ("glycan", "glycan", card.glycan_ids),
                )
            )
        elif isinstance(card, EnzymeEntryCard):
            pairs.extend(
                (
                    ("reaction", "reaction", card.reaction_ids),
                    ("ko", "ko", card.ko_identifiers),
                )
            )
        elif isinstance(card, (CompoundEntryCard, GlycanEntryCard)):
            pairs.extend(
                (
                    ("reaction", "reaction", card.reactions),
                    ("pathway", "pathway", card.pathways),
                )
            )
        elif isinstance(card, GeneEntryCard):
            pairs.extend(
                (
                    ("orthology", "ko", card.orthology),
                    ("pathway", "pathway", card.pathways),
                )
            )
        else:
            if card.taxonomy_id is not None:
                pairs.append(("taxonomy", "taxonomy", (card.taxonomy_id,)))
        for relationship, target_database, values in pairs:
            for value in values:
                if isinstance(value, KeggEntryCardReference):
                    identifier = value.identifier
                    label = value.label
                else:
                    identifier = value
                    label = None
                key = (
                    card.entity.database.value,
                    card.entity.identifier,
                    relationship,
                    target_database,
                    identifier,
                )
                if key in observed:
                    continue
                observed.add(key)
                rows.append(
                    _ReferenceRelationship(
                        source_database=card.entity.database.value,
                        source_identifier=card.entity.identifier,
                        relationship=relationship,
                        target_database=target_database,
                        target_identifier=identifier,
                        target_label=label,
                    )
                )
                if len(rows) > MAX_REFERENCE_RELATIONSHIPS:
                    fail(
                        ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                        "The selected reference cards exceed the relationship-row bound.",
                        suggested_action="Select fewer KEGG entries and retry.",
                        safe_details=(
                            SafeDetail(
                                name="relationship_row_limit",
                                value=str(MAX_REFERENCE_RELATIONSHIPS),
                            ),
                        ),
                    )
    return tuple(rows)


def _relationships_tsv(rows: tuple[_ReferenceRelationship, ...]) -> str:
    target = io.StringIO(newline="")
    writer = csv.writer(target, delimiter="\t", lineterminator="\n")
    writer.writerow(
        (
            "source_database",
            "source_identifier",
            "relationship",
            "target_database",
            "target_identifier",
            "target_label",
        )
    )
    for row in rows:
        writer.writerow(
            tuple(
                escape_spreadsheet_formula(value)
                for value in (
                    row.source_database,
                    row.source_identifier,
                    row.relationship,
                    row.target_database,
                    row.target_identifier,
                    row.target_label or "",
                )
            )
        )
    return target.getvalue()


def _mime_type(name: str) -> str:
    return _TSV_MIME_TYPE if name.endswith(".tsv") else _JSON_MIME_TYPE


def _json_text(value: object) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


__all__ = [
    "MAX_REFERENCE_BRITE_ROWS",
    "MAX_REFERENCE_BUNDLE_ARTIFACT_BYTES",
    "MAX_REFERENCE_BUNDLE_BYTES",
    "MAX_REFERENCE_RELATIONSHIPS",
    "REFERENCE_BRITE_PATHS_NAME",
    "REFERENCE_BUNDLE_SCHEMA_VERSION",
    "REFERENCE_MANIFEST_NAME",
    "REFERENCE_RELATIONSHIPS_NAME",
    "REFERENCE_SNAPSHOT_NAME",
    "KeggReferenceBundle",
    "ReferenceBundleArtifact",
    "ReferenceBundleBriteStatus",
    "ReferenceBundleBriteSummary",
    "ReferenceBundleFileRecord",
    "ReferenceBundleManifest",
    "ReferenceBundleSource",
    "WriteKeggReferenceBundleRequest",
    "write_kegg_reference_bundle",
]
