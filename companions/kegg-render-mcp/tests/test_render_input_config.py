"""Configuration and renderer-input filesystem boundary tests."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from kegg_mcp.kegg import contracts as core_contracts
from pydantic import ValidationError

from kegg_render_mcp import config as config_module
from kegg_render_mcp._platform import UnsupportedRendererPlatformError
from kegg_render_mcp.config import (
    ACADEMIC_CONFIRMATION_ENV,
    ACCESS_MODE_ENV,
    ALLOWED_ROOTS_ENV,
    CACHE_PATH_ENV,
    LICENSED_CONFIRMATION_ENV,
    LICENSED_ENDPOINT_ENV,
    MAX_RESULTS_ENV,
    OFFLINE_ALLOW_STALE_ENV,
    STATE_ROOT_ENV,
    RendererLimits,
    RendererRuntimeConfig,
    load_runtime_config,
)
from kegg_render_mcp.contracts import ErrorCode, RenderMcpError
from kegg_render_mcp.render_input import (
    load_render_input,
    open_allowed_directory,
    resolve_output_directory,
)


def test_config_requires_private_state_and_nonempty_allowed_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = load_runtime_config(
        {
            STATE_ROOT_ENV: str(tmp_path / "state"),
            ALLOWED_ROOTS_ENV: str(allowed),
            ACCESS_MODE_ENV: "unconfigured",
            MAX_RESULTS_ENV: "7",
        }
    )
    assert config.access_mode == "unconfigured"
    assert config.allowed_roots == (allowed.resolve(),)
    assert config.limits.max_results == 7
    with pytest.raises(ValueError, match=ALLOWED_ROOTS_ENV):
        load_runtime_config({STATE_ROOT_ENV: str(tmp_path / "state")})


def test_default_access_is_unconfigured_and_public_requires_confirmation(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    environment = {
        STATE_ROOT_ENV: str(tmp_path / "state"),
        ALLOWED_ROOTS_ENV: str(allowed),
    }

    assert load_runtime_config(environment).access_mode == "unconfigured"
    environment[ACCESS_MODE_ENV] = "public_academic"
    with pytest.raises(ValueError, match=ACADEMIC_CONFIRMATION_ENV):
        load_runtime_config(environment)
    environment[ACADEMIC_CONFIRMATION_ENV] = "true"
    assert load_runtime_config(environment).access_mode == "public_academic"


def test_platform_gate_runs_before_required_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject_platform() -> None:
        raise UnsupportedRendererPlatformError("synthetic unsupported platform")

    monkeypatch.setattr(config_module, "validate_renderer_platform", reject_platform)

    with pytest.raises(UnsupportedRendererPlatformError, match="unsupported platform"):
        load_runtime_config({})


def test_darwin_uses_the_shared_core_cache_and_rate_limit_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(core_contracts.sys, "platform", "darwin")

    config = load_runtime_config(
        {
            "HOME": str(tmp_path),
            STATE_ROOT_ENV: str(tmp_path / "state"),
            ALLOWED_ROOTS_ENV: str(allowed),
            ACCESS_MODE_ENV: "public_academic",
            ACADEMIC_CONFIRMATION_ENV: "true",
        }
    )

    expected_root = tmp_path / "Library" / "Caches" / "kegg-mcp"
    assert config.cache_path == expected_root / "kegg.sqlite3"
    assert str(config.cache_path) == core_contracts.default_cache_path({"HOME": str(tmp_path)})
    assert config.rate_limit_root == str(expected_root / "rate-limit")
    assert config.rate_limit_root == core_contracts.default_rate_limit_root({"HOME": str(tmp_path)})


def test_omitted_output_selects_fresh_candidate_beneath_last_configured_root(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "inputs"
    output_root = tmp_path / "renders"
    input_root.mkdir(mode=0o700)
    output_root.mkdir(mode=0o700)

    output = resolve_output_directory(None, (input_root, output_root))

    assert output is not None
    assert output.parent == output_root
    assert output.name.startswith("kegg-render-")
    assert not output.exists()


def test_final_output_open_failure_removes_just_created_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "render-output"
    real_open = os.open

    def fail_output_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == output.name and flags & os.O_DIRECTORY:
            raise OSError("synthetic final open failure")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fail_output_open)
    with pytest.raises(RenderMcpError):
        open_allowed_directory(output, (root,))

    assert not output.exists()


def test_created_output_replacement_is_rejected_and_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "render-output"
    displaced = root / "displaced-render-output"
    real_open = os.open

    def replace_before_output_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == output.name and flags & os.O_DIRECTORY:
            output.rename(displaced)
            output.mkdir(mode=0o700)
            (output / "caller-owned.txt").write_text("keep", encoding="utf-8")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_output_open)
    with pytest.raises(RenderMcpError):
        open_allowed_directory(output, (root,))

    assert (output / "caller-owned.txt").read_text(encoding="utf-8") == "keep"
    assert displaced.is_dir()
    assert tuple(displaced.iterdir()) == ()


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


def test_offline_cache_config_requires_an_explicit_absolute_cache_path(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    environment = {
        STATE_ROOT_ENV: str(tmp_path / "state"),
        ALLOWED_ROOTS_ENV: str(allowed),
        ACCESS_MODE_ENV: "offline_cache",
    }
    with pytest.raises(ValueError, match=CACHE_PATH_ENV):
        load_runtime_config(environment)

    environment[CACHE_PATH_ENV] = str(tmp_path / "cache" / "kegg.sqlite3")
    config = load_runtime_config(environment)
    assert config.access_mode == "offline_cache"
    assert config.cache_path == Path(environment[CACHE_PATH_ENV])
    assert config.offline_allow_stale is False

    environment[CACHE_PATH_ENV] = "relative.sqlite3"
    with pytest.raises(ValueError, match="absolute"):
        load_runtime_config(environment)


def test_offline_licensed_namespace_requires_confirmation_and_controls_stale_policy(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    environment = {
        STATE_ROOT_ENV: str(tmp_path / "state"),
        ALLOWED_ROOTS_ENV: str(allowed),
        ACCESS_MODE_ENV: "offline_cache",
        CACHE_PATH_ENV: str(tmp_path / "cache.sqlite3"),
        LICENSED_ENDPOINT_ENV: "https://licensed.example.invalid",
    }
    with pytest.raises(ValueError, match=LICENSED_CONFIRMATION_ENV):
        load_runtime_config(environment)

    environment[LICENSED_CONFIRMATION_ENV] = "true"
    environment[OFFLINE_ALLOW_STALE_ENV] = "true"
    config = load_runtime_config(environment)
    assert config.licensed_endpoint == environment[LICENSED_ENDPOINT_ENV]
    assert config.offline_allow_stale is True

    environment[OFFLINE_ALLOW_STALE_ENV] = "yes"
    with pytest.raises(ValueError, match=OFFLINE_ALLOW_STALE_ENV):
        load_runtime_config(environment)


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


def test_render_input_strictly_validates_schema(
    render_input_file: Path, runtime_config: RendererRuntimeConfig
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    assert loaded.document.schema_version == "3"
    assert loaded.accepted_ko_ids == {"K00001"}
    assert loaded.uncertain_ko_ids == {"K00002"}
    assert loaded.target_ids == ("ko00010", "M00001")


def test_inline_render_input_uses_the_same_bounded_strict_parser(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
) -> None:
    payload = render_input_file.read_text(encoding="utf-8")
    loaded = load_render_input(None, runtime_config, render_input_json=payload)

    assert loaded.document.schema_version == "3"
    assert loaded.target_ids == ("ko00010", "M00001")
    with pytest.raises(RenderMcpError) as ambiguous:
        load_render_input(
            str(render_input_file),
            runtime_config,
            render_input_json=payload,
        )
    assert ambiguous.value.detail.code is ErrorCode.INVALID_REQUEST


def test_handoff_schema_error_reports_only_bounded_field_path_and_stage(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
) -> None:
    payload = json.loads(render_input_file.read_text(encoding="utf-8"))
    payload["dataset"]["analysis_unit"] = 42

    with pytest.raises(RenderMcpError) as raised:
        load_render_input(None, runtime_config, render_input_json=json.dumps(payload))

    details = {item.name: item.value for item in raised.value.detail.safe_details}
    assert details == {
        "field_path": "dataset.analysis_unit",
        "validation_issue_count": "1",
        "stage": "render_input_schema",
    }


def test_version_two_requests_new_core_analysis(
    allowed_root: Path, runtime_config: RendererRuntimeConfig
) -> None:
    path = allowed_root / "render_input.json"
    path.write_text(json.dumps({"schema_version": "2"}), encoding="utf-8")
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
