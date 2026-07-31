"""Private race-resistant writer for committed local text bundles."""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail

_BUNDLE_FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DirectoryIdentity = tuple[int, int, int]
_FileIdentity = tuple[int, int]


@dataclass(frozen=True, slots=True)
class _InstalledFile:
    """One direct bundle file installed into the pinned directory."""

    name: str
    identity: _FileIdentity
    byte_size: int


@dataclass(frozen=True, slots=True)
class _PinnedOutputDirectory:
    """One opened final directory and its still-open immediate parent."""

    descriptor: int
    parent_descriptor: int
    name: str
    identity: _DirectoryIdentity
    parent_identity: _DirectoryIdentity
    created: bool


def write_text_bundle(
    output_directory: Path,
    files: Mapping[str, str],
    *,
    manifest_name: str = "bundle_manifest.json",
    remove_created_directory_on_failure: bool = False,
    max_artifact_bytes: int | None = None,
    max_total_bytes: int | None = None,
) -> None:
    """Write immutable UTF-8 files and publish one manifest as the commit marker."""
    if manifest_name not in files:
        raise AssertionError("output bundles require a commit manifest")
    if not _BUNDLE_FILE_NAME.fullmatch(manifest_name):
        raise AssertionError("bundle manifest name must be a direct safe file name")
    encoded_files: dict[str, bytes] = {}
    total_bytes = 0
    for name, content in files.items():
        if not _BUNDLE_FILE_NAME.fullmatch(name):
            fail(
                ErrorCode.OUTPUT_WRITE_FAILED,
                "The output bundle contains an invalid file name.",
                suggested_action="Use the fixed server-defined bundle file names.",
            )
        encoded = content.encode("utf-8")
        if max_artifact_bytes is not None and len(encoded) > max_artifact_bytes:
            _fail_size_limit(
                "bundle_artifact_bytes",
                observed=len(encoded),
                limit=max_artifact_bytes,
            )
        total_bytes += len(encoded)
        if max_total_bytes is not None and total_bytes > max_total_bytes:
            _fail_size_limit(
                "bundle_total_bytes",
                observed=total_bytes,
                limit=max_total_bytes,
            )
        encoded_files[name] = encoded
    _write_encoded_files(
        output_directory,
        encoded_files,
        manifest_name=manifest_name,
        remove_created_directory_on_failure=remove_created_directory_on_failure,
    )


def preflight_text_bundle_output(output_directory: Path) -> None:
    """Read-only validation before a workflow performs remote or expensive work."""
    try:
        _preflight_output_directory(output_directory)
    except OSError:
        fail(
            ErrorCode.OUTPUT_WRITE_FAILED,
            "The requested output bundle directory could not be validated safely.",
            suggested_action=(
                "Use a new or empty private directory beneath a configured allowed root."
            ),
        )


def _fail_size_limit(name: str, *, observed: int, limit: int) -> None:
    fail(
        ErrorCode.OUTPUT_LIMIT_EXCEEDED,
        "The requested output bundle exceeds its fixed byte bound.",
        suggested_action="Select fewer records or relationships and retry.",
        safe_details=(
            SafeDetail(name=name, value=str(observed)),
            SafeDetail(name=f"{name}_limit", value=str(limit)),
        ),
    )


