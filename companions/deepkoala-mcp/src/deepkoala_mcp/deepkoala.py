"""Safe installation, resource, and device preflight for external DeepKOALA."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

WEIGHT_SOURCE_GITHUB_BUNDLED: Final = "github_bundled"
_WEIGHT_SOURCES: Final = frozenset({"github_bundled", "user_provided"})

_ARTIFACT_READ_SIZE: Final = 1024 * 1024
_DEVICE_VALUES: Final = frozenset({"auto", "cpu", "cuda", "mps"})
_MODEL_VALUES: Final = frozenset({"full", "frag"})
_RESOURCE_DATE: Final = re.compile(r"^[0-9]{4}(?:0[1-9]|1[0-2])$")
_SAFE_SOURCE_VERSION: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_PYPROJECT_BYTES: Final = 256 * 1024
_MAX_SOURCE_FILES: Final = 512
_MAX_SOURCE_DIRECTORIES: Final = 512
_MAX_SOURCE_ENTRIES: Final = 4_096
_MAX_SOURCE_BYTES: Final = 32 * 1024 * 1024
_MAX_PYTHON_BYTES: Final = 128 * 1024 * 1024
_MAX_WEIGHT_BYTES: Final = 512 * 1024 * 1024
_MAX_MODEL_CONFIG_BYTES: Final = 64 * 1024 * 1024
_MAX_PROBE_TIMEOUT_SECONDS: Final = 120.0
_PROBE_STDOUT_LIMIT: Final = 1024
_PROBE_STDERR_LIMIT: Final = 16 * 1024
_MAX_RESOURCE_ENTRIES: Final = 1_024
_MAX_RESOURCE_DATES: Final = 128

# Checked on 2026-07-15 against official DeepKOALA commit
# bebbe0c43f50a26488f7092f6b355aae870a4ed9. This is the only Python source the companion may pass
# to ``python -c``. It does not load a model or inspect resource contents. Origin checks prevent an
# ambient package from satisfying a preflight for a different checkout.
_DEVICE_PROBE: Final = """\
import json
import pathlib
import sys

import deepkoala
from deepkoala import utils as deepkoala_utils

checkout = pathlib.Path.cwd().resolve(strict=True)
package_root = (checkout / "deepkoala").resolve(strict=True)
for module in (deepkoala, deepkoala_utils):
    module_file = getattr(module, "__file__", None)
    if not isinstance(module_file, str):
        raise RuntimeError("DeepKOALA module origin is unavailable")
    resolved_module = pathlib.Path(module_file).resolve(strict=True)
    try:
        resolved_module.relative_to(package_root)
    except ValueError as error:
        raise RuntimeError("DeepKOALA module origin does not match checkout") from error

