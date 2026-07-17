"""Fixed local subprocess adapter with bounded Linux lifecycle ownership."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from deepkoala_mcp.contracts import MAX_OUTPUT_BYTES

OUTPUT_FILENAME: Final = "output.csv"
_TERMINATION_GRACE_SECONDS: Final = 5.0
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
    "SYSTEMROOT",
    "TMPDIR",
    "VIRTUAL_ENV",
    "XDG_CACHE_HOME",
)

# This fixed launcher installs parent-death and output-file controls before importing upstream
# code. Caller text is never interpolated into it, and the child receives only fixed flags plus
# validated scalar values.
_CHILD_LAUNCHER: Final = """\
import ctypes
import os
import resource
import runpy
import signal
import sys

def terminate_process_group(_signal_number, _frame):
    os.killpg(os.getpgrp(), signal.SIGKILL)

expected_parent_pid = int(sys.argv[1])
if expected_parent_pid <= 1:
    raise SystemExit("invalid parent process")
signal.signal(signal.SIGTERM, terminate_process_group)
libc = ctypes.CDLL(None, use_errno=True)
prctl = libc.prctl
prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
prctl.restype = ctypes.c_int
if prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "PR_SET_PDEATHSIG failed")
if os.getppid() != expected_parent_pid:
    terminate_process_group(signal.SIGTERM, None)

limit = int(sys.argv[2])
if limit < 1:
    raise SystemExit("invalid output limit")
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
effective = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
resource.setrlimit(resource.RLIMIT_FSIZE, (effective, effective))
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
os.umask(0o077)
del sys.argv[1:3]
runpy.run_module("deepkoala.cli", run_name="__main__", alter_sys=True)
"""


@dataclass(frozen=True, slots=True)
class RunnerPlan:
    """Private paths and validated settings for one fixed child command."""

    python_executable: Path
    checkout: Path
    job_directory: Path
    model: str
    resolved_date: str
    batch_size: int
    topk: int
    timeout_seconds: int
    cpu_threads: int
    max_output_bytes: int = MAX_OUTPUT_BYTES

    @property
    def input_path(self) -> Path:
        return self.job_directory / "input.fasta"

    @property
    def output_path(self) -> Path:
        return self.job_directory / OUTPUT_FILENAME


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """Non-sensitive child completion facts."""

    return_code: int


class RunnerTimedOutError(TimeoutError):
    """The complete child process group exceeded its deadline."""


class DeepKoalaProcessRunner:
    """Run only the fixed local DeepKOALA command without a shell."""

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        if plan.output_path.exists() or plan.output_path.is_symlink():
            raise RuntimeError("private output already exists")
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *build_argv(plan),
                cwd=str(plan.checkout),
                env=build_child_environment(plan),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        )
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            process = await _finish_spawn(spawn)
            await _finish_termination(process)
            raise
        except OSError as error:
            raise RuntimeError("DeepKOALA process could not be started") from error

        try:
            try:
                async with asyncio.timeout(plan.timeout_seconds):
                    return_code = await process.wait()
            except TimeoutError as error:
                cancelled = await _finish_termination(process)
                if cancelled:
                    raise asyncio.CancelledError from None
                raise RunnerTimedOutError("DeepKOALA exceeded the execution timeout") from error
            except asyncio.CancelledError:
                await _finish_termination(process)
                raise
        finally:
            if (
                process.returncode is None or _process_group_exists(process.pid)
            ) and await _finish_termination(process):
                raise asyncio.CancelledError from None
        return ProcessOutcome(return_code=return_code)


def build_argv(plan: RunnerPlan) -> tuple[str, ...]:
    """Build the only child argument vector accepted by the companion."""
    return (
        str(plan.python_executable),
        "-c",
        _CHILD_LAUNCHER,
        str(os.getpid()),
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
        "auto",
        "--detail",
        "--batch_size",
        str(plan.batch_size),
        "--num_workers",
        "0",
        "--topk",
        str(plan.topk),
    )


def build_child_environment(plan: RunnerPlan) -> dict[str, str]:
    """Build a small environment that preserves deployment GPU visibility and bounds threads."""
    return build_runtime_environment(plan.checkout, plan.cpu_threads)


def build_runtime_environment(checkout: Path, cpu_threads: int) -> dict[str, str]:
    """Build the shared fixed environment for probes and inference children."""
    environment = {name: os.environ[name] for name in _INHERITED_ENVIRONMENT if name in os.environ}
    threads = str(cpu_threads)
    environment.update(
        {
            "OMP_NUM_THREADS": threads,
            "MKL_NUM_THREADS": threads,
            "OPENBLAS_NUM_THREADS": threads,
            "NUMEXPR_NUM_THREADS": threads,
            "VECLIB_MAXIMUM_THREADS": threads,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(checkout),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return environment


async def _finish_spawn(
    spawn: asyncio.Task[asyncio.subprocess.Process],
) -> asyncio.subprocess.Process:
    """Finish an in-flight spawn despite repeated shutdown cancellation."""
    while True:
        try:
            return await asyncio.shield(spawn)
        except asyncio.CancelledError:
            if spawn.cancelled():
                raise
            continue


async def _finish_termination(process: asyncio.subprocess.Process) -> bool:
    """Finish process-group cleanup and report cancellation received while waiting."""
    cleanup = asyncio.create_task(_terminate_process_group(process))
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            return cancelled
        except asyncio.CancelledError:
            cancelled = True


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    process_group_id = process.pid
    if _process_group_exists(process_group_id):
        _signal_group(process_group_id, signal.SIGTERM)
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        async with asyncio.timeout(_TERMINATION_GRACE_SECONDS):
            await _wait_until_gone(process, process_group_id)
            return
    except TimeoutError:
        pass
    if _process_group_exists(process_group_id):
        _signal_group(process_group_id, signal.SIGKILL)
    elif process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    try:
        async with asyncio.timeout(_TERMINATION_GRACE_SECONDS):
            await _wait_until_gone(process, process_group_id)
    except TimeoutError:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(1.0):
                    await process.wait()


async def _wait_until_gone(
    process: asyncio.subprocess.Process,
    process_group_id: int,
) -> None:
    if process.returncode is None:
        await process.wait()
    while _process_group_exists(process_group_id):
        await asyncio.sleep(0.01)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_group(process_group_id: int, signal_number: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group_id, signal_number)


__all__ = [
    "OUTPUT_FILENAME",
    "DeepKoalaProcessRunner",
    "ProcessOutcome",
    "RunnerPlan",
    "RunnerTimedOutError",
    "build_argv",
    "build_child_environment",
    "build_runtime_environment",
]
