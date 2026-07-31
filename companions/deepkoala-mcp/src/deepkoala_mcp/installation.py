"""Bounded inspection and runtime probing of a configured DeepKOALA installation."""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

from deepkoala_mcp.contracts import CompanionRouteState, ErrorCode, InstalledResource, fail
from deepkoala_mcp.runner import build_runtime_environment

_DATE = re.compile(r"^[0-9]{4}(?:0[1-9]|1[0-2])$")
_MAX_RESOURCE_DIRECTORIES = 128
_MAX_PYPROJECT_BYTES = 256 * 1024
_PROBE_TIMEOUT_SECONDS = 20
_CUDA_AVAILABLE_EXIT_CODE = 42
_MULTI_COMPATIBLE_EXIT_CODE = 43
_CUDA_AND_MULTI_EXIT_CODE = 44
_MPS_AVAILABLE_EXIT_CODE = 45
_MPS_AND_MULTI_EXIT_CODE = 46
_CUDA_AND_MPS_EXIT_CODE = 47
_CUDA_MPS_AND_MULTI_EXIT_CODE = 48
_HMMSEARCH_PROBE_TIMEOUT_SECONDS = 5
_PROFILE = re.compile(r"^K[0-9]{5}\.hmm$")
_MAX_PROFILE_ENTRIES = 100_000
_RUNTIME_PROBE = f"""\
import argparse
import importlib
import inspect
import platform
import sys

macos_parts = platform.mac_ver()[0].split(".")
macos_major = (
    int(macos_parts[0])
    if 1 <= len(macos_parts) <= 3
    and all(part.isdigit() and part for part in macos_parts)
    else 0
)
runtime_supported = (
    platform.python_implementation() == "CPython"
    and sys.version_info[:2] == (3, 11)
    and (
        sys.platform.startswith("linux")
        or (
            sys.platform == "darwin"
            and platform.machine().strip().lower() == "arm64"
            and macos_major >= 14
        )
    )
)
if not runtime_supported:
    raise SystemExit(1)

importlib.import_module("deepkoala")
utils = importlib.import_module("deepkoala.utils")
cli = importlib.import_module("deepkoala.cli")
torch = importlib.import_module("torch")
try:
    infer_multi = importlib.import_module("deepkoala.infer_multi")
    candidate = getattr(infer_multi, "_run_hmmsearch")
    parameters = tuple(inspect.signature(candidate).parameters.values())
    multi_compatible = (
        tuple(parameter.name for parameter in parameters) == ("hmm_file", "seq")
        and all(
            parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters
        )
    )
except Exception:
    multi_compatible = False
cuda_available = torch.cuda.is_available()
try:
    mps_available = torch.backends.mps.is_available()
except (AttributeError, RuntimeError):
    mps_available = False

resolver = getattr(utils, "resolve_device", None)
resolver_compatible = callable(resolver)
if resolver_compatible:
    try:
        resolver_compatible = getattr(resolver("cpu"), "type", None) == "cpu"
        if cuda_available:
            resolver_compatible = (
                resolver_compatible and getattr(resolver("cuda"), "type", None) == "cuda"
            )
        if mps_available:
            resolver_compatible = (
                resolver_compatible and getattr(resolver("mps"), "type", None) == "mps"
            )
    except Exception:
        resolver_compatible = False

cli_compatible = False
multi_cli_compatible = False
original_parse_args = argparse.ArgumentParser.parse_args

class ParserInspected(Exception):
    pass

def inspect_parser(parser, *_args, **_kwargs):
    global cli_compatible, multi_cli_compatible
    option_actions = {{}}
    for action in parser._actions:
        for option in action.option_strings:
            option_actions[option] = action
    required_options = (
        "--input_path",
        "--output_path",
        "--model",
        "--date",
        "--device",
        "--detail",
        "--batch_size",
        "--num_workers",
        "--topk",
    )
    device_action = option_actions.get("--device")
    choices = getattr(device_action, "choices", None)
    cli_compatible = (
        all(option in option_actions for option in required_options)
        and choices is not None
        and set(choices) == set(("auto", "cpu", "cuda", "mps"))
    )
    multi_cli_compatible = (
        "--multi" in option_actions and "--profiles_dir" in option_actions
    )
    raise ParserInspected

argparse.ArgumentParser.parse_args = inspect_parser
try:
    cli.main()
except ParserInspected:
    pass
except BaseException:
    cli_compatible = False
finally:
    argparse.ArgumentParser.parse_args = original_parse_args

multi_compatible = multi_compatible and multi_cli_compatible
if not resolver_compatible or not cli_compatible:
    raise SystemExit(1)
if cuda_available and mps_available and multi_compatible:
    raise SystemExit({_CUDA_MPS_AND_MULTI_EXIT_CODE})
if cuda_available and mps_available:
    raise SystemExit({_CUDA_AND_MPS_EXIT_CODE})
if cuda_available and multi_compatible:
    raise SystemExit({_CUDA_AND_MULTI_EXIT_CODE})
if mps_available and multi_compatible:
    raise SystemExit({_MPS_AND_MULTI_EXIT_CODE})
if cuda_available:
    raise SystemExit({_CUDA_AVAILABLE_EXIT_CODE})
if mps_available:
    raise SystemExit({_MPS_AVAILABLE_EXIT_CODE})
raise SystemExit({_MULTI_COMPATIBLE_EXIT_CODE} if multi_compatible else 0)
"""


