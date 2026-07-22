#!/usr/bin/env python3
"""Install all KEGG MCP distributions and register one generated Codex plugin."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NoReturn, cast
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "kegg-mcp"
DEFAULT_MARKETPLACE_NAME = "kegg-mcp-local"
DEEPKOALA_REPOSITORY = "https://github.com/zhaoxi120/deepkoala.git"
DEFAULT_DEEPKOALA_MODEL_DATE = "202502"
SERVER_NAMES = ("deepkoala-mcp", "kegg-mcp", "kegg-render-mcp")
SKILL_NAMES = ("deepkoala-annotation", "kegg-ko-analysis", "kegg-pathway-rendering")
RUNTIME_NAMES = ("core", "deepkoala", "renderer")
NAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?\Z")
MAX_CONFIG_BYTES = 65_536
MAX_GENERATED_JSON_BYTES = 256 * 1024
MAX_SKILL_ENTRIES = 512
MAX_SKILL_BYTES = 8 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 1024 * 1024
DEFAULT_COMMAND_TIMEOUT_SECONDS = 60
DOCTOR_TIMEOUT_SECONDS = 300
UV_SYNC_TIMEOUT_SECONDS = 1_800
DEEPKOALA_INSTALL_TIMEOUT_SECONDS = 3_600
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\Z"
)
UV_VERSION_PATTERN = re.compile(
    r"uv (?P<major>0|[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)(?:[ \t].*)?\Z"
)
MINIMUM_UV_VERSION = (0, 11, 16)
UV_REQUIRED_SYNC_OPTIONS = (
    "--locked",
    "--no-dev",
    "--no-editable",
    "--no-python-downloads",
    "--no-progress",
    "--offline",
    "--python",
)
PROJECT_FILES = {
    "kegg-mcp": "pyproject.toml",
    "deepkoala-mcp": "companions/deepkoala-mcp/pyproject.toml",
    "kegg-render-mcp": "companions/kegg-render-mcp/pyproject.toml",
}
RUNTIME_PROJECTS = {
    "core": ".",
    "deepkoala": "companions/deepkoala-mcp",
    "renderer": "companions/kegg-render-mcp",
}
RUNTIME_DISTRIBUTIONS = {
    "core": ("kegg-mcp",),
    "deepkoala": ("deepkoala-mcp",),
    "renderer": ("kegg-mcp", "kegg-render-mcp"),
}
RUNTIME_COMMANDS = {
    "deepkoala-mcp": ("deepkoala", "deepkoala-mcp"),
    "kegg-mcp": ("core", "kegg-mcp"),
    "kegg-render-mcp": ("renderer", "kegg-render-mcp"),
}


class InstallError(Exception):
    """One classified installation failure with a non-sensitive public message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class KeggAccessConfig:
    mode: str
    academic_use_confirmed: bool
    licensed_endpoint: str | None
    licensed_use_confirmed: bool
    cache_path: Path | None
    rate_limit_root: Path


@dataclass(frozen=True)
class CoreConfig:
    result_store_path: Path
    allowed_roots: tuple[Path, ...]


@dataclass(frozen=True)
class DeepKoalaConfig:
    state_root: Path
    input_roots: tuple[Path, ...]
    output_roots: tuple[Path, ...]
    allowed_models: tuple[str, ...]
    cpu_threads: int
    allow_multi: bool
    profiles_dir: Path | None
    hmmsearch_executable: Path | None


@dataclass(frozen=True)
class RendererConfig:
    state_root: Path
    allowed_roots: tuple[Path, ...]
    offline_allow_stale: bool


@dataclass(frozen=True)
class DeploymentConfig:
    kegg: KeggAccessConfig
    core: CoreConfig
    deepkoala: DeepKoalaConfig
    renderer: RendererConfig


@dataclass(frozen=True)
class SourceSnapshot:
    root: Path
    versions: dict[str, str]


@dataclass(frozen=True)
class InstallRequest:
    install_root: Path
    marketplace_name: str
    uv: Path
    codex: Path
    git: Path
    python: Path
    allow_locked_dependency_downloads: bool
    dry_run: bool
    allow_deepkoala_install: bool = False


@dataclass
class RegistrationJournal:
    marketplace_attempted: bool = False
    marketplace_added: bool = False
    plugin_attempted: bool = False
    plugin_added: bool = False


def _error(code: str, message: str) -> NoReturn:
    raise InstallError(code, message)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install the three version-matched KEGG MCP distributions and one Codex plugin "
            "containing all three focused Skills and MCP registrations."
        )
    )
    parser.add_argument("--config", required=True, type=Path, help="owner-only deployment TOML")
    parser.add_argument(
        "--install-root",
        required=True,
        type=Path,
        help="new absolute private directory for runtimes and the generated local marketplace",
    )
    parser.add_argument(
        "--marketplace-name",
        default=DEFAULT_MARKETPLACE_NAME,
        help="new Codex marketplace name; existing names are never replaced",
    )
    parser.add_argument("--uv", required=True, type=Path, help="absolute uv executable")
    parser.add_argument("--codex", required=True, type=Path, help="absolute Codex executable")
    parser.add_argument("--git", required=True, type=Path, help="absolute Git executable")
    parser.add_argument(
        "--python",
        required=True,
        type=Path,
        help="absolute CPython 3.11 executable used for the three locked environments",
    )
    parser.add_argument(
        "--allow-locked-dependency-downloads",
        action="store_true",
        help=(
            "allow uv to download dependencies selected by the checked-in lock files; "
            "Python downloads remain disabled"
        ),
    )
    parser.add_argument(
        "--allow-deepkoala-install",
        action="store_true",
        help=(
            "confirm the first-time clone of official DeepKOALA and installation of its "
            "upstream requirements; model updates and multi-domain dependencies are excluded"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "validate source, tools, paths, configuration, and Codex conflicts without a "
            "persistent installation or Codex changes"
        ),
    )
    return parser


def _read_private_toml(path: Path) -> dict[str, object]:
    checked = _existing_regular_file(path, "config", executable=False, private=True)
    _existing_directory(checked.parent, "config parent", private=True)
    expected_metadata = checked.lstat()
    if expected_metadata.st_size > MAX_CONFIG_BYTES:
        _error("deployment_config_invalid", "the deployment configuration exceeds its byte limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        _error("platform_unsupported", "the platform lacks no-follow filesystem controls")
    try:
        descriptor = os.open(checked, flags | os.O_NOFOLLOW)
        opened_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or (opened_metadata.st_dev, opened_metadata.st_ino)
            != (expected_metadata.st_dev, expected_metadata.st_ino)
            or opened_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(opened_metadata.st_mode) & 0o077
            or opened_metadata.st_size > MAX_CONFIG_BYTES
        ):
            os.close(descriptor)
            _error(
                "deployment_config_invalid",
                "the deployment configuration changed or became unsafe while opening",
            )
        with os.fdopen(descriptor, "rb") as stream:
            payload = stream.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        _error("deployment_config_invalid", "the deployment configuration cannot be read safely")
    if len(payload) > MAX_CONFIG_BYTES:
        _error("deployment_config_invalid", "the deployment configuration exceeds its byte limit")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        _error("deployment_config_invalid", "the deployment configuration is not valid UTF-8 TOML")
    return cast(dict[str, object], document)


