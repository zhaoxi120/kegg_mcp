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
    environment = dict(os.environ)
    environment.update(
        {
            "KEGG_MCP_ACCESS_MODE": "public_academic",
            "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "true",
            "KEGG_MCP_CACHE_PATH": str(cache_path),
            "KEGG_MCP_RESULT_STORE_PATH": str(result_path),
            "KEGG_MCP_RATE_LIMIT_ROOT": str(rate_root),
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

    assert cache_path.is_file()
    assert result_path.is_file()
    assert rate_root.is_dir()
    _assert_owner_only(state_root)
    _assert_owner_only(cache_path)
    _assert_owner_only(result_path)
    _assert_owner_only(rate_root)
    state_files = tuple(rate_root.iterdir())
    assert len(state_files) == 1
    _assert_owner_only(state_files[0])
    with sqlite3.connect(result_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stored_results").fetchone() == (0,)
