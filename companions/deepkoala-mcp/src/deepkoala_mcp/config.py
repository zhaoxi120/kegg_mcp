"""Small environment-only configuration for the local companion."""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deepkoala_mcp.contracts import MAX_QUEUE_SIZE

ENV_PREFIX = "DEEPKOALA_MCP_"
CHECKOUT_ENV = f"{ENV_PREFIX}CHECKOUT"
PYTHON_ENV = f"{ENV_PREFIX}PYTHON"
STATE_ROOT_ENV = f"{ENV_PREFIX}STATE_ROOT"
ALLOWED_ROOTS_ENV = f"{ENV_PREFIX}ALLOWED_ROOTS"
CPU_THREADS_ENV = f"{ENV_PREFIX}CPU_THREADS"
MAX_QUEUE_SIZE_ENV = f"{ENV_PREFIX}MAX_QUEUE_SIZE"
DEFAULT_TIMEOUT_SECONDS_ENV = f"{ENV_PREFIX}DEFAULT_TIMEOUT_SECONDS"
PLAN_TTL_SECONDS_ENV = f"{ENV_PREFIX}PLAN_TTL_SECONDS"
RETENTION_SECONDS_ENV = f"{ENV_PREFIX}RETENTION_SECONDS"

DEFAULT_CPU_THREADS = 2
DEFAULT_MAX_QUEUE_SIZE = 4
DEFAULT_TIMEOUT_SECONDS = 3_600
DEFAULT_PLAN_TTL_SECONDS = 600
DEFAULT_RETENTION_SECONDS = 86_400


