"""Environment-only deployment policy for the local companion."""

from __future__ import annotations

import os
import platform
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deepkoala_mcp.contracts import (
    MAX_FASTA_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_SEQUENCE_COUNT,
    ErrorCode,
    ModelName,
    RunDeepKoalaInput,
    fail,
)

ENV_PREFIX = "DEEPKOALA_MCP_"
CHECKOUT_ENV = f"{ENV_PREFIX}CHECKOUT"
PYTHON_ENV = f"{ENV_PREFIX}PYTHON"
STATE_ROOT_ENV = f"{ENV_PREFIX}STATE_ROOT"
INPUT_ROOTS_ENV = f"{ENV_PREFIX}INPUT_ROOTS"
OUTPUT_ROOTS_ENV = f"{ENV_PREFIX}OUTPUT_ROOTS"
ALLOWED_MODELS_ENV = f"{ENV_PREFIX}ALLOWED_MODELS"
ALLOWED_DEVICES_ENV = f"{ENV_PREFIX}ALLOWED_DEVICES"
CPU_THREADS_ENV = f"{ENV_PREFIX}CPU_THREADS"
MAX_FASTA_BYTES_ENV = f"{ENV_PREFIX}MAX_FASTA_BYTES"
MAX_SEQUENCES_ENV = f"{ENV_PREFIX}MAX_SEQUENCES"
MAX_OUTPUT_BYTES_ENV = f"{ENV_PREFIX}MAX_OUTPUT_BYTES"
MAX_TIMEOUT_SECONDS_ENV = f"{ENV_PREFIX}MAX_TIMEOUT_SECONDS"
ALLOW_MULTI_ENV = f"{ENV_PREFIX}ALLOW_MULTI"
PROFILES_DIR_ENV = f"{ENV_PREFIX}PROFILES_DIR"
HMMSEARCH_EXECUTABLE_ENV = f"{ENV_PREFIX}HMMSEARCH_EXECUTABLE"

DEFAULT_CPU_THREADS = 2
DEFAULT_MAX_TIMEOUT_SECONDS = 3_600
MINIMUM_MACOS_MAJOR_VERSION = 14
_MACOS_VERSION = re.compile(r"(?P<major>[1-9][0-9]*)(?:\.[0-9]+){0,2}\Z")


def _default_devices() -> tuple[Literal["cpu", "cuda", "mps"], ...]:
    return ("cpu", "mps") if sys.platform == "darwin" else ("cpu", "cuda")


