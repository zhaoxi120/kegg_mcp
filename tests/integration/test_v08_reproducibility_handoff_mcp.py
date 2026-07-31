"""End-to-end MCP contracts for v0.8 durable references and local handoffs."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema import Draft202012Validator
from mcp import types

from kegg_mcp.domain.errors import ErrorCode
from kegg_mcp.kegg import (
    GetRequest,
    GetResult,
    KeggClientConfig,
    KeggRequestOptions,
    PublicAcademicAccess,
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
from kegg_mcp.services.reference_budget import KeggMcpClient
from kegg_mcp.services.result_store import SQLiteResultStore

_NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _provenance(operation: KeggOperation, marker: str) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        request_key=f"synthetic-private-request:{marker}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        response_bytes=256,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release="synthetic-v08-release",
        attempt_count=1,
        is_stale=False,
    )


class _V08Client:
    """Minimal typed fake that makes local versus KEGG-backed behavior observable."""

    def __init__(self) -> None:
        self._config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))
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
                f"NAME        Synthetic {entry.identifier}\n"
                "DEFINITION  Synthetic function\n"
                "PATHWAY     ko00010  Synthetic pathway\n"
                "MODULE      M00001  Synthetic module\n"
                "REFERENCE   PMID:123456 PMID:234567 PMID:123456\n"
                "///\n"
            ).encode("ascii")
            for entry in request.entries
        )
        return GetResult(
            request=request,
            documents=(parse_flat_file_response(body),),
            missing_entries=(),
            batches=(_provenance(KeggOperation.GET, "cards"),),
        )


def _runtime(
    tmp_path: Path,
    client: _V08Client,
    store: SQLiteResultStore,
    *,
    scope_id: str = "v08-scope",
) -> McpRuntime:
    return McpRuntime(
        client=cast(KeggMcpClient, client),
        result_store=store,
        scope_id=scope_id,
        allowed_roots=(str(tmp_path.resolve()),),
    )


def _tools() -> dict[str, types.Tool]:
    return {tool.name: tool for tool in tool_definitions()}


def _structured(result: types.CallToolResult) -> dict[str, Any]:
    assert result.structuredContent is not None
    return result.structuredContent


def _data(result: types.CallToolResult) -> dict[str, Any]:
    structured = _structured(result)
    assert structured["ok"] is True
    return cast(dict[str, Any], structured["result"]["data"])


def _validate(tool: types.Tool, result: types.CallToolResult) -> None:
    assert tool.outputSchema is not None
    Draft202012Validator(tool.outputSchema).validate(  # pyright: ignore[reportUnknownMemberType]
        _structured(result)
    )


def _assert_private_bundle(path: Path, expected_names: set[str]) -> None:
    assert path.is_dir()
    assert {item.name for item in path.iterdir()} == expected_names
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    for item in path.iterdir():
        assert item.is_file()
        assert not item.is_symlink()
        assert stat.S_IMODE(item.stat().st_mode) == 0o600


def test_handoff_schema_has_seven_closed_target_branches() -> None:
    tool = _tools()["prepare_kegg_handoff"]
    handoff = cast(dict[str, Any], tool.inputSchema["properties"]["handoff"])

    assert handoff["discriminator"] == {"propertyName": "target"}
    assert len(handoff["oneOf"]) == 7
    assert all("target" in branch["required"] for branch in handoff["oneOf"])
    assert {branch["properties"]["target"]["const"] for branch in handoff["oneOf"]} == {
        "mapper_reconstruct",
        "mapper_search",
        "mapper_color",
        "mapper_join",
        "mapper_mwsearch",
        "syntax_ko_composition",
        "syntax_ko_sequence",
    }
    assert all(branch["additionalProperties"] is False for branch in handoff["oneOf"])
    assert "$defs" not in json.dumps(tool.inputSchema)
    assert tool.annotations is not None
    assert tool.annotations.openWorldHint is False
    assert tool.annotations.readOnlyHint is False
    assert tool.annotations.destructiveHint is False
    Draft202012Validator.check_schema(tool.inputSchema)
    assert tool.outputSchema is not None
    Draft202012Validator.check_schema(tool.outputSchema)


@pytest.mark.asyncio
async def test_reference_and_handoff_workflows_round_trip_through_mcp(tmp_path: Path) -> None:
    tools = _tools()
    client = _V08Client()
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    runtime = _runtime(tmp_path, client, store)

    card_result = await dispatch_tool(
        "get_kegg_entries",
        {
            "entries": [
                {"database": "ko", "identifier": "K00001"},
                {"database": "ko", "identifier": "K00002"},
            ],
            "projection": "card",
        },
        runtime,
    )
    assert card_result.isError is False
    card_data = _data(card_result)
    assert len(client.get_calls) == 1

    reference_output = tmp_path / "reference-bundle"
    calls_before_reference = len(client.get_calls)
    reference_result = await dispatch_tool(
        "write_kegg_reference_bundle",
        {
            "source": {"result_id": card_data["result"]["result_id"]},
            "entries": [{"database": "ko", "identifier": "K00001"}],
            "output_directory": str(reference_output),
        },
        runtime,
    )
    assert reference_result.isError is False
    _validate(tools["write_kegg_reference_bundle"], reference_result)
    assert len(client.get_calls) == calls_before_reference
    reference_data = _data(reference_result)
    assert reference_data["requested_entry_count"] == 1
    assert reference_data["returned_entry_count"] == 1
    _assert_private_bundle(
        reference_output,
        {
            "reference_snapshot.json",
            "relationships.tsv",
            "reference_manifest.json",
        },
    )
    reference_text = "\n".join(
        item.read_text(encoding="utf-8") for item in reference_output.iterdir()
    )
    assert "synthetic-private-request" not in reference_text
    assert PUBLIC_KEGG_ENDPOINT_LABEL not in reference_text

    external_cases: tuple[dict[str, object], ...] = (
        {
            "target": "mapper_reconstruct",
            "rows": [{"user_id": "sample-1", "ko_id": "K00001"}],
        },
        {"target": "mapper_search", "identifiers": ["K00001"]},
        {
            "target": "mapper_color",
            "rows": [{"identifier": "K00001", "background_color": "#ff0000"}],
        },
        {
            "target": "mapper_join",
            "mode": "ko",
            "rows": [{"identifier": "K00001", "attribute": "sample-1"}],
        },
        {
            "target": "mapper_mwsearch",
            "mode": "c_number",
            "values": ["C00031"],
        },
        {"target": "syntax_ko_composition", "ko_ids": ["K00001"]},
        {
            "target": "syntax_ko_sequence",
            "order_semantics": "caller_supplied_genomic_order",
            "rows": [{"gene_id": "gene-1", "ko_id": "K00001"}],
        },
    )
    for handoff in external_cases:
        output = tmp_path / f"handoff-{handoff['target']}"
        calls_before = len(client.get_calls)
        result = await dispatch_tool(
            "prepare_kegg_handoff",
            {"output_directory": str(output), "handoff": handoff},
            runtime,
        )
        assert result.isError is False, (handoff, _structured(result))
        _validate(tools["prepare_kegg_handoff"], result)
        assert len(client.get_calls) == calls_before
        data = _data(result)
        assert data["target"] == handoff["target"]
        manifest = json.loads(Path(data["manifest"]).read_text(encoding="utf-8"))
        assert manifest["execution_boundary"] == {
            "browser_started": False,
            "external_result_parsed": False,
            "external_tool_executed": False,
            "uploaded": False,
        }
        _assert_private_bundle(output, {Path(data["data_file"]).name, "handoff_manifest.json"})

    store.delete_scope(runtime.scope_id)
    assert reference_output.is_dir()
    assert all((tmp_path / f"handoff-{handoff['target']}").is_dir() for handoff in external_cases)


@pytest.mark.asyncio
async def test_handoff_rejects_occupied_output_without_any_kegg_request(
    tmp_path: Path,
) -> None:
    client = _V08Client()
    store = SQLiteResultStore(tmp_path / "results-preflight.sqlite3")
    runtime = _runtime(tmp_path, client, store)
    output = tmp_path / "occupied-handoff"
    output.mkdir(mode=0o700)
    sentinel = output / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")

    result = await dispatch_tool(
        "prepare_kegg_handoff",
        {
            "output_directory": str(output),
            "handoff": {
                "target": "syntax_ko_composition",
                "ko_ids": ["K00001"],
            },
        },
        runtime,
    )

    assert result.isError is True
    assert _structured(result)["error"]["code"] == ErrorCode.OUTPUT_ALREADY_EXISTS.value
    assert client.get_calls == []
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert {item.name for item in output.iterdir()} == {"caller-owned.txt"}


@pytest.mark.asyncio
async def test_v08_mcp_rejects_cross_scope_removed_enrichment_and_output_paths(
    tmp_path: Path,
) -> None:
    client = _V08Client()
    store = SQLiteResultStore(tmp_path / "results-invalid.sqlite3")
    runtime = _runtime(tmp_path, client, store, scope_id="source-scope")
    card_result = await dispatch_tool(
        "get_kegg_entries",
        {
            "entries": [{"database": "ko", "identifier": "K00001"}],
            "projection": "card",
        },
        runtime,
    )
    card_data = _data(card_result)

    wrong_scope = _runtime(tmp_path, client, store, scope_id="other-scope")
    cross_scope_output = tmp_path / "cross-scope"
    cross_scope = await dispatch_tool(
        "write_kegg_reference_bundle",
        {
            "source": {"result_id": card_data["result"]["result_id"]},
            "output_directory": str(cross_scope_output),
        },
        wrong_scope,
    )
    assert cross_scope.isError is True
    assert not cross_scope_output.exists()

    removed_enrichment_output = tmp_path / "removed-enrichment"
    removed_enrichment = await dispatch_tool(
        "prepare_kegg_handoff",
        {
            "output_directory": str(removed_enrichment_output),
            "handoff": {
                "target": "enrichment",
                "foreground": {"namespace": "ko", "identifiers": ["K00001"]},
                "universe": {"namespace": "ko", "identifiers": ["K00001"]},
            },
        },
        runtime,
    )
    assert removed_enrichment.isError is True
    assert (
        _structured(removed_enrichment)["error"]["code"]
        == ErrorCode.ANALYSIS_CONFIGURATION_INVALID.value
    )
    assert not removed_enrichment_output.exists()

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    disallowed = await dispatch_tool(
        "prepare_kegg_handoff",
        {
            "output_directory": str(outside),
            "handoff": {
                "target": "syntax_ko_composition",
                "ko_ids": ["K00001"],
            },
        },
        runtime,
    )
    assert disallowed.isError is True
    assert not outside.exists()
