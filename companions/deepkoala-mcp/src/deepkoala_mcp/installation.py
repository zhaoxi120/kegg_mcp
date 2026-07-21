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

from deepkoala_mcp.contracts import ErrorCode, InstalledResource, fail
from deepkoala_mcp.runner import build_runtime_environment

_DATE = re.compile(r"^[0-9]{4}(?:0[1-9]|1[0-2])$")
_MAX_RESOURCE_DIRECTORIES = 128
_MAX_PYPROJECT_BYTES = 256 * 1024
_PROBE_TIMEOUT_SECONDS = 20
_CUDA_AVAILABLE_EXIT_CODE = 42
_RUNTIME_PROBE = f"""\
import importlib
import sys

importlib.import_module("deepkoala")
importlib.import_module("deepkoala.utils")
torch = importlib.import_module("torch")
raise SystemExit({_CUDA_AVAILABLE_EXIT_CODE} if torch.cuda.is_available() else 0)
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
        return RuntimeProbeResult(runtime_ready=False, cuda_available=False)
    if completed.returncode == _CUDA_AVAILABLE_EXIT_CODE:
        return RuntimeProbeResult(runtime_ready=True, cuda_available=True)
    return RuntimeProbeResult(
        runtime_ready=completed.returncode == 0,
        cuda_available=False,
    )


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
        return RuntimeProbeResult(runtime_ready=False, cuda_available=False)
    try:
        async with asyncio.timeout(_PROBE_TIMEOUT_SECONDS):
            return_code = await process.wait()
    except TimeoutError:
        process.kill()
        await process.wait()
        return RuntimeProbeResult(runtime_ready=False, cuda_available=False)
    if return_code == _CUDA_AVAILABLE_EXIT_CODE:
        return RuntimeProbeResult(runtime_ready=True, cuda_available=True)
    return RuntimeProbeResult(runtime_ready=return_code == 0, cuda_available=False)


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


__all__ = [
    "Installation",
    "InstallationError",
    "RuntimeProbeResult",
    "fail_installation",
    "inspect_installation",
    "probe_runtime",
    "probe_runtime_async",
    "select_installation",
]