@dataclass(frozen=True, slots=True)
class Installation:
    """One selected source version and local resource pair."""

    source_version: str
    resource: InstalledResource


@dataclass(frozen=True, slots=True)
class RuntimeProbeResult:
    """Redacted interpreter readiness facts."""

    runtime_ready: bool
    cuda_available: bool
    mps_available: bool = False
    multi_adapter_compatible: bool = False


@dataclass(frozen=True, slots=True)
class ReadinessRoute:
    """Stable status routing without private paths or raw exception text."""

    route_state: CompanionRouteState
    issue: str | None
    next_action: str


class InstallationError(RuntimeError):
    """A structural checkout or resource inspection failure."""

    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def select_installation(checkout: Path, model: str, requested_date: str) -> Installation:
    """Select the newest or exact readable installed resource pair."""
    version, resources = inspect_installation(checkout)
    matches = [
        resource
        for resource in resources
        if resource.model == model
        and (requested_date == "latest" or resource.model_date == requested_date)
    ]
    if not matches:
        raise InstallationError(ErrorCode.WEIGHTS_NOT_FOUND)
    selected = max(matches, key=lambda item: item.model_date)
    return Installation(source_version=version, resource=selected)


def inspect_installation(checkout: Path) -> tuple[str, tuple[InstalledResource, ...]]:
    """Inspect only bounded local source and resource metadata."""
    package = checkout / "deepkoala"
    for path in (package, checkout / "resources"):
        if not _direct_directory(path):
            raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
    if not _direct_regular(package / "__init__.py", nonempty=False):
        raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
    for path in (package / "cli.py", package / "utils.py"):
        if not _direct_regular(path, nonempty=True):
            raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
    version = _read_source_version(checkout / "pyproject.toml")
    resources: list[InstalledResource] = []
    try:
        candidates: list[Path] = []
        with os.scandir(checkout / "resources") as entries:
            for entry in entries:
                candidates.append(Path(entry.path))
                if len(candidates) > _MAX_RESOURCE_DIRECTORIES:
                    raise InstallationError(ErrorCode.WEIGHTS_NOT_FOUND)
        for candidate in sorted(candidates, key=lambda item: item.name):
            if _DATE.fullmatch(candidate.name) is None or not _direct_directory(candidate):
                continue
            for model in ("full", "frag"):
                if _direct_regular(
                    candidate / f"weights_{model}.pt", nonempty=True
                ) and _direct_regular(
                    candidate / f"ko_config_{model}.json",
                    nonempty=True,
                ):
                    resources.append(
                        InstalledResource(
                            model=model,
                            model_date=candidate.name,
                        )
                    )
    except OSError as error:
        raise InstallationError(ErrorCode.WEIGHTS_NOT_FOUND) from error
    return version, tuple(resources)


