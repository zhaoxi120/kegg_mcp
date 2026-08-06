"""Process-scoped retention, quota, export, and deletion tests."""

from __future__ import annotations

import contextlib
import fcntl
import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from types import TracebackType
from typing import Self, cast

import pytest
from kegg_mcp.analysis import PathwayReferenceScope
from kegg_mcp.services.render_contracts import RenderabilityStatus, serialize_render_input

from conftest import SyntheticProvider, make_render_input
from kegg_render_mcp import _state_scope as state_scope_module
from kegg_render_mcp import artifacts as artifacts_module
from kegg_render_mcp import export_writer
from kegg_render_mcp import render_service as render_service_module
from kegg_render_mcp.artifacts import ArtifactBlob, RenderArtifactStore
from kegg_render_mcp.config import RendererLimits, RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    MAX_ARTIFACTS,
    ErrorCode,
    RenderFormat,
    RenderMcpError,
)
from kegg_render_mcp.pathway_scene import UnconfiguredAssetProvider
from kegg_render_mcp.raster import PngArtifact
from kegg_render_mcp.render_service import RendererService
from kegg_render_mcp.svg import SvgArtifact


def _hold_renderer_state_scope(
    state_root: str,
    max_results: int,
    connection: Connection,
) -> None:
    scope = state_scope_module.open_state_scope(Path(state_root), max_results)
    try:
        connection.send(scope.scope_name)
        connection.recv()
        state_scope_module.cleanup_state_scope(scope, max_results)
    finally:
        state_scope_module.release_state_scope(scope)
        connection.close()


def _small_result_config(
    runtime_config: RendererRuntimeConfig,
    *,
    max_result_bytes: int = 1_000,
) -> RendererRuntimeConfig:
    limits = runtime_config.limits.model_copy(
        update={
            "max_asset_bytes": max_result_bytes,
            "max_svg_bytes": max_result_bytes,
            "max_result_bytes": max_result_bytes,
        }
    )
    return runtime_config.model_copy(update={"limits": limits})


def _assert_no_partial_result(
    service: RendererService,
    allowed_root: Path,
    allocated_before: tuple[Path, ...],
) -> None:
    snapshot = service.store.snapshot()
    assert snapshot.active_result_count == 0
    assert snapshot.cleanup_pending_result_count == 0
    assert snapshot.retained_bytes == 0
    assert snapshot.retained_storage_bytes == 0
    assert tuple(allowed_root.glob("kegg-render-*")) == allocated_before


