"""Environment-only runtime configuration for the DeepKOALA MCP companion."""

from __future__ import annotations

import os
import re
import signal
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deepkoala_mcp.contracts import (
    MAX_DIAGNOSTIC_BYTES,
    MAX_FASTA_BYTES,
    MAX_HEADER_LENGTH,
    MAX_OUTPUT_BYTES,
    MAX_QUEUE_SIZE,
    MAX_SEQUENCE_COUNT,
    MAX_SEQUENCE_LENGTH,
    WeightSource,
)

ENV_PREFIX = "DEEPKOALA_MCP_"
CHECKOUT_ENV = f"{ENV_PREFIX}CHECKOUT"
PYTHON_ENV = f"{ENV_PREFIX}PYTHON"
STATE_ROOT_ENV = f"{ENV_PREFIX}STATE_ROOT"
ALLOWED_ROOTS_ENV = f"{ENV_PREFIX}ALLOWED_ROOTS"
WEIGHT_SOURCE_ENV = f"{ENV_PREFIX}WEIGHT_SOURCE"
MAX_CONCURRENT_JOBS_ENV = f"{ENV_PREFIX}MAX_CONCURRENT_JOBS"
MAX_QUEUE_SIZE_ENV = f"{ENV_PREFIX}MAX_QUEUE_SIZE"
CPU_THREADS_ENV = f"{ENV_PREFIX}CPU_THREADS"
DEFAULT_TIMEOUT_SECONDS_ENV = f"{ENV_PREFIX}DEFAULT_TIMEOUT_SECONDS"
PLAN_TTL_SECONDS_ENV = f"{ENV_PREFIX}PLAN_TTL_SECONDS"
RETENTION_SECONDS_ENV = f"{ENV_PREFIX}RETENTION_SECONDS"
MAX_DIAGNOSTIC_BYTES_ENV = f"{ENV_PREFIX}MAX_DIAGNOSTIC_BYTES"
MAX_INPUT_BYTES_ENV = f"{ENV_PREFIX}MAX_INPUT_BYTES"
MAX_OUTPUT_BYTES_ENV = f"{ENV_PREFIX}MAX_OUTPUT_BYTES"
MAX_SEQUENCES_ENV = f"{ENV_PREFIX}MAX_SEQUENCES"
MAX_RESIDUES_ENV = f"{ENV_PREFIX}MAX_RESIDUES"
MAX_SEQUENCE_LENGTH_ENV = f"{ENV_PREFIX}MAX_SEQUENCE_LENGTH"
MAX_HEADER_LENGTH_ENV = f"{ENV_PREFIX}MAX_HEADER_LENGTH"

DEFAULT_MAX_CONCURRENT_JOBS: Literal[1] = 1
DEFAULT_MAX_QUEUE_SIZE = 32
DEFAULT_CPU_THREADS = 2
DEFAULT_TIMEOUT_SECONDS = 3_600
DEFAULT_PLAN_TTL_SECONDS = 600
DEFAULT_RETENTION_SECONDS = 86_400
_POSIX_RUNTIME_ERROR = (
    "deepkoala-mcp requires a POSIX runtime with process-group and file-size-limit support"
)

PositiveInputBytes = Annotated[int, Field(strict=True, ge=1, le=MAX_FASTA_BYTES)]
PositiveOutputBytes = Annotated[int, Field(strict=True, ge=1, le=MAX_OUTPUT_BYTES)]
PositiveDiagnosticBytes = Annotated[int, Field(strict=True, ge=1, le=MAX_DIAGNOSTIC_BYTES)]


