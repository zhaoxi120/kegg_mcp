"""Bounded subprocess adapter for a configured external DeepKOALA checkout."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from deepkoala_mcp.diagnostics import SanitizedDiagnosticTail, drain_sanitized_stream

_INHERITED_ENVIRONMENT: Final = (
    "CONDA_PREFIX",
    "CUDA_VISIBLE_DEVICES",
    "DYLD_LIBRARY_PATH",
    "HOME",
    "HIP_VISIBLE_DEVICES",
    "LANG",
    "LC_ALL",
    "LD_LIBRARY_PATH",
    "PATH",
    "ROCR_VISIBLE_DEVICES",
    "TMPDIR",
    "VIRTUAL_ENV",
    "XDG_CACHE_HOME",
)
_TERMINATION_GRACE_SECONDS: Final = 5.0
_OUTPUT_LIMIT_LAUNCHER: Final = """\
import resource
import runpy
import signal
import sys

requested_limit = int(sys.argv[1])
if requested_limit < 1:
    raise SystemExit("invalid output file limit")
_soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
effective_limit = (
    requested_limit
    if hard_limit == resource.RLIM_INFINITY
    else min(requested_limit, hard_limit)
)
resource.setrlimit(resource.RLIMIT_FSIZE, (effective_limit, effective_limit))
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
del sys.argv[1]
runpy.run_module("deepkoala.cli", run_name="__main__", alter_sys=True)
"""


@dataclass(frozen=True, slots=True)
class RunnerPlan:
    """Exact immutable settings confirmed for one DeepKOALA process."""

    python_executable: Path
    checkout: Path
    job_directory: Path
    model: str
    resolved_date: str
    resolved_device: str
    batch_size_override: int | None
    num_workers_override: int | None
    topk_override: int | None
    timeout_seconds: int
    cpu_threads: int
    diagnostic_max_bytes: int
    max_output_bytes: int

    @property
    def input_path(self) -> Path:
        return self.job_directory / "input.fasta"

    @property
    def output_path(self) -> Path:
        return self.job_directory / "deepkoala-output.csv"


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Bounded process completion facts without raw paths or environment values."""

    return_code: int
    diagnostic_text: str
    diagnostics_truncated: bool


class RunnerTimedOutError(Exception):
    """The runner terminated a process after its configured deadline."""

    def __init__(self, diagnostic_text: str, diagnostics_truncated: bool) -> None:
        super().__init__("DeepKOALA exceeded the configured timeout")
        self.diagnostic_text = diagnostic_text
        self.diagnostics_truncated = diagnostics_truncated


