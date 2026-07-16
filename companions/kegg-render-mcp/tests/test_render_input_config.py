"""Configuration and renderer-input filesystem boundary tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_render_mcp.config import (
    ACCESS_MODE_ENV,
    ALLOWED_ROOTS_ENV,
    LICENSED_CONFIRMATION_ENV,
    LICENSED_ENDPOINT_ENV,
    STATE_ROOT_ENV,
    RendererLimits,
    RendererRuntimeConfig,
    load_runtime_config,
)
from kegg_render_mcp.contracts import ErrorCode, RenderMcpError
from kegg_render_mcp.render_input import load_render_input, resolve_output_directory


def test_config_requires_private_state_and_nonempty_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = load_runtime_config(
        {
            STATE_ROOT_ENV: str(tmp_path / "state"),
            ALLOWED_ROOTS_ENV: str(allowed),
            ACCESS_MODE_ENV: "unconfigured",
        }
    )
    assert config.access_mode == "unconfigured"
    assert config.allowed_roots == (allowed.resolve(),)
    with pytest.raises(ValueError, match=ALLOWED_ROOTS_ENV):
        load_runtime_config({STATE_ROOT_ENV: str(tmp_path / "state")})


def test_licensed_config_requires_acknowledgement_and_endpoint(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    environment = {
        STATE_ROOT_ENV: str(tmp_path / "state"),
        ALLOWED_ROOTS_ENV: str(allowed),
        ACCESS_MODE_ENV: "licensed",
    }
    with pytest.raises(ValueError, match=LICENSED_CONFIRMATION_ENV):
        load_runtime_config(environment)
    environment[LICENSED_CONFIRMATION_ENV] = "true"
    with pytest.raises(ValueError, match=LICENSED_ENDPOINT_ENV):
        load_runtime_config(environment)
    environment[LICENSED_ENDPOINT_ENV] = "https://licensed.example.invalid"
    assert load_runtime_config(environment).licensed_endpoint == environment[LICENSED_ENDPOINT_ENV]


def test_config_rejects_state_overlap(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    with pytest.raises(ValidationError, match="overlap"):
        RendererRuntimeConfig(
            state_root=allowed / "state",
            allowed_roots=(allowed.resolve(),),
            access_mode="unconfigured",
        )


def test_asset_limit_cannot_exceed_the_core_client_contract() -> None:
    assert RendererLimits(max_asset_bytes=50_000_000).max_asset_bytes == 50_000_000
    with pytest.raises(ValidationError):
        RendererLimits(max_asset_bytes=50_000_001)


def test_render_input_strictly_validates_v2(
    render_input_file: Path, runtime_config: RendererRuntimeConfig
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    assert loaded.document.schema_version == "2"
    assert loaded.accepted_ko_ids == {"K00001"}
    assert loaded.uncertain_ko_ids == {"K00002"}
    assert loaded.target_ids == ("ko00010", "M00001")


def test_version_one_requests_new_core_analysis(
    allowed_root: Path, runtime_config: RendererRuntimeConfig
) -> None:
    path = allowed_root / "render_input.json"
    path.write_text(json.dumps({"schema_version": "1"}), encoding="utf-8")
    with pytest.raises(RenderMcpError) as raised:
        load_render_input(str(path), runtime_config)
    assert raised.value.detail.code is ErrorCode.INCOMPATIBLE_SCHEMA


def test_path_traversal_relative_and_symlink_escape_are_rejected(
    tmp_path: Path, allowed_root: Path, runtime_config: RendererRuntimeConfig
) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = allowed_root / "link.json"
    link.symlink_to(outside)
    for path in ("relative.json", str(allowed_root / ".." / "outside.json"), str(link)):
        with pytest.raises(RenderMcpError) as raised:
            load_render_input(path, runtime_config)
        assert raised.value.detail.code is ErrorCode.INPUT_PATH_REJECTED


def test_unsafe_writable_intermediate_directory_is_rejected(
    allowed_root: Path, runtime_config: RendererRuntimeConfig
) -> None:
    unsafe = allowed_root / "unsafe"
    unsafe.mkdir(mode=0o777)
    os.chmod(unsafe, 0o777)
    path = unsafe / "render_input.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RenderMcpError, match="unsafe writable"):
        load_render_input(str(path), runtime_config)


def test_intermediate_symlink_swap_is_rejected(
    tmp_path: Path,
    allowed_root: Path,
    runtime_config: RendererRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kegg_render_mcp import render_input as module

    inside = allowed_root / "inside"
    inside.mkdir(mode=0o700)
    (inside / "render_input.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "render_input.json").write_text("{}", encoding="utf-8")
    moved = allowed_root / "inside-original"
    original_open = module.os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == "inside" and dir_fd is not None and not swapped:
            inside.rename(moved)
            inside.symlink_to(outside, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(module.os, "open", swapping_open)
    with pytest.raises(RenderMcpError) as raised:
        load_render_input(str(inside / "render_input.json"), runtime_config)
    assert raised.value.detail.code is ErrorCode.INPUT_PATH_REJECTED


def test_output_directory_is_created_owner_only(
    allowed_root: Path, runtime_config: RendererRuntimeConfig
) -> None:
    output = allowed_root / "images"
    resolved = resolve_output_directory(str(output), runtime_config.allowed_roots)
    assert resolved == output.resolve()
    assert output.stat().st_mode & 0o777 == 0o700
