"""Process-local dependencies for the core MCP transport."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from anyio import CapacityLimiter

from kegg_mcp.kegg import KeggClient
from kegg_mcp.mcp.config import McpRuntimeConfig, load_runtime_config
from kegg_mcp.services.models import ConnectivityProbeResult
from kegg_mcp.services.reference_budget import KeggMcpClient
from kegg_mcp.services.result_store import SQLiteResultStore


@dataclass(slots=True)
class McpRuntime:
    """Injected services, one opaque stdio result scope, and process-local probe state."""

    client: KeggMcpClient
    result_store: SQLiteResultStore
    scope_id: str
    allowed_roots: tuple[str, ...] = ()
    last_connectivity_probe: ConnectivityProbeResult | None = None
    client_handler_limiter: CapacityLimiter = field(
        default_factory=lambda: CapacityLimiter(1),
        init=False,
        repr=False,
        compare=False,
    )
    local_handler_limiter: CapacityLimiter = field(
        default_factory=lambda: CapacityLimiter(4),
        init=False,
        repr=False,
        compare=False,
    )


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