class DeepKoalaProcessRunner:
    """Launch one fixed DeepKOALA argv and own its complete process lifecycle."""

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        """Run one process, draining both pipes and reaping on timeout or cancellation."""
        if plan.output_path.exists() or plan.output_path.is_symlink():
            raise RuntimeError("private runner output already exists")
        child_environment = build_child_environment(plan)
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *build_argv(plan),
                cwd=str(plan.checkout),
                env=child_environment,
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
            await asyncio.shield(_terminate_process(process, _process_group_id(process)))
            raise
        process_group_id = _process_group_id(process)
        if process.stdout is None or process.stderr is None:
            await _terminate_process(process, process_group_id)
            raise RuntimeError("runner pipes were not created")

        redacted_paths = (
            plan.python_executable,
            plan.python_executable.parent.parent,
            plan.checkout,
            plan.job_directory,
            *_environment_paths(child_environment),
        )
        stdout_tail = SanitizedDiagnosticTail(
            max_bytes=max(1, plan.diagnostic_max_bytes // 2),
            redacted_paths=redacted_paths,
        )
        stderr_tail = SanitizedDiagnosticTail(
            max_bytes=max(1, plan.diagnostic_max_bytes // 2),
            redacted_paths=redacted_paths,
        )
        drain_stdout = asyncio.create_task(drain_sanitized_stream(process.stdout, stdout_tail))
        drain_stderr = asyncio.create_task(drain_sanitized_stream(process.stderr, stderr_tail))
        drains = (drain_stdout, drain_stderr)
        try:
            try:
                async with asyncio.timeout(plan.timeout_seconds):
                    return_code = await _wait_for_exit(process)
                    await _terminate_process(process, process_group_id)
                    await asyncio.gather(*drains)
            except TimeoutError as error:
                await asyncio.shield(_terminate_process(process, process_group_id))
                await _cancel_tasks(drains)
                stdout_tail.finish()
                stderr_tail.finish()
                diagnostic, combined_truncated = _combine_diagnostics(
                    stdout_tail.text(),
                    stderr_tail.text(),
                    maximum_bytes=plan.diagnostic_max_bytes,
                )
                raise RunnerTimedOutError(
                    diagnostic,
                    stdout_tail.truncated or stderr_tail.truncated or combined_truncated,
                ) from error
        except asyncio.CancelledError:
            await asyncio.shield(_terminate_process(process, process_group_id))
            await _cancel_tasks(drains)
            raise
        finally:
            if process.returncode is None or _process_group_exists(process_group_id):
                await asyncio.shield(_terminate_process(process, process_group_id))
            await _cancel_tasks(drains)
        diagnostic, combined_truncated = _combine_diagnostics(
            stdout_tail.text(),
            stderr_tail.text(),
            maximum_bytes=plan.diagnostic_max_bytes,
        )
        return ProcessOutcome(
            return_code=return_code,
            diagnostic_text=diagnostic,
            diagnostics_truncated=(
                stdout_tail.truncated or stderr_tail.truncated or combined_truncated
            ),
        )


def build_argv(plan: RunnerPlan) -> tuple[str, ...]:
    """Build the only accepted external command; caller text never becomes an option."""
    argv = [
        str(plan.python_executable),
        "-c",
        _OUTPUT_LIMIT_LAUNCHER,
        str(plan.max_output_bytes),
        "--input_path",
        str(plan.input_path),
        "--output_path",
        str(plan.output_path),
        "--model",
        plan.model,
        "--date",
        plan.resolved_date,
        "--device",
        plan.resolved_device,
        "--detail",
    ]
    if plan.batch_size_override is not None:
        argv.extend(("--batch_size", str(plan.batch_size_override)))
    if plan.num_workers_override is not None:
        argv.extend(("--num_workers", str(plan.num_workers_override)))
    if plan.topk_override is not None:
        argv.extend(("--topk", str(plan.topk_override)))
    return tuple(argv)


def build_child_environment(plan: RunnerPlan) -> dict[str, str]:
    """Pass only runtime necessities and enforce a small CPU thread budget."""
    environment = {name: os.environ[name] for name in _INHERITED_ENVIRONMENT if name in os.environ}
    environment["PYTHONPATH"] = str(plan.checkout)
    threads = str(plan.cpu_threads)
    environment.update(
        {
            "OMP_NUM_THREADS": threads,
            "MKL_NUM_THREADS": threads,
            "OPENBLAS_NUM_THREADS": threads,
            "NUMEXPR_NUM_THREADS": threads,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "VECLIB_MAXIMUM_THREADS": threads,
        }
    )
    return environment


def _environment_paths(environment: dict[str, str]) -> tuple[Path, ...]:
    path_keys = {
        "CONDA_PREFIX",
        "DYLD_LIBRARY_PATH",
        "HOME",
        "LD_LIBRARY_PATH",
        "PATH",
        "TMPDIR",
        "VIRTUAL_ENV",
        "XDG_CACHE_HOME",
    }
    paths: set[Path] = set()
    for name in path_keys:
        value = environment.get(name)
        if not value:
            continue
        for component in value.split(os.pathsep):
            candidate = Path(component)
            if candidate.is_absolute():
                paths.add(candidate)
    return tuple(sorted(paths, key=lambda item: len(str(item)), reverse=True))


async def _terminate_process(
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
        async with asyncio.timeout(_TERMINATION_GRACE_SECONDS):
            await _wait_for_process_group_exit(process, process_group_id)
            return
    except TimeoutError:
        pass
    if process_group_id is not None:
        _signal_process_group(process_group_id, signal.SIGKILL)
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    async with asyncio.timeout(_TERMINATION_GRACE_SECONDS):
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


async def _cancel_tasks(tasks: tuple[asyncio.Task[None], asyncio.Task[None]]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _combine_diagnostics(
    stdout: str,
    stderr: str,
    *,
    maximum_bytes: int,
) -> tuple[str, bool]:
    parts: list[str] = []
    if stdout:
        parts.append(f"stdout:\n{stdout}")
    if stderr:
        parts.append(f"stderr:\n{stderr}")
    combined = "\n".join(parts)
    encoded = combined.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return combined, False
    return encoded[-maximum_bytes:].decode("utf-8", errors="ignore"), True


__all__ = [
    "DeepKoalaProcessRunner",
    "ProcessOutcome",
    "RunnerPlan",
    "RunnerTimedOutError",
    "build_argv",
    "build_child_environment",
]
