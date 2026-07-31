"""MCP integration contracts for typed entry cards and local snapshot comparison."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from mcp import types

from kegg_mcp.kegg import (
    GetRequest,
    GetResult,
    KeggClientConfig,
    KeggGetDatabase,
    KeggRequestOptions,
)
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.parsers import parse_flat_file_response
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.tool_registry import dispatch_tool, tool_definitions
from kegg_mcp.services.entry_cards import ENTRY_CARD_SNAPSHOT_SECTION
from kegg_mcp.services.entry_references import ENTRY_REFERENCE_SNAPSHOT_SECTION
from kegg_mcp.services.query_support import MAX_QUERY_DIRECT_BYTES
from kegg_mcp.services.reference_budget import KeggMcpClient
from kegg_mcp.services.reference_snapshots import REFERENCE_DIFF_SECTION
from kegg_mcp.services.result_store import SQLiteResultStore

_NOW = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
_ENTRY_COUNT = 50


class _CountingEntryClient:
    """GET-only fake whose call count makes local-only comparison observable."""

    def __init__(self) -> None:
        self._config = KeggClientConfig()
        self.get_calls: list[GetRequest] = []

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del options
        self.get_calls.append(request)
        body = b"".join(
            (
                f"ENTRY       {entry.identifier}                      KO\n"
                f"NAME        Synthetic {entry.identifier} {'n' * 500}\n"
                f"DEFINITION  Synthetic definition {'d' * 2_000}\n"
                "PATHWAY     map00010  Synthetic pathway\n"
                "REFERENCE   PMID:123456 PMID:234567 PMID:123456\n"
                "///\n"
            ).encode("ascii")
            for entry in request.entries
        )
        return GetResult(
            request=request,
            documents=(parse_flat_file_response(body),),
            missing_entries=(),
            batches=(_provenance(len(self.get_calls)),),
        )


def _provenance(call_number: int) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=KeggOperation.GET,
        request_key=f"synthetic:card:{call_number}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        response_bytes=150_000,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release="synthetic-card-release",
        attempt_count=1,
        is_stale=False,
    )


def _runtime(
    store: SQLiteResultStore,
    client: _CountingEntryClient,
    *,
    scope_id: str,
) -> McpRuntime:
    return McpRuntime(
        client=cast(KeggMcpClient, client),
        result_store=store,
        scope_id=scope_id,
    )


def _tool_map() -> dict[str, types.Tool]:
    return {tool.name: tool for tool in tool_definitions()}


def _structured(result: types.CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    return result.structuredContent


def _data(result: types.CallToolResult) -> dict[str, Any]:
    structured = _structured(result)
    assert structured["ok"] is True
    return cast(dict[str, Any], structured["result"]["data"])


def _validate_output(tool: types.Tool, result: types.CallToolResult) -> None:
    assert tool.outputSchema is not None
    Draft202012Validator(tool.outputSchema).validate(  # pyright: ignore[reportUnknownMemberType]
        _structured(result)
    )


def _card_arguments() -> dict[str, object]:
    return {
        "entries": [
            {
                "database": KeggGetDatabase.KO.value,
                "identifier": f"K{index:05d}",
            }
            for index in range(1, _ENTRY_COUNT + 1)
        ],
        "projection": "card",
    }


def test_registry_declares_card_and_local_snapshot_comparison_contracts() -> None:
    tools = _tool_map()
    get_tool = tools["get_kegg_entries"]
    compare_tool = tools["compare_kegg_reference_snapshots"]

    assert get_tool.annotations is not None
    assert get_tool.annotations.openWorldHint is True
    assert compare_tool.annotations is not None
    assert compare_tool.annotations.openWorldHint is False
    assert compare_tool.annotations.destructiveHint is False
    assert compare_tool.annotations.readOnlyHint is False
    assert get_tool.inputSchema["properties"]["projection"]["enum"] == [
        "preview",
        "card",
        "references",
    ]
    assert set(compare_tool.inputSchema["properties"]) == {"left", "right", "compare"}
    Draft202012Validator.check_schema(get_tool.inputSchema)
    Draft202012Validator.check_schema(compare_tool.inputSchema)
    assert get_tool.outputSchema is not None
    assert compare_tool.outputSchema is not None
    Draft202012Validator.check_schema(get_tool.outputSchema)
    Draft202012Validator.check_schema(compare_tool.outputSchema)


@pytest.mark.asyncio
async def test_literature_references_round_trip_without_card_snapshot(tmp_path: Path) -> None:
    tools = _tool_map()
    store = SQLiteResultStore(tmp_path / "literature-results.sqlite3")
    client = _CountingEntryClient()
    runtime = _runtime(store, client, scope_id="literature-scope")
    arguments = _card_arguments()
    arguments["projection"] = "references"

    result = await dispatch_tool("get_kegg_entries", arguments, runtime)

    assert result.isError is False
    _validate_output(tools["get_kegg_entries"], result)
    data = _data(result)
    assert data["projection"] == "references"
    assert data["snapshot_artifact"] is None
    assert data["card_preview"] is None
    assert data["literature_artifact"]["section"] == ENTRY_REFERENCE_SNAPSHOT_SECTION
    assert data["literature_preview"]["entry_count"] == _ENTRY_COUNT
    assert data["literature_preview"]["referenced_entry_count"] == _ENTRY_COUNT
    assert data["literature_preview"]["pubmed_id_count"] == 2
    assert len(data["literature_preview"]["previews"]) == 10
    assert data["literature_preview"]["previews"][0]["pubmed_ids"] == [
        "123456",
        "234567",
    ]
    assert data["literature_preview"]["previews_truncated"] is True
    result_id = cast(str, data["result"]["result_id"])
    artifacts = store.list_artifacts(runtime.scope_id, result_id)
    assert tuple(item.section for item in artifacts.items) == (
        "detail",
        ENTRY_REFERENCE_SNAPSHOT_SECTION,
    )


@pytest.mark.asyncio
async def test_card_snapshots_round_trip_through_mcp_and_compare_without_kegg(
    tmp_path: Path,
) -> None:
    tools = _tool_map()
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    client = _CountingEntryClient()
    runtime = _runtime(store, client, scope_id="card-snapshot-scope")

    first = await dispatch_tool("get_kegg_entries", _card_arguments(), runtime)
    second = await dispatch_tool("get_kegg_entries", _card_arguments(), runtime)

    assert first.isError is False
    assert second.isError is False
    _validate_output(tools["get_kegg_entries"], first)
    _validate_output(tools["get_kegg_entries"], second)
    first_data = _data(first)
    second_data = _data(second)
    assert len(client.get_calls) == 2
    for data in (first_data, second_data):
        assert data["projection"] == "card"
        assert data["result"]["artifact_count"] == 2
        assert data["artifact"]["section"] == "detail"
        assert data["snapshot_artifact"]["section"] == ENTRY_CARD_SNAPSHOT_SECTION
        assert data["previews"] == []
        assert data["previews_truncated"] is False
        assert data["card_preview"]["entry_count"] == _ENTRY_COUNT
        assert len(data["card_preview"]["previews"]) == 10
        assert data["card_preview"]["previews_truncated"] is True
        assert (
            len(
                json.dumps(
                    data,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            <= MAX_QUERY_DIRECT_BYTES
        )

        result_id = cast(str, data["result"]["result_id"])
        artifacts = store.list_artifacts(runtime.scope_id, result_id)
        assert tuple(item.section for item in artifacts.items) == (
            "detail",
            ENTRY_CARD_SNAPSHOT_SECTION,
        )
        detail = store.read_artifact(
            runtime.scope_id,
            result_id,
            "detail",
            limit=store.limits.max_range_bytes,
        )
        snapshot = store.read_artifact(
            runtime.scope_id,
            result_id,
            ENTRY_CARD_SNAPSHOT_SECTION,
            limit=store.limits.max_range_bytes,
        )
        assert detail.mime_type == "application/json"
        assert snapshot.mime_type == "application/json"
        snapshot_data = json.loads(snapshot.content)
        assert snapshot_data["schema_version"] == "1"
        assert len(snapshot_data["requested_entries"]) == _ENTRY_COUNT
        assert len(snapshot_data["entries"]) == _ENTRY_COUNT

    calls_before_compare = len(client.get_calls)
    comparison = await dispatch_tool(
        "compare_kegg_reference_snapshots",
        {
            "left": {"result_id": first_data["result"]["result_id"]},
            "right": {"result_id": second_data["result"]["result_id"]},
        },
        runtime,
    )

    assert comparison.isError is False
    _validate_output(tools["compare_kegg_reference_snapshots"], comparison)
    assert len(client.get_calls) == calls_before_compare
    comparison_data = _data(comparison)
    assert comparison_data["shared_entry_count"] == _ENTRY_COUNT
    assert comparison_data["added_entry_count"] == 0
    assert comparison_data["removed_entry_count"] == 0
    assert comparison_data["changed_entry_count"] == 0
    assert comparison_data["field_change_count"] == 0
    assert comparison_data["retrieval_context_compatible"] is True
    assert comparison_data["artifact"]["section"] == REFERENCE_DIFF_SECTION
    assert (
        len(
            json.dumps(
                comparison_data,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        <= MAX_QUERY_DIRECT_BYTES
    )
    comparison_id = cast(str, comparison_data["result"]["result_id"])
    retained_diff = store.read_artifact(
        runtime.scope_id,
        comparison_id,
        REFERENCE_DIFF_SECTION,
        limit=store.limits.max_range_bytes,
    )
    assert json.loads(retained_diff.content)["changes"] == []


@pytest.mark.asyncio
async def test_snapshot_compare_rejects_cross_scope_without_kegg_access(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    client = _CountingEntryClient()
    owner_runtime = _runtime(store, client, scope_id="snapshot-owner")
    other_runtime = _runtime(store, client, scope_id="snapshot-other")
    first = await dispatch_tool("get_kegg_entries", _card_arguments(), owner_runtime)
    second = await dispatch_tool("get_kegg_entries", _card_arguments(), owner_runtime)
    first_data = _data(first)
    second_data = _data(second)
    calls_before_compare = len(client.get_calls)

    rejected = await dispatch_tool(
        "compare_kegg_reference_snapshots",
        {
            "left": {"result_id": first_data["result"]["result_id"]},
            "right": {"result_id": second_data["result"]["result_id"]},
        },
        other_runtime,
    )

    assert rejected.isError is True
    assert len(client.get_calls) == calls_before_compare
    structured = _structured(rejected)
    assert structured["ok"] is False
    assert structured["error"]["code"] == "RESULT_NOT_FOUND"
    assert store.list_results(other_runtime.scope_id).total_items == 0