def _reject_unknown(table: Mapping[str, object], allowed: set[str], label: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        _error("deployment_config_invalid", f"{label} contains unknown fields")


def _required_table(document: Mapping[str, object], key: str) -> dict[str, object]:
    value = document.get(key)
    if not isinstance(value, dict):
        _error("deployment_config_invalid", f"{key} must be a TOML table")
    return cast(dict[str, object], value)


def _required_string(table: Mapping[str, object], key: str, label: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value or "\x00" in value:
        _error("deployment_config_invalid", f"{label}.{key} must be a non-empty string")
    return value


def _optional_string(table: Mapping[str, object], key: str, label: str) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        _error("deployment_config_invalid", f"{label}.{key} must be a non-empty string")
    return value


def _boolean(table: Mapping[str, object], key: str, default: bool, label: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        _error("deployment_config_invalid", f"{label}.{key} must be a boolean")
    return value


def _integer(
    table: Mapping[str, object], key: str, default: int, minimum: int, maximum: int, label: str
) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _error("deployment_config_invalid", f"{label}.{key} is outside its supported range")
    return value


def _string_list(
    table: Mapping[str, object], key: str, label: str, *, maximum: int = 64
) -> tuple[str, ...]:
    value = table.get(key)
    if not isinstance(value, list):
        _error("deployment_config_invalid", f"{label}.{key} must be a bounded string array")
    raw_items = cast(list[object], value)
    if (
        not raw_items
        or len(raw_items) > maximum
        or not all(isinstance(item, str) and item and "\x00" not in item for item in raw_items)
    ):
        _error("deployment_config_invalid", f"{label}.{key} must be a bounded string array")
    items = cast(list[str], raw_items)
    if len(items) != len(set(items)):
        _error("deployment_config_invalid", f"{label}.{key} must not contain duplicates")
    return tuple(items)


def _absolute_path(raw: str, label: str) -> Path:
    path = Path(raw)
    if (
        len(raw.encode("utf-8")) > 4_096
        or any(ord(character) < 32 for character in raw)
        or not path.is_absolute()
        or ".." in path.parts
    ):
        _error("deployment_path_invalid", f"{label} must be absolute and traversal-free")
    return path


def _existing_directory(
    path: Path, label: str, *, private: bool = False, writable: bool = False
) -> Path:
    raw = str(path)
    checked = _absolute_path(raw, label)
    try:
        metadata = checked.lstat()
        resolved = checked.resolve(strict=True)
    except OSError:
        _error("deployment_path_invalid", f"{label} must be an existing directory")
    if stat.S_ISLNK(metadata.st_mode) or resolved != checked or not stat.S_ISDIR(metadata.st_mode):
        _error("deployment_path_invalid", f"{label} must be a direct non-symlink directory")
    if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
        _error("deployment_path_invalid", f"{label} has unsafe ownership or permissions")
    if private and stat.S_IMODE(metadata.st_mode) & 0o077:
        _error("deployment_path_invalid", f"{label} must be owner-only")
    required_access = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
    if not os.access(checked, required_access):
        _error("deployment_path_invalid", f"{label} does not provide the required access")
    if checked == Path(checked.anchor):
        _error("deployment_path_invalid", f"{label} must not be a filesystem root")
    return checked


def _existing_regular_file(
    path: Path,
    label: str,
    *,
    executable: bool,
    private: bool = False,
    writable: bool = False,
) -> Path:
    checked = _absolute_path(str(path), label)
    try:
        metadata = checked.lstat()
        resolved = checked.resolve(strict=True)
    except OSError:
        _error("deployment_path_invalid", f"{label} must be an existing regular file")
    if stat.S_ISLNK(metadata.st_mode) or resolved != checked or not stat.S_ISREG(metadata.st_mode):
        _error("deployment_path_invalid", f"{label} must be a direct non-symlink regular file")
    if private and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077):
        _error("deployment_path_invalid", f"{label} has unsafe ownership or permissions")
    if executable and not os.access(checked, os.X_OK):
        _error("deployment_path_invalid", f"{label} must be executable")
    required_access = os.R_OK | (os.W_OK if writable else 0)
    if not os.access(checked, required_access):
        _error("deployment_path_invalid", f"{label} does not provide the required access")
    return checked


def _existing_safe_executable(path: Path, label: str) -> Path:
    checked = _existing_regular_file(path, label, executable=True)
    try:
        metadata = checked.lstat()
    except OSError:
        _error("deployment_path_invalid", f"{label} must remain an existing executable")
    if metadata.st_uid not in {0, os.geteuid()} or stat.S_IMODE(metadata.st_mode) & 0o022:
        _error("deployment_path_invalid", f"{label} has unsafe ownership or permissions")
    return checked


def _new_file_path(path: Path, label: str, *, writable: bool) -> Path:
    checked = _absolute_path(str(path), label)
    parent = _existing_directory(checked.parent, f"{label} parent", private=True, writable=writable)
    if checked.parent != parent:
        _error("deployment_path_invalid", f"{label} parent is invalid")
    if checked.exists() or checked.is_symlink():
        return _existing_regular_file(
            checked, label, executable=False, private=True, writable=writable
        )
    return checked


def _paths(
    table: Mapping[str, object], key: str, label: str, *, writable: bool = False
) -> tuple[Path, ...]:
    return tuple(
        _existing_directory(
            _absolute_path(item, f"{label}.{key}"),
            f"{label}.{key}",
            writable=writable,
        )
        for item in _string_list(table, key, label)
    )


def _validate_https_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    if (
        any(ord(character) < 32 for character in endpoint)
        or len(endpoint.encode("utf-8")) > 4_096
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part in {".", ".."} for part in parsed.path.split("/"))
    ):
        _error("deployment_config_invalid", "the licensed endpoint is not an authorized HTTPS base")


def _load_deployment_config(path: Path) -> DeploymentConfig:
    document = _read_private_toml(path)
    _reject_unknown(document, {"schema_version", "kegg", "core", "deepkoala", "renderer"}, "root")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        _error("deployment_config_invalid", "schema_version must be 1")

    kegg = _required_table(document, "kegg")
    _reject_unknown(
        kegg,
        {
            "access_mode",
            "academic_use_confirmed",
            "licensed_endpoint",
            "licensed_use_confirmed",
            "cache_path",
            "rate_limit_root",
        },
        "kegg",
    )
    mode = _required_string(kegg, "access_mode", "kegg")
    if mode not in {"public_academic", "licensed", "offline_cache"}:
        _error("deployment_config_invalid", "kegg.access_mode is unsupported")
    academic = _boolean(kegg, "academic_use_confirmed", False, "kegg")
    licensed_endpoint = _optional_string(kegg, "licensed_endpoint", "kegg")
    licensed_confirmed = _boolean(kegg, "licensed_use_confirmed", False, "kegg")
    raw_cache = _optional_string(kegg, "cache_path", "kegg")
    cache_path = (
        _new_file_path(
            _absolute_path(raw_cache, "kegg.cache_path"),
            "kegg.cache_path",
            writable=mode != "offline_cache",
        )
        if raw_cache is not None
        else None
    )
    rate_limit_root = _existing_directory(
        _absolute_path(_required_string(kegg, "rate_limit_root", "kegg"), "kegg.rate_limit_root"),
        "kegg.rate_limit_root",
        private=True,
        writable=True,
    )
    if mode == "public_academic":
        if not academic or licensed_endpoint is not None or licensed_confirmed:
            _error(
                "deployment_config_invalid",
                "public_academic mode requires only explicit academic-use confirmation",
            )
    elif mode == "licensed":
        if academic or licensed_endpoint is None or not licensed_confirmed:
            _error(
                "deployment_config_invalid",
                "licensed mode requires an endpoint and explicit licensed-use confirmation",
            )
        _validate_https_endpoint(licensed_endpoint)
    else:
        if (
            academic
            or cache_path is None
            or ((licensed_endpoint is None) != (not licensed_confirmed))
        ):
            _error(
                "deployment_config_invalid",
                "offline_cache mode requires a cache and either both licensed namespace "
                "fields or neither",
            )
        if licensed_endpoint is not None:
            _validate_https_endpoint(licensed_endpoint)
        if not cache_path.is_file():
            _error("deployment_path_invalid", "offline_cache mode requires an existing cache file")
        if stat.S_IMODE(cache_path.stat().st_mode) != 0o600:
            _error("deployment_path_invalid", "offline_cache mode requires cache mode 0600")

    core = _required_table(document, "core")
    _reject_unknown(core, {"result_store_path", "allowed_roots"}, "core")
    result_store = _new_file_path(
        _absolute_path(
            _required_string(core, "result_store_path", "core"), "core.result_store_path"
        ),
        "core.result_store_path",
        writable=True,
    )
    core_roots = _paths(core, "allowed_roots", "core")

    deepkoala = _required_table(document, "deepkoala")
    _reject_unknown(
        deepkoala,
        {
            "state_root",
            "input_roots",
            "output_roots",
            "allowed_models",
            "cpu_threads",
            "allow_multi",
            "profiles_dir",
            "hmmsearch_executable",
        },
        "deepkoala",
    )
    deepkoala_state = _existing_directory(
        _absolute_path(
            _required_string(deepkoala, "state_root", "deepkoala"), "deepkoala.state_root"
        ),
        "deepkoala.state_root",
        private=True,
        writable=True,
    )
    deepkoala_inputs = _paths(deepkoala, "input_roots", "deepkoala")
    deepkoala_outputs = _paths(deepkoala, "output_roots", "deepkoala", writable=True)
    raw_allowed_models = deepkoala.get("allowed_models", ["full", "frag"])
    if not isinstance(raw_allowed_models, list):
        _error("deployment_config_invalid", "deepkoala.allowed_models is invalid")
    model_items = cast(list[object], raw_allowed_models)
    if (
        not model_items
        or len(model_items) > 2
        or not all(isinstance(model, str) for model in model_items)
    ):
        _error("deployment_config_invalid", "deepkoala.allowed_models is invalid")
    allowed_models = cast(list[str], model_items)
    if len(allowed_models) != len(set(allowed_models)) or any(
        model not in {"full", "frag"} for model in allowed_models
    ):
        _error("deployment_config_invalid", "deepkoala.allowed_models is invalid")
    cpu_threads = _integer(deepkoala, "cpu_threads", 2, 1, 4, "deepkoala")
    allow_multi = _boolean(deepkoala, "allow_multi", False, "deepkoala")
    raw_profiles_dir = _optional_string(deepkoala, "profiles_dir", "deepkoala")
    raw_hmmsearch = _optional_string(deepkoala, "hmmsearch_executable", "deepkoala")
    if allow_multi:
        if raw_profiles_dir is None or raw_hmmsearch is None:
            _error(
                "deployment_config_invalid",
                "deepkoala multi-domain mode requires profiles_dir and hmmsearch_executable",
            )
        profiles_dir = _existing_directory(
            _absolute_path(raw_profiles_dir, "deepkoala.profiles_dir"),
            "deepkoala.profiles_dir",
        )
        hmmsearch_executable = _existing_safe_executable(
            _absolute_path(raw_hmmsearch, "deepkoala.hmmsearch_executable"),
            "deepkoala.hmmsearch_executable",
        )
    else:
        if raw_profiles_dir is not None or raw_hmmsearch is not None:
            _error(
                "deployment_config_invalid",
                "deepkoala multi-domain paths require allow_multi=true",
            )
        profiles_dir = None
        hmmsearch_executable = None

    renderer = _required_table(document, "renderer")
    _reject_unknown(renderer, {"state_root", "allowed_roots", "offline_allow_stale"}, "renderer")
    renderer_state = _existing_directory(
        _absolute_path(_required_string(renderer, "state_root", "renderer"), "renderer.state_root"),
        "renderer.state_root",
        private=True,
        writable=True,
    )
    renderer_roots = _paths(renderer, "allowed_roots", "renderer")
    offline_allow_stale = _boolean(renderer, "offline_allow_stale", False, "renderer")
    if mode != "offline_cache" and offline_allow_stale:
        _error(
            "deployment_config_invalid",
            "renderer.offline_allow_stale is valid only in offline_cache mode",
        )

    config = DeploymentConfig(
        kegg=KeggAccessConfig(
            mode=mode,
            academic_use_confirmed=academic,
            licensed_endpoint=licensed_endpoint,
            licensed_use_confirmed=licensed_confirmed,
            cache_path=cache_path,
            rate_limit_root=rate_limit_root,
        ),
        core=CoreConfig(result_store_path=result_store, allowed_roots=core_roots),
        deepkoala=DeepKoalaConfig(
            state_root=deepkoala_state,
            input_roots=deepkoala_inputs,
            output_roots=deepkoala_outputs,
            allowed_models=tuple(allowed_models),
            cpu_threads=cpu_threads,
            allow_multi=allow_multi,
            profiles_dir=profiles_dir,
            hmmsearch_executable=hmmsearch_executable,
        ),
        renderer=RendererConfig(
            state_root=renderer_state,
            allowed_roots=renderer_roots,
            offline_allow_stale=offline_allow_stale,
        ),
    )
    _validate_cross_component_paths(config)
    return config


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _covered(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or path.is_relative_to(root) for root in roots)


def _validate_cross_component_paths(config: DeploymentConfig) -> None:
    if any(
        not _covered(root, config.core.allowed_roots)
        for root in (*config.deepkoala.input_roots, *config.deepkoala.output_roots)
    ):
        _error(
            "deployment_path_invalid",
            "core.allowed_roots must cover every DeepKOALA input and output root",
        )
    if any(not _covered(root, config.core.allowed_roots) for root in config.renderer.allowed_roots):
        _error(
            "deployment_path_invalid",
            "core.allowed_roots must cover every renderer handoff root",
        )
    states = (config.deepkoala.state_root, config.renderer.state_root, config.kegg.rate_limit_root)
    if any(
        _overlap(first, second)
        for index, first in enumerate(states)
        for second in states[index + 1 :]
    ):
        _error("deployment_path_invalid", "private state roots must not overlap")
    shared = (
        *config.core.allowed_roots,
        *config.deepkoala.input_roots,
        *config.deepkoala.output_roots,
        *config.renderer.allowed_roots,
    )
    if any(_overlap(state, root) for state in states for root in shared):
        _error("deployment_path_invalid", "private state roots must not overlap shared roots")
    if config.kegg.cache_path is not None and any(
        _overlap(config.kegg.cache_path, root) for root in shared
    ):
        _error("deployment_path_invalid", "the KEGG cache must remain outside shared roots")
    if any(_overlap(config.core.result_store_path, root) for root in shared):
        _error("deployment_path_invalid", "the Core result store must remain outside shared roots")
    private_files = (config.core.result_store_path,)
    if config.kegg.cache_path is not None:
        private_files = (*private_files, config.kegg.cache_path)
    multi_resources = tuple(
        path
        for path in (
            config.deepkoala.profiles_dir,
            config.deepkoala.hmmsearch_executable,
        )
        if path is not None
    )
    if any(
        _overlap(resource, path)
        for resource in multi_resources
        for path in (*states, *shared, *private_files)
    ):
        _error(
            "deployment_path_invalid",
            "DeepKOALA multi-domain resources must remain outside private and shared roots",
        )
    if any(_overlap(path, state) for path in private_files for state in states) or (
        len(private_files) == 2 and _overlap(private_files[0], private_files[1])
    ):
        _error("deployment_path_invalid", "private files and state roots must not overlap")


def _resolve_executable(value: Path, name: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        _error("required_tool_unavailable", f"the {name} executable must resolve absolutely")
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError:
        _error("required_tool_unavailable", f"the required {name} executable is unavailable")
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        _error("required_tool_unavailable", f"the required {name} executable is not executable")
    return resolved


def _safe_subprocess_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith(
            ("KEGG_MCP_", "KEGG_RENDER_MCP_", "DEEPKOALA_MCP_", "GIT_", "PIP_", "UV_")
        ) or key in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
            "UV_PROJECT_ENVIRONMENT",
        }:
            environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    if extra is not None:
        environment.update(extra)
    return environment


def _run_command(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one argv-only subprocess and capture protocol-unsafe output."""
    arguments = list(argv)
    timeout_seconds = DEFAULT_COMMAND_TIMEOUT_SECONDS
    if len(arguments) > 1 and arguments[1] == "sync":
        timeout_seconds = UV_SYNC_TIMEOUT_SECONDS
    elif "clone" in arguments[1:3] or (
        "-m" in arguments and any(module in arguments for module in ("pip", "venv"))
    ):
        timeout_seconds = DEEPKOALA_INSTALL_TIMEOUT_SECONDS
    elif "doctor" in arguments:
        timeout_seconds = DOCTOR_TIMEOUT_SECONDS
    try:
        process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=dict(environment) if environment is not None else _safe_subprocess_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        _error("tool_execution_failed", "a required local command could not be executed")
    if process.stdout is None or process.stderr is None:
        _terminate_process(process)
        _error("tool_execution_failed", "a required local command could not be captured safely")
    streams = selectors.DefaultSelector()
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout_seconds
    try:
        while streams.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process(process)
                _error("tool_execution_failed", "a required local command exceeded its time limit")
            for key, _ in streams.select(min(remaining, 1.0)):
                chunk = os.read(key.fd, 65_536)
                if not chunk:
                    streams.unregister(key.fileobj)
                    cast(BinaryIO, key.fileobj).close()
                    continue
                buffer = buffers[cast(str, key.data)]
                buffer.extend(chunk)
                if len(buffer) > MAX_COMMAND_OUTPUT_BYTES:
                    _terminate_process(process)
                    _error(
                        "tool_execution_failed",
                        "a required local command exceeded its output limit",
                    )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(process)
            _error("tool_execution_failed", "a required local command exceeded its time limit")
        return_code = process.wait(timeout=remaining)
    except (OSError, subprocess.TimeoutExpired):
        _terminate_process(process)
        _error("tool_execution_failed", "a required local command failed bounded execution")
    except BaseException:
        _terminate_process(process)
        raise
    finally:
        streams.close()
    return subprocess.CompletedProcess(
        arguments,
        return_code,
        stdout=bytes(buffers["stdout"]).decode("utf-8", errors="replace"),
        stderr=bytes(buffers["stderr"]).decode("utf-8", errors="replace"),
    )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.wait(timeout=5)


def _successful(
    argv: Sequence[str],
    *,
    code: str,
    message: str,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = _run_command(argv, cwd=cwd, environment=environment)
    if result.returncode != 0:
        _error(code, message)
    return result


def _json_command(
    argv: Sequence[str],
    *,
    code: str,
    message: str,
    environment: Mapping[str, str] | None = None,
) -> object:
    result = _successful(argv, code=code, message=message, environment=environment)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        _error(code, message)


def _validate_python(python: Path) -> None:
    script = (
        "import json,platform,sys;"
        "print(json.dumps([list(sys.version_info[:2]),platform.python_implementation()]))"
    )
    result = _successful(
        [str(python), "-I", "-c", script],
        code="python_runtime_unsupported",
        message="the selected Python runtime could not be inspected",
    )
    try:
        version = json.loads(result.stdout)
    except json.JSONDecodeError:
        _error(
            "python_runtime_unsupported", "the selected Python runtime returned invalid metadata"
        )
    if version != [[3, 11], "CPython"]:
        _error("python_runtime_unsupported", "the suite requires CPython 3.11")


def _validate_uv(uv: Path) -> None:
    version = _successful(
        [str(uv), "--version"],
        code="uv_runtime_unsupported",
        message="the selected uv executable could not be inspected",
    ).stdout.strip()
    matched = UV_VERSION_PATTERN.fullmatch(version)
    if matched is None:
        _error("uv_runtime_unsupported", "the selected executable did not identify itself as uv")
    parsed_version = tuple(int(matched.group(name)) for name in ("major", "minor", "patch"))
    if parsed_version < MINIMUM_UV_VERSION:
        _error("uv_runtime_unsupported", "the selected uv version is below the supported minimum")
    help_result = _successful(
        [str(uv), "sync", "--help"],
        code="uv_runtime_unsupported",
        message="the selected uv executable does not provide compatible sync behavior",
    )
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    if any(option not in help_text for option in UV_REQUIRED_SYNC_OPTIONS):
        _error("uv_runtime_unsupported", "the selected uv sync command lacks required controls")


def _validate_install_root(install_root: Path, config_path: Path, config: DeploymentConfig) -> Path:
    checked = _absolute_path(str(install_root), "install root")
    if checked.exists() or checked.is_symlink():
        _error("install_root_exists", "the install root must not already exist")
    parent = _existing_directory(checked.parent, "install root parent", private=True, writable=True)
    if checked.parent != parent:
        _error("deployment_path_invalid", "the install root parent is invalid")
    protected = (
        PROJECT_ROOT,
        config_path,
        config.deepkoala.state_root,
        config.renderer.state_root,
        config.kegg.rate_limit_root,
        config.core.result_store_path,
        *config.core.allowed_roots,
    )
    if config.kegg.cache_path is not None:
        protected = (*protected, config.kegg.cache_path)
    if config.deepkoala.profiles_dir is not None:
        protected = (*protected, config.deepkoala.profiles_dir)
    if config.deepkoala.hmmsearch_executable is not None:
        protected = (*protected, config.deepkoala.hmmsearch_executable)
    if any(_overlap(checked, path) for path in protected):
        _error("deployment_path_invalid", "the install root overlaps source or deployment data")
    return checked


def _request_from_arguments(
    arguments: argparse.Namespace, config: DeploymentConfig
) -> InstallRequest:
    dry_run = cast(bool, arguments.dry_run)
    allow_deepkoala_install = cast(bool, arguments.allow_deepkoala_install)
    if not dry_run and not allow_deepkoala_install:
        _error(
            "deepkoala_install_confirmation_required",
            "first-time DeepKOALA installation requires explicit user confirmation",
        )
    marketplace_name = cast(str, arguments.marketplace_name)
    if NAME_PATTERN.fullmatch(marketplace_name) is None:
        _error("marketplace_name_invalid", "the marketplace name must be lowercase kebab-case")
    config_path = _existing_regular_file(
        cast(Path, arguments.config), "config", executable=False, private=True
    )
    install_root = _validate_install_root(cast(Path, arguments.install_root), config_path, config)
    uv = _resolve_executable(cast(Path, arguments.uv), "uv")
    codex = _resolve_executable(cast(Path, arguments.codex), "codex")
    git = _resolve_executable(cast(Path, arguments.git), "git")
    python = _resolve_executable(cast(Path, arguments.python), "python")
    _validate_uv(uv)
    _validate_python(python)
    return InstallRequest(
        install_root=install_root,
        marketplace_name=marketplace_name,
        uv=uv,
        codex=codex,
        git=git,
        python=python,
        allow_locked_dependency_downloads=cast(bool, arguments.allow_locked_dependency_downloads),
        dry_run=dry_run,
        allow_deepkoala_install=allow_deepkoala_install,
    )


def _project_versions(root: Path) -> dict[str, str]:
    versions: dict[str, str] = {}
    for distribution, relative in PROJECT_FILES.items():
        path = root / relative
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
            project = document["project"]
            version = project["version"]
        except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
            _error(
                "source_snapshot_invalid", "a project version could not be read from the snapshot"
            )
        if not isinstance(version, str) or VERSION_PATTERN.fullmatch(version) is None:
            _error("source_snapshot_invalid", "a project version is not strict semantic versioning")
        versions[distribution] = version
    return versions


def _prepare_source_snapshot(root: Path = PROJECT_ROOT) -> SourceSnapshot:
    try:
        source_root = root.resolve(strict=True)
    except OSError:
        _error("source_checkout_invalid", "the source checkout is unavailable")
    required = (
        "uv.lock",
        "companions/deepkoala-mcp/uv.lock",
        "companions/kegg-render-mcp/uv.lock",
        "scripts/install-suite.py",
        "scripts/run-installed-mcp.py",
        *(f".agents/skills/{name}/SKILL.md" for name in SKILL_NAMES),
    )
    if any(not (source_root / relative).is_file() for relative in required):
        _error("source_snapshot_invalid", "the source checkout is incomplete")
    return SourceSnapshot(root=source_root, versions=_project_versions(source_root))


def _marketplace_entries(document: object) -> dict[str, str | None]:
    if not isinstance(document, dict):
        _error("codex_state_invalid", "Codex returned an unsupported marketplace document")
    typed_document = cast(dict[str, object], document)
    raw_marketplaces = typed_document.get("marketplaces")
    if not isinstance(raw_marketplaces, list):
        _error("codex_state_invalid", "Codex returned an unsupported marketplace document")
    entries: dict[str, str | None] = {}
    for raw in cast(list[object], raw_marketplaces):
        if not isinstance(raw, dict):
            _error("codex_state_invalid", "Codex returned an invalid marketplace entry")
        entry = cast(dict[str, object], raw)
        name = entry.get("name")
        if not isinstance(name, str):
            _error("codex_state_invalid", "Codex returned an invalid marketplace entry")
        root = entry.get("root")
        if root is not None and not isinstance(root, str):
            _error("codex_state_invalid", "Codex returned an invalid marketplace root")
        entries[name] = root
    return entries


def _plugin_identity(entry: object) -> tuple[str | None, str | None]:
    if not isinstance(entry, dict):
        return None, None
    typed_entry = cast(dict[str, object], entry)
    name = typed_entry.get("name")
    marketplace = typed_entry.get("marketplaceName") or typed_entry.get("marketplace")
    selector = typed_entry.get("pluginId") or typed_entry.get("id") or typed_entry.get("selector")
    if isinstance(selector, str) and "@" in selector:
        selected_name, selected_marketplace = selector.rsplit("@", 1)
        if not isinstance(name, str):
            name = selected_name
        if not isinstance(marketplace, str):
            marketplace = selected_marketplace
    return (
        name if isinstance(name, str) else None,
        marketplace if isinstance(marketplace, str) else None,
    )


def _installed_plugins(document: object) -> tuple[tuple[str | None, str | None], ...]:
    if not isinstance(document, dict):
        _error("codex_state_invalid", "Codex returned an unsupported plugin document")
    typed_document = cast(dict[str, object], document)
    installed = typed_document.get("installed")
    if not isinstance(installed, list):
        _error("codex_state_invalid", "Codex returned an unsupported plugin document")
    identities: list[tuple[str | None, str | None]] = []
    for entry in cast(list[object], installed):
        identity = _plugin_identity(entry)
        if identity[0] is None:
            _error("codex_state_invalid", "Codex returned an invalid installed plugin entry")
        identities.append(identity)
    return tuple(identities)


def _mcp_entries(document: object) -> dict[str, dict[str, object]]:
    if not isinstance(document, list):
        _error("codex_state_invalid", "Codex returned an unsupported MCP document")
    entries: dict[str, dict[str, object]] = {}
    for raw_entry in cast(list[object], document):
        if not isinstance(raw_entry, dict):
            _error("codex_state_invalid", "Codex returned an invalid MCP entry")
        entry = cast(dict[str, object], raw_entry)
        name = entry.get("name")
        if not isinstance(name, str):
            _error("codex_state_invalid", "Codex returned an invalid MCP entry")
        if name in entries:
            _error("codex_state_invalid", "Codex returned duplicate MCP names")
        entries[name] = entry
    return entries


def _mcp_names(document: object) -> set[str]:
    return set(_mcp_entries(document))


def _codex_marketplaces(request: InstallRequest) -> dict[str, str | None]:
    document = _json_command(
        [str(request.codex), "plugin", "marketplace", "list", "--json"],
        code="codex_plugin_unsupported",
        message="Codex plugin marketplace discovery is unavailable",
    )
    return _marketplace_entries(document)


def _codex_plugins(request: InstallRequest) -> tuple[tuple[str | None, str | None], ...]:
    document = _json_command(
        [str(request.codex), "plugin", "list", "--json"],
        code="codex_plugin_unsupported",
        message="Codex plugin discovery is unavailable",
    )
    return _installed_plugins(document)


def _codex_mcps(request: InstallRequest) -> set[str]:
    document = _json_command(
        [str(request.codex), "mcp", "list", "--json"],
        code="codex_mcp_discovery_failed",
        message="Codex MCP discovery is unavailable",
    )
    return _mcp_names(document)


def _codex_mcp_entries(request: InstallRequest) -> dict[str, dict[str, object]]:
    document = _json_command(
        [str(request.codex), "mcp", "list", "--json"],
        code="codex_mcp_discovery_failed",
        message="Codex MCP discovery is unavailable",
    )
    return _mcp_entries(document)


def _preflight_codex(request: InstallRequest) -> None:
    if request.marketplace_name in _codex_marketplaces(request):
        _error("marketplace_conflict", "the requested Codex marketplace name already exists")
    if any(name == PLUGIN_NAME for name, _ in _codex_plugins(request)):
        _error("plugin_conflict", "an installed Codex plugin already uses the suite plugin name")
    collisions = set(SERVER_NAMES) & _codex_mcps(request)
    if collisions:
        _error("mcp_name_conflict", "one or more suite MCP names are already registered in Codex")


def _managed_deepkoala_paths(install_root: Path) -> tuple[Path, Path]:
    root = install_root / "deepkoala"
    return root / "source", root / "venv" / "bin" / "python"


def _deployment_environments(
    config: DeploymentConfig, install_root: Path
) -> dict[str, dict[str, str]]:
    core = {
        "KEGG_MCP_ACCESS_MODE": config.kegg.mode,
        "KEGG_MCP_RATE_LIMIT_ROOT": str(config.kegg.rate_limit_root),
        "KEGG_MCP_RESULT_STORE_PATH": str(config.core.result_store_path),
        "KEGG_MCP_ALLOWED_ROOTS": os.pathsep.join(str(root) for root in config.core.allowed_roots),
    }
    renderer = {
        "KEGG_RENDER_MCP_STATE_ROOT": str(config.renderer.state_root),
        "KEGG_RENDER_MCP_ALLOWED_ROOTS": os.pathsep.join(
            str(root) for root in config.renderer.allowed_roots
        ),
        "KEGG_RENDER_MCP_ACCESS_MODE": config.kegg.mode,
        "KEGG_RENDER_MCP_OFFLINE_ALLOW_STALE": str(config.renderer.offline_allow_stale).lower(),
        "KEGG_MCP_RATE_LIMIT_ROOT": str(config.kegg.rate_limit_root),
    }
    if config.kegg.academic_use_confirmed:
        core["KEGG_MCP_ACADEMIC_USE_CONFIRMED"] = "true"
        renderer["KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED"] = "true"
    if config.kegg.licensed_use_confirmed:
        core["KEGG_MCP_LICENSED_USE_CONFIRMED"] = "true"
        renderer["KEGG_RENDER_MCP_LICENSED_USE_CONFIRMED"] = "true"
    if config.kegg.licensed_endpoint is not None:
        core["KEGG_MCP_LICENSED_ENDPOINT"] = config.kegg.licensed_endpoint
        renderer["KEGG_RENDER_MCP_LICENSED_ENDPOINT"] = config.kegg.licensed_endpoint
    if config.kegg.cache_path is not None:
        core["KEGG_MCP_CACHE_PATH"] = str(config.kegg.cache_path)
        renderer["KEGG_RENDER_MCP_CACHE_PATH"] = str(config.kegg.cache_path)
    checkout, python_executable = _managed_deepkoala_paths(install_root)
    deepkoala = {
        "DEEPKOALA_MCP_CHECKOUT": str(checkout),
        "DEEPKOALA_MCP_PYTHON": str(python_executable),
        "DEEPKOALA_MCP_STATE_ROOT": str(config.deepkoala.state_root),
        "DEEPKOALA_MCP_INPUT_ROOTS": os.pathsep.join(
            str(root) for root in config.deepkoala.input_roots
        ),
        "DEEPKOALA_MCP_OUTPUT_ROOTS": os.pathsep.join(
            str(root) for root in config.deepkoala.output_roots
        ),
        "DEEPKOALA_MCP_ALLOWED_MODELS": ",".join(config.deepkoala.allowed_models),
        "DEEPKOALA_MCP_ALLOWED_DEVICES": "cpu,cuda",
        "DEEPKOALA_MCP_CPU_THREADS": str(config.deepkoala.cpu_threads),
        "DEEPKOALA_MCP_ALLOW_MULTI": str(config.deepkoala.allow_multi).lower(),
    }
    if config.deepkoala.allow_multi:
        if config.deepkoala.profiles_dir is None or config.deepkoala.hmmsearch_executable is None:
            _error(
                "deployment_config_invalid",
                "deepkoala multi-domain deployment paths are unavailable",
            )
        deepkoala["DEEPKOALA_MCP_PROFILES_DIR"] = str(config.deepkoala.profiles_dir)
        deepkoala["DEEPKOALA_MCP_HMMSEARCH_EXECUTABLE"] = str(config.deepkoala.hmmsearch_executable)
    return {
        "deepkoala-mcp": deepkoala,
        "kegg-mcp": core,
        "kegg-render-mcp": renderer,
    }


def _write_json(path: Path, document: object, *, mode: int) -> None:
    payload = (json.dumps(document, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()
    if len(payload) > MAX_GENERATED_JSON_BYTES:
        _error("installation_write_failed", "an installer-managed document exceeds its byte limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        path.chmod(mode)
    except OSError:
        _error("installation_write_failed", "an installer-managed file could not be written")


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("not a directory")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        _error("installation_write_failed", "an installer-managed directory could not be synced")


def _create_install_root(install_root: Path) -> tuple[int, int]:
    created = False
    try:
        install_root.mkdir(mode=0o700)
        created = True
        install_root.chmod(0o700)
        metadata = install_root.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OSError("the install root did not retain its private directory mode")
    except OSError:
        cleanup_ok = not created
        if created:
            try:
                current = install_root.lstat()
                if (
                    stat.S_ISDIR(current.st_mode)
                    and not stat.S_ISLNK(current.st_mode)
                    and not any(install_root.iterdir())
                ):
                    install_root.rmdir()
                    cleanup_ok = True
            except OSError:
                cleanup_ok = False
        if not cleanup_ok:
            _error(
                "installation_rollback_failed",
                "the new install root could not be initialized or removed safely",
            )
        _error("installation_write_failed", "the private install root could not be created")
    return metadata.st_dev, metadata.st_ino


def _runtime_python(install_root: Path, runtime: str) -> Path:
    return install_root / "runtimes" / runtime / "bin" / "python"


def _runtime_command(install_root: Path, server_name: str) -> Path:
    runtime, executable = RUNTIME_COMMANDS[server_name]
    return install_root / "runtimes" / runtime / "bin" / executable


def _install_runtimes(request: InstallRequest, snapshot: SourceSnapshot) -> None:
    runtimes_root = request.install_root / "runtimes"
    runtimes_root.mkdir(mode=0o700)
    uv_config_root = request.install_root / "uv-config"
    uv_config_root.mkdir(mode=0o700)
    for runtime in RUNTIME_NAMES:
        destination = runtimes_root / runtime
        project = snapshot.root / RUNTIME_PROJECTS[runtime]
        uv_arguments = [
            str(request.uv),
            "sync",
            "--locked",
            "--no-dev",
            "--no-editable",
            "--no-python-downloads",
            "--no-progress",
            "--python",
            str(request.python),
        ]
        if not request.allow_locked_dependency_downloads:
            uv_arguments.append("--offline")
        environment = _safe_subprocess_environment(
            {
                "UV_PROJECT_ENVIRONMENT": str(destination),
                "UV_NO_SYSTEM_CONFIG": "1",
                "UV_PYTHON_DOWNLOADS": "never",
                "XDG_CONFIG_HOME": str(uv_config_root),
                **({} if request.allow_locked_dependency_downloads else {"UV_OFFLINE": "1"}),
            }
        )
        _successful(
            uv_arguments,
            code="runtime_install_failed",
            message=f"the locked {runtime} runtime could not be installed",
            cwd=project,
            environment=environment,
        )
        if not destination.is_dir():
            _error("runtime_install_failed", f"the {runtime} runtime was not materialized")


def _install_managed_deepkoala(request: InstallRequest, config: DeepKoalaConfig) -> None:
    checkout, python_executable = _managed_deepkoala_paths(request.install_root)
    checkout.parent.mkdir(mode=0o700)
    git_environment = _safe_subprocess_environment(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    _successful(
        [
            str(request.git),
            "clone",
            "--depth",
            "1",
            DEEPKOALA_REPOSITORY,
            str(checkout),
        ],
        code="deepkoala_install_failed",
        message="the official DeepKOALA repository could not be cloned",
        environment=git_environment,
    )
    requirements = checkout / "requirements.txt"
    if not requirements.is_file():
        _error("deepkoala_install_failed", "the DeepKOALA requirements file is unavailable")
    venv = python_executable.parents[1]
    _successful(
        [str(request.python), "-I", "-m", "venv", "--copies", str(venv)],
        code="deepkoala_install_failed",
        message="the DeepKOALA Python environment could not be created",
    )
    if not python_executable.is_file() or not os.access(python_executable, os.X_OK):
        _error("deepkoala_install_failed", "the DeepKOALA Python environment is unavailable")
    _successful(
        [
            str(python_executable),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-input",
            "--quiet",
            "-r",
            str(requirements),
        ],
        code="deepkoala_install_failed",
        message="the DeepKOALA upstream requirements could not be installed",
    )
    resource_root = checkout / "resources" / DEFAULT_DEEPKOALA_MODEL_DATE
    for model in config.allowed_models:
        for name in (f"weights_{model}.pt", f"ko_config_{model}.json"):
            path = resource_root / name
            try:
                available = path.is_file() and path.stat().st_size > 0
            except OSError:
                available = False
            if not available:
                _error(
                    "deepkoala_install_failed",
                    f"the bundled {DEFAULT_DEEPKOALA_MODEL_DATE} DeepKOALA resources are missing",
                )


def _verify_distribution_versions(request: InstallRequest, snapshot: SourceSnapshot) -> None:
    script = (
        "import importlib.metadata,json,sys;"
        "print(json.dumps({name:importlib.metadata.version(name) for name in sys.argv[1:]}))"
    )
    for runtime, distributions in RUNTIME_DISTRIBUTIONS.items():
        python = _runtime_python(request.install_root, runtime)
        if not python.is_file() or not os.access(python, os.X_OK):
            _error("runtime_verification_failed", f"the {runtime} Python executable is unavailable")
        result = _successful(
            [str(python), "-I", "-c", script, *distributions],
            code="runtime_verification_failed",
            message=f"the {runtime} runtime metadata could not be verified",
        )
        try:
            installed = json.loads(result.stdout)
        except json.JSONDecodeError:
            _error("runtime_verification_failed", "an installed runtime returned invalid metadata")
        expected = {name: snapshot.versions[name] for name in distributions}
        if installed != expected:
            _error("runtime_verification_failed", "installed distribution versions do not match")
    for server_name in SERVER_NAMES:
        command = _runtime_command(request.install_root, server_name)
        if not command.is_file() or not os.access(command, os.X_OK):
            _error("runtime_verification_failed", "an installed MCP entry point is unavailable")


def _verify_runtime_configuration(
    request: InstallRequest, environments: Mapping[str, Mapping[str, str]]
) -> None:
    core_result = _successful(
        [str(_runtime_command(request.install_root, "kegg-mcp")), "doctor", "--json"],
        code="runtime_configuration_invalid",
        message="the Core MCP rejected the deployment configuration",
        environment=_safe_subprocess_environment(environments["kegg-mcp"]),
    )
    try:
        core_document = json.loads(core_result.stdout)
    except json.JSONDecodeError:
        _error("runtime_configuration_invalid", "the Core MCP returned invalid doctor output")
    if not isinstance(core_document, dict):
        _error(
            "runtime_configuration_invalid", "the Core MCP rejected the deployment configuration"
        )
    typed_core_document = cast(dict[str, object], core_document)
    if typed_core_document.get("configuration_valid") is not True:
        _error(
            "runtime_configuration_invalid", "the Core MCP rejected the deployment configuration"
        )

    if environments["kegg-mcp"].get("KEGG_MCP_ACCESS_MODE") == "offline_cache":
        cache_probe = (
            "import os;from datetime import UTC,datetime;"
            "from kegg_mcp.kegg.cache import SQLiteKeggCache;"
            "SQLiteKeggCache(os.environ['KEGG_MCP_CACHE_PATH'],read_only=True).status("
            "now=datetime.now(UTC))"
        )
        _successful(
            [str(_runtime_python(request.install_root, "core")), "-I", "-c", cache_probe],
            code="runtime_configuration_invalid",
            message="the offline KEGG cache failed its read-only schema check",
            environment=_safe_subprocess_environment(environments["kegg-mcp"]),
        )

    deep_result = _run_command(
        [str(_runtime_command(request.install_root, "deepkoala-mcp")), "doctor", "--json"],
        environment=_safe_subprocess_environment(environments["deepkoala-mcp"]),
    )
    if deep_result.returncode != 0:
        _error("runtime_configuration_invalid", "the installed DeepKOALA runtime is not ready")
    try:
        deep_document = json.loads(deep_result.stdout)
    except json.JSONDecodeError:
        _error("runtime_configuration_invalid", "the DeepKOALA MCP returned invalid doctor output")
    if not isinstance(deep_document, dict):
        _error("runtime_configuration_invalid", "the DeepKOALA MCP rejected the deployment policy")
    typed_deep_document = cast(dict[str, object], deep_document)
    if (
        typed_deep_document.get("configuration_valid") is not True
        or typed_deep_document.get("route_state") != "local_ready"
    ):
        _error("runtime_configuration_invalid", "the installed DeepKOALA runtime is not ready")

    renderer_script = "from kegg_render_mcp.config import load_runtime_config;load_runtime_config()"
    _successful(
        [str(_runtime_python(request.install_root, "renderer")), "-I", "-c", renderer_script],
        code="runtime_configuration_invalid",
        message="the Renderer MCP rejected the deployment configuration",
        environment=_safe_subprocess_environment(environments["kegg-render-mcp"]),
    )


def _bounded_tree_entries(root: Path) -> list[tuple[str, Path, os.stat_result]]:
    try:
        root_metadata = root.lstat()
    except OSError:
        _error("skill_source_invalid", "the Skill source is unavailable")
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        _error("skill_source_invalid", "the Skill source is not a direct directory")
    entries: list[tuple[str, Path, os.stat_result]] = []
    total_bytes = 0
    try:
        candidates = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    except OSError:
        _error("skill_source_invalid", "the Skill source could not be enumerated")
    if len(candidates) > MAX_SKILL_ENTRIES:
        _error("skill_source_invalid", "the Skill source has too many entries")
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        try:
            metadata = path.lstat()
        except OSError:
            _error("skill_source_invalid", "a Skill entry could not be inspected")
        if stat.S_ISLNK(metadata.st_mode) or not (
            stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)
        ):
            _error("skill_source_invalid", "the Skill source contains an unsafe entry")
        if stat.S_ISREG(metadata.st_mode):
            total_bytes += metadata.st_size
            if total_bytes > MAX_SKILL_BYTES:
                _error("skill_source_invalid", "the Skill source exceeds its byte limit")
        entries.append((relative, path, metadata))
    return entries


def _copy_skill_bundle(source: Path, destination: Path) -> None:
    try:
        source_names = {path.name for path in source.iterdir()}
    except OSError:
        _error("skill_source_invalid", "the Skill source could not be read")
    if source_names != set(SKILL_NAMES):
        _error("skill_source_invalid", "the Skill set does not match the suite contract")
    entries = _bounded_tree_entries(source)
    destination.mkdir(mode=0o755)
    for relative, path, metadata in entries:
        target = destination.joinpath(*PurePosixPath(relative).parts)
        if stat.S_ISDIR(metadata.st_mode):
            target.mkdir(mode=0o755)
            continue
        try:
            with path.open("rb") as input_stream, target.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            target.chmod(0o755 if stat.S_IMODE(metadata.st_mode) & 0o111 else 0o644)
        except OSError:
            _error(
                "installation_write_failed", "the generated plugin Skill bundle could not be copied"
            )


def _copy_launcher(snapshot: SourceSnapshot, deployment_root: Path) -> Path:
    source = snapshot.root / "scripts" / "run-installed-mcp.py"
    try:
        metadata = source.lstat()
    except OSError:
        _error("source_snapshot_invalid", "the installed launcher source is unavailable")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _error("source_snapshot_invalid", "the installed launcher source is unsafe")
    target = deployment_root / "run-installed-mcp.py"
    try:
        with source.open("rb") as input_stream, target.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
        target.chmod(0o700)
    except OSError:
        _error("installation_write_failed", "the installed launcher could not be written")
    return target


def _materialize_deployment(
    request: InstallRequest,
    snapshot: SourceSnapshot,
    environments: Mapping[str, Mapping[str, str]],
) -> Path:
    deployment_root = request.install_root / "deployment"
    deployment_root.mkdir(mode=0o700)
    launcher = _copy_launcher(snapshot, deployment_root)
    commands = {
        server_name: str(_runtime_command(request.install_root, server_name))
        for server_name in SERVER_NAMES
    }
    _write_json(
        deployment_root / "deployment.json",
        {
            "schema_version": 1,
            "commands": commands,
            "environments": {name: dict(values) for name, values in environments.items()},
        },
        mode=0o600,
    )
    return launcher


def _plugin_manifest(version: str) -> dict[str, object]:
    return {
        "name": PLUGIN_NAME,
        "version": version,
        "description": "Local KEGG KO annotation, analysis, and evidence rendering workflows.",
        "author": {"name": "zhaoxi120", "url": "https://github.com/zhaoxi120"},
        "homepage": "https://github.com/zhaoxi120/kegg_mcp",
        "repository": "https://github.com/zhaoxi120/kegg_mcp",
        "license": "MIT",
        "keywords": ["bioinformatics", "KEGG", "KO", "MCP"],
        "skills": "./skills/",
        "mcpServers": "./.mcp.json",
        "interface": {
            "displayName": "KEGG MCP Suite",
            "shortDescription": "Local KO annotation, KEGG analysis, and evidence rendering.",
            "longDescription": (
                "Use three focused local Skills backed by independent stdio MCP processes for "
                "DeepKOALA orchestration, KEGG-aware analysis, and bounded pathway rendering."
            ),
            "developerName": "zhaoxi120",
            "category": "Education & Research",
            "capabilities": [],
            "websiteURL": "https://github.com/zhaoxi120/kegg_mcp",
            "defaultPrompt": [
                "Annotate this protein FASTA with my configured DeepKOALA companion.",
                "Analyze these KO annotations with cautious KEGG interpretation.",
                "Render the validated KEGG analysis handoff as static evidence figures.",
            ],
        },
    }


def _materialize_plugin(
    request: InstallRequest,
    snapshot: SourceSnapshot,
    launcher: Path,
    config: DeploymentConfig,
) -> Path:
    marketplace_root = request.install_root / "marketplace"
    plugin_root = marketplace_root / "plugins" / PLUGIN_NAME
    manifest_root = plugin_root / ".codex-plugin"
    manifest_root.mkdir(mode=0o755, parents=True)
    _copy_skill_bundle(snapshot.root / ".agents" / "skills", plugin_root / "skills")
    _write_json(
        manifest_root / "plugin.json",
        _plugin_manifest(snapshot.versions["kegg-mcp"]),
        mode=0o644,
    )
    mcp_servers = {
        server_name: {
            "command": str(_runtime_python(request.install_root, RUNTIME_COMMANDS[server_name][0])),
            "args": ["-I", str(launcher), server_name],
            "cwd": ".",
        }
        for server_name in SERVER_NAMES
    }
    _write_json(plugin_root / ".mcp.json", {"mcpServers": mcp_servers}, mode=0o644)
    marketplace_manifest_root = marketplace_root / ".agents" / "plugins"
    marketplace_manifest_root.mkdir(mode=0o755, parents=True)
    _write_json(
        marketplace_manifest_root / "marketplace.json",
        {
            "name": request.marketplace_name,
            "interface": {"displayName": "Local KEGG MCP Suite"},
            "plugins": [
                {
                    "name": PLUGIN_NAME,
                    "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
                    "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                    "category": "Education & Research",
                }
            ],
        },
        mode=0o644,
    )
    _validate_generated_plugin(request, snapshot, config, plugin_root, launcher)
    return marketplace_root


def _read_json_file(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _error("plugin_validation_failed", "a generated plugin document is invalid")


def _validate_generated_plugin(
    request: InstallRequest,
    snapshot: SourceSnapshot,
    config: DeploymentConfig,
    plugin_root: Path,
    launcher: Path,
) -> None:
    manifest = _read_json_file(plugin_root / ".codex-plugin" / "plugin.json")
    if not isinstance(manifest, dict):
        _error("plugin_validation_failed", "the generated plugin manifest is invalid")
    typed_manifest = cast(dict[str, object], manifest)
    if typed_manifest.get("name") != PLUGIN_NAME:
        _error("plugin_validation_failed", "the generated plugin manifest is invalid")
    if typed_manifest.get("version") != snapshot.versions["kegg-mcp"]:
        _error("plugin_validation_failed", "the generated plugin version is invalid")
    if (
        typed_manifest.get("skills") != "./skills/"
        or typed_manifest.get("mcpServers") != "./.mcp.json"
    ):
        _error("plugin_validation_failed", "the generated plugin component paths are invalid")
    skill_root = plugin_root / "skills"
    try:
        names = {path.name for path in skill_root.iterdir()}
    except OSError:
        _error("plugin_validation_failed", "the generated plugin Skills are unavailable")
    if names != set(SKILL_NAMES):
        _error("plugin_validation_failed", "the generated plugin Skill set is invalid")
    if any(not (skill_root / name / "SKILL.md").is_file() for name in SKILL_NAMES):
        _error("plugin_validation_failed", "a generated plugin Skill entrypoint is missing")

    document = _read_json_file(plugin_root / ".mcp.json")
    if not isinstance(document, dict):
        _error("plugin_validation_failed", "the generated MCP document is invalid")
    typed_document = cast(dict[str, object], document)
    if set(typed_document) != {"mcpServers"}:
        _error("plugin_validation_failed", "the generated MCP document is invalid")
    servers = typed_document.get("mcpServers")
    if not isinstance(servers, dict):
        _error("plugin_validation_failed", "the generated MCP set is invalid")
    typed_servers = cast(dict[str, object], servers)
    if set(typed_servers) != set(SERVER_NAMES):
        _error("plugin_validation_failed", "the generated MCP set is invalid")
    for server_name in SERVER_NAMES:
        raw = typed_servers.get(server_name)
        runtime = RUNTIME_COMMANDS[server_name][0]
        expected = {
            "command": str(_runtime_python(request.install_root, runtime)),
            "args": ["-I", str(launcher), server_name],
            "cwd": ".",
        }
        if raw != expected:
            _error("plugin_validation_failed", "a generated MCP launcher is invalid")
    serialized_public_config = json.dumps(
        {"plugin": typed_manifest, "mcp": typed_document}, ensure_ascii=True, sort_keys=True
    )
    private_values = {
        str(config.deepkoala.state_root),
        str(config.renderer.state_root),
        str(config.kegg.rate_limit_root),
        str(config.core.result_store_path),
        *(str(path) for path in config.core.allowed_roots),
    }
    if config.kegg.cache_path is not None:
        private_values.add(str(config.kegg.cache_path))
    if config.kegg.licensed_endpoint is not None:
        private_values.add(config.kegg.licensed_endpoint)
    if any(value in serialized_public_config for value in private_values):
        _error("plugin_validation_failed", "private deployment values escaped into plugin metadata")


def _plugin_is_installed(request: InstallRequest) -> bool:
    return any(
        name == PLUGIN_NAME and marketplace == request.marketplace_name
        for name, marketplace in _codex_plugins(request)
    )


def _plugin_is_ready(request: InstallRequest) -> bool:
    document = _json_command(
        [str(request.codex), "plugin", "list", "--json"],
        code="codex_plugin_unsupported",
        message="Codex plugin discovery is unavailable",
    )
    if not isinstance(document, dict):
        _error("codex_state_invalid", "Codex returned an unsupported plugin document")
    typed_document = cast(dict[str, object], document)
    installed = typed_document.get("installed")
    if not isinstance(installed, list):
        _error("codex_state_invalid", "Codex returned an unsupported plugin document")
    for raw in cast(list[object], installed):
        name, marketplace = _plugin_identity(raw)
        if name != PLUGIN_NAME or marketplace != request.marketplace_name:
            continue
        if not isinstance(raw, dict):
            return False
        typed_raw = cast(dict[str, object], raw)
        return typed_raw.get("installed") is True and typed_raw.get("enabled") is True
    return False


def _codex_mcp_bindings_match(request: InstallRequest) -> bool:
    entries = _codex_mcp_entries(request)
    if not set(SERVER_NAMES).issubset(entries):
        return False
    launcher = request.install_root / "deployment" / "run-installed-mcp.py"
    for server_name in SERVER_NAMES:
        entry = entries[server_name]
        transport = entry.get("transport")
        if not isinstance(transport, dict):
            return False
        typed_transport = cast(dict[str, object], transport)
        runtime = RUNTIME_COMMANDS[server_name][0]
        expected_command = str(_runtime_python(request.install_root, runtime))
        if (
            entry.get("enabled") is not True
            or typed_transport.get("type") != "stdio"
            or typed_transport.get("command") != expected_command
            or typed_transport.get("args") != ["-I", str(launcher), server_name]
            or typed_transport.get("env") is not None
            or typed_transport.get("env_vars") != []
        ):
            return False
    return True


def _register_plugin(
    request: InstallRequest, marketplace_root: Path, journal: RegistrationJournal
) -> None:
    journal.marketplace_attempted = True
    marketplace_result = _run_command(
        [
            str(request.codex),
            "plugin",
            "marketplace",
            "add",
            str(marketplace_root),
            "--json",
        ]
    )
    if marketplace_result.returncode != 0:
        _error("marketplace_registration_failed", "the local Codex marketplace could not be added")
    journal.marketplace_added = True
    journal.plugin_attempted = True
    selector = f"{PLUGIN_NAME}@{request.marketplace_name}"
    plugin_result = _run_command([str(request.codex), "plugin", "add", selector, "--json"])
    if plugin_result.returncode != 0:
        _error("plugin_registration_failed", "the generated Codex plugin could not be installed")
    journal.plugin_added = True

    if not _plugin_is_ready(request):
        _error("plugin_verification_failed", "Codex did not report an enabled suite plugin")
    if not _codex_mcp_bindings_match(request):
        _error("plugin_verification_failed", "Codex did not expose the exact suite MCP bindings")


def _rollback_codex(request: InstallRequest, journal: RegistrationJournal) -> bool:
    selector = f"{PLUGIN_NAME}@{request.marketplace_name}"
    try:
        marketplaces = _codex_marketplaces(request)
        registered_root = marketplaces.get(request.marketplace_name)
        marketplace_present = request.marketplace_name in marketplaces
        expected_root = (
            (request.install_root / "marketplace").resolve(strict=True)
            if marketplace_present
            else None
        )
        marketplace_owned = (
            expected_root is not None
            and isinstance(registered_root, str)
            and Path(registered_root).resolve(strict=True) == expected_root
        )
        plugin_present = _plugin_is_installed(request) if journal.plugin_attempted else False
    except (InstallError, OSError):
        return False

    if journal.plugin_added and plugin_present:
        if not marketplace_owned:
            return False
        try:
            result = _run_command([str(request.codex), "plugin", "remove", selector, "--json"])
            if result.returncode != 0 or _plugin_is_installed(request):
                return False
        except InstallError:
            return False
    elif journal.plugin_attempted and plugin_present:
        # A failed or interrupted add does not prove ownership of a concurrently visible plugin.
        return False

    if journal.marketplace_added:
        if not marketplace_present:
            return True
        if not marketplace_owned:
            return False
        try:
            result = _run_command(
                [
                    str(request.codex),
                    "plugin",
                    "marketplace",
                    "remove",
                    request.marketplace_name,
                    "--json",
                ]
            )
            return result.returncode == 0 and request.marketplace_name not in _codex_marketplaces(
                request
            )
        except InstallError:
            return False
    return not marketplace_present


def _remove_install_root(install_root: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = install_root.lstat()
        current_identity = (metadata.st_dev, metadata.st_ino)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or current_identity != identity
            or install_root == PROJECT_ROOT
            or install_root == Path(install_root.anchor)
        ):
            return False
        shutil.rmtree(install_root)
    except OSError:
        return False
    if install_root.exists() or install_root.is_symlink():
        return False
    try:
        _fsync_directory(install_root.parent)
    except InstallError:
        return False
    return True


def _record_rollback_failure(
    install_root: Path,
    marketplace_name: str,
    original_error: BaseException | None,
    journal: RegistrationJournal,
) -> None:
    marker = install_root / ".rollback-required"
    if marker.exists() or marker.is_symlink():
        return
    try:
        if isinstance(original_error, InstallError):
            initial_failure_code = original_error.code
        elif isinstance(original_error, KeyboardInterrupt):
            initial_failure_code = "installation_interrupted"
        else:
            initial_failure_code = "installation_failed"
        if journal.plugin_added:
            registration_stage = "plugin_added"
        elif journal.plugin_attempted:
            registration_stage = "plugin_add_attempted"
        elif journal.marketplace_added:
            registration_stage = "marketplace_added"
        elif journal.marketplace_attempted:
            registration_stage = "marketplace_add_attempted"
        else:
            registration_stage = "before_codex_registration"
        _write_json(
            marker,
            {
                "schema_version": 1,
                "status": "manual_recovery_required",
                "managed_marketplace": marketplace_name,
                "managed_plugin": PLUGIN_NAME,
                "initial_failure_code": initial_failure_code,
                "registration_stage": registration_stage,
            },
            mode=0o600,
        )
    except InstallError:
        return


def _perform_install(
    request: InstallRequest, config: DeploymentConfig, snapshot: SourceSnapshot
) -> None:
    if not request.allow_deepkoala_install:
        _error(
            "deepkoala_install_confirmation_required",
            "first-time DeepKOALA installation requires explicit user confirmation",
        )
    identity = _create_install_root(request.install_root)
    journal = RegistrationJournal()
    original_error: BaseException | None = None
    try:
        _fsync_directory(request.install_root.parent)
        _write_json(
            request.install_root / ".incomplete",
            {
                "schema_version": 1,
                "managed_marketplace": request.marketplace_name,
                "managed_plugin": PLUGIN_NAME,
                "status": "installation_in_progress",
            },
            mode=0o600,
        )
        _fsync_directory(request.install_root)
        _install_runtimes(request, snapshot)
        _install_managed_deepkoala(request, config.deepkoala)
        environments = _deployment_environments(config, request.install_root)
        _verify_distribution_versions(request, snapshot)
        _verify_runtime_configuration(request, environments)
        launcher = _materialize_deployment(request, snapshot, environments)
        marketplace_root = _materialize_plugin(request, snapshot, launcher, config)
        _register_plugin(request, marketplace_root, journal)
        _write_json(
            request.install_root / "installation.json",
            {
                "schema_version": 1,
                "status": "complete",
                "distribution_versions": snapshot.versions,
                "marketplace": request.marketplace_name,
                "plugin": PLUGIN_NAME,
                "servers": list(SERVER_NAMES),
                "skills": list(SKILL_NAMES),
                "deepkoala_repository": DEEPKOALA_REPOSITORY,
                "deepkoala_default_model_date": DEFAULT_DEEPKOALA_MODEL_DATE,
            },
            mode=0o600,
        )
        _fsync_directory(request.install_root)
        (request.install_root / ".incomplete").unlink()
        _fsync_directory(request.install_root)
        return
    except BaseException as error:
        original_error = error

    codex_rollback_ok = _rollback_codex(request, journal)
    filesystem_rollback_ok = False
    if codex_rollback_ok:
        filesystem_rollback_ok = _remove_install_root(request.install_root, identity)
    if not codex_rollback_ok or not filesystem_rollback_ok:
        _record_rollback_failure(
            request.install_root,
            request.marketplace_name,
            original_error,
            journal,
        )
        _error(
            "installation_rollback_failed",
            "installation failed and automatic rollback was incomplete; preserve the install root",
        )
    if isinstance(original_error, InstallError):
        raise original_error
    if isinstance(original_error, KeyboardInterrupt):
        _error("installation_interrupted", "installation was interrupted and rolled back")
    _error("installation_failed", "installation failed unexpectedly and was rolled back")


def _safe_summary(snapshot: SourceSnapshot, *, dry_run: bool) -> dict[str, object]:
    return {
        "status": "validated" if dry_run else "installed",
        "distribution_versions": snapshot.versions,
        "plugin": PLUGIN_NAME,
        "server_count": len(SERVER_NAMES),
        "skill_count": len(SKILL_NAMES),
        "deepkoala_default_model_date": DEFAULT_DEEPKOALA_MODEL_DATE,
        "new_task_required": not dry_run,
        "current_task_reload_supported": False,
        "repeat_installation_required": False,
        "next_action": "run_confirmed_install" if dry_run else "open_new_codex_task",
    }


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _parser().parse_args(argv)
        config_path = cast(Path, arguments.config)
        config = _load_deployment_config(config_path)
        request = _request_from_arguments(arguments, config)
        _preflight_codex(request)
        snapshot = _prepare_source_snapshot()
        if not request.dry_run:
            _perform_install(request, config, snapshot)
        print(json.dumps(_safe_summary(snapshot, dry_run=request.dry_run), sort_keys=True))
        return 0
    except InstallError as error:
        print(f"ERROR [{error.code}] {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ERROR [installation_interrupted] installation was interrupted", file=sys.stderr)
        return 130
    except Exception:
        print("ERROR [installation_failed] installation failed unexpectedly", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
