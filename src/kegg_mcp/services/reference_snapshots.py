"""Deterministic local comparison of retained KEGG entry-card snapshots."""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Self, cast

from pydantic import Field, field_validator, model_validator

from kegg_mcp.domain.annotations import FrozenModel
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.services.entry_cards import (
    ENTRY_CARD_SNAPSHOT_SECTION,
    KeggEntryCard,
    KeggEntryCardEntity,
    KeggEntryCardKind,
    KeggEntryCardSnapshot,
)
from kegg_mcp.services.entry_snapshot_io import read_entry_card_snapshot
from kegg_mcp.services.query_support import (
    bounded_query_payload,
    require_bounded_query_direct_result,
)
from kegg_mcp.services.result_builders import _artifact_metadata
from kegg_mcp.services.result_store import (
    RESULT_ID_SCHEMA_PATTERN,
    ResultArtifactInput,
    ResultArtifactMetadata,
    ResultMetadata,
    SQLiteResultStore,
    create_retained_result,
)

ENTRY_SNAPSHOT_SECTION = ENTRY_CARD_SNAPSHOT_SECTION
REFERENCE_DIFF_SECTION = "reference_diff"
REFERENCE_DIFF_SCHEMA_VERSION = "1"
MAX_REFERENCE_DIFF_CHANGES = 4_096
MAX_REFERENCE_DIFF_PREVIEW = 25
MAX_REFERENCE_RELEASE_PREVIEW = 8
MAX_REFERENCE_CONTEXT_VALUES = 8

_CARD_METADATA_FIELDS = frozenset({"entity", "kind"})
_RELATION_FIELDS = frozenset(
    {
        "modules",
        "pathways",
        "reactions",
        "compounds",
        "glycans",
        "orthology",
        "ec_numbers",
        "enzyme_ids",
        "ko_identifiers",
        "rclass_ids",
        "reaction_ids",
        "compound_ids",
        "glycan_ids",
    }
)


class ReferenceSnapshotComparisonDimension(StrEnum):
    """Allowlisted semantic groups for a local snapshot comparison."""

    ENTRY_FIELDS = "entry_fields"
    RELATIONSHIPS = "relationships"
    MODULE_DEFINITIONS = "module_definitions"
    PATHWAY_DENOMINATORS = "pathway_denominators"


class ReferenceSnapshotSource(FrozenModel):
    """One current-scope retained entry-card snapshot."""

    result_id: str = Field(pattern=RESULT_ID_SCHEMA_PATTERN)


class CompareKeggReferenceSnapshotsRequest(FrozenModel):
    """Compare two current-scope snapshots without retrieving more KEGG data."""

    left: ReferenceSnapshotSource
    right: ReferenceSnapshotSource
    compare: Annotated[
        tuple[ReferenceSnapshotComparisonDimension, ...],
        Field(min_length=1, max_length=len(ReferenceSnapshotComparisonDimension)),
    ] = tuple(ReferenceSnapshotComparisonDimension)

    @field_validator("compare")
    @classmethod
    def canonicalize_dimensions(
        cls,
        value: tuple[ReferenceSnapshotComparisonDimension, ...],
    ) -> tuple[ReferenceSnapshotComparisonDimension, ...]:
        if len(value) != len(set(value)):
            raise ValueError("snapshot comparison dimensions must be unique")
        return tuple(
            dimension for dimension in ReferenceSnapshotComparisonDimension if dimension in value
        )


class ReferenceReleaseComparisonStatus(StrEnum):
    """Whether source release labels permit a simple release comparison."""

    SAME = "same"
    DIFFERENT = "different"
    MIXED = "mixed"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class ReferenceSnapshotChangeType(StrEnum):
    ENTRY_ADDED = "entry_added"
    ENTRY_REMOVED = "entry_removed"
    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    FIELD_MODIFIED = "field_modified"


class ReferenceSnapshotChangeCategory(StrEnum):
    """Retained change categories, including non-selectable request membership."""

    ENTRY_MEMBERSHIP = "entry_membership"
    ENTRY_FIELDS = ReferenceSnapshotComparisonDimension.ENTRY_FIELDS
    RELATIONSHIPS = ReferenceSnapshotComparisonDimension.RELATIONSHIPS
    MODULE_DEFINITIONS = ReferenceSnapshotComparisonDimension.MODULE_DEFINITIONS
    PATHWAY_DENOMINATORS = ReferenceSnapshotComparisonDimension.PATHWAY_DENOMINATORS


