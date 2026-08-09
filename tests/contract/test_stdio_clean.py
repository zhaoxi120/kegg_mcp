"""The executable stdio channel contains protocol traffic only."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kegg_mcp.mcp.server import SERVER_INSTRUCTIONS


def test_server_instructions_are_concise_and_core_scoped() -> None:
    assert 150 <= len(SERVER_INSTRUCTIONS.split()) <= 250
    for required in (
        "Raw protein FASTA is not a Core analysis input",
        "sorted unique accepted-KO view",
        "KEGG-listed PubMed identifiers",
        "KEGG Mapper or KEGG Syntax input bundles",
        "normalize_ko_annotations",
        "Result identifiers are scoped to the current stdio process",
        "does not establish presence, activity, flux, phenotype, or enrichment",
        "never executes annotators",
        "or renders graphics",
    ):
        assert required in SERVER_INSTRUCTIONS
    for orchestration_detail in (
        "install or repair",
        "plugin inventories",
        "restart Codex",
        "new Codex task",
        "deepkoala-annotation Skill",
        "kegg-render-mcp",
    ):
        assert orchestration_detail not in SERVER_INSTRUCTIONS


@pytest.mark.asyncio
async def test_stdio_process_initializes_and_lists_tools_without_noise(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["KEGG_MCP_ACCESS_MODE"] = "public_academic"
    environment["KEGG_MCP_ACADEMIC_USE_CONFIRMED"] = "true"
    environment["KEGG_MCP_CACHE_PATH"] = str(tmp_path / "cache.sqlite3")
    environment["KEGG_MCP_RESULT_STORE_PATH"] = str(tmp_path / "results.sqlite3")
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
        assert initialized.instructions == SERVER_INSTRUCTIONS
        tools = await session.list_tools()
        tool_names = {tool.name for tool in tools.tools}
        assert len(tool_names) == 18
        assert {
            "search_kegg_entries",
            "resolve_kegg_entities",
            "trace_kegg_relations",
            "map_brite_hierarchy",
            "audit_annotation_mapping",
            "compare_kegg_reference_snapshots",
            "write_kegg_reference_bundle",
            "prepare_kegg_handoff",
        } <= tool_names
        normalized = await session.call_tool("normalize_ko_annotations", {"text": "K00844"})
        assert normalized.isError is False

    with sqlite3.connect(tmp_path / "results.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM stored_results").fetchone() == (0,)


@pytest.mark.asyncio
async def test_stdio_query_rejects_unwritable_result_store_before_network(tmp_path: Path) -> None:
    invalid_store = tmp_path / "result-store-is-a-directory"
    invalid_store.mkdir(mode=0o700)
    cache_path = tmp_path / "unused-cache.sqlite3"
    rate_root = tmp_path / "unused-rate-limit"
    environment = dict(os.environ)
    environment.update(
        {
            "KEGG_MCP_ACCESS_MODE": "public_academic",
            "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "true",
            "KEGG_MCP_CACHE_PATH": str(cache_path),
            "KEGG_MCP_RESULT_STORE_PATH": str(invalid_store),
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
        await session.initialize()
        result = await session.call_tool(
            "search_kegg_entries",
            {
                "database": "ko",
                "query": "hexokinase",
                "mode": "keyword",
                "max_results": 1,
            },
        )

    assert result.isError is True
    assert isinstance(result.structuredContent, dict)
    assert result.structuredContent["error"]["code"] == "RESULT_STORE_FAILED"
    assert not cache_path.exists()
    assert not rate_root.exists()
