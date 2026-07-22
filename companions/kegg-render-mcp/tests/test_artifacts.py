"""Process-scoped retention, quota, export, and deletion tests."""

from __future__ import annotations

import contextlib
import fcntl
import multiprocessing
import os
from datetime import UTC, datetime, timedelta
from multiprocessing.connection import Connection
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from conftest import SyntheticProvider
from kegg_render_mcp import _state_scope as state_scope_module
from kegg_render_mcp import artifacts as artifacts_module
from kegg_render_mcp import export_writer
from kegg_render_mcp.artifacts import ArtifactBlob, RenderArtifactStore
from kegg_render_mcp.config import RendererLimits, RendererRuntimeConfig
from kegg_render_mcp.contracts import MAX_ARTIFACTS, ErrorCode, RenderFormat, RenderMcpError
from kegg_render_mcp.render_service import RendererService


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
    assert snapshot.active_result_count == 0
    assert tuple(sorted(path.name for path in retained_directory.iterdir())) == before

    store._purge_expired()  # pyright: ignore[reportPrivateUsage]
    assert not retained_directory.exists()
    store.close()