class DeepKoalaRuntimeConfig(BaseModel):
    """Validated private runtime settings that must not be returned by status tools."""

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
    weight_source: WeightSource = "github_bundled"
    max_concurrent_jobs: Literal[1] = DEFAULT_MAX_CONCURRENT_JOBS
    max_queue_size: int = Field(
        default=DEFAULT_MAX_QUEUE_SIZE,
        strict=True,
        ge=1,
        le=MAX_QUEUE_SIZE,
    )
    cpu_threads: int = Field(default=DEFAULT_CPU_THREADS, strict=True, ge=1, le=32)
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
    diagnostic_max_bytes: PositiveDiagnosticBytes = MAX_DIAGNOSTIC_BYTES
    max_input_bytes: PositiveInputBytes = MAX_FASTA_BYTES
    max_output_bytes: PositiveOutputBytes = MAX_OUTPUT_BYTES
    max_sequences: int = Field(default=MAX_SEQUENCE_COUNT, strict=True, ge=1, le=MAX_SEQUENCE_COUNT)
    max_residues: int = Field(default=MAX_FASTA_BYTES, strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_sequence_length: int = Field(
        default=MAX_SEQUENCE_LENGTH,
        strict=True,
        ge=1,
        le=MAX_SEQUENCE_LENGTH,
    )
    max_header_length: int = Field(
        default=MAX_HEADER_LENGTH,
        strict=True,
        ge=1,
        le=MAX_HEADER_LENGTH,
    )

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        _require_supported_runtime()
        for field_name, path in (
            ("checkout", self.checkout),
            ("python_executable", self.python_executable),
            ("state_root", self.state_root),
        ):
            if not path.is_absolute():
                raise ValueError(f"{field_name} must be absolute")
        if not self.checkout.is_dir():
            raise ValueError("checkout must be an existing directory")
        if not self.python_executable.is_file() or not os.access(self.python_executable, os.X_OK):
            raise ValueError("python_executable must be an executable file")
        if self.state_root == self.state_root.parent:
            raise ValueError("state_root must not be a filesystem root")
        if (
            self.state_root == self.checkout
            or self.checkout in self.state_root.parents
            or self.state_root in self.checkout.parents
        ):
            raise ValueError("state_root must be outside the DeepKOALA checkout")
        if len(self.allowed_roots) != len(set(self.allowed_roots)):
            raise ValueError("allowed_roots must be unique")
        for root in self.allowed_roots:
            if not root.is_absolute() or not root.is_dir():
                raise ValueError("allowed_roots must contain existing absolute directories")
            if root == root.parent:
                raise ValueError("the filesystem root cannot be an allowed input root")
            if (
                root == self.state_root
                or root in self.state_root.parents
                or self.state_root in root.parents
            ):
                raise ValueError("allowed_roots must not overlap state_root")
        if self.max_sequence_length > self.max_residues:
            raise ValueError("max_sequence_length must not exceed max_residues")
        return self


def _supports_required_posix_runtime() -> bool:
    """Return whether required process-group and output-file controls are available."""
    try:
        import resource
    except ImportError:
        return False
    return (
        os.name == "posix"
        and hasattr(os, "geteuid")
        and hasattr(os, "killpg")
        and hasattr(os, "setsid")
        and hasattr(resource, "RLIMIT_FSIZE")
        and hasattr(resource, "RLIM_INFINITY")
        and hasattr(resource, "getrlimit")
        and hasattr(resource, "setrlimit")
        and hasattr(signal, "SIGXFSZ")
    )


def _require_supported_runtime() -> None:
    """Fail before path handling or private state creation on unsupported platforms."""
    if not _supports_required_posix_runtime():
        raise ValueError(_POSIX_RUNTIME_ERROR)


def load_runtime_config(
    environment: Mapping[str, str] | None = None,
) -> DeepKoalaRuntimeConfig:
    """Load the documented ``DEEPKOALA_MCP_*`` environment contract."""
    _require_supported_runtime()
    values = os.environ if environment is None else environment
    raw_checkout = values.get(CHECKOUT_ENV)
    if raw_checkout is None or not raw_checkout:
        raise ValueError(f"{CHECKOUT_ENV} is required")

    raw_python = values.get(PYTHON_ENV)
    if raw_python is None or not raw_python:
        raise ValueError(f"{PYTHON_ENV} is required")

    raw_state_root = values.get(STATE_ROOT_ENV)
    if raw_state_root is None or not raw_state_root:
        raise ValueError(f"{STATE_ROOT_ENV} is required")

    checkout = _resolve_existing_directory(raw_checkout, name=CHECKOUT_ENV)
    python_executable = _resolve_python(raw_python)
    state_root = _resolve_state_root(raw_state_root)
    allowed_roots = _resolve_allowed_roots(values.get(ALLOWED_ROOTS_ENV))

    _read_integer(
        values,
        MAX_CONCURRENT_JOBS_ENV,
        default=DEFAULT_MAX_CONCURRENT_JOBS,
        minimum=1,
        maximum=1,
    )
    return DeepKoalaRuntimeConfig(
        checkout=checkout,
        python_executable=python_executable,
        state_root=state_root,
        allowed_roots=allowed_roots,
        weight_source=_read_weight_source(values),
        max_concurrent_jobs=DEFAULT_MAX_CONCURRENT_JOBS,
        max_queue_size=_read_integer(
            values,
            MAX_QUEUE_SIZE_ENV,
            default=DEFAULT_MAX_QUEUE_SIZE,
            minimum=1,
            maximum=MAX_QUEUE_SIZE,
        ),
        cpu_threads=_read_integer(
            values,
            CPU_THREADS_ENV,
            default=DEFAULT_CPU_THREADS,
            minimum=1,
            maximum=32,
        ),
        default_timeout_seconds=_read_integer(
            values,
            DEFAULT_TIMEOUT_SECONDS_ENV,
            default=DEFAULT_TIMEOUT_SECONDS,
            minimum=1,
            maximum=86_400,
        ),
        plan_ttl_seconds=_read_integer(
            values,
            PLAN_TTL_SECONDS_ENV,
            default=DEFAULT_PLAN_TTL_SECONDS,
            minimum=1,
            maximum=86_400,
        ),
        retention_seconds=_read_integer(
            values,
            RETENTION_SECONDS_ENV,
            default=DEFAULT_RETENTION_SECONDS,
            minimum=1,
            maximum=2_592_000,
        ),
        diagnostic_max_bytes=_read_integer(
            values,
            MAX_DIAGNOSTIC_BYTES_ENV,
            default=MAX_DIAGNOSTIC_BYTES,
            minimum=1,
            maximum=MAX_DIAGNOSTIC_BYTES,
        ),
        max_input_bytes=_read_integer(
            values,
            MAX_INPUT_BYTES_ENV,
            default=MAX_FASTA_BYTES,
            minimum=1,
            maximum=MAX_FASTA_BYTES,
        ),
        max_output_bytes=_read_integer(
            values,
            MAX_OUTPUT_BYTES_ENV,
            default=MAX_OUTPUT_BYTES,
            minimum=1,
            maximum=MAX_OUTPUT_BYTES,
        ),
        max_sequences=_read_integer(
            values,
            MAX_SEQUENCES_ENV,
            default=MAX_SEQUENCE_COUNT,
            minimum=1,
            maximum=MAX_SEQUENCE_COUNT,
        ),
        max_residues=_read_integer(
            values,
            MAX_RESIDUES_ENV,
            default=MAX_FASTA_BYTES,
            minimum=1,
            maximum=MAX_FASTA_BYTES,
        ),
        max_sequence_length=_read_integer(
            values,
            MAX_SEQUENCE_LENGTH_ENV,
            default=MAX_SEQUENCE_LENGTH,
            minimum=1,
            maximum=MAX_SEQUENCE_LENGTH,
        ),
        max_header_length=_read_integer(
            values,
            MAX_HEADER_LENGTH_ENV,
            default=MAX_HEADER_LENGTH,
            minimum=1,
            maximum=MAX_HEADER_LENGTH,
        ),
    )


def _resolve_existing_directory(raw_value: str, *, name: str) -> Path:
    if "\x00" in raw_value:
        raise ValueError(f"{name} must not contain a NUL character")
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{name} must be an absolute directory")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{name} must be an existing directory") from error
    if not resolved.is_dir():
        raise ValueError(f"{name} must be an existing directory")
    return resolved