@pytest.mark.asyncio
async def test_output_path_budget_is_rejected_before_assets_or_allocation(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    synthetic_provider: SyntheticProvider,
) -> None:
    prefix_bytes = len(str(allowed_root).encode("utf-8")) + 1
    output = allowed_root / ("x" * (4_000 - prefix_bytes))
    allocated_before = tuple(allowed_root.glob("kegg-render-*"))
    entries_before = tuple(allowed_root.iterdir())
    service = RendererService(runtime_config, synthetic_provider)
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=("ko00010",),
                formats=(RenderFormat.SVG,),
                output_directory=str(output),
            )
        assert raised.value.detail.code is ErrorCode.INPUT_PATH_REJECTED
        assert synthetic_provider.calls == []
        assert tuple(allowed_root.iterdir()) == entries_before
        _assert_no_partial_result(service, allowed_root, allocated_before)
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("later_target", ["unknown", "summary_only"])
async def test_preflight_rejects_a_later_invalid_target_before_assets_or_output(
    later_target: str,
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    synthetic_provider: SyntheticProvider,
) -> None:
    target_ids = ("ko00010", "unknown")
    if later_target == "summary_only":
        document = make_render_input()
        summary = document.pathways[0].model_copy(
            update={
                "renderability": RenderabilityStatus.SUMMARY_ONLY,
                "not_renderable_reason": "synthetic_summary_only",
            }
        )
        document = document.model_copy(update={"pathways": (summary,)})
        render_input_file.write_text(serialize_render_input(document), encoding="utf-8")
        target_ids = ("M00001", "ko00010")

    allocated_before = tuple(allowed_root.glob("kegg-render-*"))
    service = RendererService(runtime_config, synthetic_provider)
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=target_ids,
                formats=(RenderFormat.SVG,),
                output_directory=None,
            )
        expected_code = (
            ErrorCode.TARGET_NOT_FOUND
            if later_target == "unknown"
            else ErrorCode.TARGET_NOT_RENDERABLE
        )
        assert raised.value.detail.code is expected_code
        assert synthetic_provider.calls == []
        _assert_no_partial_result(service, allowed_root, allocated_before)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_unconfigured_mixed_bundle_fails_before_output_allocation(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
) -> None:
    allocated_before = tuple(allowed_root.glob("kegg-render-*"))
    service = RendererService(runtime_config, UnconfiguredAssetProvider())
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=("M00001", "ko00010"),
                formats=(RenderFormat.SVG,),
                output_directory=None,
            )
        assert raised.value.detail.code is ErrorCode.ASSET_UNAVAILABLE
        assert {item.name: item.value for item in raised.value.detail.safe_details}[
            "target_id"
        ] == "ko00010"
        _assert_no_partial_result(service, allowed_root, allocated_before)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_second_format_budget_failure_names_target_and_format(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _small_result_config(runtime_config)
    allocated_before = tuple(allowed_root.glob("kegg-render-*"))

    def render_svg(*args: object, **kwargs: object) -> SvgArtifact:
        del args, kwargs
        return SvgArtifact(b"s" * 300, 1, 1)

    def render_png(*args: object, **kwargs: object) -> PngArtifact:
        del args, kwargs
        return PngArtifact(b"p" * 201, 1, 1)

    monkeypatch.setattr(
        render_service_module,
        "render_module_svg",
        render_svg,
    )
    monkeypatch.setattr(
        render_service_module,
        "render_module_png",
        render_png,
    )
    service = RendererService(config, SyntheticProvider())
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=("M00001",),
                formats=(RenderFormat.SVG, RenderFormat.PNG),
                output_directory=None,
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
        assert {item.name: item.value for item in raised.value.detail.safe_details} == {
            "target_id": "M00001",
            "asset_kind": "png_output",
        }
        _assert_no_partial_result(service, allowed_root, allocated_before)
    finally:
        service.close()


def test_manifest_reserve_failure_retains_no_partial_result(
    runtime_config: RendererRuntimeConfig,
    allowed_root: Path,
) -> None:
    config = _small_result_config(runtime_config)
    explicit_output = allowed_root / "manifest-failure-output"
    store = RenderArtifactStore(config)
    store.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            store.retain(
                target_ids=("M00001",),
                artifacts=(ArtifactBlob("M00001.svg", "image/svg+xml", b"x", 1, 1),),
                warnings=(),
                manifest_context={"padding": "x" * 600},
                output_directory=explicit_output,
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
        assert "manifest" in raised.value.detail.message.lower()
        assert not explicit_output.exists()
        assert store.snapshot().active_result_count == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_pathway_asset_failure_adds_target_context_without_partial_result(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    synthetic_provider: SyntheticProvider,
) -> None:
    synthetic_provider.kgml = b"not valid KGML"
    explicit_output = allowed_root / "failed-pathway-output"
    allocated_before = tuple(allowed_root.glob("kegg-render-*"))
    service = RendererService(runtime_config, synthetic_provider)
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=("ko00010",),
                formats=(RenderFormat.SVG,),
                output_directory=str(explicit_output),
            )
        assert raised.value.detail.code is ErrorCode.ASSET_INVALID
        assert {item.name: item.value for item in raised.value.detail.safe_details}[
            "target_id"
        ] == "ko00010"
        assert synthetic_provider.calls == [("ko00010", "image"), ("ko00010", "kgml")]
        assert not explicit_output.exists()
        _assert_no_partial_result(service, allowed_root, allocated_before)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_opted_in_global_v4_handoff_renders_polyline_bundle_and_manifest(
    runtime_config: RendererRuntimeConfig,
    allowed_root: Path,
) -> None:
    document = make_render_input(
        pathway_id="ko01100",
        pathway_scope=PathwayReferenceScope.GLOBAL_OR_OVERVIEW,
        allow_global_or_overview=True,
    )
    input_path = allowed_root / "global-render-input.json"
    input_path.write_text(serialize_render_input(document), encoding="utf-8")
    provider = SyntheticProvider(pathway_id="ko01100")
    provider.kgml = _synthetic_overview_kgml()
    service = RendererService(runtime_config, provider)
    service.open()
    try:
        result = await service.render(
            render_input_path=str(input_path),
            target_ids=("ko01100",),
            formats=(RenderFormat.SVG, RenderFormat.PNG),
            output_directory=None,
        )

        assert {item.name for item in result.artifacts} == {
            "ko01100.svg",
            "ko01100.png",
            "render_manifest.json",
        }
        assert provider.calls == [("ko01100", "image"), ("ko01100", "kgml")]
        assert any("global or overview base map" in warning for warning in result.warnings)
        svg = service.store.read(result.render_id, "ko01100.svg").content
        assert b'<path d="M ' in svg
        assert b'stroke-dasharray="8 4"' in svg
        manifest = cast(
            dict[str, object],
            json.loads(service.store.read(result.render_id, "render_manifest.json").content),
        )
        provenance = cast(dict[str, object], manifest["provenance"])
        target = cast(list[dict[str, object]], provenance["targets"])[0]
        assert target["reference_scope"] == "global_or_overview"
        assert target["kgml_parser_version"] == "1.3"
        assert target["retained_box_graphic_count"] == 0
        assert target["retained_polyline_graphic_count"] == 2
        assert target["mapped_detected_ko_ids"] == ["K00001", "K00002"]
        assert target["polyline_overlay_count"] == 2
    finally:
        service.close()


@pytest.mark.asyncio
async def test_global_handoff_without_opt_in_fails_before_assets(
    runtime_config: RendererRuntimeConfig,
    allowed_root: Path,
) -> None:
    document = make_render_input(
        pathway_id="ko01100",
        pathway_scope=PathwayReferenceScope.GLOBAL_OR_OVERVIEW,
        allow_global_or_overview=True,
    )
    payload = json.loads(serialize_render_input(document))
    payload["execution"]["analysis"]["pathway_parameters"]["allow_global_or_overview"] = False
    provider = SyntheticProvider(pathway_id="ko01100")
    provider.kgml = _synthetic_overview_kgml()
    allocated_before = tuple(allowed_root.glob("kegg-render-*"))
    service = RendererService(runtime_config, provider)
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=None,
                render_input_json=json.dumps(payload),
                target_ids=("ko01100",),
                formats=(RenderFormat.SVG,),
                output_directory=None,
            )

        assert raised.value.detail.code is ErrorCode.INVALID_REQUEST
        assert provider.calls == []
        _assert_no_partial_result(service, allowed_root, allocated_before)
    finally:
        service.close()


