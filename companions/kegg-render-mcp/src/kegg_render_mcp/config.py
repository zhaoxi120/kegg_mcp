"""Small environment-only configuration for the renderer companion."""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

ENV_PREFIX = "KEGG_RENDER_MCP_"
STATE_ROOT_ENV = f"{ENV_PREFIX}STATE_ROOT"
ALLOWED_ROOTS_ENV = f"{ENV_PREFIX}ALLOWED_ROOTS"
ACCESS_MODE_ENV = f"{ENV_PREFIX}ACCESS_MODE"
ACADEMIC_CONFIRMATION_ENV = f"{ENV_PREFIX}ACADEMIC_USE_CONFIRMED"
LICENSED_ENDPOINT_ENV = f"{ENV_PREFIX}LICENSED_ENDPOINT"
LICENSED_CONFIRMATION_ENV = f"{ENV_PREFIX}LICENSED_USE_CONFIRMED"
RETENTION_SECONDS_ENV = f"{ENV_PREFIX}RETENTION_SECONDS"
MAX_DISK_BYTES_ENV = f"{ENV_PREFIX}MAX_DISK_BYTES"

DEFAULT_RETENTION_SECONDS = 86_400
DEFAULT_MAX_INPUT_BYTES = 50_000_000
DEFAULT_MAX_ASSET_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PIXELS = 20_000_000
DEFAULT_MAX_SVG_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_RESULT_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_DISK_BYTES = 256 * 1024 * 1024


class RendererLimits(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, validate_default=True, hide_input_in_errors=True
    )

    max_input_bytes: int = Field(default=DEFAULT_MAX_INPUT_BYTES, ge=1, le=64 * 1024 * 1024)
    max_asset_bytes: int = Field(default=DEFAULT_MAX_ASSET_BYTES, ge=1, le=50_000_000)
    max_pixels: int = Field(default=DEFAULT_MAX_PIXELS, ge=1, le=100_000_000)
    max_svg_bytes: int = Field(default=DEFAULT_MAX_SVG_BYTES, ge=1, le=64 * 1024 * 1024)
    max_result_bytes: int = Field(default=DEFAULT_MAX_RESULT_BYTES, ge=1, le=256 * 1024 * 1024)
    max_disk_bytes: int = Field(default=DEFAULT_MAX_DISK_BYTES, ge=1, le=2 * 1024 * 1024 * 1024)
    max_xml_elements: int = Field(default=20_000, ge=1, le=100_000)
    max_xml_attributes: int = Field(default=100_000, ge=1, le=500_000)
    max_xml_depth: int = Field(default=32, ge=1, le=128)
    max_svg_nodes: int = Field(default=50_000, ge=1, le=200_000)

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
    access_mode: Literal["public_academic", "licensed", "unconfigured"] = "public_academic"
    licensed_endpoint: str | None = Field(default=None, min_length=1, max_length=2048, repr=False)
    retention_seconds: int = Field(default=DEFAULT_RETENTION_SECONDS, ge=1, le=2_592_000)
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
        if (self.access_mode == "licensed") != (self.licensed_endpoint is not None):
            raise ValueError("licensed access requires exactly one private endpoint")
        if self.licensed_endpoint is not None:
            from kegg_mcp.kegg import LicensedAccess

            LicensedAccess(
                endpoint=self.licensed_endpoint,
                endpoint_label="licensed-renderer-endpoint",
                authorized_use_confirmed=True,
            )
        return self


def load_runtime_config(environment: Mapping[str, str] | None = None) -> RendererRuntimeConfig:
    values = os.environ if environment is None else environment
    state_raw = _required(values, STATE_ROOT_ENV)
    roots_raw = _required(values, ALLOWED_ROOTS_ENV)
    roots = tuple(_existing_root(part) for part in roots_raw.split(os.pathsep) if part)
    if len(roots) != len(roots_raw.split(os.pathsep)):
        raise ValueError(f"{ALLOWED_ROOTS_ENV} contains an empty root")
    raw_mode = values.get(ACCESS_MODE_ENV, "public_academic")
    if raw_mode not in {"public_academic", "licensed", "unconfigured"}:
        raise ValueError(f"{ACCESS_MODE_ENV} is invalid")
    mode = cast(Literal["public_academic", "licensed", "unconfigured"], raw_mode)
    if mode == "public_academic" and values.get(ACADEMIC_CONFIRMATION_ENV, "true") != "true":
        raise ValueError(f"{ACADEMIC_CONFIRMATION_ENV}=true is required")
    licensed_endpoint: str | None = None
    if mode == "licensed":
        if values.get(LICENSED_CONFIRMATION_ENV) != "true":
            raise ValueError(f"{LICENSED_CONFIRMATION_ENV}=true is required")
        licensed_endpoint = _required(values, LICENSED_ENDPOINT_ENV)
    limits = RendererLimits(
        max_disk_bytes=_integer(
            values, MAX_DISK_BYTES_ENV, DEFAULT_MAX_DISK_BYTES, 1, 2 * 1024 * 1024 * 1024
        )
    )
    return RendererRuntimeConfig(
        state_root=_safe_absolute(Path(state_raw), STATE_ROOT_ENV),
        allowed_roots=roots,
        access_mode=mode,
        licensed_endpoint=licensed_endpoint,
        retention_seconds=_integer(
            values, RETENTION_SECONDS_ENV, DEFAULT_RETENTION_SECONDS, 1, 2_592_000
        ),
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
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
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


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
