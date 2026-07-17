"""Process-scoped retention, quota, export, and deletion tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import SyntheticProvider
from kegg_render_mcp.artifacts import ArtifactBlob, RenderArtifactStore
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import ErrorCode, RenderFormat, RenderMcpError
from kegg_render_mcp.render_service import RendererService


@pytest.mark.asyncio
async def test_service_renders_both_targets_formats_and_manifest(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    synthetic_provider: SyntheticProvider,
) -> None:
    service = RendererService(runtime_config, synthetic_provider)
    service.open()
    try:
        output = allowed_root / "images"
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
        manifest = service.store.read(result.render_id, "render_manifest.json")
        assert b'"calculation_method"' in manifest.content
        assert b'"parser_version"' in manifest.content
        assert b'"analysis_unit":"unknown"' in manifest.content
        assert str(runtime_config.state_root).encode() not in manifest.content
    finally:
        service.close()


@pytest.mark.asyncio
async def test_result_is_process_scoped_and_deletion_uses_safe_not_found(
    runtime_config: RendererRuntimeConfig, render_input_file: Path
) -> None:
    first = RendererService(runtime_config, SyntheticProvider())
    first.open()
    result = await first.render(
        render_input_path=str(render_input_file),
        target_ids=("M00001",),
        formats=(RenderFormat.SVG,),
        output_directory=None,
    )
    second_config = runtime_config.model_copy(
        update={"state_root": runtime_config.state_root.parent / "other-state"}
    )
    second = RendererService(second_config, SyntheticProvider())
    second.open()
    try:
        with pytest.raises(RenderMcpError) as missing:
            second.store.get(result.render_id)
        assert missing.value.detail.code is ErrorCode.RESULT_NOT_FOUND
        assert first.store.delete(result.render_id).deleted is True
        for operation in (
            lambda: first.store.get(result.render_id),
            lambda: first.store.read(result.render_id, "M00001.svg"),
            lambda: first.store.delete(result.render_id),
        ):
            with pytest.raises(RenderMcpError) as deleted:
                operation()
            assert deleted.value.detail.code is ErrorCode.RESULT_NOT_FOUND
    finally:
        second.close()
        first.close()


def test_state_root_is_exclusive_and_cleans_abandoned_scope(
    runtime_config: RendererRuntimeConfig,
) -> None:
    first = RenderArtifactStore(runtime_config)
    first.open()
    second = RenderArtifactStore(runtime_config)
    try:
        with pytest.raises(ValueError, match="already active"):
            second.open()
    finally:
        first.close()
    abandoned = runtime_config.state_root / "scope_abandoned"
    abandoned.mkdir(mode=0o700)
    result = abandoned / ("render_" + "a" * 32)
    result.mkdir(mode=0o700)
    (result / "M00001.svg").write_text("<svg/>", encoding="utf-8")
    third = RenderArtifactStore(runtime_config)
    third.open()
    try:
        assert not abandoned.exists()
    finally:
        third.close()


@pytest.mark.asyncio
async def test_export_rejects_symlink_artifact_destination(
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
        assert raised.value.detail.code is ErrorCode.INPUT_PATH_REJECTED
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


@pytest.mark.asyncio
async def test_failed_export_rolls_back_new_files_and_commit_manifest(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    allowed_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = allowed_root / "images"
    output.mkdir(mode=0o700)
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
                output_directory=str(output),
            )
        assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
        assert not tuple(output.iterdir())
    finally:
        service.close()


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
        assert store.result_count == 1
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
    )
    scope_name = store._scope_name  # pyright: ignore[reportPrivateUsage]
    assert scope_name is not None
    retained_directory = runtime_config.state_root / scope_name / result.render_id
    before = tuple(sorted(path.name for path in retained_directory.iterdir()))

    snapshot = store.snapshot()

    assert snapshot.active_result_count == 0
    assert snapshot.cleanup_pending_result_count == 1
    assert snapshot.retained_bytes == stored.total_bytes
    assert store.result_count == 0
    assert tuple(sorted(path.name for path in retained_directory.iterdir())) == before

    store._purge_expired()  # pyright: ignore[reportPrivateUsage]
    assert not retained_directory.exists()
    store.close()
