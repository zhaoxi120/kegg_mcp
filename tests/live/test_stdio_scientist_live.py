"""Manual live acceptance check through the installed-style stdio MCP process."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import sys
from pathlib import Path
from typing import cast

import pytest
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import AnyUrl

pytestmark = [
    pytest.mark.live_kegg,
    pytest.mark.skipif(
        os.environ.get("KEGG_MCP_RUN_LIVE_STDIO_E2E", "").lower() != "true",
        reason=(
            "set KEGG_MCP_RUN_LIVE_STDIO_E2E=true for the "
            "two-logical-operation stdio acceptance check"
        ),
    ),
]


def _successful_payload(result: types.CallToolResult) -> tuple[dict[str, object], str | None]:
    assert result.isError is False
    structured = result.structuredContent
    assert isinstance(structured, dict)
    envelope = cast(dict[str, object], structured)
    assert envelope["ok"] is True
    payload = envelope["result"]
    assert isinstance(payload, dict)
    payload_mapping = cast(dict[str, object], payload)
    data = payload_mapping["data"]
    assert isinstance(data, dict)
    resource_uri = payload_mapping["resource_uri"]
    assert resource_uri is None or isinstance(resource_uri, str)
    return cast(dict[str, object], data), resource_uri


async def _read_all_text_sections(session: ClientSession, resource_uri: str) -> None:
    root = await session.read_resource(AnyUrl(resource_uri))
    root_content = root.contents[0]
    assert isinstance(root_content, types.TextResourceContents)
    index = cast(dict[str, object], json.loads(root_content.text))
    section_uris = index["section_uris"]
    assert isinstance(section_uris, list)
    assert section_uris
    for section_uri in cast(list[object], section_uris):
        assert isinstance(section_uri, str)
        section = await session.read_resource(AnyUrl(section_uri))
        content = section.contents[0]
        assert isinstance(content, types.TextResourceContents)
        assert content.text


def _assert_owner_only(path: Path) -> None:
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


def _assert_output_bundle(path: Path, expected_names: set[str]) -> None:
    assert path.is_dir()
    assert not path.is_symlink()
    _assert_owner_only(path)
    assert {item.name for item in path.iterdir()} == expected_names
    for item in path.iterdir():
        assert item.is_file()
        assert not item.is_symlink()
        assert stat.S_IMODE(item.stat().st_mode) == 0o600


def _cache_snapshot(path: Path) -> tuple[tuple[object, ...], ...]:
    """Capture the complete bounded cache state around local-only handoffs."""
    with sqlite3.connect(path) as connection:
        return tuple(
            connection.execute(
                """
                SELECT operation, normalized_request_key, endpoint_class,
                       endpoint_fingerprint, response_body, retrieved_at, expires_at,
                       parser_version, database_release, http_metadata_json
                FROM kegg_responses
                ORDER BY operation, normalized_request_key, endpoint_class, endpoint_fingerprint
                """
            ).fetchall()
        )


def _local_handoff_cases() -> tuple[tuple[str, dict[str, object], str], ...]:
    return (
        (
            "mapper_reconstruct",
            {
                "target": "mapper_reconstruct",
                "rows": [{"user_id": "stdio-live", "ko_id": "K00844"}],
            },
            "mapper_reconstruct.tsv",
        ),
        (
            "mapper_search",
            {"target": "mapper_search", "identifiers": ["K00844"]},
            "mapper_search.txt",
        ),
        (
            "mapper_color",
            {
                "target": "mapper_color",
                "rows": [
                    {
                        "identifier": "K00844",
                        "background_color": "#ff0000",
                    }
                ],
            },
            "mapper_color.tsv",
        ),
        (
            "mapper_join",
            {
                "target": "mapper_join",
                "mode": "ko",
                "rows": [{"identifier": "K00844", "attribute": "glycolysis"}],
            },
            "mapper_join.tsv",
        ),
        (
            "mapper_mwsearch",
            {
                "target": "mapper_mwsearch",
                "mode": "c_number",
                "values": ["C00031"],
            },
            "mapper_mwsearch.txt",
        ),
        (
            "syntax_ko_composition",
            {"target": "syntax_ko_composition", "ko_ids": ["K00844"]},
            "syntax_ko_composition.txt",
        ),
        (
            "syntax_ko_sequence",
            {
                "target": "syntax_ko_sequence",
                "order_semantics": "caller_supplied_genomic_order",
                "rows": [{"gene_id": "gene-1", "ko_id": "K00844"}],
            },
            "syntax_ko_sequence.tsv",
        ),
    )


@pytest.mark.asyncio
async def test_live_stdio_card_query_uses_private_local_state_and_cleans_scope(
    tmp_path: Path,
) -> None:
    """Exercise configuration, live network, retained reads, and normal-exit cleanup."""
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    cache_path = state_root / "cache.sqlite3"
    result_path = state_root / "results.sqlite3"
    rate_root = state_root / "rate-limit"
    output_root = tmp_path / "outputs"
    output_root.mkdir(mode=0o700)
    reference_output = output_root / "reference-bundle"
    local_handoff_outputs = {
        target: output_root / target for target, _, _ in _local_handoff_cases()
    }
    environment = dict(os.environ)
    environment.update(
        {
            "KEGG_MCP_ACCESS_MODE": "public_academic",
            "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "true",
            "KEGG_MCP_CACHE_PATH": str(cache_path),
            "KEGG_MCP_RESULT_STORE_PATH": str(result_path),
            "KEGG_MCP_RATE_LIMIT_ROOT": str(rate_root),
            "KEGG_MCP_ALLOWED_ROOTS": str(output_root),
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from kegg_mcp.mcp.cli import main; main()"],
        env=environment,
    )

    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        initialized = await session.initialize()
        assert initialized.serverInfo.name == "kegg-mcp"

        status, status_uri = _successful_payload(await session.call_tool("get_server_status", {}))
        assert status["network_enabled"] is True
        assert status_uri is None

        probe, probe_uri = _successful_payload(
            await session.call_tool("probe_kegg_connectivity", {})
        )
        assert probe["state"] == "reachable"
        assert probe_uri is None

        cards, cards_uri = _successful_payload(
            await session.call_tool(
                "get_kegg_entries",
                {
                    "entries": [
                        {"database": "ko", "identifier": "K00844"},
                        {"database": "reaction", "identifier": "R01786"},
                    ],
                    "projection": "card",
                },
            )
        )
        assert cards["projection"] == "card"
        assert cards["returned_count"] == 2
        assert isinstance(cards_uri, str)
        await _read_all_text_sections(session, cards_uri)

        references, references_uri = _successful_payload(
            await session.call_tool(
                "get_kegg_entries",
                {
                    "entries": [
                        {"database": "ko", "identifier": "K00844"},
                        {"database": "reaction", "identifier": "R01786"},
                    ],
                    "projection": "references",
                },
            )
        )
        assert references["projection"] == "references"
        assert any(
            item["origin"] == "cache"
            for item in cast(list[dict[str, object]], references["provenance"])
        )
        assert isinstance(references_uri, str)
        await _read_all_text_sections(session, references_uri)

        cache_before_local_writes = _cache_snapshot(cache_path)
        card_result = cast(dict[str, object], cards["result"])
        reference_bundle, reference_uri = _successful_payload(
            await session.call_tool(
                "write_kegg_reference_bundle",
                {
                    "source": {"result_id": card_result["result_id"]},
                    "output_directory": str(reference_output),
                },
            )
        )
        assert reference_uri is None
        assert reference_bundle["returned_entry_count"] == 2
        assert Path(cast(str, reference_bundle["output_directory"])) == reference_output
        _assert_output_bundle(
            reference_output,
            {
                "entities.json",
                "relationships.tsv",
                "brite_paths.tsv",
                "retrieval_provenance.json",
                "request_contract.json",
                "reference_manifest.json",
            },
        )

        for target, handoff, data_name in _local_handoff_cases():
            output_directory = local_handoff_outputs[target]
            local_handoff, local_handoff_uri = _successful_payload(
                await session.call_tool(
                    "prepare_kegg_handoff",
                    {
                        "output_directory": str(output_directory),
                        "handoff": handoff,
                    },
                )
            )
            assert local_handoff_uri is None
            assert local_handoff["target"] == target
            assert Path(cast(str, local_handoff["output_directory"])) == output_directory
            assert Path(cast(str, local_handoff["data_file"])) == output_directory / data_name
            _assert_output_bundle(
                output_directory,
                {data_name, "handoff_manifest.json"},
            )
            manifest = cast(
                dict[str, object],
                json.loads(
                    (output_directory / "handoff_manifest.json").read_text(encoding="utf-8")
                ),
            )
            assert manifest["target"] == target
            assert manifest["execution_boundary"] == {
                "browser_started": False,
                "external_result_parsed": False,
                "external_tool_executed": False,
                "uploaded": False,
            }

        assert _cache_snapshot(cache_path) == cache_before_local_writes

    assert cache_path.is_file()
    assert result_path.is_file()
    assert rate_root.is_dir()
    _assert_owner_only(state_root)
    _assert_owner_only(output_root)
    _assert_owner_only(cache_path)
    _assert_owner_only(result_path)
    _assert_owner_only(rate_root)
    state_files = tuple(rate_root.iterdir())
    assert len(state_files) == 1
    _assert_owner_only(state_files[0])
    with sqlite3.connect(result_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stored_results").fetchone() == (0,)
    _assert_output_bundle(
        reference_output,
        {
            "entities.json",
            "relationships.tsv",
            "brite_paths.tsv",
            "retrieval_provenance.json",
            "request_contract.json",
            "reference_manifest.json",
        },
    )
    for target, _, data_name in _local_handoff_cases():
        _assert_output_bundle(
            local_handoff_outputs[target],
            {data_name, "handoff_manifest.json"},
        )
