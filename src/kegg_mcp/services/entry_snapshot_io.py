"""Shared bounded loading for current-scope KEGG entry-card snapshots."""

from __future__ import annotations

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, SafeDetail, fail
from kegg_mcp.services.entry_cards import (
    ENTRY_CARD_SCHEMA_VERSION,
    ENTRY_CARD_SNAPSHOT_SECTION,
    KeggEntryCardSnapshot,
)
from kegg_mcp.services.query_support import MAX_QUERY_ARTIFACT_BYTES
from kegg_mcp.services.result_store import SQLiteResultStore


def read_entry_card_snapshot(
    result_store: SQLiteResultStore,
    scope_id: str,
    result_id: str,
) -> KeggEntryCardSnapshot:
    """Load and validate one bounded card snapshot from its originating scope."""
    chunks: list[bytes] = []
    offset = 0
    while True:
        try:
            page = result_store.read_artifact(
                scope_id,
                result_id,
                ENTRY_CARD_SNAPSHOT_SECTION,
                offset=offset,
                limit=result_store.limits.max_range_bytes,
            )
        except KeggMcpError as error:
            if error.detail.code is not ErrorCode.RESULT_NOT_FOUND:
                raise
            artifacts = result_store.list_artifacts(scope_id, result_id, limit=1)
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "The selected active result has an incompatible artifact kind.",
                suggested_action=(
                    "Use a result identifier returned by get_kegg_entries with projection set "
                    "to card or references."
                ),
                safe_details=(
                    SafeDetail(
                        name="expected_artifact_kind",
                        value=ENTRY_CARD_SNAPSHOT_SECTION,
                    ),
                    SafeDetail(
                        name="actual_artifact_kind",
                        value=artifacts.items[0].section,
                    ),
                ),
            )
        if page.mime_type != "application/json":
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "The retained entry-card snapshot has an incompatible media type.",
                suggested_action=(
                    "Use a result identifier returned by get_kegg_entries with projection set "
                    "to card or references."
                ),
            )
        if page.total_bytes > MAX_QUERY_ARTIFACT_BYTES:
            fail(
                ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                "The retained entry-card snapshot exceeds the fixed query-artifact bound.",
                suggested_action="Create card snapshots from fewer KEGG entries.",
                safe_details=(
                    SafeDetail(name="observed_bytes", value=str(page.total_bytes)),
                    SafeDetail(name="limit_bytes", value=str(MAX_QUERY_ARTIFACT_BYTES)),
                ),
            )
        chunks.append(page.content)
        if page.next_offset is None:
            break
        offset = page.next_offset
    payload = b"".join(chunks)
    try:
        return KeggEntryCardSnapshot.model_validate_json(payload)
    except ValueError:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The retained result does not contain a compatible KEGG entry-card snapshot.",
            suggested_action=(
                "Use result identifiers returned by get_kegg_entries with projection set to "
                "card or references."
            ),
            safe_details=(
                SafeDetail(
                    name="required_snapshot_schema_version",
                    value=ENTRY_CARD_SCHEMA_VERSION,
                ),
            ),
        )


__all__ = ["read_entry_card_snapshot"]