class ReferenceSnapshotSourceSummary(FrozenModel):
    """Compact non-payload facts for one retained source snapshot."""

    result_id: str = Field(pattern=RESULT_ID_SCHEMA_PATTERN)
    schema_version: str = Field(min_length=1, max_length=32)
    parser_name: str = Field(min_length=1, max_length=100)
    parser_version: str = Field(min_length=1, max_length=32)
    response_parser_version: str = Field(min_length=1, max_length=32)
    entry_count: int = Field(strict=True, ge=0, le=50)
    missing_entry_count: int = Field(strict=True, ge=0, le=50)
    provenance_batch_count: int = Field(strict=True, ge=1, le=50)
    endpoint_classes: Annotated[
        tuple[str, ...],
        Field(max_length=MAX_REFERENCE_CONTEXT_VALUES),
    ]
    endpoint_labels: Annotated[
        tuple[str, ...],
        Field(max_length=MAX_REFERENCE_CONTEXT_VALUES),
    ]
    database_release_count: int = Field(strict=True, ge=0, le=50)
    release_labeled_batch_count: int = Field(strict=True, ge=0, le=50)
    release_unavailable_batch_count: int = Field(strict=True, ge=0, le=50)
    database_releases: Annotated[
        tuple[str, ...],
        Field(max_length=MAX_REFERENCE_RELEASE_PREVIEW),
    ]
    database_releases_truncated: bool
    network_request_count: int = Field(strict=True, ge=0)
    cache_hit_count: int = Field(strict=True, ge=0, le=50)
    stale_batch_count: int = Field(strict=True, ge=0, le=50)
    earliest_retrieved_at: datetime
    latest_retrieved_at: datetime

    @model_validator(mode="after")
    def validate_release_preview(self) -> Self:
        if self.database_release_count < len(self.database_releases):
            raise ValueError("database release count cannot be smaller than its preview")
        if self.database_releases_truncated != (
            self.database_release_count > len(self.database_releases)
        ):
            raise ValueError("database release truncation must match its preview")
        if (
            self.release_labeled_batch_count + self.release_unavailable_batch_count
            != self.provenance_batch_count
        ):
            raise ValueError("release batch counts must cover provenance")
        if self.cache_hit_count > self.provenance_batch_count:
            raise ValueError("cache hits cannot exceed provenance batches")
        if self.stale_batch_count > self.cache_hit_count:
            raise ValueError("stale batches must be cache hits")
        if self.latest_retrieved_at < self.earliest_retrieved_at:
            raise ValueError("latest retrieval cannot precede earliest retrieval")
        return self


class ReferenceSnapshotChangePreview(FrozenModel):
    """One compact change location without endpoint-returned field values."""

    entity: KeggEntryCardEntity
    category: ReferenceSnapshotChangeCategory
    change_type: ReferenceSnapshotChangeType
    field_name: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_]*$",
        max_length=64,
    )
    added_value_count: int = Field(default=0, strict=True, ge=0)
    removed_value_count: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode="after")
    def validate_entry_change(self) -> Self:
        is_entry_change = self.change_type in {
            ReferenceSnapshotChangeType.ENTRY_ADDED,
            ReferenceSnapshotChangeType.ENTRY_REMOVED,
        }
        if is_entry_change != (self.field_name is None):
            raise ValueError("only entry membership changes omit field_name")
        return self


class ReferenceSnapshotDimensionCount(FrozenModel):
    dimension: ReferenceSnapshotComparisonDimension
    change_count: int = Field(strict=True, ge=0, le=MAX_REFERENCE_DIFF_CHANGES)


