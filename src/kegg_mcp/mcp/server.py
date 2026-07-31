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
    "This core server supports bounded KEGG entity search, typed entry cards, local reference-"
    "snapshot comparison, identifier resolution, relation tracing, BRITE hierarchy mapping, and "
    "annotation-mapping audits. It can project only PubMed identifiers explicitly listed in KEGG, "
    "persist selected current-session card snapshots as durable reference bundles, and prepare "
    "local KEGG Mapper or KEGG Syntax input bundles. It does not retrieve or interpret papers, "
    "upload files, execute external KEGG tools, or parse their results. For analysis it accepts "
    "existing K numbers or supported KO "
    "annotation evidence, not raw protein FASTA. If the only biological input is "
    "protein FASTA, do not call a core analysis tool. If the user explicitly selected another "
    "annotator, wait for its supported KO evidence "
    "and resume this core server only after that evidence is available. Otherwise prefer the "
    "installed deepkoala-annotation Skill and deepkoala-mcp as the first FASTA annotation route. "
    "If either is unavailable, stop, report an incomplete suite deployment, and request explicit "
    "permission once to install or repair the complete repository suite. If the user declines that "
    "action, remain stopped until a user-selected route supplies supported KO evidence. After the "
    "suite action succeeds, preserve the original downstream goals and continue the requested "
    "stages in a new Codex task: DeepKOALA annotation, then core KO analysis, then rendering when "
    "graphics were requested. Pathway graphics require the independent "
    "kegg-pathway-rendering Skill and kegg-render-mcp renderer. Prefer "
    "analyze_ko_annotations for an existing KO list or annotation table. Call "
    "get_server_status first when deployment state is unknown, and call probe_kegg_connectivity "
    "before the first network-dependent analysis. File paths and output directories require a "
    "configured allowed root. Result IDs are valid only in the same stdio process; use a committed "
    "output, reference, or handoff bundle for cross-process use. K-number assignments are "
    "annotations, exact MODULE "
    "completion is separate from block coverage, and pathway KO coverage does not prove pathway "
    "presence, activity, flux, or phenotype. This server never runs an external annotator or "
    "renders pathway graphics."
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
