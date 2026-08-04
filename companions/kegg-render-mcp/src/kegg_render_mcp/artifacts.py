"""Process-scoped, bounded, atomic render artifact retention."""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from kegg_render_mcp import __version__
from kegg_render_mcp._filesystem import bounded_directory_names as _bounded_directory_names
from kegg_render_mcp._state_scope import (
    RendererStateScope,
    cleanup_state_scope,
    open_state_scope,
    release_state_scope,
)
from kegg_render_mcp._state_scope import (
    validate_named_directory as _validate_named_directory,
)
from kegg_render_mcp._state_scope import (
    validate_owner_only_directory as _validate_owner_only_directory,
)
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    ARTIFACT_NAME_PATTERN,
    MAX_ARTIFACTS,
    RENDER_ID_PATTERN,
    ArtifactKind,
    ArtifactMetadata,
    DeleteRenderResult,
    ErrorCode,
    ErrorDetail,
    RenderMcpError,
    RenderResult,
)
from kegg_render_mcp.export_writer import export_bundle

_ARTIFACT_NAME = re.compile(rf"{ARTIFACT_NAME_PATTERN}\Z")
_RESULT_ID = re.compile(rf"{RENDER_ID_PATTERN}\Z")
_MIME_TYPES: Final = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}
_ALLOCATION_UNIT_BYTES: Final = 4_096
_ARTIFACT_METADATA_RESERVE_BYTES: Final = 1_024
_RESULT_METADATA_RESERVE_BYTES: Final = 4_096


@dataclass(frozen=True, slots=True)
class ArtifactBlob:
    name: str
    mime_type: str
    content: bytes
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class _StoredResult:
    result: RenderResult
    total_bytes: int
    storage_bytes: int


@dataclass(frozen=True, slots=True)
class ArtifactStoreSnapshot:
    """Read-only in-memory retention and cleanup-pending counts."""

    active_result_count: int
    cleanup_pending_result_count: int
    retained_bytes: int
    retained_storage_bytes: int


