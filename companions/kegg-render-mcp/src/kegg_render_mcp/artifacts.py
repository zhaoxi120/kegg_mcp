"""Render orchestration plus process-scoped, bounded, atomic artifact retention."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from kegg_mcp.kegg import KeggBatchProvenance

from kegg_render_mcp import __version__
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    ArtifactKind,
    ArtifactMetadata,
    DeleteRenderResult,
    ErrorCode,
    ErrorDetail,
    RenderFormat,
    RenderMcpError,
    RenderResult,
)
from kegg_render_mcp.module_scene import construct_module_scene
from kegg_render_mcp.pathway_scene import PathwayAssetProvider, construct_pathway_scene
from kegg_render_mcp.raster import render_module_png, render_pathway_png, validate_png
from kegg_render_mcp.render_input import (
    load_render_input,
    open_allowed_directory,
    resolve_output_directory,
)
from kegg_render_mcp.svg import render_module_svg, render_pathway_svg

_ARTIFACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESULT_ID = re.compile(r"render_[A-Za-z0-9_-]{32}\Z")
_MIME_TYPES: Final = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".json": "application/json",
}


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


class RenderArtifactStore:
    """One-process scope with opaque IDs and no cross-process namespace reuse."""

    def __init__(self, config: RendererRuntimeConfig) -> None:
        self._config = config
        self._state_fd: int | None = None
        self._scope_fd: int | None = None
        self._scope_name: str | None = None
        self._lock_fd: int | None = None
        self._results: dict[str, _StoredResult] = {}

    @property
    def result_count(self) -> int:
        self._purge_expired()
        return len(self._results)

    def open(self) -> None:
        if self._state_fd is not None:
            raise RuntimeError("renderer artifact store is already open")
        self._state_fd = _open_or_create_private_directory(self._config.state_root)
        try:
            self._lock_fd = os.open(
                ".renderer.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._state_fd,
            )
            os.fchmod(self._lock_fd, 0o600)
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._cleanup_abandoned_scopes()
        except Exception:
            if self._lock_fd is not None:
                os.close(self._lock_fd)
            os.close(self._state_fd)
            self._lock_fd = None
            self._state_fd = None
            raise ValueError("renderer state root is already active or unsafe") from None
        for _ in range(8):
            name = f"scope_{secrets.token_urlsafe(24)}"
            try:
                os.mkdir(name, mode=0o700, dir_fd=self._state_fd)
            except FileExistsError:
                continue
            self._scope_fd = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self._state_fd,
            )
            _validate_owner_only_directory(self._scope_fd)
            self._scope_name = name
            return
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        os.close(self._state_fd)
        self._lock_fd = None
        self._state_fd = None
        raise OSError("could not allocate renderer process scope")

    def close(self) -> None:
        if self._scope_fd is not None:
            for render_id in tuple(self._results):
                self._remove_result_directory(render_id, ignore_errors=True)
            os.close(self._scope_fd)
        if self._state_fd is not None and self._scope_name is not None:
            with contextlib.suppress(OSError):
                os.rmdir(self._scope_name, dir_fd=self._state_fd)
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
        if self._state_fd is not None:
            os.close(self._state_fd)
        self._scope_fd = None
        self._state_fd = None
        self._scope_name = None
        self._lock_fd = None
        self._results.clear()

    def retain(
        self,
        *,
        target_ids: tuple[str, ...],
        artifacts: tuple[ArtifactBlob, ...],
        warnings: tuple[str, ...],
        manifest_context: dict[str, object],
        output_directory: Path | None,
    ) -> RenderResult:
        self._require_open()
        self._purge_expired()
        if not artifacts:
            raise ValueError("a render result requires at least one image artifact")
        render_id = self._new_id()
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=self._config.retention_seconds)
        manifest_name = "render_manifest.json"
        manifest = {
            "schema_version": "1",
            "renderer": {"name": "kegg-render-mcp", "version": __version__},
            "render_id": render_id,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "target_ids": target_ids,
            "warnings": warnings,
            "artifacts": [
                {
                    "name": item.name,
                    "mime_type": item.mime_type,
                    "byte_size": len(item.content),
                    "width": item.width,
                    "height": item.height,
                    "resource_uri": f"kegg-render://results/{render_id}/{item.name}",
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
        total_bytes = sum(len(item.content) for item in all_artifacts)
        if total_bytes > self._config.limits.max_result_bytes:
            raise _output_limit("The retained result exceeds the configured byte limit.")
        current_bytes = sum(item.total_bytes for item in self._results.values())
        if current_bytes + total_bytes > self._config.limits.max_disk_bytes:
            raise _output_limit("The process-scoped renderer quota is exhausted.")
        assert self._scope_fd is not None
        os.mkdir(render_id, mode=0o700, dir_fd=self._scope_fd)
        result_fd = os.open(
            render_id,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=self._scope_fd,
        )
        _validate_owner_only_directory(result_fd)
        output_fd: int | None = None
        try:
            for item in all_artifacts:
                _validate_blob(item)
                _atomic_write_fd(result_fd, item.name, item.content)
            if output_directory is not None:
                output_fd = open_allowed_directory(output_directory, self._config.allowed_roots)
                _remove_commit_manifest(output_fd, manifest_name)
                for item in all_artifacts:
                    _atomic_write_fd(output_fd, item.name, item.content)
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
            )
            self._results[render_id] = _StoredResult(result, total_bytes)
            return result
        except Exception:
            self._remove_result_directory(render_id, ignore_errors=True)
            raise
        finally:
            if output_fd is not None:
                os.close(output_fd)
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
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self._scope_fd,
            )
            try:
                for name in os.listdir(result_fd):
                    os.unlink(name, dir_fd=result_fd)
            finally:
                os.close(result_fd)
            os.rmdir(render_id, dir_fd=self._scope_fd)
        except OSError:
            if not ignore_errors:
                raise

    def _cleanup_abandoned_scopes(self) -> None:
        assert self._state_fd is not None
        for scope_name in os.listdir(self._state_fd):
            if not scope_name.startswith("scope_"):
                continue
            scope_fd = os.open(
                scope_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=self._state_fd,
            )
            try:
                _validate_owner_only_directory(scope_fd)
                for result_name in os.listdir(scope_fd):
                    result_fd = os.open(
                        result_name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=scope_fd,
                    )
                    try:
                        for artifact_name in os.listdir(result_fd):
                            os.unlink(artifact_name, dir_fd=result_fd)
                    finally:
                        os.close(result_fd)
                    os.rmdir(result_name, dir_fd=scope_fd)
            finally:
                os.close(scope_fd)
            os.rmdir(scope_name, dir_fd=self._state_fd)


class RendererService:
    """Pure-layer coordinator; it does not normalize or evaluate biological evidence."""

    def __init__(
        self,
        config: RendererRuntimeConfig,
        provider: PathwayAssetProvider,
        store: RenderArtifactStore | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.store = store or RenderArtifactStore(config)

    def open(self) -> None:
        self.store.open()

    def close(self) -> None:
        self.store.close()

    async def render(
        self,
        *,
        render_input_path: str,
        target_ids: tuple[str, ...] | None,
        formats: tuple[RenderFormat, ...],
        output_directory: str | None,
    ) -> RenderResult:
        source = load_render_input(render_input_path, self.config)
        selected = source.target_ids if target_ids is None else target_ids
        if not selected or len(selected) > 32 or len(selected) != len(set(selected)):
            raise RenderMcpError(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The selected render target set is empty, duplicated, or too large.",
                    suggested_action="Select one through 32 retained target identifiers.",
                )
            )
        output = resolve_output_directory(output_directory, self.config.allowed_roots)
        artifacts: list[ArtifactBlob] = []
        warnings: list[str] = []
        target_provenance: list[dict[str, object]] = []
        pathway_ids = {str(item.pathway_id) for item in source.document.pathways}
        module_ids = {str(item.module_id) for item in source.document.modules}
        for target_id in selected:
            if target_id in pathway_ids:
                if not target_id.startswith("ko"):
                    raise RenderMcpError(
                        ErrorDetail(
                            code=ErrorCode.TARGET_NOT_RENDERABLE,
                            message=(
                                "Only regular KO reference pathways are renderable in this release."
                            ),
                            suggested_action="Select a retained koNNNNN regular pathway target.",
                        )
                    )
                target = source.pathway(target_id)
                scene = await construct_pathway_scene(
                    source,
                    target,
                    self.provider,
                    max_asset_bytes=self.config.limits.max_asset_bytes,
                    max_pixels=self.config.limits.max_pixels,
                    limits=self.config.limits,
                )
                decoded = validate_png(
                    scene.source_png,
                    max_bytes=self.config.limits.max_asset_bytes,
                    max_pixels=self.config.limits.max_pixels,
                )
                if decoded != (scene.width, scene.height):
                    raise RenderMcpError(
                        ErrorDetail(
                            code=ErrorCode.ASSET_INVALID,
                            message="Pathway PNG dimensions do not match asset metadata.",
                            suggested_action="Refresh the matching pathway assets.",
                        )
                    )
                if RenderFormat.SVG in formats:
                    svg = render_pathway_svg(
                        scene,
                        max_bytes=self.config.limits.max_svg_bytes,
                        max_nodes=self.config.limits.max_svg_nodes,
                    )
                    artifacts.append(
                        ArtifactBlob(
                            f"{target_id}.svg", "image/svg+xml", svg.content, svg.width, svg.height
                        )
                    )
                if RenderFormat.PNG in formats:
                    png = render_pathway_png(
                        scene,
                        max_asset_bytes=self.config.limits.max_asset_bytes,
                        max_pixels=self.config.limits.max_pixels,
                        max_output_bytes=self.config.limits.max_result_bytes,
                    )
                    artifacts.append(
                        ArtifactBlob(
                            f"{target_id}.png", "image/png", png.content, png.width, png.height
                        )
                    )
                warnings.extend(scene.warnings)
                target_provenance.append(
                    {
                        "target_id": target_id,
                        "kind": "pathway",
                        "reference_namespace": scene.reference_namespace,
                        "reference_scope": target.reference_scope.value,
                        "evidence_mode": scene.evidence_mode,
                        "coverage_numerator": scene.coverage_numerator,
                        "coverage_denominator": scene.coverage_denominator,
                        "coverage_ratio": scene.coverage_ratio,
                        "calculation_method": target.calculation_method,
                        "calculation_version": target.calculation_version,
                        "reference_link_provenance": [
                            _safe_batch(item) for item in target.reference_link_provenance
                        ],
                        "reference_metadata_provenance": [
                            _safe_batch(item) for item in target.reference_metadata_provenance
                        ],
                        "assets": scene.asset_provenance,
                    }
                )
            elif target_id in module_ids:
                target = source.module(target_id)
                scene = construct_module_scene(
                    target,
                    analysis_unit=source.document.dataset.analysis_unit,
                    max_nodes=self.config.limits.max_svg_nodes,
                )
                if RenderFormat.SVG in formats:
                    svg = render_module_svg(
                        scene,
                        max_bytes=self.config.limits.max_svg_bytes,
                        max_nodes=self.config.limits.max_svg_nodes,
                    )
                    artifacts.append(
                        ArtifactBlob(
                            f"{target_id}.svg", "image/svg+xml", svg.content, svg.width, svg.height
                        )
                    )
                if RenderFormat.PNG in formats:
                    png = render_module_png(
                        scene,
                        max_pixels=self.config.limits.max_pixels,
                        max_output_bytes=self.config.limits.max_result_bytes,
                    )
                    artifacts.append(
                        ArtifactBlob(
                            f"{target_id}.png", "image/png", png.content, png.width, png.height
                        )
                    )
                warnings.extend(scene.warnings)
                target_provenance.append(
                    {
                        "target_id": target_id,
                        "kind": "module",
                        "strict_exact_completion": scene.strict_exact_completion,
                        "strict_block_coverage": scene.strict_block_coverage,
                        "lenient_exact_completion": scene.lenient_exact_completion,
                        "lenient_block_coverage": scene.lenient_block_coverage,
                        "parser_name": target.parser_name,
                        "parser_version": target.parser_version,
                        "resolver_version": target.resolver_version,
                        "strict_calculation_method": (
                            target.strict.calculation_method.model_dump(mode="json")
                        ),
                        "lenient_calculation_method": (
                            target.lenient.calculation_method.model_dump(mode="json")
                        ),
                        "reference_retrieval_provenance": [
                            _safe_batch(item) for item in target.reference_retrieval_provenance
                        ],
                    }
                )
            else:
                # Preserve one safe not-found behavior without guessing a target type.
                source.pathway(target_id)
        safe_warnings = tuple(dict.fromkeys(item[:1000] for item in warnings))[:32]
        return self.store.retain(
            target_ids=selected,
            artifacts=tuple(artifacts),
            warnings=safe_warnings,
            manifest_context={
                "render_input_schema_version": "2",
                "producer": source.document.producer.model_dump(mode="json"),
                "dataset_id": source.document.dataset.dataset_id,
                "analysis_unit": source.document.dataset.analysis_unit.value,
                "taxon_id": source.document.dataset.taxon_id,
                "kegg_organism_code": source.document.dataset.kegg_organism_code,
                "decision_policy": source.document.decision_policy.model_dump(mode="json"),
                "targets": target_provenance,
            },
            output_directory=output,
        )


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


def _remove_commit_manifest(directory_descriptor: int, name: str) -> None:
    """Invalidate an older export before installing a replacement bundle."""
    try:
        existing = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(existing.st_mode):
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.INPUT_PATH_REJECTED,
                message="The output manifest path is not a direct regular file.",
                suggested_action="Use an empty controlled output directory.",
            )
        )
    os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


def _open_or_create_private_directory(path: Path) -> int:
    if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
        raise ValueError("state root must be an absolute non-root path")
    parent_fd = _open_absolute_directory(path.parent)
    try:
        try:
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except FileNotFoundError:
            os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
            descriptor = os.open(
                path.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        _validate_owner_only_directory(descriptor)
        return descriptor
    finally:
        os.close(parent_fd)


def _open_absolute_directory(path: Path) -> int:
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
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_owner_only_directory(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("renderer state directories must be owner-only direct directories")


def _safe_batch(value: KeggBatchProvenance) -> dict[str, object]:
    result: dict[str, object] = {
        "operation": value.operation.value,
        "request_key": value.request_key,
        "access_mode": value.access_mode.value,
        "retrieval_endpoint_class": value.retrieval_endpoint_class.value,
        "origin": value.origin.value,
        "cache_lookup_state": value.cache_lookup_state.value,
        "retrieved_at": value.retrieved_at.isoformat(),
        "served_at": value.served_at.isoformat(),
        "is_stale": value.is_stale,
        "parser_name": value.parser_name,
        "parser_version": value.parser_version,
    }
    if value.database_release is not None:
        result["database_release"] = value.database_release
    return result


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