def _synthetic_overview_kgml() -> bytes:
    return b"""<pathway name="path:ko01100" title="Synthetic overview">
  <entry id="1" name="ko:K00001 ko:K00002" type="gene">
    <graphics name="K00001..." type="line" coords="10,20,100,20"/>
  </entry>
  <entry id="2" name="ko:K00002" type="gene">
    <graphics name="K00002" type="line" coords="10,40,100,40"/>
  </entry>
</pathway>"""


@pytest.mark.asyncio
async def test_service_renders_both_targets_formats_and_durable_manifest(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    synthetic_provider: SyntheticProvider,
) -> None:
    service = RendererService(runtime_config, synthetic_provider)
    service.open()
    durable_files: dict[str, bytes] | None = None
    render_id: str | None = None
    output = allowed_root / "images"
    try:
        result = await service.render(
            render_input_path=str(render_input_file),
            target_ids=("ko00010", "M00001"),
            formats=(RenderFormat.SVG, RenderFormat.PNG),
            output_directory=str(output),
        )
        assert len(result.artifacts) == 5
        assert {item.name for item in result.artifacts} == {
            "ko00010.svg",
            "ko00010.png",
            "M00001.svg",
            "M00001.png",
            "render_manifest.json",
        }
        for metadata in result.artifacts:
            assert (output / metadata.name).is_file()
            assert (output / metadata.name).stat().st_mode & 0o777 == 0o600
            assert metadata.resource_uri == (
                f"kegg-render://results/{result.render_id}/{metadata.name}"
            )
        assert result.result_uri == f"kegg-render://results/{result.render_id}"
        assert output.stat().st_mode & 0o777 == 0o700

        manifest_blob = service.store.read(result.render_id, "render_manifest.json")
        assert manifest_blob.content == (output / "render_manifest.json").read_bytes()
        manifest = cast(dict[str, object], json.loads(manifest_blob.content))
        assert manifest["schema_version"] == "2"
        assert "render_id" not in manifest
        assert "expires_at" not in manifest
        assert "resource_uri" not in manifest
        assert result.render_id.encode() not in manifest_blob.content
        assert b"kegg-render://" not in manifest_blob.content
        assert b'"calculation_method"' in manifest_blob.content
        assert b'"parser_version"' in manifest_blob.content
        assert b'"analysis_unit":"unknown"' in manifest_blob.content
        assert str(runtime_config.state_root).encode() not in manifest_blob.content
        provenance = cast(dict[str, object], manifest["provenance"])
        targets = cast(list[dict[str, object]], provenance["targets"])
        pathway = next(item for item in targets if item["target_id"] == "ko00010")
        assert pathway["kgml_parser_name"] == "kegg_render_safe_kgml"
        assert pathway["kgml_parser_version"] == "1.3"
        assert pathway["retained_box_graphic_count"] == 2
        assert pathway["retained_polyline_graphic_count"] == 0
        assert pathway["mapped_detected_ko_ids"] == ["K00001", "K00002"]
        assert pathway["box_overlay_count"] == 2
        assert pathway["polyline_overlay_count"] == 0

        artifact_records = cast(list[dict[str, object]], manifest["artifacts"])
        assert len(artifact_records) == 4
        for record in artifact_records:
            assert set(record) == {
                "path",
                "mime_type",
                "byte_size",
                "width",
                "height",
            }
            relative_path = record["path"]
            assert isinstance(relative_path, str)
            assert Path(relative_path).name == relative_path
            content = (output / relative_path).read_bytes()
            assert record["byte_size"] == len(content)

        render_id = result.render_id
        durable_files = {path.name: path.read_bytes() for path in output.iterdir()}
    finally:
        service.close()

    assert render_id is not None
    assert durable_files is not None
    assert {path.name: path.read_bytes() for path in output.iterdir()} == durable_files
    with pytest.raises(RenderMcpError) as closed:
        service.store.get(render_id)
    assert closed.value.detail.code is ErrorCode.RESULT_NOT_FOUND