class DeepKoalaRuntimeConfig(BaseModel):
    """Private deployment settings that status output must not expose."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )

    checkout: Path
    python_executable: Path
    state_root: Path
    allowed_roots: tuple[Path, ...] = ()
    cpu_threads: int = Field(default=DEFAULT_CPU_THREADS, strict=True, ge=1, le=4)
    max_queue_size: int = Field(
        default=DEFAULT_MAX_QUEUE_SIZE,
        strict=True,
        ge=1,
        le=MAX_QUEUE_SIZE,
    )
    default_timeout_seconds: int = Field(
        default=DEFAULT_TIMEOUT_SECONDS,
        strict=True,
        ge=1,
        le=86_400,
    )
    plan_ttl_seconds: int = Field(
        default=DEFAULT_PLAN_TTL_SECONDS,
        strict=True,
        ge=1,
        le=86_400,
    )
    retention_seconds: int = Field(
        default=DEFAULT_RETENTION_SECONDS,
        strict=True,
        ge=1,
        le=2_592_000,
    )

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        _require_posix_runtime()
        for name, path in (
            ("checkout", self.checkout),
            ("python_executable", self.python_executable),
            ("state_root", self.state_root),
        ):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{name} must be an absolute traversal-free path")
        if not self.checkout.is_dir():
            raise ValueError("checkout must be an existing directory")
        if not self.python_executable.is_file() or not os.access(self.python_executable, os.X_OK):
            raise ValueError("python_executable must be an executable file")
        if self.state_root == Path(self.state_root.anchor):
            raise ValueError("state_root must not be a filesystem root")
        if _overlap(self.state_root, self.checkout):
            raise ValueError("state_root must not overlap checkout")
        if len(self.allowed_roots) != len(set(self.allowed_roots)):
            raise ValueError("allowed_roots must be unique")
        for root in self.allowed_roots:
            if (
                not root.is_absolute()
                or ".." in root.parts
                or not root.is_dir()
                or root == Path(root.anchor)
            ):
                raise ValueError(
                    "allowed_roots must contain traversal-free non-root absolute directories"
                )
            if _overlap(root, self.state_root):
                raise ValueError("allowed_roots must not overlap state_root")
        return self


def load_runtime_config(
    environment: Mapping[str, str] | None = None,
) -> DeepKoalaRuntimeConfig:
    """Load the documented minimal ``DEEPKOALA_MCP_*`` environment contract."""
    values = os.environ if environment is None else environment
    checkout = _required_existing(values, CHECKOUT_ENV, directory=True)
    python = _required_existing(values, PYTHON_ENV, directory=False)
    raw_state = _required(values, STATE_ROOT_ENV)
    state = _absolute(raw_state, STATE_ROOT_ENV)
    roots = _allowed_roots(values.get(ALLOWED_ROOTS_ENV))
    return DeepKoalaRuntimeConfig(
        checkout=checkout,
        python_executable=python,
        state_root=state,
        allowed_roots=roots,
        cpu_threads=_integer(values, CPU_THREADS_ENV, DEFAULT_CPU_THREADS, 1, 4),
        max_queue_size=_integer(
            values,
            MAX_QUEUE_SIZE_ENV,
            DEFAULT_MAX_QUEUE_SIZE,
            1,
            MAX_QUEUE_SIZE,
        ),
        default_timeout_seconds=_integer(
            values,
            DEFAULT_TIMEOUT_SECONDS_ENV,
            DEFAULT_TIMEOUT_SECONDS,
            1,
            86_400,
        ),
        plan_ttl_seconds=_integer(
            values,
            PLAN_TTL_SECONDS_ENV,
            DEFAULT_PLAN_TTL_SECONDS,
            1,
            86_400,
        ),
        retention_seconds=_integer(
            values,
            RETENTION_SECONDS_ENV,
            DEFAULT_RETENTION_SECONDS,
            1,
            2_592_000,
        ),
    )


def _require_posix_runtime() -> None:
    try:
        import resource
    except ImportError:
        resource = None  # type: ignore[assignment]
    if (
        sys.platform != "linux"
        or os.name != "posix"
        or not hasattr(os, "killpg")
        or not hasattr(os, "setsid")
        or not hasattr(os, "geteuid")
        or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in os.supports_dir_fd
        or resource is None
        or not hasattr(resource, "RLIMIT_FSIZE")
        or not hasattr(resource, "setrlimit")
    ):
        raise ValueError(
            "deepkoala-mcp requires Linux process groups, parent-death signals, and RLIMIT_FSIZE"
        )


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name)
    if value is None or not value:
        raise ValueError(f"{name} is required")
    return value


def _required_existing(
    values: Mapping[str, str],
    name: str,
    *,
    directory: bool,
) -> Path:
    path = _absolute(_required(values, name), name)
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{name} is unavailable") from error
    if (directory and not resolved.is_dir()) or (not directory and not resolved.is_file()):
        raise ValueError(f"{name} has the wrong filesystem type")
    if not directory and not os.access(resolved, os.X_OK):
        raise ValueError(f"{name} must be executable")
    return resolved


def _absolute(value: str, name: str) -> Path:
    if "\x00" in value:
        raise ValueError(f"{name} contains NUL")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be absolute and traversal-free")
    return path


def _allowed_roots(value: str | None) -> tuple[Path, ...]:
    if not value:
        return ()
    parts = value.split(os.pathsep)
    if any(not part for part in parts):
        raise ValueError(f"{ALLOWED_ROOTS_ENV} contains an empty root")
    roots = tuple(
        _required_existing({ALLOWED_ROOTS_ENV: part}, ALLOWED_ROOTS_ENV, directory=True)
        for part in parts
    )
    if len(roots) != len(set(roots)):
        raise ValueError(f"{ALLOWED_ROOTS_ENV} contains duplicate roots")
    return roots


def _integer(
    values: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = values.get(name)
    if value is None:
        return default
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", value) is None:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return parsed


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


__all__ = [
    "ALLOWED_ROOTS_ENV",
    "CHECKOUT_ENV",
    "CPU_THREADS_ENV",
    "DEFAULT_TIMEOUT_SECONDS_ENV",
    "MAX_QUEUE_SIZE_ENV",
    "PLAN_TTL_SECONDS_ENV",
    "PYTHON_ENV",
    "RETENTION_SECONDS_ENV",
    "STATE_ROOT_ENV",
    "DeepKoalaRuntimeConfig",
    "load_runtime_config",
]
