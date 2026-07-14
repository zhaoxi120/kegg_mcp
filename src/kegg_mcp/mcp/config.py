"""Environment-only local runtime configuration for the stdio MCP server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from kegg_mcp.kegg import (
    AccessMode,
    CachePolicy,
    KeggClientConfig,
    LicensedAccess,
    OfflineCacheAccess,
    PublicAcademicAccess,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.contracts import endpoint_fingerprint

ACCESS_MODE_ENV = "KEGG_MCP_ACCESS_MODE"
ACADEMIC_CONFIRMATION_ENV = "KEGG_MCP_ACADEMIC_USE_CONFIRMED"
LICENSED_ENDPOINT_ENV = "KEGG_MCP_LICENSED_ENDPOINT"
LICENSED_CONFIRMATION_ENV = "KEGG_MCP_LICENSED_USE_CONFIRMED"
CACHE_PATH_ENV = "KEGG_MCP_CACHE_PATH"
RESULT_STORE_PATH_ENV = "KEGG_MCP_RESULT_STORE_PATH"


class McpRuntimeConfig(BaseModel):
    """Validated server dependencies without exposing paths through status."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kegg: KeggClientConfig
    result_store_path: str


def default_result_store_path(environment: Mapping[str, str] | None = None) -> str:
    """Return a user-local result path without creating it."""
    values = os.environ if environment is None else environment
    cache_home = values.get("XDG_CACHE_HOME")
    root = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
    return str(root / "kegg-mcp" / "results.sqlite3")


def load_runtime_config(environment: Mapping[str, str] | None = None) -> McpRuntimeConfig:
    """Load the small documented environment contract with offline-safe defaults."""
    values = os.environ if environment is None else environment
    raw_mode = values.get(ACCESS_MODE_ENV, AccessMode.OFFLINE_CACHE.value)
    try:
        mode = AccessMode(raw_mode)
    except ValueError as error:
        raise ValueError(
            f"{ACCESS_MODE_ENV} must be offline_cache, public_academic, or licensed"
        ) from error

    if mode is AccessMode.PUBLIC_ACADEMIC:
        if values.get(ACADEMIC_CONFIRMATION_ENV) != "true":
            raise ValueError(
                f"{ACADEMIC_CONFIRMATION_ENV}=true is required for public_academic access"
            )
        access = PublicAcademicAccess(academic_use_confirmed=True)
    elif mode is AccessMode.LICENSED:
        if values.get(LICENSED_CONFIRMATION_ENV) != "true":
            raise ValueError(f"{LICENSED_CONFIRMATION_ENV}=true is required for licensed access")
        endpoint = values.get(LICENSED_ENDPOINT_ENV)
        if endpoint is None:
            raise ValueError(f"{LICENSED_ENDPOINT_ENV} is required for licensed access")
        access = LicensedAccess(
            authorized_use_confirmed=True,
            endpoint=endpoint,
            endpoint_label="licensed-endpoint",
        )
    else:
        licensed_endpoint = values.get(LICENSED_ENDPOINT_ENV)
        licensed_confirmation = values.get(LICENSED_CONFIRMATION_ENV)
        if licensed_endpoint is None and licensed_confirmation is None:
            access = OfflineCacheAccess()
        elif licensed_endpoint is None or licensed_confirmation != "true":
            raise ValueError(
                "offline licensed-cache reuse requires both licensed endpoint and confirmation"
            )
        else:
            licensed = LicensedAccess(
                authorized_use_confirmed=True,
                endpoint=licensed_endpoint,
                endpoint_label="licensed-endpoint",
            )
            access = OfflineCacheAccess(
                retrieval_endpoint_class=RetrievalEndpointClass.LICENSED,
                endpoint_fingerprint=endpoint_fingerprint(licensed.endpoint),
            )

    cache_path = values.get(CACHE_PATH_ENV)
    cache = CachePolicy(path=cache_path) if cache_path is not None else CachePolicy()
    result_path = values.get(RESULT_STORE_PATH_ENV, default_result_store_path(values))
    return McpRuntimeConfig(
        kegg=KeggClientConfig(access=access, cache=cache),
        result_store_path=result_path,
    )


__all__ = [
    "ACADEMIC_CONFIRMATION_ENV",
    "ACCESS_MODE_ENV",
    "CACHE_PATH_ENV",
    "LICENSED_CONFIRMATION_ENV",
    "LICENSED_ENDPOINT_ENV",
    "RESULT_STORE_PATH_ENV",
    "McpRuntimeConfig",
    "default_result_store_path",
    "load_runtime_config",
]
