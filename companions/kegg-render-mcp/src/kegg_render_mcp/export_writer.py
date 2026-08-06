"""Non-overwriting, commit-marked renderer export bundles."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kegg_render_mcp.contracts import (
    ARTIFACT_NAME_PATTERN,
    ErrorCode,
    ErrorDetail,
    RenderMcpError,
)
from kegg_render_mcp.render_input import (
    assert_allowed_directory_identity,
    open_allowed_directory,
    remove_created_empty_directory,
)

_ARTIFACT_NAME = re.compile(rf"{ARTIFACT_NAME_PATTERN}\Z")


@dataclass(frozen=True, slots=True)
class _TemporaryArtifact:
    name: str
    descriptor: int
    identity: tuple[int, int]
    byte_size: int
    digest: bytes


@dataclass(frozen=True, slots=True)
class _InstalledArtifact:
    name: str
    identity: tuple[int, int]
    byte_size: int
    digest: bytes
    remove_on_rollback: bool


class ExportArtifact(Protocol):
    """Small structural contract shared with retained renderer artifacts."""

    @property
    def name(self) -> str: ...

    @property
    def content(self) -> bytes: ...


def export_bundle(
    output_directory: Path,
    allowed_roots: tuple[Path, ...],
    artifacts: tuple[ExportArtifact, ...],
    *,
    manifest_name: str,
) -> None:
    """Install one complete export into a new or empty controlled directory."""
    ordered, manifest = _partition_bundle(artifacts, manifest_name)
    descriptor, created = open_allowed_directory(output_directory, allowed_roots)
    temporaries: dict[str, _TemporaryArtifact] = {}
    installed: list[_InstalledArtifact] = []
    temporary_aliases_removed = False
    committed = False
    try:
        _require_empty_directory(descriptor)
        for item in (*ordered, manifest):
            temporaries[item.name] = _write_temporary(descriptor, item.content)
        for item in ordered:
            temporary = temporaries[item.name]
            _link_new(descriptor, item.name, temporary.name)
            installed_item = _installed_artifact(descriptor, item.name, temporary)
            installed.append(installed_item)
        assert_allowed_directory_identity(output_directory, allowed_roots, descriptor)
        _assert_installed_artifacts(descriptor, installed)
        manifest_temporary = temporaries[manifest.name]
        _assert_temporary_artifact(descriptor, manifest_temporary)
        _link_new(descriptor, manifest.name, manifest_temporary.name)
        installed_manifest = _installed_artifact(
            descriptor,
            manifest.name,
            manifest_temporary,
        )
        installed.append(installed_manifest)
        for temporary in temporaries.values():
            _unlink_temporary_alias(descriptor, temporary)
        temporary_aliases_removed = True
        os.fsync(descriptor)
        assert_allowed_directory_identity(output_directory, allowed_roots, descriptor)
        _assert_installed_artifacts(descriptor, installed)
        committed = True
    except FileExistsError:
        raise _already_exists() from None
    except RenderMcpError:
        raise
    except (OSError, ValueError):
        raise _write_failed() from None
    finally:
        if not committed:
            _rollback_installed(descriptor, installed)
        if not temporary_aliases_removed:
            for temporary in temporaries.values():
                with contextlib.suppress(OSError):
                    _unlink_temporary_alias(descriptor, temporary)
        for temporary in temporaries.values():
            os.close(temporary.descriptor)
        if not committed:
            with contextlib.suppress(OSError):
                os.fsync(descriptor)
        if not committed and created:
            remove_created_empty_directory(output_directory, allowed_roots, descriptor)
        os.close(descriptor)


def _partition_bundle(
    artifacts: tuple[ExportArtifact, ...],
    manifest_name: str,
) -> tuple[tuple[ExportArtifact, ...], ExportArtifact]:
    names = tuple(item.name for item in artifacts)
    manifests = tuple(item for item in artifacts if item.name == manifest_name)
    ordered = tuple(item for item in artifacts if item.name != manifest_name)
    if (
        not ordered
        or len(manifests) != 1
        or len(names) != len(set(names))
        or any(_ARTIFACT_NAME.fullmatch(name) is None for name in names)
    ):
        raise _write_failed()
    return ordered, manifests[0]


def _require_empty_directory(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        if next(entries, None) is not None:
            raise _already_exists()


def _write_temporary(descriptor: int, content: bytes) -> _TemporaryArtifact:
    temporary_name = f".tmp-{secrets.token_urlsafe(16)}"
    output = os.open(
        temporary_name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=descriptor,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(output, content[offset:])
        os.fchmod(output, 0o600)
        os.fsync(output)
        metadata = os.fstat(output)
        temporary = _TemporaryArtifact(
            name=temporary_name,
            descriptor=output,
            identity=(metadata.st_dev, metadata.st_ino),
            byte_size=len(content),
            digest=hashlib.sha256(content).digest(),
        )
    except BaseException:
        os.close(output)
        with contextlib.suppress(OSError):
            os.unlink(temporary_name, dir_fd=descriptor)
        raise
    return temporary


def _link_new(descriptor: int, name: str, temporary_name: str) -> None:
    os.link(
        temporary_name,
        name,
        src_dir_fd=descriptor,
        dst_dir_fd=descriptor,
        follow_symlinks=False,
    )


def _unlink_temporary_alias(descriptor: int, temporary: _TemporaryArtifact) -> None:
    metadata = os.stat(temporary.name, dir_fd=descriptor, follow_symlinks=False)
    if (metadata.st_dev, metadata.st_ino) != temporary.identity:
        raise OSError("renderer temporary artifact alias was replaced")
    os.unlink(temporary.name, dir_fd=descriptor)


def _assert_temporary_artifact(
    descriptor: int,
    temporary: _TemporaryArtifact,
) -> None:
    pinned = os.fstat(temporary.descriptor)
    named = os.stat(temporary.name, dir_fd=descriptor, follow_symlinks=False)
    if any(not _matches_artifact_contract(metadata, temporary) for metadata in (pinned, named)):
        raise OSError("renderer manifest temporary artifact was replaced")
    if _digest_descriptor(temporary.descriptor, temporary.byte_size) != temporary.digest:
        raise OSError("renderer manifest temporary artifact changed before publication")


def _installed_artifact(
    descriptor: int,
    name: str,
    temporary: _TemporaryArtifact,
) -> _InstalledArtifact:
    metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
    identity = (metadata.st_dev, metadata.st_ino)
    try:
        named_temporary = os.stat(
            temporary.name,
            dir_fd=descriptor,
            follow_symlinks=False,
        )
        named_temporary_identity: tuple[int, int] | None = (
            named_temporary.st_dev,
            named_temporary.st_ino,
        )
    except OSError:
        named_temporary_identity = None
    return _InstalledArtifact(
        name=name,
        identity=identity,
        byte_size=temporary.byte_size,
        digest=temporary.digest,
        remove_on_rollback=(identity == temporary.identity or identity == named_temporary_identity),
    )


def _assert_installed_artifacts(
    descriptor: int,
    installed: list[_InstalledArtifact],
) -> None:
    for item in installed:
        artifact_descriptor = os.open(
            item.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=descriptor,
        )
        try:
            metadata = os.fstat(artifact_descriptor)
            if (
                not _matches_artifact_contract(metadata, item)
                or _digest_descriptor(artifact_descriptor, item.byte_size) != item.digest
            ):
                raise OSError("renderer output artifact changed during publication")
            named = os.stat(item.name, dir_fd=descriptor, follow_symlinks=False)
            if not _matches_artifact_contract(named, item):
                raise OSError("renderer output artifact was replaced during validation")
        finally:
            os.close(artifact_descriptor)


def _matches_artifact_contract(
    metadata: os.stat_result,
    artifact: _InstalledArtifact | _TemporaryArtifact,
) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and (metadata.st_dev, metadata.st_ino) == artifact.identity
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and metadata.st_size == artifact.byte_size
    )


def _digest_descriptor(descriptor: int, byte_size: int) -> bytes:
    digest = hashlib.sha256()
    offset = 0
    while offset < byte_size:
        chunk = os.pread(descriptor, min(64 * 1024, byte_size - offset), offset)
        if not chunk:
            raise OSError("renderer artifact ended before its recorded byte size")
        digest.update(chunk)
        offset += len(chunk)
    if os.pread(descriptor, 1, byte_size):
        raise OSError("renderer artifact exceeds its recorded byte size")
    return digest.digest()


def _rollback_installed(descriptor: int, installed: list[_InstalledArtifact]) -> None:
    for item in reversed(installed):
        if not item.remove_on_rollback:
            continue
        with contextlib.suppress(OSError):
            final = os.stat(item.name, dir_fd=descriptor, follow_symlinks=False)
            if (final.st_dev, final.st_ino) == item.identity:
                os.unlink(item.name, dir_fd=descriptor)


def _already_exists() -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.OUTPUT_ALREADY_EXISTS,
            message="The renderer output directory is not empty.",
            suggested_action="Choose a new or empty controlled output directory.",
        )
    )


def _write_failed() -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.OUTPUT_WRITE_FAILED,
            message="The renderer export could not be committed safely.",
            suggested_action="Choose a new empty controlled output directory and retry.",
        )
    )


__all__ = ["ExportArtifact", "export_bundle"]