resolved = str(deepkoala_utils.resolve_device(sys.argv[1]))
print(json.dumps({"protocol": 1, "resolved_device": resolved}, separators=(",", ":")))
"""

# The explicit interpreter already selects its Python environment. Only variables needed to
# preserve backend visibility and dynamic-library discovery are inherited. In particular, an
# ambient PYTHONPATH is never accepted.
_INHERITED_PROBE_ENVIRONMENT: Final = (
    "CUDA_VISIBLE_DEVICES",
    "DYLD_LIBRARY_PATH",
    "HIP_VISIBLE_DEVICES",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "ROCR_VISIBLE_DEVICES",
    "SYSTEMROOT",
)


class DeepKoalaProbeError(RuntimeError):
    """A stable public preflight failure without local paths or environment values."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DeepKoalaInstallation:
    """Explicit local installation selected by the companion administrator."""

    python_executable: Path
    checkout: Path

    def __post_init__(self) -> None:
        if not self.python_executable.is_absolute() or not self.checkout.is_absolute():
            raise DeepKoalaProbeError(
                "configuration_invalid",
                "DeepKOALA Python and checkout must be explicit absolute paths",
            )


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Serializable identity for one installed artifact without its local path."""

    basename: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DeepKoalaProbeResult:
    """Immutable facts that may be shown in a launch notice and retained as provenance."""

    requested_model: str
    requested_date: str
    resolved_date: str
    requested_device: str
    resolved_device: str
    source_version: str
    weight_source: str
    python_artifact: ArtifactIdentity
    source_artifact: ArtifactIdentity
    weight_artifact: ArtifactIdentity
    config_artifact: ArtifactIdentity


@dataclass(frozen=True, slots=True)
class _InstallationSnapshot:
    python_executable: Path
    checkout: Path
    source_version: str
    resolved_date: str
    python_artifact: ArtifactIdentity
    source_artifact: ArtifactIdentity
    weight_artifact: ArtifactIdentity
    config_artifact: ArtifactIdentity


class _CaptureLimitExceeded(Exception):
    pass


async def probe_deepkoala_installation(
    installation: DeepKoalaInstallation,
    *,
    model: str = "full",
    date: str = "latest",
    device: str = "auto",
    weight_source: str = WEIGHT_SOURCE_GITHUB_BUNDLED,
    timeout_seconds: float = 15.0,
) -> DeepKoalaProbeResult:
    """Validate one checkout and resolve its device without loading model artifacts."""
    _validate_probe_options(model=model, date=date, device=device, timeout_seconds=timeout_seconds)
    if weight_source not in _WEIGHT_SOURCES:
        raise DeepKoalaProbeError(
            "weight_source_invalid",
            "DeepKOALA weight source is invalid",
        )
    snapshot = await _snapshot_in_worker(installation, model, date)
    resolved_device = await _probe_device(
        python_executable=snapshot.python_executable,
        checkout=snapshot.checkout,
        requested_device=device,
        timeout_seconds=timeout_seconds,
    )
    confirmed = await _snapshot_in_worker(installation, model, snapshot.resolved_date)
    if confirmed != snapshot:
        raise DeepKoalaProbeError(
            "artifacts_changed",
            "DeepKOALA installation artifacts changed during preflight",
        )
    return DeepKoalaProbeResult(
        requested_model=model,
        requested_date=date,
        resolved_date=snapshot.resolved_date,
        requested_device=device,
        resolved_device=resolved_device,
        source_version=snapshot.source_version,
        weight_source=weight_source,
        python_artifact=snapshot.python_artifact,
        source_artifact=snapshot.source_artifact,
        weight_artifact=snapshot.weight_artifact,
        config_artifact=snapshot.config_artifact,
    )


async def recheck_artifact_identities(
    installation: DeepKoalaInstallation,
    probe_result: DeepKoalaProbeResult,
) -> None:
    """Fail when installed artifacts differ from a confirmed preflight result.

    The submit path should call this immediately before creating the inference subprocess. The
    function intentionally re-hashes both artifacts instead of trusting size or modification time.
    """
    if (
        probe_result.requested_model not in _MODEL_VALUES
        or _RESOURCE_DATE.fullmatch(probe_result.resolved_date) is None
        or probe_result.weight_source not in _WEIGHT_SOURCES
    ):
        raise DeepKoalaProbeError(
            "artifacts_changed",
            "DeepKOALA installation artifacts changed after preflight",
        )
    try:
        snapshot = await _snapshot_in_worker(
            installation,
            probe_result.requested_model,
            probe_result.resolved_date,
        )
    except DeepKoalaProbeError:
        raise DeepKoalaProbeError(
            "artifacts_changed",
            "DeepKOALA installation artifacts changed after preflight",
        ) from None
    unchanged = (
        snapshot.resolved_date == probe_result.resolved_date
        and snapshot.source_version == probe_result.source_version
        and snapshot.python_artifact == probe_result.python_artifact
        and snapshot.source_artifact == probe_result.source_artifact
        and snapshot.weight_artifact == probe_result.weight_artifact
        and snapshot.config_artifact == probe_result.config_artifact
    )
    if not unchanged:
        raise DeepKoalaProbeError(
            "artifacts_changed",
            "DeepKOALA installation artifacts changed after preflight",
        )


def _validate_probe_options(
    *,
    model: str,
    date: str,
    device: str,
    timeout_seconds: float,
) -> None:
    if model not in _MODEL_VALUES:
        raise DeepKoalaProbeError("model_invalid", "DeepKOALA model must be full or frag")
    if date != "latest" and _RESOURCE_DATE.fullmatch(date) is None:
        raise DeepKoalaProbeError(
            "resource_date_invalid",
            "DeepKOALA resource date must be latest or a YYYYMM value",
        )
    if device not in _DEVICE_VALUES:
        raise DeepKoalaProbeError(
            "device_invalid",
            "DeepKOALA device must be auto, cpu, cuda, or mps",
        )
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
        or timeout_seconds > _MAX_PROBE_TIMEOUT_SECONDS
    ):
        raise DeepKoalaProbeError(
            "timeout_invalid",
            "DeepKOALA probe timeout must be positive and at most 120 seconds",
        )


def _snapshot_installation(
    installation: DeepKoalaInstallation,
    model: str,
    requested_date: str,
) -> _InstallationSnapshot:
    python_executable = _resolve_python_executable(installation.python_executable)
    checkout = _resolve_checkout(installation.checkout)
    _validate_direct_directory(checkout / "deepkoala", code="source_layout_invalid")
    _validate_regular_file(
        checkout / "deepkoala" / "__init__.py",
        code="source_layout_invalid",
    )
    source_version = _read_source_version(checkout / "pyproject.toml")
    python_artifact = _hash_artifact(
        python_executable,
        basename="configured-python",
        maximum_bytes=_MAX_PYTHON_BYTES,
    )
    source_artifact = _hash_source_artifact(checkout)

    resources = checkout / "resources"
    _validate_direct_directory(resources, code="resources_invalid")
    resolved_date = _resolve_resource_date(resources, requested_date)
    dated_resources = resources / resolved_date
    _validate_direct_directory(dated_resources, code="resources_invalid")

    weight_artifact = _hash_artifact(
        dated_resources / f"weights_{model}.pt",
        maximum_bytes=_MAX_WEIGHT_BYTES,
    )
    config_artifact = _hash_artifact(
        dated_resources / f"ko_config_{model}.json",
        maximum_bytes=_MAX_MODEL_CONFIG_BYTES,
    )
    return _InstallationSnapshot(
        python_executable=python_executable,
        checkout=checkout,
        source_version=source_version,
        resolved_date=resolved_date,
        python_artifact=python_artifact,
        source_artifact=source_artifact,
        weight_artifact=weight_artifact,
        config_artifact=config_artifact,
    )


async def _snapshot_in_worker(
    installation: DeepKoalaInstallation,
    model: str,
    requested_date: str,
) -> _InstallationSnapshot:
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deepkoala-preflight")
    future = asyncio.get_running_loop().run_in_executor(
        executor,
        _snapshot_installation,
        installation,
        model,
        requested_date,
    )
    try:
        while not future.done():
            await asyncio.sleep(0.01)
        return future.result()
    except asyncio.CancelledError:
        while not future.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.01)
        with contextlib.suppress(Exception):
            future.result()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=False)


def _resolve_python_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise DeepKoalaProbeError(
            "python_invalid",
            "Configured DeepKOALA Python is unavailable",
        ) from None
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise DeepKoalaProbeError(
            "python_invalid",
            "Configured DeepKOALA Python is unavailable",
        )
    return resolved


def _resolve_checkout(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        metadata = resolved.stat()
    except (OSError, RuntimeError):
        raise DeepKoalaProbeError(
            "checkout_invalid",
            "Configured DeepKOALA checkout is unavailable",
        ) from None
    if not stat.S_ISDIR(metadata.st_mode):
        raise DeepKoalaProbeError(
            "checkout_invalid",
            "Configured DeepKOALA checkout is unavailable",
        )
    return resolved


def _validate_direct_directory(path: Path, *, code: str) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise DeepKoalaProbeError(code, "DeepKOALA installation layout is invalid") from None
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise DeepKoalaProbeError(code, "DeepKOALA installation layout is invalid")


def _validate_regular_file(path: Path, *, code: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError:
        raise DeepKoalaProbeError(code, "DeepKOALA installation file is invalid") from None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise DeepKoalaProbeError(code, "DeepKOALA installation file is invalid")
    return metadata


def _read_source_version(path: Path) -> str:
    metadata = _validate_regular_file(path, code="source_metadata_invalid")
    if metadata.st_size <= 0 or metadata.st_size > _MAX_PYPROJECT_BYTES:
        raise DeepKoalaProbeError(
            "source_metadata_invalid",
            "DeepKOALA source metadata is invalid",
        )
    try:
        raw = _read_regular_file(path, maximum_bytes=_MAX_PYPROJECT_BYTES)
        document = cast(dict[str, object], tomllib.loads(raw.decode("utf-8")))
        project_value = document.get("project")
        if not isinstance(project_value, dict):
            raise ValueError
        project = cast(dict[str, object], project_value)
        if project.get("name") != "deepkoala":
            raise ValueError
        version = project.get("version")
        if not isinstance(version, str) or _SAFE_SOURCE_VERSION.fullmatch(version) is None:
            raise ValueError
    except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError):
        raise DeepKoalaProbeError(
            "source_metadata_invalid",
            "DeepKOALA source metadata is invalid",
        ) from None
    return version


def _resolve_resource_date(resources: Path, requested_date: str) -> str:
    if requested_date != "latest":
        candidate = resources / requested_date
        try:
            metadata = candidate.lstat()
        except OSError:
            raise DeepKoalaProbeError(
                "resource_date_missing",
                "Requested DeepKOALA resource date is not installed",
            ) from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise DeepKoalaProbeError(
                "resource_date_missing",
                "Requested DeepKOALA resource date is not installed",
            )
        return requested_date

    candidates: list[str] = []
    inspected_entries = 0
    try:
        with os.scandir(resources) as entries:
            for entry in entries:
                inspected_entries += 1
                if inspected_entries > _MAX_RESOURCE_ENTRIES:
                    raise DeepKoalaProbeError(
                        "resources_invalid",
                        "DeepKOALA resources exceed the supported inspection boundary",
                    )
                if _RESOURCE_DATE.fullmatch(entry.name) is None:
                    continue
                if entry.is_dir(follow_symlinks=False) and not entry.is_symlink():
                    candidates.append(entry.name)
                    if len(candidates) > _MAX_RESOURCE_DATES:
                        raise DeepKoalaProbeError(
                            "resources_invalid",
                            "DeepKOALA resources exceed the supported inspection boundary",
                        )
    except OSError:
        raise DeepKoalaProbeError(
            "resources_invalid",
            "DeepKOALA resources cannot be inspected",
        ) from None
    if not candidates:
        raise DeepKoalaProbeError(
            "resource_date_missing",
            "No installed DeepKOALA resource date is available",
        )
    return max(candidates)


def _hash_artifact(
    path: Path,
    *,
    basename: str | None = None,
    allow_empty: bool = False,
    maximum_bytes: int = _MAX_WEIGHT_BYTES,
) -> ArtifactIdentity:
    metadata = _validate_regular_file(path, code="artifact_invalid")
    if (
        metadata.st_size < 0
        or metadata.st_size > maximum_bytes
        or (metadata.st_size == 0 and not allow_empty)
    ):
        raise DeepKoalaProbeError("artifact_invalid", "DeepKOALA artifact is invalid")
    descriptor = _open_no_follow(path, code="artifact_invalid")
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(metadata, opened):
            raise DeepKoalaProbeError("artifact_invalid", "DeepKOALA artifact is invalid")
        while True:
            chunk = os.read(descriptor, _ARTIFACT_READ_SIZE)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if _file_state(opened) != _file_state(after):
            raise DeepKoalaProbeError(
                "artifact_changed",
                "DeepKOALA artifact changed while it was inspected",
            )
    except OSError:
        raise DeepKoalaProbeError("artifact_invalid", "DeepKOALA artifact is invalid") from None
    finally:
        os.close(descriptor)

    final_path_metadata = _validate_regular_file(path, code="artifact_invalid")
    if not _same_file(after, final_path_metadata) or _file_state(after) != _file_state(
        final_path_metadata
    ):
        raise DeepKoalaProbeError(
            "artifact_changed",
            "DeepKOALA artifact changed while it was inspected",
        )
    return ArtifactIdentity(
        basename=basename or path.name,
        size_bytes=after.st_size,
        sha256=digest.hexdigest(),
    )


def _hash_source_artifact(checkout: Path) -> ArtifactIdentity:
    """Hash the bounded Python execution surface without exposing checkout paths."""
    members = _source_members(checkout)
    if not members or len(members) > _MAX_SOURCE_FILES:
        raise DeepKoalaProbeError(
            "source_layout_invalid",
            "DeepKOALA source layout exceeds the supported execution boundary",
        )
    total_bytes = 0
    digest = hashlib.sha256()
    for relative_path in members:
        identity = _hash_artifact(
            checkout / relative_path,
            allow_empty=True,
            maximum_bytes=_MAX_SOURCE_BYTES,
        )
        total_bytes += identity.size_bytes
        if total_bytes > _MAX_SOURCE_BYTES:
            raise DeepKoalaProbeError(
                "source_layout_invalid",
                "DeepKOALA source layout exceeds the supported execution boundary",
            )
        digest.update(relative_path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(identity.size_bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(identity.sha256.encode("ascii"))
        digest.update(b"\n")
    if _source_members(checkout) != members:
        raise DeepKoalaProbeError(
            "artifacts_changed",
            "DeepKOALA source layout changed while it was inspected",
        )
    return ArtifactIdentity(
        basename="deepkoala-source-tree",
        size_bytes=total_bytes,
        sha256=digest.hexdigest(),
    )


def _source_members(checkout: Path) -> tuple[Path, ...]:
    members: list[Path] = [Path("pyproject.toml")]
    code_suffixes = {".py", ".pyc", ".pyo", ".pyd", ".so"}
    inspected_directories = 0
    inspected_entries = 1
    try:
        for root, directory_names, file_names in os.walk(checkout, followlinks=False):
            inspected_directories += 1
            inspected_entries += len(directory_names) + len(file_names)
            if (
                inspected_directories > _MAX_SOURCE_DIRECTORIES
                or inspected_entries > _MAX_SOURCE_ENTRIES
            ):
                raise DeepKoalaProbeError(
                    "source_layout_invalid",
                    "DeepKOALA source layout exceeds the supported execution boundary",
                )
            root_path = Path(root)
            relative_root = root_path.relative_to(checkout)
            directory_names[:] = sorted(
                name
                for name in directory_names
                if not (relative_root == Path() and name in {".git", "resources"})
            )
            for directory_name in directory_names:
                directory_path = root_path / directory_name
                metadata = directory_path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    raise DeepKoalaProbeError(
                        "source_layout_invalid",
                        "DeepKOALA source layout is invalid",
                    )
            for file_name in sorted(file_names):
                path = root_path / file_name
                relative = path.relative_to(checkout)
                include = relative.parts[0] == "deepkoala" or path.suffix.lower() in code_suffixes
                if relative == Path("pyproject.toml") or not include:
                    continue
                metadata = path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    raise DeepKoalaProbeError(
                        "source_layout_invalid",
                        "DeepKOALA source layout is invalid",
                    )
                members.append(relative)
                if len(members) > _MAX_SOURCE_FILES:
                    raise DeepKoalaProbeError(
                        "source_layout_invalid",
                        "DeepKOALA source layout exceeds the supported execution boundary",
                    )
    except (OSError, RuntimeError, ValueError):
        raise DeepKoalaProbeError(
            "source_layout_invalid",
            "DeepKOALA source layout is invalid",
        ) from None
    unique = tuple(sorted(set(members), key=lambda item: item.as_posix()))
    if len(unique) != len(members):
        raise DeepKoalaProbeError(
            "source_layout_invalid",
            "DeepKOALA source layout is invalid",
        )
    return unique


def _read_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    before = _validate_regular_file(path, code="source_metadata_invalid")
    descriptor = _open_no_follow(path, code="source_metadata_invalid")
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            raise DeepKoalaProbeError(
                "source_metadata_invalid",
                "DeepKOALA source metadata is invalid",
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise DeepKoalaProbeError(
                    "source_metadata_invalid",
                    "DeepKOALA source metadata is invalid",
                )
        after = os.fstat(descriptor)
        if _file_state(opened) != _file_state(after):
            raise DeepKoalaProbeError(
                "source_metadata_invalid",
                "DeepKOALA source metadata changed while it was inspected",
            )
        return b"".join(chunks)
    except OSError:
        raise DeepKoalaProbeError(
            "source_metadata_invalid",
            "DeepKOALA source metadata is invalid",
        ) from None
    finally:
        os.close(descriptor)


def _open_no_follow(path: Path, *, code: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError:
        raise DeepKoalaProbeError(code, "DeepKOALA installation file is invalid") from None


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _file_state(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


async def _probe_device(
    *,
    python_executable: Path,
    checkout: Path,
    requested_device: str,
    timeout_seconds: float,
) -> str:
    try:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                str(python_executable),
                "-c",
                _DEVICE_PROBE,
                requested_device,
                cwd=str(checkout),
                env=_build_probe_environment(checkout),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await spawn
            await asyncio.shield(_stop_probe(process, _process_group_id(process)))
            raise
    except OSError:
        raise DeepKoalaProbeError(
            "device_probe_failed",
            "DeepKOALA device probe could not be started",
        ) from None

    if process.stdout is None or process.stderr is None:
        await _stop_probe(process, _process_group_id(process))
        raise DeepKoalaProbeError(
            "device_probe_failed",
            "DeepKOALA device probe pipes are unavailable",
        )

    stdout_task = asyncio.create_task(_read_bounded(process.stdout, _PROBE_STDOUT_LIMIT))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, _PROBE_STDERR_LIMIT))
    tasks = (stdout_task, stderr_task)
    process_group_id = _process_group_id(process)
    try:
        async with asyncio.timeout(timeout_seconds):
            return_code = await _wait_for_probe_leader(process, tasks)
            await _stop_probe(process, process_group_id)
            stdout, _stderr = await asyncio.gather(*tasks)
    except TimeoutError:
        await asyncio.shield(_stop_probe(process, process_group_id))
        await _cancel_tasks(tasks)
        raise DeepKoalaProbeError(
            "device_probe_timeout",
            "DeepKOALA device probe exceeded its configured timeout",
        ) from None
    except _CaptureLimitExceeded:
        await _stop_probe(process, process_group_id)
        await _cancel_tasks(tasks)
        raise DeepKoalaProbeError(
            "device_probe_output_invalid",
            "DeepKOALA device probe produced excessive output",
        ) from None
    except asyncio.CancelledError:
        await asyncio.shield(_stop_probe(process, process_group_id))
        await _cancel_tasks(tasks)
        raise

    if return_code != 0:
        raise DeepKoalaProbeError(
            "device_probe_failed",
            "DeepKOALA could not resolve the requested device",
        )
    try:
        payload_value: object = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise DeepKoalaProbeError(
            "device_probe_output_invalid",
            "DeepKOALA device probe returned invalid output",
        ) from None
    if not isinstance(payload_value, dict):
        raise DeepKoalaProbeError(
            "device_probe_output_invalid",
            "DeepKOALA device probe returned invalid output",
        )
    payload = cast(dict[str, object], payload_value)
    if payload.get("protocol") != 1:
        raise DeepKoalaProbeError(
            "device_probe_output_invalid",
            "DeepKOALA device probe returned invalid output",
        )
    resolved_device = payload.get("resolved_device")
    if not isinstance(resolved_device, str) or resolved_device not in _DEVICE_VALUES - {"auto"}:
        raise DeepKoalaProbeError(
            "device_probe_output_invalid",
            "DeepKOALA device probe returned an unsupported device",
        )
    return resolved_device


def _build_probe_environment(checkout: Path) -> dict[str, str]:
    environment = {
        name: os.environ[name] for name in _INHERITED_PROBE_ENVIRONMENT if name in os.environ
    }
    environment.update(
        {
            "MKL_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(checkout),
            "PYTHONUNBUFFERED": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        }
    )
    return environment


async def _read_bounded(stream: asyncio.StreamReader, maximum_bytes: int) -> bytes:
    content = bytearray()
    while True:
        remaining = maximum_bytes + 1 - len(content)
        chunk = await stream.read(min(4096, remaining))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
        if len(content) > maximum_bytes:
            raise _CaptureLimitExceeded


async def _stop_probe(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> None:
    if process.returncode is not None and not _process_group_exists(process_group_id):
        return
    if process_group_id is not None:
        _signal_process_group(process_group_id, signal.SIGTERM)
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        async with asyncio.timeout(2.0):
            await _wait_for_process_group_exit(process, process_group_id)
            return
    except TimeoutError:
        pass
    if process_group_id is not None:
        _signal_process_group(process_group_id, signal.SIGKILL)
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    async with asyncio.timeout(2.0):
        await _wait_for_process_group_exit(process, process_group_id)


def _process_group_id(process: asyncio.subprocess.Process) -> int | None:
    return process.pid if os.name == "posix" else None


def _process_group_exists(process_group_id: int | None) -> bool:
    if process_group_id is None:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_process_group(process_group_id: int, signal_number: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group_id, signal_number)


async def _wait_for_process_group_exit(
    process: asyncio.subprocess.Process,
    process_group_id: int | None,
) -> None:
    while process.returncode is None or _process_group_exists(process_group_id):
        await asyncio.sleep(0.01)


async def _wait_for_exit(process: asyncio.subprocess.Process) -> int:
    while process.returncode is None:
        await asyncio.sleep(0.01)
    return process.returncode


async def _wait_for_probe_leader(
    process: asyncio.subprocess.Process,
    capture_tasks: tuple[asyncio.Task[bytes], asyncio.Task[bytes]],
) -> int:
    leader = asyncio.create_task(_wait_for_exit(process))
    pending_captures = set(capture_tasks)
    try:
        while not leader.done():
            completed, _pending = await asyncio.wait(
                (leader, *pending_captures),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in completed:
                if task is leader:
                    continue
                task.result()
                pending_captures.discard(task)
        return leader.result()
    finally:
        if not leader.done():
            leader.cancel()
        await asyncio.gather(leader, return_exceptions=True)


async def _cancel_tasks(tasks: tuple[asyncio.Task[bytes], asyncio.Task[bytes]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "WEIGHT_SOURCE_GITHUB_BUNDLED",
    "ArtifactIdentity",
    "DeepKoalaInstallation",
    "DeepKoalaProbeError",
    "DeepKoalaProbeResult",
    "probe_deepkoala_installation",
    "recheck_artifact_identities",
]
