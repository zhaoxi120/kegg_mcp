"""The executable stdio channel contains protocol traffic only."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kegg_mcp.mcp.server import SERVER_INSTRUCTIONS


def test_server_instructions_fail_closed_for_fasta_only_workflows() -> None:
    assert "not raw protein FASTA" in SERVER_INSTRUCTIONS
    explicit_alternative = SERVER_INSTRUCTIONS.index("explicitly selected another annotator")
    preferred_route = SERVER_INSTRUCTIONS.index("prefer the installed deepkoala-annotation Skill")
    assert explicit_alternative < preferred_route
    assert "prefer the installed deepkoala-annotation Skill" in SERVER_INSTRUCTIONS
    assert "as the first FASTA annotation route" in SERVER_INSTRUCTIONS
    assert "report an incomplete suite deployment" in SERVER_INSTRUCTIONS
    assert "request explicit permission once to install or repair" in SERVER_INSTRUCTIONS
    assert "in a new Codex task" in SERVER_INSTRUCTIONS
    annotation = SERVER_INSTRUCTIONS.index("DeepKOALA annotation")
    analysis = SERVER_INSTRUCTIONS.index("core KO analysis")
    rendering = SERVER_INSTRUCTIONS.index("rendering when graphics were requested")
    assert annotation < analysis < rendering
    assert "remain stopped until a user-selected route supplies" in SERVER_INSTRUCTIONS
    assert "kegg-pathway-rendering Skill" in SERVER_INSTRUCTIONS
    assert "kegg-render-mcp renderer" in SERVER_INSTRUCTIONS


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
        assert len(tool_names) == 15
        assert {
            "search_kegg_entries",
            "resolve_kegg_entities",
            "trace_kegg_relations",
            "map_brite_hierarchy",
            "audit_annotation_mapping",
        } <= tool_names
        normalized = await session.call_tool("normalize_ko_annotations", {"text": "K00844"})
        assert normalized.isError is False

    with sqlite3.connect(tmp_path / "results.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM stored_results").fetchone() == (0,)
