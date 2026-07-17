#!/usr/bin/env python3
"""Install the repository's three Codex Skills into a workspace discovery root."""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NoReturn, cast

INSTALLER_ID = "kegg-mcp-skill-installer"
MARKER_NAME = ".kegg-mcp-skill-install.json"
SCHEMA_VERSION = 1
TREE_DIGEST_VERSION = "kegg-mcp-tree-sha256-v2"
SKILL_NAMES = (
    "deepkoala-annotation",
    "kegg-ko-analysis",
    "kegg-pathway-rendering",
)
DISTRIBUTION_NAMES = (
    "kegg-mcp",
    "deepkoala-mcp",
    "kegg-render-mcp",
)
CHECKOUT_SOURCE_PATHS = (
    ".agents/skills",
    "pyproject.toml",
    "companions/deepkoala-mcp/pyproject.toml",
    "companions/kegg-render-mcp/pyproject.toml",
    "scripts/install-skills.py",
)
COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}\Z")
MAX_SOURCE_ENTRIES = 512
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = MAX_SOURCE_BYTES + 2 * 1024 * 1024
RENAME_NOREPLACE = 1
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class InstallError(Exception):
    """A safe, classified installation failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SourceIdentity:
    commit: str
    kind: str
    source_tree_sha256: str
    versions: dict[str, str]


@dataclass(frozen=True)
class PreparedSource:
    identity: SourceIdentity
    root: Path
    skill_digests: dict[str, str]


@dataclass(frozen=True)
class TargetState:
    exists: bool
    device: int | None = None
    inode: int | None = None


@dataclass
class AnchoredInstallRoot:
    workspace: Path
    workspace_fd: int
    agents_fd: int
    skills_fd: int
    workspace_identity: tuple[int, int]
    agents_identity: tuple[int, int]
    skills_identity: tuple[int, int]

    def close(self) -> None:
        for descriptor in (self.skills_fd, self.agents_fd, self.workspace_fd):
            os.close(descriptor)


def _error(code: str, message: str) -> NoReturn:
    raise InstallError(code, message)


def _identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _project_versions(root: Path) -> dict[str, str]:
    paths = {
        "kegg-mcp": root / "pyproject.toml",
        "deepkoala-mcp": root / "companions" / "deepkoala-mcp" / "pyproject.toml",
        "kegg-render-mcp": root / "companions" / "kegg-render-mcp" / "pyproject.toml",
    }
    versions: dict[str, str] = {}
    for distribution, path in paths.items():
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
            version = document["project"]["version"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
            _error(
                "skill_source_invalid",
                f"cannot read the {distribution} project version: {exc}",
            )
        if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
            _error("skill_source_invalid", f"the {distribution} project version is invalid")
        versions[distribution] = version
    return versions


def _collect_tree_entries(root: Path, relative_roots: tuple[str, ...]) -> list[tuple[str, Path]]:
    entries: dict[str, Path] = {}
    for relative_root in relative_roots:
        source = root if relative_root == "." else root / relative_root
        descriptor_root = relative_root == "." and source.is_symlink()
        if (source.is_symlink() and not descriptor_root) or not source.exists():
            _error("skill_source_invalid", f"missing regular source entry {relative_root}")
        candidates = (source, *source.rglob("*")) if source.is_dir() else (source,)
        for path in candidates:
            relative = path.relative_to(root).as_posix()
            try:
                file_stat = path.stat() if descriptor_root and path == source else path.lstat()
            except OSError as exc:
                _error("skill_source_invalid", f"cannot inspect source entry {relative}: {exc}")
            if stat.S_ISLNK(file_stat.st_mode):
                _error("skill_tree_unsafe", f"source tree contains a symlink: {relative}")
            if not (stat.S_ISDIR(file_stat.st_mode) or stat.S_ISREG(file_stat.st_mode)):
                _error("skill_tree_unsafe", f"source tree contains a special file: {relative}")
            entries[relative] = path
    if len(entries) > MAX_SOURCE_ENTRIES:
        _error("skill_source_invalid", "source tree contains too many entries")
    return sorted(entries.items())


def _digest_entries(entries: list[tuple[str, Path]], *, domain: str) -> str:
    digest = hashlib.sha256()
    digest.update(domain.encode("ascii"))
    digest.update(b"\0")
    total_bytes = 0
    for relative, path in entries:
        file_stat = path.stat() if relative == "." and path.is_symlink() else path.lstat()
        encoded_relative = relative.encode("utf-8")
        if stat.S_ISDIR(file_stat.st_mode):
            digest.update(b"D\0")
            digest.update(encoded_relative)
            digest.update(b"\0")
            digest.update(b"040755\0")
            continue
        total_bytes += file_stat.st_size
        if total_bytes > MAX_SOURCE_BYTES:
            _error("skill_source_invalid", "source tree exceeds the byte limit")
        digest.update(b"F\0")
        digest.update(encoded_relative)
        digest.update(b"\0")
        canonical_mode = b"100755" if stat.S_IMODE(file_stat.st_mode) & 0o111 else b"100644"
        digest.update(canonical_mode)
        digest.update(b"\0")
        digest.update(str(file_stat.st_size).encode("ascii"))
        digest.update(b"\0")
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def _canonical_file_mode(mode: int) -> int:
    return 0o755 if stat.S_IMODE(mode) & 0o111 else 0o644


def _copy_canonical_paths(
    source_root: Path,
    relative_roots: tuple[str, ...],
    destination: Path,
) -> None:
    total_bytes = 0
    for relative, source in _collect_tree_entries(source_root, relative_roots):
        source_stat = source.stat() if relative == "." and source.is_symlink() else source.lstat()
        target = (
            destination if relative == "." else destination.joinpath(*PurePosixPath(relative).parts)
        )
        if stat.S_ISDIR(source_stat.st_mode):
            target.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.chmod(0o755)
            continue
        if not stat.S_ISREG(source_stat.st_mode):
            _error("skill_tree_unsafe", "source changed to a non-regular entry")

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if not hasattr(os, "O_NOFOLLOW"):
            _error("skill_tree_unsafe", "the platform lacks no-follow source controls")
        descriptor = os.open(source, flags | os.O_NOFOLLOW)
        try:
            opened_stat = os.fstat(descriptor)
            if not stat.S_ISREG(opened_stat.st_mode) or _identity(opened_stat) != _identity(
                source_stat
            ):
                _error("skill_source_changed", "source changed while snapshotting")
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            copied_bytes = 0
            with os.fdopen(descriptor, "rb", closefd=False) as source_stream:
                with target.open("xb") as target_stream:
                    while chunk := source_stream.read(1024 * 1024):
                        copied_bytes += len(chunk)
                        total_bytes += len(chunk)
                        if total_bytes > MAX_SOURCE_BYTES:
                            _error("skill_source_invalid", "source tree exceeds the byte limit")
                        target_stream.write(chunk)
                final_stat = os.fstat(source_stream.fileno())
            if (
                copied_bytes != opened_stat.st_size
                or final_stat.st_size != opened_stat.st_size
                or _identity(final_stat) != _identity(opened_stat)
            ):
                _error("skill_source_changed", "source changed while snapshotting")
            target.chmod(_canonical_file_mode(opened_stat.st_mode))
        finally:
            os.close(descriptor)


def _digest_paths(root: Path, relative_roots: tuple[str, ...], *, domain: str) -> str:
    return _digest_entries(_collect_tree_entries(root, relative_roots), domain=domain)


def _source_tree_digest(root: Path) -> str:
    return _digest_paths(
        root,
        CHECKOUT_SOURCE_PATHS,
        domain=f"{TREE_DIGEST_VERSION}:source",
    )


def _tree_digest(root: Path, *, allow_marker: bool = False) -> str:
    entries = [
        (relative, path)
        for relative, path in _collect_tree_entries(root, (".",))
        if not (allow_marker and relative == MARKER_NAME)
    ]
    return _digest_entries(
        entries,
        domain=f"{TREE_DIGEST_VERSION}:skill",
    )


def _skill_digests(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for name in SKILL_NAMES:
        source = root / ".agents" / "skills" / name
        skill_file = source / "SKILL.md"
        if source.is_symlink() or not source.is_dir() or skill_file.is_symlink():
            _error("skill_source_invalid", f"missing regular Skill source for {name}")
        try:
            skill_text = skill_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            _error("skill_source_invalid", f"cannot read SKILL.md for {name}: {exc}")
        if f"name: {name}" not in skill_text:
            _error("skill_source_invalid", f"SKILL.md name does not match {name}")
        digests[name] = _tree_digest(source)
    return digests


def _git_text(arguments: list[str], *, failure_code: str) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        _error(failure_code, "cannot verify the local Git source")
    if completed.returncode != 0:
        _error(failure_code, "cannot verify the local Git source")
    return completed


def _checkout_files_and_directories() -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    skill_root = PROJECT_ROOT / ".agents" / "skills"
    if skill_root.is_dir() and not skill_root.is_symlink():
        directories.add(".agents/skills")
        for path in skill_root.rglob("*"):
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            file_stat = path.lstat()
            if stat.S_ISDIR(file_stat.st_mode):
                directories.add(relative)
            else:
                files.add(relative)
    elif skill_root.exists() or skill_root.is_symlink():
        files.add(".agents/skills")
    for relative in CHECKOUT_SOURCE_PATHS[1:]:
        path = PROJECT_ROOT / relative
        if path.exists() or path.is_symlink():
            files.add(relative)
    return files, directories


def _verify_checkout_source() -> None:
    _git_text(
        ["diff", "--quiet", "HEAD", "--", *CHECKOUT_SOURCE_PATHS],
        failure_code="skill_source_modified",
    )
    tree = _git_text(
        ["ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *CHECKOUT_SOURCE_PATHS],
        failure_code="skill_source_modified",
    )
    tracked_files = {name for name in tree.stdout.split("\0") if name}
    try:
        actual_files, actual_directories = _checkout_files_and_directories()
    except (OSError, RuntimeError, UnicodeError):
        _error("skill_source_modified", "cannot verify the Git checkout against HEAD")
    expected_directories = {".agents/skills"}
    for relative in tracked_files:
        path = PurePosixPath(relative)
        if path.parts[:2] != (".agents", "skills"):
            continue
        for parent in path.parents:
            parent_name = parent.as_posix()
            if parent_name == ".agents/skills":
                expected_directories.add(parent_name)
                break
            if parent_name.startswith(".agents/skills/"):
                expected_directories.add(parent_name)
    if actual_files != tracked_files or actual_directories != expected_directories:
        _error(
            "skill_source_modified",
            "the Skill, version, or installer source differs from HEAD",
        )


def _checkout_commit() -> str | None:
    try:
        top_level = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    if top_level.returncode != 0:
        return None
    try:
        discovered_root = Path(top_level.stdout.strip()).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if discovered_root != PROJECT_ROOT.resolve(strict=True):
        # A tag archive nested inside another repository is not that repository's checkout.
        return None
    commit = (
        _git_text(
            ["rev-parse", "--verify", "HEAD^{commit}"],
            failure_code="skill_source_modified",
        )
        .stdout.strip()
        .lower()
    )
    if COMMIT_PATTERN.fullmatch(commit) is None:
        _error("skill_source_modified", "the Git checkout commit identity is invalid")
    _verify_checkout_source()
    return commit


def _member_is_selected(name: str) -> bool:
    return any(
        name == selected or name.startswith(f"{selected}/") or selected.startswith(f"{name}/")
        for selected in CHECKOUT_SOURCE_PATHS
    )


def _extract_git_snapshot(archive: bytes, destination: Path) -> None:
    if len(archive) > MAX_ARCHIVE_BYTES:
        _error("skill_source_invalid", "Git source snapshot exceeds the archive limit")
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as opened:
            members = opened.getmembers()
            if len(members) > MAX_SOURCE_ENTRIES + 32:
                _error("skill_source_invalid", "Git source snapshot contains too many entries")
            for member in members:
                pure_name = PurePosixPath(member.name)
                if (
                    pure_name.is_absolute()
                    or ".." in pure_name.parts
                    or not pure_name.parts
                    or not _member_is_selected(pure_name.as_posix())
                ):
                    _error("skill_source_invalid", "Git source snapshot contains an unsafe path")
                if not (member.isdir() or member.isreg()):
                    _error(
                        "skill_tree_unsafe",
                        "Git source snapshot contains a non-regular entry",
                    )
                target = destination.joinpath(*pure_name.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                total_bytes += member.size
                if total_bytes > MAX_SOURCE_BYTES:
                    _error("skill_source_invalid", "Git source snapshot exceeds the byte limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                source_stream = opened.extractfile(member)
                if source_stream is None:
                    _error("skill_source_invalid", "Git source snapshot file is unreadable")
                with source_stream, target.open("xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                target.chmod(_canonical_file_mode(member.mode))
    except tarfile.TarError as exc:
        _error("skill_source_invalid", f"cannot read the Git source snapshot: {exc}")


def _materialize_checkout(commit: str) -> Path:
    snapshot = Path(tempfile.mkdtemp(prefix="kegg-mcp-skill-source-"))
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(PROJECT_ROOT),
                "archive",
                "--format=tar",
                commit,
                "--",
                *CHECKOUT_SOURCE_PATHS,
            ],
            check=False,
            capture_output=True,
            timeout=15,
        )
        if completed.returncode != 0:
            _error("skill_source_modified", "cannot materialize the verified Git source")
        _extract_git_snapshot(completed.stdout, snapshot)
    except (OSError, subprocess.TimeoutExpired):
        shutil.rmtree(snapshot, ignore_errors=True)
        _error("skill_source_modified", "cannot materialize the verified Git source")
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return snapshot


def _materialize_archive_source() -> Path:
    snapshot = Path(tempfile.mkdtemp(prefix="kegg-mcp-skill-source-"))
    try:
        _copy_canonical_paths(PROJECT_ROOT, CHECKOUT_SOURCE_PATHS, snapshot)
    except BaseException:
        shutil.rmtree(snapshot, ignore_errors=True)
        raise
    return snapshot


def _normalize_sha256(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            _error(
                "source_tree_sha256_required",
                "a tag source archive requires its published --source-tree-sha256",
            )
        return None
    normalized = value.lower()
    if SHA256_PATTERN.fullmatch(normalized) is None:
        _error("source_tree_sha256_invalid", "--source-tree-sha256 must be 64 hexadecimal digits")
    return normalized


def _prepare_source(
    source_commit: str | None,
    source_tree_sha256: str | None,
    expected_core_version: str | None,
) -> PreparedSource:
    detected_commit = _checkout_commit()
    if detected_commit is not None:
        if source_commit is not None:
            supplied_commit = source_commit.lower()
            if COMMIT_PATTERN.fullmatch(supplied_commit) is None:
                _error("source_commit_invalid", "--source-commit must be a full commit ID")
            if supplied_commit != detected_commit:
                _error("source_commit_mismatch", "--source-commit does not match the checkout")
        trusted_digest = _normalize_sha256(source_tree_sha256, required=False)
        root = _materialize_checkout(detected_commit)
        commit = detected_commit
        source_kind = "git_checkout"
    else:
        if source_commit is None:
            _error(
                "source_commit_unavailable",
                "a tag source archive requires its verified full commit with --source-commit",
            )
        commit = source_commit.lower()
        if COMMIT_PATTERN.fullmatch(commit) is None:
            _error("source_commit_invalid", "--source-commit must be a full commit ID")
        trusted_digest = _normalize_sha256(source_tree_sha256, required=True)
        root = _materialize_archive_source()
        source_kind = "tag_source_archive"

    try:
        actual_tree_digest = _source_tree_digest(root)
        if trusted_digest is not None and actual_tree_digest != trusted_digest:
            _error(
                "source_tree_sha256_mismatch",
                "the source tree does not match the published SHA-256",
            )
        versions = _project_versions(root)
        if expected_core_version is not None and versions["kegg-mcp"] != expected_core_version:
            _error(
                "version_mismatch",
                "the Skill source core version does not match --expected-core-version",
            )
        skill_digests = _skill_digests(root)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return PreparedSource(
        identity=SourceIdentity(
            commit=commit,
            kind=source_kind,
            source_tree_sha256=actual_tree_digest,
            versions=versions,
        ),
        root=root,
        skill_digests=skill_digests,
    )


def _validate_workspace(workspace: Path) -> Path:
    if not workspace.is_absolute():
        _error("invalid_workspace", "--workspace must be an absolute path")
    try:
        resolved = workspace.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _error("invalid_workspace", f"--workspace cannot be resolved: {exc}")
    if not resolved.is_dir():
        _error("invalid_workspace", "--workspace must name an existing directory")
    return resolved


def _directory_open_flags() -> int:
    required = ("O_DIRECTORY", "O_NOFOLLOW")
    if any(not hasattr(os, name) for name in required):
        _error("unsafe_install_root", "the platform lacks required no-follow directory controls")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_child_directory(parent_fd: int, name: str) -> tuple[int, tuple[int, int]]:
    with contextlib.suppress(FileExistsError):
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    descriptor = -1
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            _error("unsafe_install_root", "the managed install root contains a symlink or file")
        descriptor = os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        after = os.fstat(descriptor)
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        _error("unsafe_install_root", f"cannot anchor the managed install root: {exc}")
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    if _identity(before) != _identity(after):
        os.close(descriptor)
        _error("install_root_changed", "the managed install root changed while it was opened")
    return descriptor, _identity(after)


def _anchor_install_root(workspace: Path) -> AnchoredInstallRoot:
    flags = _directory_open_flags()
    workspace_fd = -1
    try:
        workspace_fd = os.open(workspace, flags)
        workspace_stat = os.fstat(workspace_fd)
    except OSError as exc:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        _error("unsafe_install_root", f"cannot anchor the workspace root: {exc}")
    except BaseException:
        if workspace_fd >= 0:
            os.close(workspace_fd)
        raise
    try:
        agents_fd, agents_identity = _open_child_directory(workspace_fd, ".agents")
        try:
            skills_fd, skills_identity = _open_child_directory(agents_fd, "skills")
        except BaseException:
            os.close(agents_fd)
            raise
    except BaseException:
        os.close(workspace_fd)
        raise
    if not Path("/proc/self/fd").is_dir():
        os.close(skills_fd)
        os.close(agents_fd)
        os.close(workspace_fd)
        _error("unsafe_install_root", "the platform lacks anchored descriptor paths")
    return AnchoredInstallRoot(
        workspace=workspace,
        workspace_fd=workspace_fd,
        agents_fd=agents_fd,
        skills_fd=skills_fd,
        workspace_identity=_identity(workspace_stat),
        agents_identity=agents_identity,
        skills_identity=skills_identity,
    )


def _assert_root_binding(root: AnchoredInstallRoot) -> None:
    paths_and_identities = (
        (root.workspace, root.workspace_identity),
        (root.workspace / ".agents", root.agents_identity),
        (root.workspace / ".agents" / "skills", root.skills_identity),
    )
    try:
        for path, expected_identity in paths_and_identities:
            current = path.lstat()
            if not stat.S_ISDIR(current.st_mode) or _identity(current) != expected_identity:
                _error("install_root_changed", "the managed install root changed before commit")
    except OSError:
        _error("install_root_changed", "the managed install root changed before commit")


def _validate_marker(marker: object, name: str) -> dict[str, object]:
    if not isinstance(marker, dict):
        _error("skill_target_conflict", f"invalid installation marker for {name}")
    typed = cast(dict[str, object], marker)
    expected_keys = {
        "content_sha256",
        "installer",
        "schema_version",
        "skill_name",
        "source_commit",
        "source_kind",
        "source_tree_sha256",
        "versions",
    }
    if set(typed) != expected_keys:
        _error("skill_target_conflict", f"invalid installation marker schema for {name}")
    if typed["installer"] != INSTALLER_ID or typed["skill_name"] != name:
        _error("skill_target_conflict", f"unrecognized installation marker for {name}")
    if type(typed["schema_version"]) is not int or typed["schema_version"] != SCHEMA_VERSION:
        _error("skill_target_conflict", f"invalid installation marker version for {name}")
    for digest_key in ("content_sha256", "source_tree_sha256"):
        digest = typed[digest_key]
        if not isinstance(digest, str) or SHA256_PATTERN.fullmatch(digest) is None:
            _error("skill_target_conflict", f"invalid {digest_key} in marker for {name}")
    commit = typed["source_commit"]
    if not isinstance(commit, str) or COMMIT_PATTERN.fullmatch(commit) is None:
        _error("skill_target_conflict", f"invalid source_commit in marker for {name}")
    source_kind = typed["source_kind"]
    if not isinstance(source_kind, str) or source_kind not in {
        "git_checkout",
        "tag_source_archive",
    }:
        _error("skill_target_conflict", f"invalid source_kind in marker for {name}")
    versions = typed["versions"]
    if not isinstance(versions, dict):
        _error("skill_target_conflict", f"invalid versions in marker for {name}")
    typed_versions = cast(dict[str, object], versions)
    if set(typed_versions) != set(DISTRIBUTION_NAMES):
        _error("skill_target_conflict", f"invalid versions in marker for {name}")
    if any(
        not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None
        for value in typed_versions.values()
    ):
        _error("skill_target_conflict", f"invalid version value in marker for {name}")
    return typed


def _validate_existing_target(root: AnchoredInstallRoot, name: str) -> TargetState:
    try:
        before = os.stat(name, dir_fd=root.skills_fd, follow_symlinks=False)
    except FileNotFoundError:
        return TargetState(exists=False)
    if not stat.S_ISDIR(before.st_mode):
        _error("skill_target_conflict", f"refusing to replace unsafe Skill target {name}")
    try:
        target_fd = os.open(name, _directory_open_flags(), dir_fd=root.skills_fd)
    except OSError:
        _error("skill_target_conflict", f"refusing to open unsafe Skill target {name}")
    try:
        opened_stat = os.fstat(target_fd)
        if _identity(opened_stat) != _identity(before):
            _error("skill_target_changed", f"Skill target changed while validating {name}")
        target_path = Path(f"/proc/self/fd/{target_fd}")
        marker_path = target_path / MARKER_NAME
        if marker_path.is_symlink() or not marker_path.is_file():
            _error("skill_target_conflict", f"refusing to overwrite unknown Skill {name}")
        try:
            marker_object: object = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            _error("skill_target_conflict", f"cannot validate installation marker for {name}")
        marker = _validate_marker(marker_object, name)
        if marker["content_sha256"] != _tree_digest(target_path, allow_marker=True):
            _error("skill_target_modified", f"refusing to overwrite modified Skill {name}")
        after = os.stat(name, dir_fd=root.skills_fd, follow_symlinks=False)
        if _identity(after) != _identity(opened_stat):
            _error("skill_target_changed", f"Skill target changed while validating {name}")
    finally:
        os.close(target_fd)
    return TargetState(exists=True, device=before.st_dev, inode=before.st_ino)


def _marker(identity: SourceIdentity, name: str, content_digest: str) -> dict[str, object]:
    return {
        "content_sha256": content_digest,
        "installer": INSTALLER_ID,
        "schema_version": SCHEMA_VERSION,
        "skill_name": name,
        "source_commit": identity.commit,
        "source_kind": identity.kind,
        "source_tree_sha256": identity.source_tree_sha256,
        "versions": identity.versions,
    }


def _write_marker(path: Path, marker: dict[str, object]) -> None:
    path.write_text(json.dumps(marker, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o644)


def _rename_no_replace(
    source_name: str,
    destination_name: str,
    *,
    source_fd: int,
    destination_fd: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable") from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _create_transaction(root: AnchoredInstallRoot) -> tuple[str, int, int, int]:
    for _attempt in range(16):
        name = f".kegg-mcp-skill-install-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root.skills_fd)
            break
        except FileExistsError:
            continue
    else:
        _error("installation_failed", "cannot allocate a private installation transaction")
    transaction_fd = -1
    stage_fd = -1
    backup_fd = -1
    try:
        transaction_fd, _ = _open_child_directory(root.skills_fd, name)
        stage_fd, _ = _open_child_directory(transaction_fd, "stage")
        backup_fd, _ = _open_child_directory(transaction_fd, "backup")
    except BaseException:
        for descriptor in (backup_fd, stage_fd):
            if descriptor >= 0:
                os.close(descriptor)
        if transaction_fd >= 0:
            try:
                _remove_transaction(root, name, transaction_fd)
            finally:
                os.close(transaction_fd)
        else:
            with contextlib.suppress(OSError):
                os.rmdir(name, dir_fd=root.skills_fd)
        raise
    return name, transaction_fd, stage_fd, backup_fd


def _remove_transaction(root: AnchoredInstallRoot, name: str, transaction_fd: int) -> None:
    transaction_path = Path(f"/proc/self/fd/{transaction_fd}")
    for child in tuple(transaction_path.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
    os.rmdir(name, dir_fd=root.skills_fd)


def _cleanup_transaction(
    root: AnchoredInstallRoot,
    name: str,
    transaction_fd: int,
) -> None:
    try:
        _remove_transaction(root, name, transaction_fd)
    except OSError as exc:
        relative = f".agents/skills/{name}"
        raise InstallError(
            "installation_cleanup_failed",
            f"transaction cleanup failed at {relative}; inspect the remaining private content",
        ) from exc


def _entry_identity(parent_fd: int, name: str) -> tuple[int, int] | None:
    try:
        return _identity(os.stat(name, dir_fd=parent_fd, follow_symlinks=False))
    except FileNotFoundError:
        return None


def _reconcile_move(
    name: str,
    expected_identity: tuple[int, int],
    *,
    source_fd: int,
    destination_fd: int,
) -> None:
    source_identity = _entry_identity(source_fd, name)
    destination_identity = _entry_identity(destination_fd, name)
    if source_identity == expected_identity and destination_identity is None:
        _rename_no_replace(
            name,
            name,
            source_fd=source_fd,
            destination_fd=destination_fd,
        )
        if (
            _entry_identity(source_fd, name) is not None
            or _entry_identity(destination_fd, name) != expected_identity
        ):
            raise OSError(errno.EBUSY, "transaction entry changed during rollback")
        return
    if source_identity is None and destination_identity == expected_identity:
        return
    raise OSError(errno.EBUSY, "transaction entry is inconsistent during rollback")


def _rollback(
    root: AnchoredInstallRoot,
    stage_fd: int,
    backup_fd: int,
    attempted_installs: dict[str, tuple[int, int]],
    attempted_backups: dict[str, tuple[int, int]],
) -> None:
    for name in reversed(tuple(attempted_installs)):
        _reconcile_move(
            name,
            attempted_installs[name],
            source_fd=root.skills_fd,
            destination_fd=stage_fd,
        )
    for name in reversed(tuple(attempted_backups)):
        _reconcile_move(
            name,
            attempted_backups[name],
            source_fd=backup_fd,
            destination_fd=root.skills_fd,
        )


def _verify_installed_target(
    root: AnchoredInstallRoot,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    if _entry_identity(root.skills_fd, name) != expected_identity:
        raise OSError(errno.EBUSY, "installed target identity changed")


def _install(workspace: Path, prepared: PreparedSource) -> None:
    root = _anchor_install_root(workspace)
    transaction_name = ""
    transaction_fd = -1
    stage_fd = -1
    backup_fd = -1
    preserve_transaction = False
    prior_state: dict[str, TargetState] = {}
    try:
        prior_state = {name: _validate_existing_target(root, name) for name in SKILL_NAMES}
        transaction_name, transaction_fd, stage_fd, backup_fd = _create_transaction(root)
        preserve_transaction = True
        stage_path = Path(f"/proc/self/fd/{stage_fd}")
        staged_identities: dict[str, tuple[int, int]] = {}
        attempted_installs: dict[str, tuple[int, int]] = {}
        attempted_backups: dict[str, tuple[int, int]] = {}
        destructive_started = False
        try:
            for name in SKILL_NAMES:
                staged = stage_path / name
                _copy_canonical_paths(
                    prepared.root / ".agents" / "skills" / name,
                    (".",),
                    staged,
                )
                staged_digest = _tree_digest(staged)
                if staged_digest != prepared.skill_digests[name]:
                    _error("skill_source_changed", f"Skill source changed while staging {name}")
                _write_marker(
                    staged / MARKER_NAME,
                    _marker(prepared.identity, name, staged_digest),
                )
                if _tree_digest(staged, allow_marker=True) != staged_digest:
                    _error("skill_source_changed", f"staged Skill digest changed for {name}")
                staged_identities[name] = _identity(staged.lstat())

            _assert_root_binding(root)
            for name in SKILL_NAMES:
                if _validate_existing_target(root, name) != prior_state[name]:
                    _error("skill_target_changed", "Skill targets changed before installation")

            destructive_started = True
            for name in SKILL_NAMES:
                _assert_root_binding(root)
                current = _validate_existing_target(root, name)
                if current != prior_state[name]:
                    _error("skill_target_changed", f"Skill target changed before replacing {name}")
                if current.exists:
                    if current.device is None or current.inode is None:
                        raise OSError(errno.EBUSY, "existing target identity is unavailable")
                    attempted_backups[name] = (current.device, current.inode)
                    _rename_no_replace(
                        name,
                        name,
                        source_fd=root.skills_fd,
                        destination_fd=backup_fd,
                    )
                    if _entry_identity(backup_fd, name) != attempted_backups[name]:
                        raise OSError(errno.EBUSY, "backup identity changed during installation")
                attempted_installs[name] = staged_identities[name]
                _rename_no_replace(
                    name,
                    name,
                    source_fd=stage_fd,
                    destination_fd=root.skills_fd,
                )
                _verify_installed_target(root, name, staged_identities[name])
        except BaseException as exc:
            try:
                _rollback(
                    root,
                    stage_fd,
                    backup_fd,
                    attempted_installs,
                    attempted_backups,
                )
            except BaseException as rollback_exc:
                relative = f".agents/skills/{transaction_name}"
                raise InstallError(
                    "installation_rollback_failed",
                    f"transaction preserved at {relative}; inspect backup/ and restore by "
                    "Skill name",
                ) from rollback_exc
            if preserve_transaction:
                _cleanup_transaction(root, transaction_name, transaction_fd)
                preserve_transaction = False
            if destructive_started and isinstance(exc, (InstallError, OSError)):
                raise InstallError(
                    "installation_failed",
                    f"Skill installation was rolled back: {type(exc).__name__}",
                ) from exc
            raise
        else:
            if preserve_transaction:
                _cleanup_transaction(root, transaction_name, transaction_fd)
                preserve_transaction = False

        installed_count = sum(not state.exists for state in prior_state.values())
        updated_count = len(SKILL_NAMES) - installed_count
        print(
            f"Installed {installed_count} and updated {updated_count} managed Skills in "
            ".agents/skills."
        )
        print(f"source_commit={prepared.identity.commit}")
        print(f"source_tree_sha256={prepared.identity.source_tree_sha256}")
        for distribution, version in prepared.identity.versions.items():
            print(f"{distribution}={version}")
        print("Verify Skill discovery separately from MCP discovery.")
    finally:
        for descriptor in (backup_fd, stage_fd, transaction_fd):
            if descriptor >= 0:
                os.close(descriptor)
        root.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy the three version-matched KEGG Skills into a Codex workspace."
    )
    parser.add_argument(
        "--workspace",
        required=True,
        type=Path,
        help="existing absolute workspace root where Codex will be launched",
    )
    parser.add_argument(
        "--source-commit",
        help="verified full commit ID required for a tag source archive without Git metadata",
    )
    parser.add_argument(
        "--source-tree-sha256",
        help="published source-tree SHA-256 required for a tag source archive",
    )
    parser.add_argument(
        "--expected-core-version",
        help="fail unless the Skill source matches this installed kegg-mcp version",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    prepared: PreparedSource | None = None
    try:
        workspace = _validate_workspace(arguments.workspace)
        prepared = _prepare_source(
            arguments.source_commit,
            arguments.source_tree_sha256,
            arguments.expected_core_version,
        )
        _install(workspace, prepared)
    except InstallError as exc:
        print(f"ERROR [{exc.code}] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR [installation_failed] {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        if prepared is not None:
            shutil.rmtree(prepared.root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