class DeepKoalaRuntimeConfig(BaseModel):
    """Private deployment paths and the complete execution allowlist."""

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
    input_roots: tuple[Path, ...]
    output_roots: tuple[Path, ...]
    allowed_models: tuple[ModelName, ...] = ("full", "frag")
    allowed_devices: tuple[Literal["cpu", "cuda", "mps"], ...] = Field(
        default_factory=_default_devices
    )
    cpu_threads: int = Field(default=DEFAULT_CPU_THREADS, strict=True, ge=1, le=4)
    max_fasta_bytes: int = Field(default=MAX_FASTA_BYTES, strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_sequences: int = Field(
        default=MAX_SEQUENCE_COUNT,
        strict=True,
        ge=1,
        le=MAX_SEQUENCE_COUNT,
    )
    max_output_bytes: int = Field(
        default=MAX_OUTPUT_BYTES,
        strict=True,
        ge=1,
        le=MAX_OUTPUT_BYTES,
    )
    max_timeout_seconds: int = Field(
        default=DEFAULT_MAX_TIMEOUT_SECONDS,
        strict=True,
        ge=1,
        le=86_400,
    )
    allow_multi: bool = Field(default=False, strict=True)
    profiles_dir: Path | None = None
    hmmsearch_executable: Path | None = None
    max_concurrent_jobs: Literal[1] = 1

    def validate_run_request(self, request: RunDeepKoalaInput) -> None:
        """Enforce deployment-owned request policy before staging input."""
        if request.model not in self.allowed_models:
            fail(
                ErrorCode.POLICY_DENIED,
                "The requested model is outside the deployment allowlist.",
                suggested_action="Choose a model reported by runner status.",
            )
        if request.device not in self.allowed_devices:
            fail(
                ErrorCode.POLICY_DENIED,
                "The requested device policy is not allowed.",
                suggested_action="Use a device reported by runner status.",
            )
        if request.multi and not self.allow_multi:
            fail(
                ErrorCode.POLICY_DENIED,
                "Multi-domain execution is disabled by deployment policy.",
                suggested_action=(
                    "Use single-domain mode or ask the operator to configure local dependencies."
                ),
            )
        if request.multi and request.batch_size != 1:
            fail(
                ErrorCode.POLICY_DENIED,
                "Multi-domain execution does not use configurable batching.",
                suggested_action="Use batch_size=1 for multi-domain annotation.",
            )
        if (
            request.timeout_seconds is not None
            and request.timeout_seconds > self.max_timeout_seconds
        ):
            fail(
                ErrorCode.POLICY_DENIED,
                "The requested timeout exceeds the deployment policy.",
                suggested_action="Use a timeout no greater than the status-reported maximum.",
            )

    @model_validator(mode="after")
    def validate_paths_and_policy(self) -> Self:
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
        _validate_roots("input_roots", self.input_roots)
        _validate_roots("output_roots", self.output_roots, writable=True)
        if any(_overlap(root, self.state_root) for root in (*self.input_roots, *self.output_roots)):
            raise ValueError("input and output roots must not overlap state_root")
        if any(_overlap(root, self.checkout) for root in self.output_roots):
            raise ValueError("output_roots must not overlap checkout")
        if len(self.allowed_models) != len(set(self.allowed_models)):
            raise ValueError("allowed_models must be unique")
        if not self.allowed_models:
            raise ValueError("allowed_models must not be empty")
        if self.allowed_devices not in {("cpu",), ("cpu", "cuda"), ("cpu", "mps")}:
            raise ValueError("the device allowlist must be 'cpu', 'cpu,cuda', or 'cpu,mps'")
        if sys.platform == "darwin" and self.allowed_devices == ("cpu", "cuda"):
            raise ValueError("the CUDA device allowlist is unavailable on macOS")
        if sys.platform == "linux" and self.allowed_devices == ("cpu", "mps"):
            raise ValueError("the MPS device allowlist is unavailable on Linux")
        if self.allow_multi and (self.profiles_dir is None or self.hmmsearch_executable is None):
            raise ValueError(
                "multi-domain execution requires profiles_dir and hmmsearch_executable"
            )
        for name, path in (
            ("profiles_dir", self.profiles_dir),
            ("hmmsearch_executable", self.hmmsearch_executable),
        ):
            if path is None:
                continue
            if not path.is_absolute() or ".." in path.parts or path == Path(path.anchor):
                raise ValueError(f"{name} must be an absolute traversal-free non-root path")
            if any(
                _overlap(path, root)
                for root in (self.state_root, *self.input_roots, *self.output_roots)
            ):
                raise ValueError(f"{name} must not overlap mutable companion roots")
        return self


def load_runtime_config(
    environment: Mapping[str, str] | None = None,
) -> DeepKoalaRuntimeConfig:
    """Load the explicit ``DEEPKOALA_MCP_*`` deployment policy."""
    values = os.environ if environment is None else environment
    allow_multi = _boolean(values, ALLOW_MULTI_ENV, False)
    return DeepKoalaRuntimeConfig(
        checkout=_required_existing(values, CHECKOUT_ENV, directory=True),
        python_executable=_required_existing(values, PYTHON_ENV, directory=False),
        state_root=_absolute(_required(values, STATE_ROOT_ENV), STATE_ROOT_ENV),
        input_roots=_roots(_required(values, INPUT_ROOTS_ENV), INPUT_ROOTS_ENV),
        output_roots=_roots(_required(values, OUTPUT_ROOTS_ENV), OUTPUT_ROOTS_ENV),
        allowed_models=_models(values.get(ALLOWED_MODELS_ENV, "full,frag")),
        allowed_devices=_devices(values.get(ALLOWED_DEVICES_ENV, ",".join(_default_devices()))),
        cpu_threads=_integer(values, CPU_THREADS_ENV, DEFAULT_CPU_THREADS, 1, 4),
        max_fasta_bytes=_integer(
            values,
            MAX_FASTA_BYTES_ENV,
            MAX_FASTA_BYTES,
            1,
            MAX_FASTA_BYTES,
        ),
        max_sequences=_integer(
            values,
            MAX_SEQUENCES_ENV,
            MAX_SEQUENCE_COUNT,
            1,
            MAX_SEQUENCE_COUNT,
        ),
        max_output_bytes=_integer(
            values,
            MAX_OUTPUT_BYTES_ENV,
            MAX_OUTPUT_BYTES,
            1,
            MAX_OUTPUT_BYTES,
        ),
        max_timeout_seconds=_integer(
            values,
            MAX_TIMEOUT_SECONDS_ENV,
            DEFAULT_MAX_TIMEOUT_SECONDS,
            1,
            86_400,
        ),
        allow_multi=allow_multi,
        profiles_dir=_optional_path(values, PROFILES_DIR_ENV, required=allow_multi),
        hmmsearch_executable=_optional_path(values, HMMSEARCH_EXECUTABLE_ENV, required=allow_multi),
    )


def _require_posix_runtime() -> None:
    try:
        import resource
    except ImportError:
        resource = None  # type: ignore[assignment]
    if (
        sys.platform not in {"linux", "darwin"}
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
            "deepkoala-mcp requires Linux or macOS POSIX process and filesystem controls"
        )
    if sys.platform == "darwin":
        version = _MACOS_VERSION.fullmatch(platform.mac_ver()[0])
        if (
            platform.machine().strip().lower() != "arm64"
            or version is None
            or int(version.group("major")) < MINIMUM_MACOS_MAJOR_VERSION
        ):
            raise ValueError(
                "deepkoala-mcp requires native Apple Silicon macOS 14 or later; "
                "Intel macOS and Rosetta are unsupported"
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
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} contains a control character")
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be absolute and traversal-free")
    return path


def _optional_path(values: Mapping[str, str], name: str, *, required: bool) -> Path | None:
    value = values.get(name)
    if value is None or not value:
        if required:
            raise ValueError(f"{name} is required when {ALLOW_MULTI_ENV}=true")
        return None
    return _absolute(value, name)


def _roots(value: str, name: str) -> tuple[Path, ...]:
    parts = value.split(os.pathsep)
    if any(not part for part in parts):
        raise ValueError(f"{name} contains an empty root")
    roots = tuple(_required_existing({name: part}, name, directory=True) for part in parts)
    if len(roots) != len(set(roots)):
        raise ValueError(f"{name} contains duplicate roots")
    return roots


def _models(value: str) -> tuple[ModelName, ...]:
    values = tuple(value.split(","))
    if not values or any(item not in {"full", "frag"} for item in values):
        raise ValueError(f"{ALLOWED_MODELS_ENV} must be a comma-separated subset of full,frag")
    return cast(tuple[ModelName, ...], values)


def _devices(value: str) -> tuple[Literal["cpu", "cuda", "mps"], ...]:
    devices = tuple(value.split(","))
    if devices not in {("cpu",), ("cpu", "cuda"), ("cpu", "mps")}:
        raise ValueError(f"{ALLOWED_DEVICES_ENV} must be exactly cpu, cpu,cuda, or cpu,mps")
    return cast(tuple[Literal["cpu", "cuda", "mps"], ...], devices)


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


def _boolean(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None:
        return default
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be exactly true or false")
    return value == "true"


def _validate_roots(name: str, roots: tuple[Path, ...], *, writable: bool = False) -> None:
    if not roots:
        raise ValueError(f"{name} must not be empty")
    if len(roots) != len(set(roots)):
        raise ValueError(f"{name} must be unique")
    for root in roots:
        if (
            not root.is_absolute()
            or ".." in root.parts
            or not root.is_dir()
            or root == Path(root.anchor)
        ):
            raise ValueError(f"{name} must contain traversal-free non-root absolute directories")
        if writable and not os.access(root, os.W_OK | os.X_OK):
            raise ValueError("output_roots must be writable directories")


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


__all__ = [
    "ALLOWED_DEVICES_ENV",
    "ALLOWED_MODELS_ENV",
    "ALLOW_MULTI_ENV",
    "CHECKOUT_ENV",
    "CPU_THREADS_ENV",
    "HMMSEARCH_EXECUTABLE_ENV",
    "INPUT_ROOTS_ENV",
    "MAX_FASTA_BYTES_ENV",
    "MAX_OUTPUT_BYTES_ENV",
    "MAX_SEQUENCES_ENV",
    "MAX_TIMEOUT_SECONDS_ENV",
    "OUTPUT_ROOTS_ENV",
    "PROFILES_DIR_ENV",
    "PYTHON_ENV",
    "STATE_ROOT_ENV",
    "DeepKoalaRuntimeConfig",
    "load_runtime_config",
]
