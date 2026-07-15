"""The executable stdio channel contains protocol traffic only."""

import os
import sys
from pathlib import Path

import pytest
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kegg_mcp.mcp.server import SERVER_INSTRUCTIONS


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
        assert len(tools.tools) == 9