def _resolve_python(raw_value: str) -> Path:
    if not raw_value or "\x00" in raw_value:
        raise ValueError(f"{PYTHON_ENV} must identify an executable")
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{PYTHON_ENV} must be an absolute executable path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError(f"{PYTHON_ENV} executable was not found") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError(f"{PYTHON_ENV} must identify an executable file")
    return resolved


def _resolve_state_root(raw_value: str) -> Path:
    if not raw_value or "\x00" in raw_value:
        raise ValueError(f"{STATE_ROOT_ENV} must be a non-empty absolute directory")
    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        raise ValueError(f"{STATE_ROOT_ENV} must be an absolute directory")
    return candidate.resolve(strict=False)


def _resolve_allowed_roots(raw_value: str | None) -> tuple[Path, ...]:
    if raw_value is None or raw_value == "":
        return ()
    raw_roots = raw_value.split(os.pathsep)
    if any(not root for root in raw_roots):
        raise ValueError(f"{ALLOWED_ROOTS_ENV} contains an empty root")
    roots = tuple(_resolve_existing_directory(root, name=ALLOWED_ROOTS_ENV) for root in raw_roots)
    if len(roots) != len(set(roots)):
        raise ValueError(f"{ALLOWED_ROOTS_ENV} must not contain duplicate roots")
    if any(root == root.parent for root in roots):
        raise ValueError(f"{ALLOWED_ROOTS_ENV} must not include a filesystem root")
    return roots


def _read_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    if re.fullmatch(r"(?:0|[1-9][0-9]*)", raw_value) is None:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    parsed = int(raw_value)
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return parsed


def _read_weight_source(environment: Mapping[str, str]) -> WeightSource:
    raw_value = environment.get(WEIGHT_SOURCE_ENV, "github_bundled")
    if raw_value not in {"github_bundled", "user_provided"}:
        raise ValueError(f"{WEIGHT_SOURCE_ENV} must be github_bundled or user_provided")
    if raw_value == "user_provided":
        return "user_provided"
    return "github_bundled"


__all__ = [
    "ALLOWED_ROOTS_ENV",
    "CHECKOUT_ENV",
    "CPU_THREADS_ENV",
    "DEFAULT_CPU_THREADS",
    "DEFAULT_MAX_CONCURRENT_JOBS",
    "DEFAULT_MAX_QUEUE_SIZE",
    "DEFAULT_PLAN_TTL_SECONDS",
    "DEFAULT_RETENTION_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS_ENV",
    "ENV_PREFIX",
    "MAX_CONCURRENT_JOBS_ENV",
    "MAX_DIAGNOSTIC_BYTES_ENV",
    "MAX_HEADER_LENGTH_ENV",
    "MAX_INPUT_BYTES_ENV",
    "MAX_OUTPUT_BYTES_ENV",
    "MAX_QUEUE_SIZE_ENV",
    "MAX_RESIDUES_ENV",
    "MAX_SEQUENCES_ENV",
    "MAX_SEQUENCE_LENGTH_ENV",
    "PLAN_TTL_SECONDS_ENV",
    "PYTHON_ENV",
    "RETENTION_SECONDS_ENV",
    "STATE_ROOT_ENV",
    "WEIGHT_SOURCE_ENV",
    "DeepKoalaRuntimeConfig",
    "load_runtime_config",
]