@pytest.mark.asyncio
async def test_result_is_process_scoped_and_deletion_uses_safe_not_found(
    runtime_config: RendererRuntimeConfig, render_input_file: Path
) -> None:
    first = RendererService(runtime_config, SyntheticProvider())
    first.open()
    first_result = await first.render(
        render_input_path=str(render_input_file),
        target_ids=("M00001",),
        formats=(RenderFormat.SVG,),
        output_directory=None,
    )
    second = RendererService(runtime_config, SyntheticProvider())
    second.open()
    try:
        with pytest.raises(RenderMcpError) as missing:
            second.store.get(first_result.render_id)
        assert missing.value.detail.code is ErrorCode.RESULT_NOT_FOUND
        second_result = await second.render(
            render_input_path=str(render_input_file),
            target_ids=("M00001",),
            formats=(RenderFormat.SVG,),
            output_directory=None,
        )
        with pytest.raises(RenderMcpError) as other_process:
            first.store.get(second_result.render_id)
        assert other_process.value.detail.code is ErrorCode.RESULT_NOT_FOUND
        assert first.store.delete(first_result.render_id).deleted is True
        for operation in (
            lambda: first.store.get(first_result.render_id),
            lambda: first.store.read(first_result.render_id, "M00001.svg"),
            lambda: first.store.delete(first_result.render_id),
        ):
            with pytest.raises(RenderMcpError) as deleted:
                operation()
            assert deleted.value.detail.code is ErrorCode.RESULT_NOT_FOUND
        first.close()
        assert second.store.get(second_result.render_id) == second_result
    finally:
        second.close()
        first.close()


def test_state_root_skips_live_scopes_and_cleans_an_unlocked_scope(
    runtime_config: RendererRuntimeConfig,
) -> None:
    first = RenderArtifactStore(runtime_config)
    first.open()
    second = RenderArtifactStore(runtime_config)
    second.open()
    first_scope = runtime_config.state_root / str(
        first._scope_name  # pyright: ignore[reportPrivateUsage]
    )
    second_scope = runtime_config.state_root / str(
        second._scope_name  # pyright: ignore[reportPrivateUsage]
    )
    abandoned = runtime_config.state_root / ("scope_" + "a" * 32)
    abandoned.mkdir(mode=0o700)
    lease = abandoned / ".scope.lock"
    lease.touch(mode=0o600)
    os.chmod(lease, 0o600)
    result = abandoned / ("render_" + "a" * 32)
    result.mkdir(mode=0o700)
    artifact = result / "M00001.svg"
    artifact.write_text("<svg/>", encoding="utf-8")
    os.chmod(artifact, 0o600)
    third = RenderArtifactStore(runtime_config)
    try:
        third.open()
        assert first_scope.is_dir()
        assert second_scope.is_dir()
        assert not abandoned.exists()
        assert (first_scope / ".scope.lock").stat().st_mode & 0o777 == 0o600
        assert (second_scope / ".scope.lock").stat().st_mode & 0o777 == 0o600
        first.close()
        assert second_scope.is_dir()
    finally:
        third.close()
        second.close()
        first.close()


def test_parent_open_and_close_preserves_a_live_spawned_scope(
    runtime_config: RendererRuntimeConfig,
) -> None:
    context = multiprocessing.get_context("spawn")
    parent_connection, child_connection = context.Pipe()
    process = context.Process(
        target=_hold_renderer_state_scope,
        args=(
            str(runtime_config.state_root),
            runtime_config.limits.max_results,
            child_connection,
        ),
    )
    process.start()
    child_connection.close()
    store = RenderArtifactStore(runtime_config)
    try:
        assert parent_connection.poll(10), "spawned renderer scope did not become ready"
        child_scope = runtime_config.state_root / parent_connection.recv()
        assert child_scope.is_dir()

        store.open()
        store.close()

        assert child_scope.is_dir()
        parent_connection.send(None)
        process.join(10)
        assert process.exitcode == 0
        assert not child_scope.exists()
    finally:
        store.close()
        if process.is_alive():
            with contextlib.suppress(BrokenPipeError, EOFError, OSError):
                parent_connection.send(None)
            process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        parent_connection.close()


def test_scope_without_a_lease_fails_closed(runtime_config: RendererRuntimeConfig) -> None:
    runtime_config.state_root.mkdir(mode=0o700)
    unsafe_scope = runtime_config.state_root / ("scope_" + "a" * 32)
    unsafe_scope.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="already active or unsafe"):
        RenderArtifactStore(runtime_config).open()

    assert unsafe_scope.is_dir()


def test_concurrent_scope_limit_is_checked_under_coordination_lock(
    runtime_config: RendererRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state_scope_module, "MAX_SCOPES", 1)
    first = RenderArtifactStore(runtime_config)
    first.open()
    try:
        with pytest.raises(ValueError, match="already active or unsafe"):
            RenderArtifactStore(runtime_config).open()
    finally:
        first.close()


