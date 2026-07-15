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
    PublicAcademicAccess,
)

ACCESS_MODE_ENV = "KEGG_MCP_ACCESS_MODE"
ACADEMIC_CONFIRMATION_ENV = "KEGG_MCP_ACADEMIC_USE_CONFIRMED"
LICENSED_ENDPOINT_ENV = "KEGG_MCP_LICENSED_ENDPOINT"
LICENSED_CONFIRMATION_ENV = "KEGG_MCP_LICENSED_USE_CONFIRMED"
CACHE_PATH_ENV = "KEGG_MCP_CACHE_PATH"
RESULT_STORE_PATH_ENV = "KEGG_MCP_RESULT_STORE_PATH"
ALLOWED_ROOTS_ENV = "KEGG_MCP_ALLOWED_ROOTS"


class McpRuntimeConfig(BaseModel):
    """Validated server dependencies without exposing paths through status."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kegg: KeggClientConfig
    result_store_path: str
    allowed_roots: tuple[str, ...] = ()


def default_result_store_path(environment: Mapping[str, str] | None = None) -> str:
    """Return a user-local result path without creating it."""
    values = os.environ if environment is None else environment
    cache_home = values.get("XDG_CACHE_HOME")
    root = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
    return str(root / "kegg-mcp" / "results.sqlite3")


def load_runtime_config(environment: Mapping[str, str] | None = None) -> McpRuntimeConfig:
    """Load the small environment contract with public academic access by default."""
    values = os.environ if environment is None else environment
    raw_mode = values.get(ACCESS_MODE_ENV, AccessMode.PUBLIC_ACADEMIC.value)
    try:
        mode = AccessMode(raw_mode)
    except ValueError as error:
        raise ValueError(f"{ACCESS_MODE_ENV} must be public_academic or licensed") from error

    if mode is AccessMode.PUBLIC_ACADEMIC:
        if values.get(ACADEMIC_CONFIRMATION_ENV, "true") != "true":
            raise ValueError(
                f"{ACADEMIC_CONFIRMATION_ENV}=true is required for public_academic access"
            )
        access = PublicAcademicAccess(academic_use_confirmed=True)
    else:
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
    cache_path = values.get(CACHE_PATH_ENV)
    cache = CachePolicy(path=cache_path) if cache_path is not None else CachePolicy()
    result_path = values.get(RESULT_STORE_PATH_ENV, default_result_store_path(values))
    allowed_roots = _load_allowed_roots(values.get(ALLOWED_ROOTS_ENV))
    return McpRuntimeConfig(
        kegg=KeggClientConfig(access=access, cache=cache),
        result_store_path=result_path,
        allowed_roots=allowed_roots,
    )


def _load_allowed_roots(raw_value: str | None) -> tuple[str, ...]:
    if raw_value is None or not raw_value.strip():
        return ()
    roots: list[str] = []
    for raw_root in raw_value.split(os.pathsep):
        if not raw_root:
            raise ValueError(f"{ALLOWED_ROOTS_ENV} contains an empty path")
        path = Path(raw_root).expanduser()
        if not path.is_absolute():
            raise ValueError(f"{ALLOWED_ROOTS_ENV} paths must be absolute")
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"{ALLOWED_ROOTS_ENV} paths must exist") from error
        if not resolved.is_dir():
            raise ValueError(f"{ALLOWED_ROOTS_ENV} paths must be directories")
        value = str(resolved)
        if value not in roots:
            roots.append(value)
    return tuple(roots)


__all__ = [
    "ACADEMIC_CONFIRMATION_ENV",
    "ACCESS_MODE_ENV",
    "ALLOWED_ROOTS_ENV",
    "CACHE_PATH_ENV",
    "LICENSED_CONFIRMATION_ENV",
    "LICENSED_ENDPOINT_ENV",
    "RESULT_STORE_PATH_ENV",
    "McpRuntimeConfig",
    "default_result_store_path",
    "load_runtime_config",
]