class RenderArtifactStore:
    """One-process scope with opaque IDs and no cross-process namespace reuse."""

    def __init__(self, config: RendererRuntimeConfig) -> None:
        self._config = config
        self._state_scope: RendererStateScope | None = None
        self._state_fd: int | None = None
        self._scope_fd: int | None = None
        self._scope_name: str | None = None
        self._lock_fd: int | None = None
        self._scope_lock_fd: int | None = None
        self._results: dict[str, _StoredResult] = {}

    def snapshot(self) -> ArtifactStoreSnapshot:
        """Inspect current records without changing memory or the filesystem."""
        now = datetime.now(UTC)
        active = sum(stored.result.expires_at > now for stored in self._results.values())
        return ArtifactStoreSnapshot(
            active_result_count=active,
            cleanup_pending_result_count=len(self._results) - active,
            retained_bytes=sum(stored.total_bytes for stored in self._results.values()),
            retained_storage_bytes=sum(stored.storage_bytes for stored in self._results.values()),
        )

    def open(self) -> None:
        if self._state_fd is not None:
            raise RuntimeError("renderer artifact store is already open")
        scope = open_state_scope(
            self._config.state_root,
            self._config.limits.max_results,
        )
        self._state_scope = scope
        self._state_fd = scope.state_fd
        self._scope_fd = scope.scope_fd
        self._scope_name = scope.scope_name
        self._lock_fd = scope.coordination_lock_fd
        self._scope_lock_fd = scope.scope_lock_fd

    def close(self) -> None:
        try:
            if self._scope_fd is not None:
                for render_id in tuple(self._results):
                    self._remove_result_directory(render_id, ignore_errors=True)
            if self._state_scope is not None:
                with contextlib.suppress(OSError, ValueError):
                    cleanup_state_scope(
                        self._state_scope,
                        self._config.limits.max_results,
                    )
        finally:
            try:
                if self._state_scope is not None:
                    release_state_scope(self._state_scope)
            finally:
                self._state_scope = None
                self._scope_fd = None
                self._state_fd = None
                self._scope_name = None
                self._lock_fd = None
                self._scope_lock_fd = None
                self._results.clear()

    def retain(
        self,
        *,
        target_ids: tuple[str, ...],
        artifacts: tuple[ArtifactBlob, ...],
        warnings: tuple[str, ...],
        manifest_context: dict[str, object],
        output_directory: Path | None,
        remove_created_output_directory_on_failure: bool = False,
    ) -> RenderResult:
        self._require_open()
        self._purge_expired()
        if not artifacts:
            raise ValueError("a render result requires at least one image artifact")
        if len(self._results) >= self._config.limits.max_results:
            raise _output_limit("The process-scoped renderer result-count quota is exhausted.")
        for item in artifacts:
            _validate_blob(item)
        render_id = self._new_id()
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=self._config.retention_seconds)
        manifest_name = "render_manifest.json"
        manifest = {
            "schema_version": "2",
            "renderer": {"name": "kegg-render-mcp", "version": __version__},
            "created_at": created_at.isoformat(),
            "target_ids": target_ids,
            "warnings": warnings,
            "artifacts": [
                {
                    "path": item.name,
                    "mime_type": item.mime_type,
                    "byte_size": len(item.content),
                    "width": item.width,
                    "height": item.height,
                }
                for item in artifacts
            ],
            "provenance": manifest_context,
            "interpretation": (
                "Graphics visualize annotation evidence. They do not establish pathway presence, "
                "activity, flux, phenotype, or experimental validation."
            ),
        }
        manifest_bytes = json.dumps(
            manifest, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        all_artifacts = (
            *artifacts,
            ArtifactBlob(manifest_name, "application/json", manifest_bytes),
        )
        if len(all_artifacts) > MAX_ARTIFACTS:
            raise _output_limit("The retained result exceeds the artifact-count limit.")
        for item in all_artifacts:
            _validate_blob(item)
        total_bytes = sum(len(item.content) for item in all_artifacts)
        storage_bytes = _estimated_storage_bytes(all_artifacts)
        if total_bytes > self._config.limits.max_result_bytes:
            raise _output_limit("The retained result exceeds the configured byte limit.")
        current_storage = sum(item.storage_bytes for item in self._results.values())
        if current_storage + storage_bytes > self._config.limits.max_disk_bytes:
            raise _output_limit("The process-scoped renderer quota is exhausted.")
        assert self._scope_fd is not None
        os.mkdir(render_id, mode=0o700, dir_fd=self._scope_fd)
        result_fd = os.open(
            render_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self._scope_fd,
        )
        _validate_owner_only_directory(result_fd)
        try:
            for item in all_artifacts:
                _atomic_write_fd(result_fd, item.name, item.content)
            if output_directory is not None:
                export_bundle(
                    output_directory,
                    self._config.allowed_roots,
                    all_artifacts,
                    manifest_name=manifest_name,
                    remove_created_directory_on_failure=(
                        remove_created_output_directory_on_failure
                    ),
                )
            metadata = tuple(
                ArtifactMetadata(
                    name=item.name,
                    kind=(
                        ArtifactKind.MANIFEST if item.name == manifest_name else ArtifactKind.IMAGE
                    ),
                    mime_type=item.mime_type,  # type: ignore[arg-type]
                    byte_size=len(item.content),
                    width=item.width,
                    height=item.height,
                    resource_uri=f"kegg-render://results/{render_id}/{item.name}",
                    output_path=(
                        str(output_directory / item.name) if output_directory is not None else None
                    ),
                )
                for item in all_artifacts
            )
            result = RenderResult(
                render_id=render_id,
                created_at=created_at,
                expires_at=expires_at,
                target_ids=target_ids,
                artifacts=metadata,
                warnings=warnings,
                result_uri=f"kegg-render://results/{render_id}",
                output_directory=(str(output_directory) if output_directory is not None else None),
            )
            self._results[render_id] = _StoredResult(result, total_bytes, storage_bytes)
            return result
        except Exception:
            self._remove_result_directory(render_id, ignore_errors=True)
            raise
        finally:
            os.close(result_fd)

    def get(self, render_id: str) -> RenderResult:
        return self._lookup(render_id).result

    def read(self, render_id: str, artifact_name: str) -> ArtifactBlob:
        stored = self._lookup(render_id)
        if _ARTIFACT_NAME.fullmatch(artifact_name) is None:
            raise _not_found()
        metadata = next(
            (item for item in stored.result.artifacts if item.name == artifact_name), None
        )
        if metadata is None:
            raise _not_found()
        assert self._scope_fd is not None
        try:
            result_fd = os.open(
                render_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self._scope_fd,
            )
            try:
                descriptor = os.open(artifact_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=result_fd)
            except Exception:
                os.close(result_fd)
                raise
        except OSError as error:
            raise _not_found() from error
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != metadata.byte_size:
                raise _not_found()
            content = bytearray()
            while len(content) <= metadata.byte_size:
                chunk = os.read(descriptor, min(64 * 1024, metadata.byte_size + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
            if len(content) != metadata.byte_size:
                raise _not_found()
        finally:
            os.close(descriptor)
            os.close(result_fd)
        return ArtifactBlob(
            name=metadata.name,
            mime_type=metadata.mime_type,
            content=bytes(content),
            width=metadata.width,
            height=metadata.height,
        )

    def delete(self, render_id: str) -> DeleteRenderResult:
        self._lookup(render_id)
        self._remove_result_directory(render_id, ignore_errors=False)
        self._results.pop(render_id, None)
        return DeleteRenderResult(render_id=render_id)

    def _lookup(self, render_id: str) -> _StoredResult:
        self._purge_expired()
        stored = self._results.get(render_id)
        if (
            _RESULT_ID.fullmatch(render_id) is None
            or stored is None
            or stored.result.expires_at <= datetime.now(UTC)
        ):
            raise _not_found()
        return stored

    def _purge_expired(self) -> None:
        now = datetime.now(UTC)
        expired = [
            render_id
            for render_id, stored in self._results.items()
            if stored.result.expires_at <= now
        ]
        for render_id in expired:
            try:
                self._remove_result_directory(render_id, ignore_errors=False)
            except OSError:
                continue
            self._results.pop(render_id)

    def _new_id(self) -> str:
        for _ in range(8):
            render_id = f"render_{secrets.token_urlsafe(24)}"
            if render_id not in self._results:
                return render_id
        raise OSError("could not allocate render ID")

    def _require_open(self) -> None:
        if self._scope_fd is None:
            raise RuntimeError("renderer artifact store is not open")

    def _remove_result_directory(self, render_id: str, *, ignore_errors: bool) -> None:
        if self._scope_fd is None:
            return
        try:
            result_fd = os.open(
                render_id,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=self._scope_fd,
            )
            try:
                _validate_owner_only_directory(result_fd)
                _validate_named_directory(
                    self._scope_fd,
                    render_id,
                    result_fd,
                    "renderer result",
                )
                for name in _bounded_directory_names(
                    result_fd,
                    MAX_ARTIFACTS + 1,
                    "renderer result directory",
                ):
                    os.unlink(name, dir_fd=result_fd)
                _validate_named_directory(
                    self._scope_fd,
                    render_id,
                    result_fd,
                    "renderer result",
                )
                os.rmdir(render_id, dir_fd=self._scope_fd)
            finally:
                os.close(result_fd)
        except OSError:
            if not ignore_errors:
                raise


def _estimated_storage_bytes(artifacts: tuple[ArtifactBlob, ...]) -> int:
    total = _RESULT_METADATA_RESERVE_BYTES
    for item in artifacts:
        allocated_payload = (
            (len(item.content) + _ALLOCATION_UNIT_BYTES - 1) // _ALLOCATION_UNIT_BYTES
        ) * _ALLOCATION_UNIT_BYTES
        total += allocated_payload + _ARTIFACT_METADATA_RESERVE_BYTES
    return total


def _atomic_write_fd(directory_descriptor: int, name: str, content: bytes) -> None:
    if _ARTIFACT_NAME.fullmatch(name) is None or Path(name).name != name:
        raise ValueError("invalid derived artifact name")
    try:
        existing = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.INPUT_PATH_REJECTED,
                message="An output artifact path is not a direct regular file.",
                suggested_action="Use an empty controlled output directory.",
            )
        )
    temporary_name = f".tmp-{secrets.token_urlsafe(16)}"
    descriptor = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_descriptor,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(descriptor, content[offset:])
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary_name,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
        os.fsync(directory_descriptor)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_descriptor)
        raise


def _validate_blob(item: ArtifactBlob) -> None:
    if _ARTIFACT_NAME.fullmatch(item.name) is None or Path(item.name).suffix not in _MIME_TYPES:
        raise ValueError("invalid derived artifact name")
    if _MIME_TYPES[Path(item.name).suffix] != item.mime_type or not item.content:
        raise ValueError("artifact media metadata is inconsistent")
    if (item.width is None) != (item.height is None):
        raise ValueError("artifact dimensions are incomplete")


def _not_found() -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.RESULT_NOT_FOUND,
            message="The scoped render result was not found.",
            suggested_action="Render again in this stdio process.",
        )
    )


def _output_limit(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            message=message,
            suggested_action="Select fewer targets or request SVG only.",
        )
    )