def probe_runtime(
    *,
    checkout: Path,
    python_executable: Path,
    cpu_threads: int,
) -> RuntimeProbeResult:
    """Import the configured local runtime with fixed argv, no output, and no downloads."""
    try:
        completed = subprocess.run(
            (str(python_executable), "-c", _RUNTIME_PROBE),
            cwd=checkout,
            env=build_runtime_environment(checkout, cpu_threads),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return RuntimeProbeResult(
            runtime_ready=False,
            cuda_available=False,
            mps_available=False,
        )
    return _runtime_probe_result(completed.returncode)


async def probe_runtime_async(
    *,
    checkout: Path,
    python_executable: Path,
    cpu_threads: int,
) -> RuntimeProbeResult:
    """Run the same fixed probe without blocking or retaining an executor thread."""
    try:
        process = await asyncio.create_subprocess_exec(
            str(python_executable),
            "-c",
            _RUNTIME_PROBE,
            cwd=checkout,
            env=build_runtime_environment(checkout, cpu_threads),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return RuntimeProbeResult(
            runtime_ready=False,
            cuda_available=False,
            mps_available=False,
        )
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            return_code = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        return RuntimeProbeResult(
            runtime_ready=False,
            cuda_available=False,
            mps_available=False,
        )
    return _runtime_probe_result(return_code)


def probe_multi_dependencies(
    *,
    allow_multi: bool,
    profiles_dir: Path | None,
    hmmsearch_executable: Path | None,
    runtime: RuntimeProbeResult,
) -> bool:
    """Probe optional local multi-domain dependencies with fixed argv and no downloads."""
    if not allow_multi or not runtime.multi_adapter_compatible:
        return False
    profiles_ready, hmmsearch_structural = _multi_dependency_structure(
        profiles_dir=profiles_dir,
        hmmsearch_executable=hmmsearch_executable,
    )
    hmmsearch_ready = bool(
        hmmsearch_structural
        and hmmsearch_executable is not None
        and _probe_hmmsearch(hmmsearch_executable)
    )
    return profiles_ready and hmmsearch_ready


async def probe_multi_dependencies_async(
    *,
    allow_multi: bool,
    profiles_dir: Path | None,
    hmmsearch_executable: Path | None,
    runtime: RuntimeProbeResult,
) -> bool:
    """Probe optional dependencies without blocking the MCP event loop."""
    if not allow_multi or not runtime.multi_adapter_compatible:
        return False
    return await asyncio.to_thread(
        probe_multi_dependencies,
        allow_multi=allow_multi,
        profiles_dir=profiles_dir,
        hmmsearch_executable=hmmsearch_executable,
        runtime=runtime,
    )


def classify_readiness_route(
    *,
    checkout_ready: bool,
    runtime_ready: bool,
    model_resources_ready: bool,
    allow_multi: bool,
    multi_ready: bool,
) -> ReadinessRoute:
    """Classify one stable repair route from redacted readiness booleans."""
    if not checkout_ready:
        return ReadinessRoute(
            route_state=CompanionRouteState.DEEPKOALA_CHECKOUT_UNAVAILABLE,
            issue="The configured checkout is not a readable official DeepKOALA layout.",
            next_action="Repair the configured checkout outside this serving companion.",
        )
    if not runtime_ready:
        return ReadinessRoute(
            route_state=CompanionRouteState.DEEPKOALA_RUNTIME_UNAVAILABLE,
            issue="The configured interpreter cannot import DeepKOALA and its runtime.",
            next_action="Repair the configured Python environment and rerun doctor.",
        )
    if not model_resources_ready:
        return ReadinessRoute(
            route_state=CompanionRouteState.MODEL_RESOURCES_UNAVAILABLE,
            issue="No readable model resource pair matches the deployment allowlist.",
            next_action=(
                "Install local resources outside this serving companion or adjust the allowlist."
            ),
        )
    if allow_multi and not multi_ready:
        return ReadinessRoute(
            route_state=CompanionRouteState.MULTI_DEPENDENCIES_UNAVAILABLE,
            issue="Configured multi-domain dependencies are unavailable or incompatible.",
            next_action=(
                "Repair the configured HMMER executable, profile directory, or DeepKOALA "
                "multi-domain interface."
            ),
        )
    return ReadinessRoute(
        route_state=CompanionRouteState.LOCAL_READY,
        issue=None,
        next_action="Call get_deepkoala_runner_status, then run_deepkoala_job.",
    )


def fail_installation(error: InstallationError) -> NoReturn:
    """Map one structural error to a bounded public companion error."""
    if error.code is ErrorCode.WEIGHTS_NOT_FOUND:
        fail(
            ErrorCode.WEIGHTS_NOT_FOUND,
            "The requested local DeepKOALA model resources are unavailable.",
            suggested_action="Install or select an existing local model/date pair.",
        )
    fail(
        ErrorCode.DEEPKOALA_UNAVAILABLE,
        "The configured directory is not an available official DeepKOALA checkout.",
        suggested_action="Check the configured checkout and Python environment.",
    )


def fail_multi_unavailable() -> NoReturn:
    """Return the fixed public repair route for unavailable optional dependencies."""
    fail(
        ErrorCode.RUNTIME_UNAVAILABLE,
        "Configured multi-domain dependencies are unavailable or incompatible.",
        suggested_action=(
            "Run doctor and repair the configured HMMER executable, profile directory, or "
            "DeepKOALA interface."
        ),
    )


def fail_runtime_unavailable(
    *,
    cuda_requested: bool = False,
    mps_requested: bool = False,
) -> NoReturn:
    """Return a bounded repair route for the configured DeepKOALA runtime."""
    if cuda_requested and mps_requested:
        raise ValueError("only one accelerator may be requested")
    if cuda_requested:
        message = "The requested CUDA device is unavailable in the configured runtime."
        action = "Use the default CPU device or explicitly repair and verify the CUDA runtime."
    elif mps_requested:
        message = "The requested MPS device is unavailable in the configured runtime."
        action = "Use the default CPU device or explicitly repair and verify the MPS runtime."
    else:
        message = "The configured Python cannot import the required DeepKOALA runtime."
        action = "Run the redacted doctor command and repair the configured environment."
    fail(ErrorCode.RUNTIME_UNAVAILABLE, message, suggested_action=action)


def _read_source_version(path: Path) -> str:
    if not _direct_regular(path, nonempty=True):
        raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
    try:
        metadata = path.stat()
        if metadata.st_size > _MAX_PYPROJECT_BYTES:
            raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
        document = cast(dict[str, object], tomllib.loads(path.read_text(encoding="utf-8")))
        project_value = document.get("project")
        if not isinstance(project_value, dict):
            raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
        project = cast(dict[str, object], project_value)
        if project.get("name") != "deepkoala":
            raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
        version = project.get("version")
        if not isinstance(version, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", version
        ):
            raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE)
        return version
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise InstallationError(ErrorCode.DEEPKOALA_UNAVAILABLE) from error


def _direct_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and os.access(path, os.R_OK | os.X_OK)
    )


