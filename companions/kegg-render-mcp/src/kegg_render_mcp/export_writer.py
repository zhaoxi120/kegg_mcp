"""Non-overwriting, commit-marked renderer export bundles."""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path
from typing import Protocol

from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError
from kegg_render_mcp.render_input import open_allowed_directory, remove_created_empty_directory


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
    remove_created_directory_on_failure: bool = False,
) -> None:
    """Install one complete export into a new or empty controlled directory."""
    descriptor, created = open_allowed_directory(output_directory, allowed_roots)
    temporary_names: dict[str, str] = {}
    installed_names: list[tuple[str, str]] = []
    committed = False
    try:
        _require_empty_directory(descriptor)
        ordered = tuple(item for item in artifacts if item.name != manifest_name)
        manifest = tuple(item for item in artifacts if item.name == manifest_name)
        if len(manifest) != 1:
            raise ValueError("renderer exports require exactly one commit manifest")
        for item in (*ordered, manifest[0]):
            temporary_names[item.name] = _write_temporary(descriptor, item.content)
        os.fsync(descriptor)
        for item in ordered:
            _link_new(descriptor, item.name, temporary_names[item.name])
            installed_names.append((item.name, temporary_names[item.name]))
        _link_new(descriptor, manifest_name, temporary_names[manifest_name])
        installed_names.append((manifest_name, temporary_names[manifest_name]))
        os.fsync(descriptor)
        committed = True
    except FileExistsError:
        raise _already_exists() from None
    except RenderMcpError:
        raise
    except (OSError, ValueError):
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.OUTPUT_WRITE_FAILED,
                message="The renderer export could not be committed safely.",
                suggested_action="Choose a new empty controlled output directory and retry.",
            )
        ) from None
    finally:
        if not committed:
            _rollback_installed(descriptor, installed_names)
        for temporary_name in temporary_names.values():
            with contextlib.suppress(OSError):
                os.unlink(temporary_name, dir_fd=descriptor)
        with contextlib.suppress(OSError):
            os.fsync(descriptor)
        if not committed and created and remove_created_directory_on_failure:
            remove_created_empty_directory(output_directory, allowed_roots, descriptor)
        os.close(descriptor)


def _require_empty_directory(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        entry = next(entries, None)
    if entry is None:
        return
    try:
        metadata = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
    except OSError:
        raise _unsafe_entry() from None
    if not stat.S_ISREG(metadata.st_mode):
        raise _unsafe_entry()
    raise _already_exists()


def _write_temporary(descriptor: int, content: bytes) -> str:
    temporary_name = f".tmp-{secrets.token_urlsafe(16)}"
    output = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=descriptor,
    )
    try:
        offset = 0
        while offset < len(content):
            offset += os.write(output, content[offset:])
        os.fchmod(output, 0o600)
        os.fsync(output)
    except BaseException:
        os.close(output)
        with contextlib.suppress(OSError):
            os.unlink(temporary_name, dir_fd=descriptor)
        raise
    os.close(output)
    return temporary_name


def _link_new(descriptor: int, name: str, temporary_name: str) -> None:
    os.link(
        temporary_name,
        name,
        src_dir_fd=descriptor,
        dst_dir_fd=descriptor,
        follow_symlinks=False,
    )


def _rollback_installed(descriptor: int, installed: list[tuple[str, str]]) -> None:
    for name, temporary_name in reversed(installed):
        with contextlib.suppress(OSError):
            temporary = os.stat(
                temporary_name,
                dir_fd=descriptor,
                follow_symlinks=False,
            )
            final = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (temporary.st_dev, temporary.st_ino) == (final.st_dev, final.st_ino):
                os.unlink(name, dir_fd=descriptor)


def _already_exists() -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.OUTPUT_ALREADY_EXISTS,
            message="The renderer output directory is not empty.",
            suggested_action="Choose a new or empty controlled output directory.",
        )
    )


def _unsafe_entry() -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.INPUT_PATH_REJECTED,
            message="The renderer output directory contains an unsafe non-regular entry.",
            suggested_action="Use a new empty controlled output directory.",
        )
    )


__all__ = ["ExportArtifact", "export_bundle"]
