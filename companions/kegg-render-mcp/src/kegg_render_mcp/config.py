"""Small environment-only configuration for the renderer companion."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self, cast

from kegg_mcp.kegg import CachePolicy, LicensedAccess, RateLimitPolicy
from kegg_mcp.kegg.contracts import default_rate_limit_root
from kegg_mcp.services.render_contracts import (
    MODULE_RENDER_MAX_CANVAS_PIXELS,
    MODULE_RENDER_MAX_SVG_NODES,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from kegg_render_mcp._filesystem import open_absolute_directory

ENV_PREFIX = "KEGG_RENDER_MCP_"
STATE_ROOT_ENV = f"{ENV_PREFIX}STATE_ROOT"
ALLOWED_ROOTS_ENV = f"{ENV_PREFIX}ALLOWED_ROOTS"
ACCESS_MODE_ENV = f"{ENV_PREFIX}ACCESS_MODE"
ACADEMIC_CONFIRMATION_ENV = f"{ENV_PREFIX}ACADEMIC_USE_CONFIRMED"
LICENSED_ENDPOINT_ENV = f"{ENV_PREFIX}LICENSED_ENDPOINT"
LICENSED_CONFIRMATION_ENV = f"{ENV_PREFIX}LICENSED_USE_CONFIRMED"
CACHE_PATH_ENV = f"{ENV_PREFIX}CACHE_PATH"
OFFLINE_ALLOW_STALE_ENV = f"{ENV_PREFIX}OFFLINE_ALLOW_STALE"
RETENTION_SECONDS_ENV = f"{ENV_PREFIX}RETENTION_SECONDS"
MAX_DISK_BYTES_ENV = f"{ENV_PREFIX}MAX_DISK_BYTES"
MAX_RESULTS_ENV = f"{ENV_PREFIX}MAX_RESULTS"
RATE_LIMIT_ROOT_ENV = "KEGG_MCP_RATE_LIMIT_ROOT"

DEFAULT_RETENTION_SECONDS = 86_400
DEFAULT_MAX_INPUT_BYTES = 50_000_000
DEFAULT_MAX_ASSET_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PIXELS = 20_000_000
DEFAULT_MAX_SVG_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_RESULT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_DISK_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_RESULTS = 128

RendererAccessMode = Literal["public_academic", "licensed", "offline_cache", "unconfigured"]


class RendererLimits(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True, hide_input_in_errors=True
    )

    max_input_bytes: int = Field(default=DEFAULT_MAX_INPUT_BYTES, ge=1, le=64 * 1024 * 1024)
    max_asset_bytes: int = Field(default=DEFAULT_MAX_ASSET_BYTES, ge=1, le=50_000_000)
    max_pixels: int = Field(
        default=DEFAULT_MAX_PIXELS,
        ge=MODULE_RENDER_MAX_CANVAS_PIXELS,
        le=100_000_000,
    )
    max_svg_bytes: int = Field(default=DEFAULT_MAX_SVG_BYTES, ge=1, le=64 * 1024 * 1024)
    max_result_bytes: int = Field(default=DEFAULT_MAX_RESULT_BYTES, ge=1, le=256 * 1024 * 1024)
    max_disk_bytes: int = Field(default=DEFAULT_MAX_DISK_BYTES, ge=1, le=2 * 1024 * 1024 * 1024)
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=4_096)
    max_xml_elements: int = Field(default=20_000, ge=1, le=100_000)
    max_xml_attributes: int = Field(default=100_000, ge=1, le=500_000)
    max_xml_depth: int = Field(default=32, ge=1, le=128)
    max_svg_nodes: int = Field(
        default=50_000,
        ge=MODULE_RENDER_MAX_SVG_NODES,
        le=200_000,
    )

    @model_validator(mode="after")
    def related_bounds(self) -> Self:
        if self.max_asset_bytes > self.max_result_bytes:
            raise ValueError("max_asset_bytes must not exceed max_result_bytes")
        if self.max_svg_bytes > self.max_result_bytes:
            raise ValueError("max_svg_bytes must not exceed max_result_bytes")
        if self.max_result_bytes > self.max_disk_bytes:
            raise ValueError("max_result_bytes must not exceed max_disk_bytes")
        return self


class RendererRuntimeConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True, hide_input_in_errors=True
    )

    state_root: Path
    allowed_roots: tuple[Path, ...]
    access_mode: RendererAccessMode = "public_academic"
    licensed_endpoint: str | None = Field(default=None, min_length=1, max_length=2048, repr=False)
    cache_path: Path | None = Field(default=None, repr=False)
    offline_allow_stale: bool = False
    retention_seconds: int = Field(default=DEFAULT_RETENTION_SECONDS, ge=1, le=2_592_000)
    rate_limit_root: str = Field(default_factory=default_rate_limit_root)
    limits: RendererLimits = RendererLimits()

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        _validate_posix()
        state = _safe_absolute(self.state_root, "state_root")
        if state == Path(state.anchor):
            raise ValueError("state_root must not be a filesystem root")
        if (
            len(self.allowed_roots) != len(set(self.allowed_roots))
            or not self.allowed_roots
            or len(self.allowed_roots) > 64
        ):
            raise ValueError("allowed_roots must be non-empty and unique")
        for root in self.allowed_roots:
            checked = _safe_absolute(root, "allowed_root")
            if checked == Path(checked.anchor):
                raise ValueError("allowed_roots must contain existing non-root directories")
            _validate_allowed_root(checked)
            if _overlap(state, checked):
                raise ValueError("allowed_roots must not overlap state_root")
        if self.access_mode == "licensed" and self.licensed_endpoint is None:
            raise ValueError("licensed access requires exactly one private endpoint")
        if self.access_mode not in {"licensed", "offline_cache"} and (
            self.licensed_endpoint is not None
        ):
            raise ValueError("a private endpoint is supported only for licensed namespaces")
        if self.licensed_endpoint is not None:
            LicensedAccess(
                endpoint=self.licensed_endpoint,
                endpoint_label="licensed-renderer-endpoint",
                authorized_use_confirmed=True,
            )
        if self.access_mode == "offline_cache" and self.cache_path is None:
            raise ValueError("offline cache access requires an explicit cache path")
        if self.cache_path is not None:
            checked_cache = _safe_absolute(self.cache_path, "cache_path")
            CachePolicy(path=str(checked_cache))
        if self.access_mode != "offline_cache" and self.offline_allow_stale:
            raise ValueError("offline_allow_stale applies only to offline cache access")
        RateLimitPolicy(state_root=self.rate_limit_root)
        return self


def load_runtime_config(environment: Mapping[str, str] | None = None) -> RendererRuntimeConfig:
    values = os.environ if environment is None else environment
    state_raw = _required(values, STATE_ROOT_ENV)
    roots_raw = _required(values, ALLOWED_ROOTS_ENV)
    roots = tuple(_existing_root(part) for part in roots_raw.split(os.pathsep) if part)
    if len(roots) != len(roots_raw.split(os.pathsep)):
        raise ValueError(f"{ALLOWED_ROOTS_ENV} contains an empty root")
    raw_mode = values.get(ACCESS_MODE_ENV, "public_academic")
    if raw_mode not in {"public_academic", "licensed", "offline_cache", "unconfigured"}:
        raise ValueError(f"{ACCESS_MODE_ENV} is invalid")
    mode = cast(RendererAccessMode, raw_mode)
    if mode == "public_academic" and values.get(ACADEMIC_CONFIRMATION_ENV, "true") != "true":
        raise ValueError(f"{ACADEMIC_CONFIRMATION_ENV}=true is required")
    licensed_endpoint: str | None = None
    licensed_namespace_requested = (
        LICENSED_ENDPOINT_ENV in values or LICENSED_CONFIRMATION_ENV in values
    )
    if mode == "licensed" or (mode == "offline_cache" and licensed_namespace_requested):
        if values.get(LICENSED_CONFIRMATION_ENV) != "true":
            raise ValueError(f"{LICENSED_CONFIRMATION_ENV}=true is required")
        licensed_endpoint = _required(values, LICENSED_ENDPOINT_ENV)
    cache_path: Path | None = None
    if mode == "offline_cache":
        cache_path = _safe_absolute(Path(_required(values, CACHE_PATH_ENV)), CACHE_PATH_ENV)
    elif raw_cache_path := values.get(CACHE_PATH_ENV):
        cache_path = _safe_absolute(Path(raw_cache_path), CACHE_PATH_ENV)
    limits = RendererLimits(
        max_disk_bytes=_integer(
            values, MAX_DISK_BYTES_ENV, DEFAULT_MAX_DISK_BYTES, 1, 2 * 1024 * 1024 * 1024
        ),
        max_results=_integer(values, MAX_RESULTS_ENV, DEFAULT_MAX_RESULTS, 1, 4_096),
    )
    return RendererRuntimeConfig(
        state_root=_safe_absolute(Path(state_raw), STATE_ROOT_ENV),
        allowed_roots=roots,
        access_mode=mode,
        licensed_endpoint=licensed_endpoint,
        cache_path=cache_path,
        offline_allow_stale=_boolean(values, OFFLINE_ALLOW_STALE_ENV, False),
        retention_seconds=_integer(
            values, RETENTION_SECONDS_ENV, DEFAULT_RETENTION_SECONDS, 1, 2_592_000
        ),
        rate_limit_root=values.get(RATE_LIMIT_ROOT_ENV, default_rate_limit_root()),
        limits=limits,
    )


def _validate_posix() -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise ValueError("kegg-render-mcp requires POSIX no-follow filesystem operations")


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None or not value:
        raise ValueError(f"{name} is required")
    return value


def _safe_absolute(path: Path, name: str) -> Path:
    value = str(path)
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{name} exceeds the path-length limit")
    if "\x00" in value or not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be absolute and traversal-free")
    return path


def _existing_root(value: str) -> Path:
    path = _safe_absolute(Path(value), ALLOWED_ROOTS_ENV)
    try:
        _validate_allowed_root(path)
    except OSError as error:
        raise ValueError(f"{ALLOWED_ROOTS_ENV} contains an unavailable root") from error
    return path


def _validate_allowed_root(path: Path) -> None:
    descriptor = open_absolute_directory(path)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o022
        ):
            raise ValueError("allowed roots must be owned, direct, and not group/world writable")
    finally:
        os.close(descriptor)


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = values.get(name)
    if raw is None:
        return default
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", raw) is None:
        raise ValueError(f"{name} must be an integer")
    result = int(raw)
    if not minimum <= result <= maximum:
        raise ValueError(f"{name} is outside its supported range")
    return result


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    if raw not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return raw == "true"


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
