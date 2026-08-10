"""Low-level stdio composition for the core MCP server."""

from __future__ import annotations

import sys
from typing import Any

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from kegg_mcp import __version__
from kegg_mcp.kegg.rate_limit import (
    UNSUPPORTED_PLATFORM_DIAGNOSTIC,
    UnsupportedRuntimePlatformError,
)
from kegg_mcp.mcp.resources import (
    MAX_INLINE_RESOURCE_BYTES,
    read_resource,
    resource_definitions,
    resource_templates,
)
from kegg_mcp.mcp.runtime import McpRuntime, build_runtime
from kegg_mcp.mcp.tool_registry import TOOL_NAMES, dispatch_tool, tool_definitions
from kegg_mcp.services.result_store import ResultStoreError

SERVER_NAME = "kegg-mcp"
SERVER_INSTRUCTIONS = (
    "This core server provides bounded KEGG entity search, typed entry cards, KEGG-listed PubMed "
    "identifiers, identifier resolution, typed relation tracing, BRITE mapping, annotation-mapping "
    "audits, selected-reference bundles, and validated KEGG Mapper or KEGG Syntax input bundles. "
    "It also normalizes supplied K numbers or supported KO annotation evidence and evaluates "
    "MODULE logic, descriptive pathway KO coverage, and deterministic KO-set comparisons. Prefer "
    "analyze_ko_annotations for a complete high-level workflow. It derives one compact sorted "
    "unique accepted-KO view; use normalize_ko_annotations or the audit workflow when record-level "
    "evidence, protein-to-KO mappings, or duplicate/conflict accounting are required. Raw protein "
    "FASTA is not a Core analysis input; obtain supported KO evidence from an independently "
    "configured annotator before calling analysis tools. Call get_server_status when deployment "
    "state is unknown and probe_kegg_connectivity before network-dependent work. File inputs and "
    "output directories must be inside configured allowed roots. Result identifiers are scoped to "
    "the current stdio process; use a committed output, reference, or handoff bundle across "
    "processes. Search matches are candidates, relationships are database cross-references, "
    "K-number assignments are annotations, exact MODULE completion is separate from block "
    "coverage, and pathway KO coverage does not establish presence, activity, flux, phenotype, "
    "or enrichment. "
    "This server never executes annotators, retrieves or interprets papers, uploads files, runs "
    "external KEGG tools, parses their results, or renders graphics."
)


def create_server(runtime: McpRuntime | None = None) -> Server[object]:
    """Compose one MCP server around injected process-local dependencies."""
    state = runtime or build_runtime()
    server: Server[object] = Server(
        SERVER_NAME,
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
    )

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:  # pyright: ignore[reportUnusedFunction]
        return tool_definitions()

    @server.call_tool(validate_input=False)
    async def _call_tool(  # pyright: ignore[reportUnusedFunction]
        name: str,
        arguments: dict[str, Any],
    ) -> types.CallToolResult:
        return await dispatch_tool(name, arguments, state)

    @server.list_resources()
    async def _list_resources() -> list[types.Resource]:  # pyright: ignore[reportUnusedFunction]
        return resource_definitions()

    @server.list_resource_templates()
    async def _list_resource_templates(  # pyright: ignore[reportUnusedFunction]
    ) -> list[types.ResourceTemplate]:
        return resource_templates()

    @server.read_resource()
    async def _read_resource(uri: AnyUrl):  # pyright: ignore[reportUnusedFunction]
        return await read_resource(uri, state)

    original_call_handler = server.request_handlers[types.CallToolRequest]

    async def _reject_unknown_tools(request: types.CallToolRequest) -> types.ServerResult:
        if request.params.name not in TOOL_NAMES:
            raise McpError(
                types.ErrorData(code=types.INVALID_PARAMS, message="Unknown MCP tool name.")
            )
        return await original_call_handler(request)

    server.request_handlers[types.CallToolRequest] = _reject_unknown_tools
    return server


async def _run_stdio() -> None:
    runtime = build_runtime()
    server = create_server(runtime)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )
    finally:
        try:
            runtime.result_store.delete_scope(runtime.scope_id)
        except ResultStoreError:
            print(
                "KEGG MCP could not clear session-scoped retained results during shutdown.",
                file=sys.stderr,
            )


def main() -> None:
    """Run the local stdio server without application output on stdout."""
    try:
        anyio.run(_run_stdio)
    except UnsupportedRuntimePlatformError:
        print(
            f"kegg-mcp startup failed: {UNSUPPORTED_PLATFORM_DIAGNOSTIC}",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except (OSError, ValueError) as error:
        raise SystemExit(
            "Invalid KEGG MCP local configuration; review the documented environment variables."
        ) from error


__all__ = [
    "MAX_INLINE_RESOURCE_BYTES",
    "SERVER_INSTRUCTIONS",
    "SERVER_NAME",
    "TOOL_NAMES",
    "McpRuntime",
    "build_runtime",
    "create_server",
    "main",
]