def _direct_regular(path: Path, *, nonempty: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and os.access(path, os.R_OK)
        and (not nonempty or metadata.st_size > 0)
    )


def _multi_dependency_structure(
    *,
    profiles_dir: Path | None,
    hmmsearch_executable: Path | None,
) -> tuple[bool, bool]:
    if profiles_dir is None or hmmsearch_executable is None:
        return False, False
    return _profiles_ready(profiles_dir), _trusted_executable(hmmsearch_executable)


def _profiles_ready(path: Path) -> bool:
    try:
        named = path.lstat()
        if stat.S_ISLNK(named.st_mode):
            return False
        resolved = path.resolve(strict=True)
        if resolved != path:
            return False
        metadata = resolved.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not os.access(resolved, os.R_OK | os.X_OK)
        ):
            return False
        count = 0
        with os.scandir(resolved) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_PROFILE_ENTRIES:
                    return False
                if _PROFILE.fullmatch(entry.name) is None:
                    continue
                candidate = entry.stat(follow_symlinks=False)
                if (
                    stat.S_ISREG(candidate.st_mode)
                    and candidate.st_uid in {0, os.geteuid()}
                    and not stat.S_IMODE(candidate.st_mode) & 0o022
                    and candidate.st_size > 0
                    and os.access(entry.path, os.R_OK)
                ):
                    return True
    except OSError:
        return False
    return False