class CompareKeggReferenceSnapshotsResult(FrozenModel):
    """Compact direct summary with a complete deterministic diff retained."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    left: ReferenceSnapshotSourceSummary
    right: ReferenceSnapshotSourceSummary
    parser_compatible: bool
    endpoint_context_compatible: bool
    retrieval_context_compatible: bool
    release_status: ReferenceReleaseComparisonStatus
    shared_entry_count: int = Field(strict=True, ge=0, le=50)
    added_entry_count: int = Field(strict=True, ge=0, le=50)
    removed_entry_count: int = Field(strict=True, ge=0, le=50)
    changed_entry_count: int = Field(strict=True, ge=0, le=50)
    field_change_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_REFERENCE_DIFF_CHANGES,
    )
    dimension_counts: Annotated[
        tuple[ReferenceSnapshotDimensionCount, ...],
        Field(
            min_length=len(ReferenceSnapshotComparisonDimension),
            max_length=len(ReferenceSnapshotComparisonDimension),
        ),
    ]
    change_preview: Annotated[
        tuple[ReferenceSnapshotChangePreview, ...],
        Field(max_length=MAX_REFERENCE_DIFF_PREVIEW),
    ]
    changes_truncated: bool
    interpretation_caveats: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=512)], ...],
        Field(min_length=2, max_length=5),
    ]

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        total_change_count = (
            self.added_entry_count + self.removed_entry_count + self.field_change_count
        )
        if total_change_count < len(self.change_preview):
            raise ValueError("change counts cannot be smaller than their preview")
        if self.changes_truncated != (total_change_count > len(self.change_preview)):
            raise ValueError("changes_truncated must match the direct preview")
        dimensions = tuple(item.dimension for item in self.dimension_counts)
        if dimensions != tuple(ReferenceSnapshotComparisonDimension):
            raise ValueError("dimension counts must cover every dimension in stable order")
        if sum(item.change_count for item in self.dimension_counts) != self.field_change_count:
            raise ValueError("dimension counts must sum to field_change_count")
        if self.changed_entry_count > self.shared_entry_count:
            raise ValueError("changed_entry_count cannot exceed shared entries")
        if self.left.entry_count != self.shared_entry_count + self.removed_entry_count:
            raise ValueError("left returned entries must equal shared plus removed entries")
        if self.right.entry_count != self.shared_entry_count + self.added_entry_count:
            raise ValueError("right returned entries must equal shared plus added entries")
        return self


def compare_kegg_reference_snapshots(
    request: CompareKeggReferenceSnapshotsRequest,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> CompareKeggReferenceSnapshotsResult:
    """Compare two retained entry-card snapshots without network access."""
    left_snapshot = read_entry_card_snapshot(
        result_store,
        scope_id,
        request.left.result_id,
    )
    right_snapshot = read_entry_card_snapshot(
        result_store,
        scope_id,
        request.right.result_id,
    )
    left_summary = _source_summary(request.left.result_id, left_snapshot)
    right_summary = _source_summary(request.right.result_id, right_snapshot)
    left_requested_keys = _requested_keys(left_snapshot)
    right_requested_keys = _requested_keys(right_snapshot)
    if left_requested_keys != right_requested_keys:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "KEGG reference snapshots must cover the same requested entries.",
            suggested_action=(
                "Create both card snapshots from the same database-qualified entry request."
            ),
            safe_details=(
                SafeDetail(
                    name="left_requested_entry_count",
                    value=str(len(left_requested_keys)),
                ),
                SafeDetail(
                    name="right_requested_entry_count",
                    value=str(len(right_requested_keys)),
                ),
            ),
        )
    parser_compatible = (
        left_snapshot.schema_version == right_snapshot.schema_version
        and left_snapshot.parser_name == right_snapshot.parser_name
        and left_snapshot.parser_version == right_snapshot.parser_version
        and left_snapshot.response_parser_version == right_snapshot.response_parser_version
    )
    endpoint_context_compatible = _endpoint_context(left_snapshot) == _endpoint_context(
        right_snapshot
    )
    retrieval_context_compatible = _retrieval_context(left_snapshot) == _retrieval_context(
        right_snapshot
    )
    release_status = _release_status(left_snapshot, right_snapshot)

    left_by_key = _cards_by_key(left_snapshot.entries)
    right_by_key = _cards_by_key(right_snapshot.entries)
    left_keys = set(left_by_key)
    right_keys = set(right_by_key)
    shared_keys = left_keys & right_keys
    changes: list[dict[str, Any]] = []

    for key in sorted(left_keys - right_keys):
        changes.append(_entry_membership_change(left_by_key[key], added=False))
    for key in sorted(right_keys - left_keys):
        changes.append(_entry_membership_change(right_by_key[key], added=True))
    if parser_compatible:
        for key in sorted(shared_keys):
            changes.extend(
                _card_field_changes(
                    left_by_key[key],
                    right_by_key[key],
                    selected=frozenset(request.compare),
                )
            )

    entry_change_count = len(left_keys ^ right_keys)
    field_changes = tuple(change for change in changes if change["field_name"] is not None)
    if len(field_changes) > MAX_REFERENCE_DIFF_CHANGES:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "The selected snapshots exceeded the fixed semantic-change bound.",
            suggested_action="Compare fewer entries or fewer change dimensions.",
        )
    changed_entry_keys = {_entity_key_from_payload(change["entity"]) for change in field_changes}
    dimension_counts = tuple(
        ReferenceSnapshotDimensionCount(
            dimension=dimension,
            change_count=sum(change["category"] == dimension.value for change in field_changes),
        )
        for dimension in ReferenceSnapshotComparisonDimension
    )
    detail = {
        "schema_version": REFERENCE_DIFF_SCHEMA_VERSION,
        "request": request.model_dump(mode="json"),
        "context": {
            "parser_compatible": parser_compatible,
            "endpoint_context_compatible": endpoint_context_compatible,
            "retrieval_context_compatible": retrieval_context_compatible,
            "release_status": release_status.value,
            "left": left_summary.model_dump(mode="json"),
            "right": right_summary.model_dump(mode="json"),
        },
        "summary": {
            "shared_entry_count": len(shared_keys),
            "added_entry_count": len(right_keys - left_keys),
            "removed_entry_count": len(left_keys - right_keys),
            "changed_entry_count": len(changed_entry_keys),
            "field_change_count": len(field_changes),
            "entry_membership_change_count": entry_change_count,
        },
        "changes": changes,
    }
    payload = bounded_query_payload(detail)
    previews = tuple(_change_preview(change) for change in changes[:MAX_REFERENCE_DIFF_PREVIEW])
    with create_retained_result(
        result_store,
        scope_id,
        (
            ResultArtifactInput(
                section=REFERENCE_DIFF_SECTION,
                mime_type="application/json",
                content=payload,
            ),
        ),
    ) as stored:
        result = CompareKeggReferenceSnapshotsResult(
            result=stored,
            artifact=_artifact_metadata(
                REFERENCE_DIFF_SECTION,
                "application/json",
                payload,
            ),
            left=left_summary,
            right=right_summary,
            parser_compatible=parser_compatible,
            endpoint_context_compatible=endpoint_context_compatible,
            retrieval_context_compatible=retrieval_context_compatible,
            release_status=release_status,
            shared_entry_count=len(shared_keys),
            added_entry_count=len(right_keys - left_keys),
            removed_entry_count=len(left_keys - right_keys),
            changed_entry_count=len(changed_entry_keys),
            field_change_count=len(field_changes),
            dimension_counts=dimension_counts,
            change_preview=previews,
            changes_truncated=len(changes) > len(previews),
            interpretation_caveats=_comparison_caveats(
                parser_compatible=parser_compatible,
                endpoint_context_compatible=endpoint_context_compatible,
                retrieval_context_compatible=retrieval_context_compatible,
            ),
        )
        require_bounded_query_direct_result(result)
        return result


def _source_summary(
    result_id: str,
    snapshot: KeggEntryCardSnapshot,
) -> ReferenceSnapshotSourceSummary:
    releases = tuple(
        dict.fromkeys(
            batch.database_release
            for batch in snapshot.provenance
            if batch.database_release is not None
        )
    )
    labeled_batches = sum(batch.database_release is not None for batch in snapshot.provenance)
    retrieved_at = tuple(batch.retrieved_at for batch in snapshot.provenance)
    return ReferenceSnapshotSourceSummary(
        result_id=result_id,
        schema_version=snapshot.schema_version,
        parser_name=snapshot.parser_name,
        parser_version=snapshot.parser_version,
        response_parser_version=snapshot.response_parser_version,
        entry_count=len(snapshot.entries),
        missing_entry_count=len(snapshot.missing_entries),
        provenance_batch_count=len(snapshot.provenance),
        endpoint_classes=tuple(
            sorted({batch.retrieval_endpoint_class.value for batch in snapshot.provenance})
        ),
        endpoint_labels=tuple(sorted({batch.endpoint_label for batch in snapshot.provenance})),
        database_release_count=len(releases),
        release_labeled_batch_count=labeled_batches,
        release_unavailable_batch_count=len(snapshot.provenance) - labeled_batches,
        database_releases=releases[:MAX_REFERENCE_RELEASE_PREVIEW],
        database_releases_truncated=len(releases) > MAX_REFERENCE_RELEASE_PREVIEW,
        network_request_count=sum(
            batch.attempt_count for batch in snapshot.provenance if batch.origin.value == "network"
        ),
        cache_hit_count=sum(batch.origin.value == "cache" for batch in snapshot.provenance),
        stale_batch_count=sum(batch.is_stale for batch in snapshot.provenance),
        earliest_retrieved_at=min(retrieved_at),
        latest_retrieved_at=max(retrieved_at),
    )


def _endpoint_context(
    snapshot: KeggEntryCardSnapshot,
) -> frozenset[tuple[str, str]]:
    return frozenset(
        (batch.retrieval_endpoint_class.value, batch.endpoint_label)
        for batch in snapshot.provenance
    )


def _retrieval_context(
    snapshot: KeggEntryCardSnapshot,
) -> frozenset[tuple[str, str, bool]]:
    return frozenset(
        (batch.origin.value, batch.cache_lookup_state.value, batch.is_stale)
        for batch in snapshot.provenance
    )


def _release_status(
    left: KeggEntryCardSnapshot,
    right: KeggEntryCardSnapshot,
) -> ReferenceReleaseComparisonStatus:
    left_releases = {
        batch.database_release for batch in left.provenance if batch.database_release is not None
    }
    right_releases = {
        batch.database_release for batch in right.provenance if batch.database_release is not None
    }
    left_missing = any(batch.database_release is None for batch in left.provenance)
    right_missing = any(batch.database_release is None for batch in right.provenance)
    if not left_releases and not right_releases:
        return ReferenceReleaseComparisonStatus.UNAVAILABLE
    if left_missing or right_missing or not left_releases or not right_releases:
        return ReferenceReleaseComparisonStatus.PARTIAL
    if len(left_releases) > 1 or len(right_releases) > 1:
        return ReferenceReleaseComparisonStatus.MIXED
    if left_releases == right_releases:
        return ReferenceReleaseComparisonStatus.SAME
    return ReferenceReleaseComparisonStatus.DIFFERENT


def _cards_by_key(
    cards: tuple[KeggEntryCard, ...],
) -> dict[tuple[str, str], KeggEntryCard]:
    return {(card.entity.database.value, card.entity.identifier): card for card in cards}


def _requested_keys(
    snapshot: KeggEntryCardSnapshot,
) -> frozenset[tuple[str, str]]:
    return frozenset(
        (
            entity.database.value,
            entity.identifier,
        )
        for entity in snapshot.requested_entries
    )


def _entry_membership_change(
    card: KeggEntryCard,
    *,
    added: bool,
) -> dict[str, Any]:
    return {
        "entity": card.entity.model_dump(mode="json"),
        "category": ReferenceSnapshotChangeCategory.ENTRY_MEMBERSHIP.value,
        "change_type": (
            ReferenceSnapshotChangeType.ENTRY_ADDED.value
            if added
            else ReferenceSnapshotChangeType.ENTRY_REMOVED.value
        ),
        "field_name": None,
        "left_present": not added,
        "right_present": added,
        "left_value": None,
        "right_value": None,
        "added_values": [],
        "removed_values": [],
    }


def _card_field_changes(
    left: KeggEntryCard,
    right: KeggEntryCard,
    *,
    selected: frozenset[ReferenceSnapshotComparisonDimension],
) -> tuple[dict[str, Any], ...]:
    left_payload = left.model_dump(mode="json")
    right_payload = right.model_dump(mode="json")
    fields = (set(left_payload) | set(right_payload)) - _CARD_METADATA_FIELDS
    if left.entity.database is KeggEntryCardKind.MODULE:
        fields.discard("definition")
    changes: list[dict[str, Any]] = []
    for field_name in sorted(fields):
        category = _field_category(left.entity.database, field_name)
        if ReferenceSnapshotComparisonDimension(category.value) not in selected:
            continue
        left_present = field_name in left_payload
        right_present = field_name in right_payload
        left_value = left_payload.get(field_name)
        right_value = right_payload.get(field_name)
        if left_present and right_present and left_value == right_value:
            continue
        if not left_present:
            change_type = ReferenceSnapshotChangeType.FIELD_ADDED
        elif not right_present:
            change_type = ReferenceSnapshotChangeType.FIELD_REMOVED
        else:
            change_type = ReferenceSnapshotChangeType.FIELD_MODIFIED
        added_values, removed_values = _collection_delta(left_value, right_value)
        changes.append(
            {
                "entity": left.entity.model_dump(mode="json"),
                "category": category.value,
                "change_type": change_type.value,
                "field_name": field_name,
                "left_present": left_present,
                "right_present": right_present,
                "left_value": left_value,
                "right_value": right_value,
                "added_values": added_values,
                "removed_values": removed_values,
            }
        )
        if len(changes) > MAX_REFERENCE_DIFF_CHANGES:
            fail(
                ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                "The selected snapshots exceeded the fixed semantic-change bound.",
                suggested_action="Compare fewer entries or fewer change dimensions.",
            )
    return tuple(changes)


def _field_category(
    kind: KeggEntryCardKind,
    field_name: str,
) -> ReferenceSnapshotChangeCategory:
    if kind is KeggEntryCardKind.MODULE and field_name == "module_definition":
        return ReferenceSnapshotChangeCategory.MODULE_DEFINITIONS
    if kind is KeggEntryCardKind.PATHWAY and field_name == "ko_identifiers":
        return ReferenceSnapshotChangeCategory.PATHWAY_DENOMINATORS
    if field_name in _RELATION_FIELDS:
        return ReferenceSnapshotChangeCategory.RELATIONSHIPS
    return ReferenceSnapshotChangeCategory.ENTRY_FIELDS


def _collection_delta(
    left: object,
    right: object,
) -> tuple[list[Any], list[Any]]:
    if not isinstance(left, list) or not isinstance(right, list):
        return [], []
    left_values = cast(list[Any], left)
    right_values = cast(list[Any], right)
    left_by_key: dict[str, Any] = {_canonical_json(value): value for value in left_values}
    right_by_key: dict[str, Any] = {_canonical_json(value): value for value in right_values}
    added = [right_by_key[key] for key in sorted(set(right_by_key) - set(left_by_key))]
    removed = [left_by_key[key] for key in sorted(set(left_by_key) - set(right_by_key))]
    return added, removed


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _change_preview(change: dict[str, Any]) -> ReferenceSnapshotChangePreview:
    return ReferenceSnapshotChangePreview(
        entity=KeggEntryCardEntity.model_validate(change["entity"], strict=False),
        category=ReferenceSnapshotChangeCategory(change["category"]),
        change_type=ReferenceSnapshotChangeType(change["change_type"]),
        field_name=change["field_name"],
        added_value_count=len(change["added_values"]),
        removed_value_count=len(change["removed_values"]),
    )


def _entity_key_from_payload(value: object) -> tuple[str, str]:
    entity = KeggEntryCardEntity.model_validate(value, strict=False)
    return entity.database.value, entity.identifier


def _comparison_caveats(
    *,
    parser_compatible: bool,
    endpoint_context_compatible: bool,
    retrieval_context_compatible: bool,
) -> tuple[str, ...]:
    caveats = [
        (
            "Snapshot changes are deterministic differences between retained KEGG reference "
            "fields; they do not establish biological gain, loss, validation, or contradiction."
        ),
        (
            "A removed relationship means it was absent from the right retained reference "
            "snapshot, not that the underlying biological relationship was disproved."
        ),
    ]
    if not parser_compatible:
        caveats.append(
            "Entry-field comparison was skipped because the snapshot parser contracts differ."
        )
    if not endpoint_context_compatible:
        caveats.append(
            "Endpoint contexts differ, so structural changes must not be attributed solely to a "
            "KEGG release."
        )
    if not retrieval_context_compatible:
        caveats.append(
            "Retrieval contexts differ in network, cache, or stale state; field differences "
            "remain structural and are not attributed automatically to a KEGG release."
        )
    return tuple(caveats)


__all__ = [
    "ENTRY_SNAPSHOT_SECTION",
    "REFERENCE_DIFF_SCHEMA_VERSION",
    "REFERENCE_DIFF_SECTION",
    "CompareKeggReferenceSnapshotsRequest",
    "CompareKeggReferenceSnapshotsResult",
    "ReferenceReleaseComparisonStatus",
    "ReferenceSnapshotChangeCategory",
    "ReferenceSnapshotChangePreview",
    "ReferenceSnapshotChangeType",
    "ReferenceSnapshotComparisonDimension",
    "ReferenceSnapshotDimensionCount",
    "ReferenceSnapshotSource",
    "ReferenceSnapshotSourceSummary",
    "compare_kegg_reference_snapshots",
]