def test_close_releases_scope_lease_when_result_cleanup_is_unsafe(
    runtime_config: RendererRuntimeConfig,
) -> None:
    store = RenderArtifactStore(runtime_config)
    store.open()
    result = store.retain(
        target_ids=("M00001",),
        artifacts=(ArtifactBlob("M00001.svg", "image/svg+xml", b"<svg/>", 1, 1),),
        warnings=(),
        manifest_context={},
        output_directory=None,
    )
    scope_name = store._scope_name  # pyright: ignore[reportPrivateUsage]
    assert scope_name is not None
    scope = runtime_config.state_root / scope_name
    result_directory = scope / result.render_id
    for index in range(MAX_ARTIFACTS + 2):
        marker = result_directory / f"extra-{index}"
        marker.touch(mode=0o600)
        os.chmod(marker, 0o600)

    with pytest.raises(ValueError, match="entry-count limit"):
        store.close()

    assert store._scope_lock_fd is None  # pyright: ignore[reportPrivateUsage]
    lease_fd = os.open(scope / ".scope.lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(lease_fd)


def test_result_replacement_is_not_removed(
    runtime_config: RendererRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RenderArtifactStore(runtime_config)
    store.open()
    result = store.retain(
        target_ids=("M00001",),
        artifacts=(ArtifactBlob("M00001.svg", "image/svg+xml", b"<svg/>", 1, 1),),
        warnings=(),
        manifest_context={},
        output_directory=None,
    )
    scope_name = store._scope_name  # pyright: ignore[reportPrivateUsage]
    assert scope_name is not None
    scope = runtime_config.state_root / scope_name
    result_directory = scope / result.render_id
    moved_directory = scope / "moved-result"
    replacement_marker = result_directory / "replacement"
    real_bounded_names = artifacts_module._bounded_directory_names  # pyright: ignore[reportPrivateUsage]
    replaced = False

    def replace_after_scan(descriptor: int, limit: int, label: str) -> tuple[str, ...]:
        nonlocal replaced
        names = real_bounded_names(descriptor, limit, label)
        if label == "renderer result directory" and not replaced:
            replaced = True
            result_directory.rename(moved_directory)
            result_directory.mkdir(mode=0o700)
            replacement_marker.touch(mode=0o600)
            os.chmod(replacement_marker, 0o600)
        return names

    monkeypatch.setattr(artifacts_module, "_bounded_directory_names", replace_after_scan)
    try:
        with pytest.raises(ValueError, match="was replaced"):
            store.delete(result.render_id)
        assert replacement_marker.is_file()
        assert moved_directory.is_dir()
    finally:
        monkeypatch.setattr(artifacts_module, "_bounded_directory_names", real_bounded_names)
        replacement_marker.unlink(missing_ok=True)
        result_directory.rmdir()
        moved_directory.rename(result_directory)
        store.close()


def test_scope_replacement_after_close_scan_is_not_removed(
    runtime_config: RendererRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RenderArtifactStore(runtime_config)
    store.open()
    scope_name = store._scope_name  # pyright: ignore[reportPrivateUsage]
    assert scope_name is not None
    scope = runtime_config.state_root / scope_name
    moved_scope = runtime_config.state_root / "moved-scope"
    replacement_marker = scope / "replacement"
    real_bounded_names = state_scope_module.bounded_directory_names
    replaced = False

    def replace_after_scan(descriptor: int, limit: int, label: str) -> tuple[str, ...]:
        nonlocal replaced
        names = real_bounded_names(descriptor, limit, label)
        if label == "renderer scope" and not replaced:
            replaced = True
            scope.rename(moved_scope)
            scope.mkdir(mode=0o700)
            replacement_marker.touch(mode=0o600)
            os.chmod(replacement_marker, 0o600)
        return names

    monkeypatch.setattr(state_scope_module, "bounded_directory_names", replace_after_scan)
    store.close()

    assert replacement_marker.is_file()
    assert (moved_scope / ".scope.lock").is_file()


@pytest.mark.asyncio
async def test_export_rejects_nonempty_symlink_destination_without_following_it(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    tmp_path: Path,
) -> None:
    output = allowed_root / "images"
    output.mkdir(mode=0o700)
    outside = tmp_path / "outside.svg"
    outside.write_text("private", encoding="utf-8")
    (output / "M00001.svg").symlink_to(outside)
    service = RendererService(runtime_config, SyntheticProvider())
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=("M00001",),
                formats=(RenderFormat.SVG,),
                output_directory=str(output),
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
        assert outside.read_text(encoding="utf-8") == "private"
    finally:
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "existing_name",
    ("existing.txt", "M00001.svg", "M00001.png", "render_manifest.json"),
)
async def test_export_rejects_every_nonempty_regular_directory_without_changes(
    existing_name: str,
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
) -> None:
    output = allowed_root / "images"
    output.mkdir(mode=0o700)
    existing = output / existing_name
    existing.write_bytes(b"existing")
    service = RendererService(runtime_config, SyntheticProvider())
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=("M00001",),
                formats=(RenderFormat.SVG, RenderFormat.PNG),
                output_directory=str(output),
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
        assert existing.read_bytes() == b"existing"
        assert tuple(output.iterdir()) == (existing,)
    finally:
        service.close()


def test_nonempty_output_check_does_not_enumerate_the_whole_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "many-existing-files"
    output.mkdir(mode=0o700)
    existing = output / "first.txt"
    existing.write_bytes(b"existing")
    real_scandir = os.scandir

    class OneEntryScanner:
        def __init__(self) -> None:
            self.next_calls = 0

        def __enter__(self) -> Self:
            return self

        def __exit__(
            self,
            exception_type: type[BaseException] | None,
            exception: BaseException | None,
            traceback: TracebackType | None,
        ) -> None:
            del exception_type, exception, traceback

        def __iter__(self) -> Self:
            return self

        def __next__(self) -> os.DirEntry[str]:
            self.next_calls += 1
            if self.next_calls > 1:
                raise AssertionError("the output check enumerated beyond the first entry")
            with real_scandir(output) as real_entries:
                return next(real_entries)

    scanner = OneEntryScanner()

    def fake_scandir(_: int) -> OneEntryScanner:
        return scanner

    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(export_writer.os, "scandir", fake_scandir)
            with pytest.raises(RenderMcpError) as raised:
                export_writer._require_empty_directory(  # pyright: ignore[reportPrivateUsage]
                    descriptor
                )
        assert raised.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
        assert scanner.next_calls == 1
    finally:
        os.close(descriptor)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "output_mode",
    ("automatic", "explicit_existing"),
)
async def test_failed_export_rolls_back_new_files_and_commit_manifest(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_mode: str,
) -> None:
    output = allowed_root / "images"
    if output_mode == "explicit_existing":
        output.mkdir(mode=0o700)
    allocated_before = tuple(allowed_root.glob("kegg-render-*"))
    real_link = os.link
    links = 0

    def fail_second_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal links
        links += 1
        if links == 2:
            raise OSError("synthetic export failure")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", fail_second_link)
    service = RendererService(runtime_config, SyntheticProvider())
    service.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            await service.render(
                render_input_path=str(render_input_file),
                target_ids=("M00001",),
                formats=(RenderFormat.SVG, RenderFormat.PNG),
                output_directory=(str(output) if output_mode != "automatic" else None),
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
        if output_mode == "explicit_existing":
            assert not tuple(output.iterdir())
        else:
            assert tuple(allowed_root.glob("kegg-render-*")) == allocated_before
    finally:
        service.close()


@pytest.mark.parametrize(
    "replacement_timing",
    ("before_link", "after_link", "in_place_after_link"),
)
def test_export_rejects_temporary_artifact_content_races(
    replacement_timing: str,
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = allowed_root / f"temp-race-{replacement_timing}"
    artifacts = (
        ArtifactBlob("a.svg", "image/svg+xml", b"GOOD", 1, 1),
        ArtifactBlob(
            "render_manifest.json",
            "application/json",
            b"{}",
            None,
            None,
        ),
    )
    real_link_new = export_writer._link_new  # pyright: ignore[reportPrivateUsage]
    replaced = False

    def replace_alias(descriptor: int, temporary_name: str) -> None:
        os.unlink(temporary_name, dir_fd=descriptor)
        replacement = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=descriptor,
        )
        try:
            assert os.write(replacement, b"EVIL") == 4
            os.fchmod(replacement, 0o600)
            os.fsync(replacement)
        finally:
            os.close(replacement)

    def race_first_link(
        descriptor: int,
        name: str,
        temporary_name: str,
    ) -> None:
        nonlocal replaced
        if replaced:
            real_link_new(descriptor, name, temporary_name)
            return
        replaced = True
        if replacement_timing == "before_link":
            replace_alias(descriptor, temporary_name)
            real_link_new(descriptor, name, temporary_name)
            return
        real_link_new(descriptor, name, temporary_name)
        if replacement_timing == "after_link":
            replace_alias(descriptor, temporary_name)
            return
        replacement = os.open(
            temporary_name,
            os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=descriptor,
        )
        try:
            assert os.write(replacement, b"EVIL") == 4
            os.fsync(replacement)
        finally:
            os.close(replacement)

    monkeypatch.setattr(export_writer, "_link_new", race_first_link)
    with pytest.raises(RenderMcpError) as raised:
        export_writer.export_bundle(
            output,
            (allowed_root,),
            artifacts,
            manifest_name="render_manifest.json",
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert replaced is True
    if replacement_timing == "in_place_after_link":
        assert not output.exists()
    else:
        remaining = tuple(output.iterdir())
        assert len(remaining) == 1
        assert remaining[0].read_bytes() == b"EVIL"


@pytest.mark.parametrize("mutation", ("replace", "in_place"))
def test_export_validates_manifest_temporary_before_commit(
    mutation: str,
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = allowed_root / f"manifest-temp-race-{mutation}"
    artifacts = (
        ArtifactBlob("a.svg", "image/svg+xml", b"GOOD", 1, 1),
        ArtifactBlob("render_manifest.json", "application/json", b"{}"),
    )
    real_write = export_writer._write_temporary  # pyright: ignore[reportPrivateUsage]
    writes = 0

    def write_then_mutate_manifest(
        descriptor: int,
        content: bytes,
    ) -> export_writer._TemporaryArtifact:  # pyright: ignore[reportPrivateUsage]
        nonlocal writes
        temporary = real_write(descriptor, content)
        writes += 1
        if writes != 2:
            return temporary
        if mutation == "replace":
            os.unlink(temporary.name, dir_fd=descriptor)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        else:
            flags = os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        replacement = os.open(temporary.name, flags, 0o600, dir_fd=descriptor)
        try:
            assert os.write(replacement, b"[]") == 2
            os.fsync(replacement)
        finally:
            os.close(replacement)
        return temporary

    monkeypatch.setattr(export_writer, "_write_temporary", write_then_mutate_manifest)

    with pytest.raises(RenderMcpError) as raised:
        export_writer.export_bundle(
            output,
            (allowed_root,),
            artifacts,
            manifest_name="render_manifest.json",
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    if mutation == "replace":
        remaining = tuple(output.iterdir())
        assert len(remaining) == 1
        assert remaining[0].read_bytes() == b"[]"
    else:
        assert not output.exists()


def test_export_rejects_duplicate_names(allowed_root: Path) -> None:
    output = allowed_root / "duplicate-export"
    artifacts = (
        ArtifactBlob("a.svg", "image/svg+xml", b"first", 1, 1),
        ArtifactBlob("a.svg", "image/svg+xml", b"second", 1, 1),
        ArtifactBlob("render_manifest.json", "application/json", b"{}"),
    )

    with pytest.raises(RenderMcpError) as raised:
        export_writer.export_bundle(
            output,
            (allowed_root,),
            artifacts,
            manifest_name="render_manifest.json",
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert not output.exists()


@pytest.mark.parametrize("reserved_name", (False, True))
def test_store_rejects_nonunique_image_artifact_names_before_result_allocation(
    reserved_name: bool,
    runtime_config: RendererRuntimeConfig,
) -> None:
    name = "render_manifest.json" if reserved_name else "M00001.svg"
    first = ArtifactBlob(
        name,
        "application/json" if reserved_name else "image/svg+xml",
        b"first",
        None if reserved_name else 1,
        None if reserved_name else 1,
    )
    artifacts = (first,) if reserved_name else (first, first)
    store = RenderArtifactStore(runtime_config)
    store.open()
    try:
        scope = runtime_config.state_root / str(
            store._scope_name  # pyright: ignore[reportPrivateUsage]
        )
        entries_before = {item.name for item in scope.iterdir()}
        with pytest.raises(ValueError, match="must be unique"):
            store.retain(
                target_ids=("M00001",),
                artifacts=artifacts,
                warnings=(),
                manifest_context={},
                output_directory=None,
            )
        assert {item.name for item in scope.iterdir()} == entries_before
        assert store.snapshot().active_result_count == 0
    finally:
        store.close()


def test_export_rejects_output_directory_replacement_during_publication(
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = allowed_root / "images"
    moved = allowed_root / "moved-images"
    replacement_marker = output / "replacement"
    artifacts = (
        ArtifactBlob("a.svg", "image/svg+xml", b"GOOD", 1, 1),
        ArtifactBlob("render_manifest.json", "application/json", b"{}"),
    )
    real_link_new = export_writer._link_new  # pyright: ignore[reportPrivateUsage]
    replaced = False

    def replace_before_first_link(
        descriptor: int,
        name: str,
        temporary_name: str,
    ) -> None:
        nonlocal replaced
        if not replaced:
            output.rename(moved)
            output.mkdir(mode=0o700)
            replacement_marker.write_bytes(b"preserve")
            os.chmod(replacement_marker, 0o600)
            replaced = True
        real_link_new(descriptor, name, temporary_name)

    monkeypatch.setattr(export_writer, "_link_new", replace_before_first_link)
    with pytest.raises(RenderMcpError) as raised:
        export_writer.export_bundle(
            output,
            (allowed_root,),
            artifacts,
            manifest_name="render_manifest.json",
        )
    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert replacement_marker.read_bytes() == b"preserve"
    assert tuple(moved.iterdir()) == ()


def test_export_rejects_artifact_replacement_during_publication(
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = allowed_root / "images"
    artifact = output / "a.svg"
    artifacts = (
        ArtifactBlob("a.svg", "image/svg+xml", b"GOOD", 1, 1),
        ArtifactBlob("render_manifest.json", "application/json", b"{}"),
    )
    real_link_new = export_writer._link_new  # pyright: ignore[reportPrivateUsage]
    replaced = False

    def replace_after_first_link(
        descriptor: int,
        name: str,
        temporary_name: str,
    ) -> None:
        nonlocal replaced
        real_link_new(descriptor, name, temporary_name)
        if not replaced:
            artifact.unlink()
            artifact.write_bytes(b"preserve")
            os.chmod(artifact, 0o600)
            replaced = True

    monkeypatch.setattr(export_writer, "_link_new", replace_after_first_link)
    with pytest.raises(RenderMcpError) as raised:
        export_writer.export_bundle(
            output,
            (allowed_root,),
            artifacts,
            manifest_name="render_manifest.json",
        )
    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert artifact.read_bytes() == b"preserve"
    assert {item.name for item in output.iterdir()} == {"a.svg"}


def test_state_root_symlink_is_rejected(
    runtime_config: RendererRuntimeConfig, tmp_path: Path
) -> None:
    real = tmp_path / "real-state"
    real.mkdir(mode=0o700)
    link = tmp_path / "state-link"
    link.symlink_to(real, target_is_directory=True)
    config = runtime_config.model_copy(update={"state_root": link})
    with pytest.raises((OSError, ValueError)):
        RenderArtifactStore(config).open()


def test_owner_only_state_root_is_required(runtime_config: RendererRuntimeConfig) -> None:
    runtime_config.state_root.mkdir(mode=0o700)
    os.chmod(runtime_config.state_root, 0o755)
    with pytest.raises(ValueError, match="owner-only"):
        RenderArtifactStore(runtime_config).open()


def test_result_count_quota_is_checked_before_allocating_a_directory(
    runtime_config: RendererRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(
        update={"limits": runtime_config.limits.model_copy(update={"max_results": 1})}
    )
    store = RenderArtifactStore(config)
    store.open()
    try:
        store.retain(
            target_ids=("M00001",),
            artifacts=(ArtifactBlob("M00001.svg", "image/svg+xml", b"<svg/>", 1, 1),),
            warnings=(),
            manifest_context={},
            output_directory=None,
        )
        with pytest.raises(RenderMcpError) as raised:
            store.retain(
                target_ids=("M00002",),
                artifacts=(ArtifactBlob("M00002.svg", "image/svg+xml", b"<svg/>", 1, 1),),
                warnings=(),
                manifest_context={},
                output_directory=None,
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
        assert store.snapshot().active_result_count == 1
        scope = config.state_root / str(store._scope_name)  # pyright: ignore[reportPrivateUsage]
        assert {path.name for path in scope.iterdir()} == {
            ".scope.lock",
            next(iter(store._results)),  # pyright: ignore[reportPrivateUsage]
        }
    finally:
        store.close()


def test_storage_quota_reserves_blocks_and_metadata_before_mkdir(
    runtime_config: RendererRuntimeConfig,
) -> None:
    limits = RendererLimits(
        max_asset_bytes=100,
        max_svg_bytes=100,
        max_result_bytes=2_000,
        max_disk_bytes=10_000,
    )
    config = runtime_config.model_copy(update={"limits": limits})
    store = RenderArtifactStore(config)
    store.open()
    try:
        with pytest.raises(RenderMcpError) as raised:
            store.retain(
                target_ids=("M00001",),
                artifacts=(ArtifactBlob("M00001.svg", "image/svg+xml", b"<svg/>", 1, 1),),
                warnings=(),
                manifest_context={},
                output_directory=None,
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
        scope = config.state_root / str(store._scope_name)  # pyright: ignore[reportPrivateUsage]
        assert {path.name for path in scope.iterdir()} == {".scope.lock"}
    finally:
        store.close()


def test_abandoned_scope_cleanup_rejects_unbounded_result_counts(
    runtime_config: RendererRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(
        update={"limits": runtime_config.limits.model_copy(update={"max_results": 1})}
    )
    config.state_root.mkdir(mode=0o700)
    abandoned = config.state_root / ("scope_" + "a" * 32)
    abandoned.mkdir(mode=0o700)
    lease = abandoned / ".scope.lock"
    lease.touch(mode=0o600)
    os.chmod(lease, 0o600)
    for marker in ("a", "b"):
        (abandoned / ("render_" + marker * 32)).mkdir(mode=0o700)

    with pytest.raises(ValueError, match="already active or unsafe"):
        RenderArtifactStore(config).open()
    assert abandoned.exists()


def test_failed_deletion_remains_accounted_and_retryable(
    runtime_config: RendererRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RenderArtifactStore(runtime_config)
    store.open()
    result = store.retain(
        target_ids=("M00001",),
        artifacts=(ArtifactBlob("M00001.svg", "image/svg+xml", b"<svg/>", 1, 1),),
        warnings=(),
        manifest_context={},
        output_directory=None,
    )
    real_remove = store._remove_result_directory  # pyright: ignore[reportPrivateUsage]

    def fail_remove(render_id: str, *, ignore_errors: bool) -> None:
        del render_id, ignore_errors
        raise OSError("synthetic delete failure")

    monkeypatch.setattr(store, "_remove_result_directory", fail_remove)
    try:
        with pytest.raises(OSError, match="synthetic delete failure"):
            store.delete(result.render_id)
        assert store.get(result.render_id) == result
        assert store.snapshot().active_result_count == 1
    finally:
        monkeypatch.setattr(store, "_remove_result_directory", real_remove)
        store.close()


def test_read_only_snapshot_does_not_delete_expired_files_and_explains_quota(
    runtime_config: RendererRuntimeConfig,
) -> None:
    store = RenderArtifactStore(runtime_config)
    store.open()
    result = store.retain(
        target_ids=("M00001",),
        artifacts=(ArtifactBlob("M00001.svg", "image/svg+xml", b"<svg/>", 1, 1),),
        warnings=(),
        manifest_context={},
        output_directory=None,
    )
    stored = store._results[result.render_id]  # pyright: ignore[reportPrivateUsage]
    expired_result = stored.result.model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    store._results[result.render_id] = type(stored)(  # pyright: ignore[reportPrivateUsage]
        expired_result,
        stored.total_bytes,
        stored.storage_bytes,
    )
    scope_name = store._scope_name  # pyright: ignore[reportPrivateUsage]
    assert scope_name is not None
    retained_directory = runtime_config.state_root / scope_name / result.render_id
    before = tuple(sorted(path.name for path in retained_directory.iterdir()))

    snapshot = store.snapshot()

    assert snapshot.active_result_count == 0
    assert snapshot.cleanup_pending_result_count == 1
    assert snapshot.retained_bytes == stored.total_bytes
    assert snapshot.retained_storage_bytes == stored.storage_bytes
    assert tuple(sorted(path.name for path in retained_directory.iterdir())) == before

    store._purge_expired()  # pyright: ignore[reportPrivateUsage]
    assert not retained_directory.exists()
    store.close()
