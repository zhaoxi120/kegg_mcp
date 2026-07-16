"""Process-local dependencies for the core MCP transport."""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from kegg_mcp.kegg import KeggClient
from kegg_mcp.mcp.config import McpRuntimeConfig, load_runtime_config
from kegg_mcp.services import KeggPrimitiveClient, SQLiteResultStore


@dataclass(frozen=True, slots=True)
class McpRuntime:
    """Injected services and one opaque stdio result scope."""

    client: KeggPrimitiveClient
    result_store: SQLiteResultStore
    scope_id: str
    allowed_roots: tuple[str, ...] = ()


def build_runtime(config: McpRuntimeConfig | None = None) -> McpRuntime:
    """Construct the default user-local runtime."""
    effective = config or load_runtime_config()
    return McpRuntime(
        client=KeggClient(effective.kegg),
        result_store=SQLiteResultStore(effective.result_store_path),
        scope_id=f"stdio-{secrets.token_urlsafe(24)}",
        allowed_roots=effective.allowed_roots,
    )


__all__ = ["McpRuntime", "build_runtime"]