def _write_encoded_files(
    output_directory: Path,
    files: Mapping[str, bytes],
    *,
    manifest_name: str,
    remove_created_directory_on_failure: bool,
) -> None:
    temporary_names: dict[str, str] = {}
    installed_files: list[_InstalledFile] = []
    pinned: _PinnedOutputDirectory | None = None
    committed = False
    try:
        pinned = _open_pinned_output_directory(output_directory, create_missing=True)
        directory_fd = pinned.descriptor
        if _directory_has_entries(directory_fd):
            fail(
                ErrorCode.OUTPUT_ALREADY_EXISTS,
                "The requested output bundle directory is not empty.",
                suggested_action="Choose a new or empty output_directory.",
            )
        for name, content in files.items():
            temporary = f".{name}.{secrets.token_hex(8)}.tmp"
            temporary_names[name] = temporary
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            file_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
            try:
                with os.fdopen(file_fd, "wb", closefd=True) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                with suppress(OSError):
                    os.close(file_fd)
                raise
        os.fsync(directory_fd)

        # Hard-link every temporary into place without replacement, then publish
        # the manifest last as the bundle commit marker.
        for name in files:
            if name == manifest_name:
                continue
            temporary = temporary_names[name]
            os.link(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            installed_files.append(
                _InstalledFile(
                    name=name,
                    identity=_file_identity(
                        os.stat(temporary, dir_fd=directory_fd, follow_symlinks=False)
                    ),
                    byte_size=len(files[name]),
                )
            )
        _assert_pinned_output_directory(output_directory, pinned)
        _assert_installed_files(directory_fd, installed_files)
        manifest_temporary = temporary_names[manifest_name]
        os.link(
            manifest_temporary,
            manifest_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        installed_files.append(
            _InstalledFile(
                name=manifest_name,
                identity=_file_identity(
                    os.stat(
                        manifest_temporary,
                        dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                ),
                byte_size=len(files[manifest_name]),
            )
        )
        os.fsync(directory_fd)
        _assert_pinned_output_directory(output_directory, pinned)
        _assert_installed_files(directory_fd, installed_files)

        # Remove temporary aliases before the final path-identity check so a
        # replacement during cleanup cannot be reported as a successful bundle.
        for temporary in temporary_names.values():
            os.unlink(temporary, dir_fd=directory_fd)
        temporary_names.clear()
        os.fsync(directory_fd)
        os.fsync(pinned.parent_descriptor)
        _assert_pinned_output_directory(output_directory, pinned)
        _assert_installed_files(directory_fd, installed_files)
        committed = True
    except FileExistsError:
        fail(
            ErrorCode.OUTPUT_ALREADY_EXISTS,
            "The requested output bundle would replace an existing file.",
            suggested_action="Choose a new or empty output_directory.",
        )
    except OSError:
        fail(
            ErrorCode.OUTPUT_WRITE_FAILED,
            "The requested output bundle could not be written safely.",
            suggested_action="Check the output directory permissions and available storage.",
        )
    finally:
        if pinned is not None:
            directory_fd = pinned.descriptor
            if not committed:
                for installed in installed_files:
                    with suppress(OSError):
                        installed_stat = os.stat(
                            installed.name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        if _file_identity(installed_stat) == installed.identity:
                            os.unlink(installed.name, dir_fd=directory_fd)
            for temporary in temporary_names.values():
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=directory_fd)
            with suppress(OSError):
                os.fsync(directory_fd)
            if not committed and pinned.created and remove_created_directory_on_failure:
                _remove_pinned_created_empty_directory(pinned)
            os.close(directory_fd)
            os.close(pinned.parent_descriptor)


def _open_existing_directory_fd(path: Path) -> int:
    pinned = _open_pinned_output_directory(path, create_missing=False)
    os.close(pinned.parent_descriptor)
    if pinned.created:  # pragma: no cover - impossible when creation is disabled
        os.close(pinned.descriptor)
        raise AssertionError("existing-directory walk unexpectedly created an entry")
    return pinned.descriptor


def _open_pinned_output_directory(
    path: Path,
    *,
    create_missing: bool,
) -> _PinnedOutputDirectory:
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("output directory must be an absolute normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(os.sep, flags)
    private_boundary = _validate_output_directory_fd(
        current_fd,
        private_boundary=False,
    )
    try:
        components = path.parts[1:]
        if not components:
            raise OSError("output directory must not be the filesystem root")
        for index, component in enumerate(components):
            if component in {"", ".", ".."}:
                raise OSError("output directory contains an invalid component")
            created = False
            created_identity: _DirectoryIdentity | None = None
            if create_missing:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    created = True
                    created_metadata = os.stat(
                        component,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISDIR(created_metadata.st_mode):
                        raise OSError("created output entry is not a directory")
                    created_identity = (
                        created_metadata.st_dev,
                        created_metadata.st_ino,
                        created_metadata.st_uid,
                    )
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except BaseException:
                if created and created_identity is not None:
                    _remove_named_empty_directory_if_identity(
                        current_fd,
                        component,
                        created_identity,
                    )
                raise
            try:
                if created_identity is not None:
                    opened_metadata = os.fstat(next_fd)
                    if (
                        opened_metadata.st_dev,
                        opened_metadata.st_ino,
                        opened_metadata.st_uid,
                    ) != created_identity:
                        raise OSError("created output directory was replaced before opening")
                private_boundary = _validate_output_directory_fd(
                    next_fd,
                    private_boundary=private_boundary,
                )
            except BaseException:
                os.close(next_fd)
                if created and created_identity is not None:
                    _remove_named_empty_directory_if_identity(
                        current_fd,
                        component,
                        created_identity,
                    )
                raise
            if index == len(components) - 1:
                if not private_boundary:
                    os.close(next_fd)
                    raise OSError("output directory must establish a private ownership boundary")
                opened_metadata = os.fstat(next_fd)
                return _PinnedOutputDirectory(
                    descriptor=next_fd,
                    parent_descriptor=current_fd,
                    name=component,
                    identity=_directory_identity(opened_metadata),
                    parent_identity=_directory_identity(os.fstat(current_fd)),
                    created=created,
                )
            os.close(current_fd)
            current_fd = next_fd
        raise AssertionError("validated output path had no final component")
    except BaseException:
        os.close(current_fd)
        raise


def _preflight_output_directory(path: Path) -> None:
    """Read the existing prefix and reject an occupied final directory without writing."""
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("output directory must be an absolute normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    current_fd = os.open(os.sep, flags)
    private_boundary = _validate_output_directory_fd(
        current_fd,
        private_boundary=False,
    )
    try:
        components = path.parts[1:]
        if not components:
            raise OSError("output directory must not be the filesystem root")
        if any(component in {"", ".", ".."} for component in components):
            raise OSError("output directory contains an invalid component")
        for index, component in enumerate(components):
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except FileNotFoundError:
                # Missing components will be created with mode 0700 by the final
                # writer and therefore establish a private boundary.
                if not _directory_allows_creation(os.fstat(current_fd)):
                    raise OSError("output ancestor does not allow directory creation") from None
                return
            try:
                private_boundary = _validate_output_directory_fd(
                    next_fd,
                    private_boundary=private_boundary,
                )
            except BaseException:
                os.close(next_fd)
                raise
            os.close(current_fd)
            current_fd = next_fd
            if index == len(components) - 1:
                if not private_boundary:
                    raise OSError("output directory must establish a private ownership boundary")
                if not _directory_allows_creation(os.fstat(current_fd)):
                    raise OSError("output directory does not allow file creation")
                if _directory_has_entries(current_fd):
                    fail(
                        ErrorCode.OUTPUT_ALREADY_EXISTS,
                        "The requested output bundle directory is not empty.",
                        suggested_action="Choose a new or empty output_directory.",
                    )
                return
    finally:
        os.close(current_fd)


def _assert_pinned_output_directory(
    path: Path,
    pinned: _PinnedOutputDirectory,
) -> None:
    """Require the public path to resolve to the still-open directory."""
    if _directory_identity(os.fstat(pinned.parent_descriptor)) != pinned.parent_identity:
        raise OSError("output parent directory identity changed during publication")
    named = os.stat(
        pinned.name,
        dir_fd=pinned.parent_descriptor,
        follow_symlinks=False,
    )
    if not stat.S_ISDIR(named.st_mode) or _directory_identity(named) != pinned.identity:
        raise OSError("output directory was replaced during publication")
    reopened = _open_existing_directory_fd(path)
    try:
        if _directory_identity(os.fstat(reopened)) != pinned.identity:
            raise OSError("output path no longer resolves to the published directory")
    finally:
        os.close(reopened)


def _assert_installed_files(
    directory_fd: int,
    installed_files: list[_InstalledFile],
) -> None:
    """Require every published direct name to remain the installed regular file."""
    for installed in installed_files:
        metadata = os.stat(
            installed.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(metadata.st_mode)
            or _file_identity(metadata) != installed.identity
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != installed.byte_size
        ):
            raise OSError("an installed bundle file changed during publication")


def _remove_pinned_created_empty_directory(pinned: _PinnedOutputDirectory) -> bool:
    """Remove only the unchanged service-created final entry from its pinned parent."""
    try:
        if _directory_has_entries(pinned.descriptor):
            return False
        return _remove_named_empty_directory_if_identity(
            pinned.parent_descriptor,
            pinned.name,
            pinned.identity,
        )
    except OSError:
        return False


def _directory_identity(metadata: os.stat_result) -> _DirectoryIdentity:
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid)


def _file_identity(metadata: os.stat_result) -> _FileIdentity:
    return (metadata.st_dev, metadata.st_ino)


def _directory_allows_creation(metadata: os.stat_result) -> bool:
    """Check effective write and search permission without changing the directory."""
    effective_uid = os.geteuid()
    if effective_uid == 0:
        return True
    mode = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid == effective_uid:
        permissions = mode >> 6
    elif metadata.st_gid == os.getegid() or metadata.st_gid in os.getgroups():
        permissions = mode >> 3
    else:
        permissions = mode
    return permissions & 0o3 == 0o3


def _directory_has_entries(descriptor: int) -> bool:
    """Check directory emptiness without materializing caller-controlled names."""
    with os.scandir(descriptor) as entries:
        return next(entries, None) is not None


def _remove_named_empty_directory_if_identity(
    parent_fd: int,
    name: str,
    identity: _DirectoryIdentity,
) -> bool:
    """Best-effort rmdir for one unchanged named directory; rmdir itself enforces emptiness."""
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_uid,
            )
            != identity
        ):
            return False
        os.rmdir(name, dir_fd=parent_fd)
        return True
    except OSError:
        return False


def _validate_output_directory_fd(descriptor: int, *, private_boundary: bool) -> bool:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("output ancestor must be a directory")
    owned = metadata.st_uid == os.geteuid()
    privately_owned = owned and not stat.S_IMODE(metadata.st_mode) & 0o022
    if private_boundary and not owned:
        raise OSError("output ancestors below the private boundary must retain ownership")
    if private_boundary and not privately_owned:
        raise OSError("output ancestors must not be group- or world-writable")
    return private_boundary or privately_owned


__all__ = ["preflight_text_bundle_output", "write_text_bundle"]
