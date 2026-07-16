"""Bounded inspection and selection of an external DeepKOALA installation."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

from deepkoala_mcp.contracts import ErrorCode, InstalledResource, fail

_DATE = re.compile(r"^[0-9]{4}(?:0[1-9]|1[0-2])$")
_MAX_RESOURCE_DIRECTORIES = 128
_MAX_PYPROJECT_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class Installation:
    source_version: str
    resource: InstalledResource


class InstallationError(RuntimeError):
    def __init__(self, code: ErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


def select_installation(checkout: Path, model: str, requested_date: str) -> Installation:
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


def fail_installation(error: InstallationError) -> NoReturn:
    if error.code is ErrorCode.WEIGHTS_NOT_FOUND:
        fail(
            ErrorCode.WEIGHTS_NOT_FOUND,
            "The requested local DeepKOALA model resources are unavailable.",
            suggested_action="Install or select an existing local model/date pair.",
        )
    fail(
        ErrorCode.DEEPKOALA_UNAVAILABLE,
        "The configured directory is not an available official DeepKOALA checkout.",
        suggested_action="Check the configured checkout and external Python environment.",
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
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _direct_regular(path: Path, *, nonempty: bool) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and (not nonempty or metadata.st_size > 0)
    )


__all__ = [
    "Installation",
    "InstallationError",
    "fail_installation",
    "inspect_installation",
    "select_installation",
]