def _trusted_executable(path: Path) -> bool:
    try:
        named = path.lstat()
        if stat.S_ISLNK(named.st_mode):
            return False
        resolved = path.resolve(strict=True)
        if resolved != path:
            return False
        metadata = resolved.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid in {0, os.geteuid()}
        and not stat.S_IMODE(metadata.st_mode) & 0o022
        and os.access(resolved, os.R_OK | os.X_OK)
    )


def _probe_hmmsearch(executable: Path) -> bool:
    try:
        completed = subprocess.run(
            (str(executable.resolve(strict=True)), "-h"),
            env=_hmmsearch_probe_environment(executable),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_HMMSEARCH_PROBE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _hmmsearch_probe_environment(executable: Path) -> dict[str, str]:
    return {
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": str(executable.resolve(strict=True).parent),
    }


def _runtime_probe_result(return_code: int) -> RuntimeProbeResult:
    ready_exit_codes = {
        0,
        _CUDA_AVAILABLE_EXIT_CODE,
        _MULTI_COMPATIBLE_EXIT_CODE,
        _CUDA_AND_MULTI_EXIT_CODE,
        _MPS_AVAILABLE_EXIT_CODE,
        _MPS_AND_MULTI_EXIT_CODE,
        _CUDA_AND_MPS_EXIT_CODE,
        _CUDA_MPS_AND_MULTI_EXIT_CODE,
    }
    return RuntimeProbeResult(
        runtime_ready=return_code in ready_exit_codes,
        cuda_available=return_code
        in {
            _CUDA_AVAILABLE_EXIT_CODE,
            _CUDA_AND_MULTI_EXIT_CODE,
            _CUDA_AND_MPS_EXIT_CODE,
            _CUDA_MPS_AND_MULTI_EXIT_CODE,
        },
        mps_available=return_code
        in {
            _MPS_AVAILABLE_EXIT_CODE,
            _MPS_AND_MULTI_EXIT_CODE,
            _CUDA_AND_MPS_EXIT_CODE,
            _CUDA_MPS_AND_MULTI_EXIT_CODE,
        },
        multi_adapter_compatible=return_code
        in {
            _MULTI_COMPATIBLE_EXIT_CODE,
            _CUDA_AND_MULTI_EXIT_CODE,
            _MPS_AND_MULTI_EXIT_CODE,
            _CUDA_MPS_AND_MULTI_EXIT_CODE,
        },
    )


__all__ = [
    "Installation",
    "InstallationError",
    "ReadinessRoute",
    "RuntimeProbeResult",
    "classify_readiness_route",
    "fail_installation",
    "fail_multi_unavailable",
    "fail_runtime_unavailable",
    "inspect_installation",
    "probe_multi_dependencies",
    "probe_multi_dependencies_async",
    "probe_runtime",
    "probe_runtime_async",
    "select_installation",
]
