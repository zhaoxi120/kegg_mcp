"""Fixed local subprocess adapter with bounded Linux lifecycle ownership."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from deepkoala_mcp.contracts import MAX_OUTPUT_BYTES, ExecutionPlan

if TYPE_CHECKING:
    from deepkoala_mcp.config import DeepKoalaRuntimeConfig

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
import inspect
import os
import resource
import runpy
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

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
multi_enabled = sys.argv[3] == "1"
if sys.argv[3] not in {"0", "1"}:
    raise SystemExit("invalid multi-domain policy")
hmmsearch_value = sys.argv[4]
profiles_value = sys.argv[5]
cpu_threads = int(sys.argv[6])
scratch_value = sys.argv[7]
if not 1 <= cpu_threads <= 4:
    raise SystemExit("invalid CPU thread limit")
soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
effective = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
resource.setrlimit(resource.RLIMIT_FSIZE, (effective, effective))
signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
os.umask(0o077)

original_popen = subprocess.Popen
popen_signature = inspect.signature(original_popen)
def reject_shell_popen(*args, **kwargs):
    effective = popen_signature.bind_partial(*args, **kwargs)
    if effective.arguments.get("shell", False):
        raise RuntimeError("shell execution is disabled in the DeepKOALA child")
    return original_popen(*args, **kwargs)
subprocess.Popen = reject_shell_popen

if multi_enabled:
    hmmsearch = Path(hmmsearch_value).resolve(strict=True)
    profiles = Path(profiles_value).resolve(strict=True)
    scratch = Path(scratch_value).resolve(strict=True)
    hmmsearch_metadata = hmmsearch.lstat()
    profiles_metadata = profiles.lstat()
    scratch_metadata = scratch.lstat()
    if (
        not hmmsearch.is_absolute()
        or not stat.S_ISREG(hmmsearch_metadata.st_mode)
        or stat.S_ISLNK(hmmsearch_metadata.st_mode)
        or hmmsearch_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(hmmsearch_metadata.st_mode) & 0o022
        or not os.access(hmmsearch, os.R_OK | os.X_OK)
    ):
        raise SystemExit("unsafe hmmsearch executable")
    if (
        not profiles.is_absolute()
        or not stat.S_ISDIR(profiles_metadata.st_mode)
        or stat.S_ISLNK(profiles_metadata.st_mode)
        or profiles_metadata.st_uid not in {0, os.geteuid()}
        or stat.S_IMODE(profiles_metadata.st_mode) & 0o022
        or not os.access(profiles, os.R_OK | os.X_OK)
    ):
        raise SystemExit("unsafe profile directory")
    if (
        not scratch.is_absolute()
        or not stat.S_ISDIR(scratch_metadata.st_mode)
        or stat.S_ISLNK(scratch_metadata.st_mode)
        or scratch_metadata.st_uid != os.geteuid()
        or stat.S_IMODE(scratch_metadata.st_mode) & 0o077
    ):
        raise SystemExit("unsafe scratch directory")
    infer_multi = __import__("deepkoala.infer_multi", fromlist=["_run_hmmsearch"])
    candidate = getattr(infer_multi, "_run_hmmsearch")
    parameters = tuple(inspect.signature(candidate).parameters.values())
    if (
        tuple(parameter.name for parameter in parameters) != ("hmm_file", "seq")
        or any(
            parameter.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
            for parameter in parameters
        )
    ):
        raise SystemExit("unsupported DeepKOALA multi-domain interface")

    def safe_run_hmmsearch(hmm_file, seq):
        profile = Path(hmm_file)
        try:
            metadata = profile.lstat()
        except OSError as error:
            raise RuntimeError("configured HMM profile became unavailable") from error
        stem = profile.name.removesuffix(".hmm")
        if (
            profile.parent.resolve(strict=True) != profiles
            or len(stem) != 6
            or not stem.startswith("K")
            or not stem[1:].isdigit()
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_size < 1
        ):
            raise RuntimeError("DeepKOALA requested an unsafe HMM profile")
        fasta_descriptor = -1
        fasta_name = ""
        domtbl_name = ""
        try:
            fasta_descriptor, fasta_name = tempfile.mkstemp(
                prefix=".hmm-query-", suffix=".fasta", dir=scratch
            )
            domtbl_descriptor, domtbl_name = tempfile.mkstemp(
                prefix=".hmm-domain-", suffix=".tbl", dir=scratch
            )
            os.close(domtbl_descriptor)
            with os.fdopen(fasta_descriptor, "w", encoding="ascii", newline="\\n") as stream:
                fasta_descriptor = -1
                stream.write(">query\\n")
                stream.write(seq)
                stream.write("\\n")
            command = (
                str(hmmsearch),
                "--noali",
                "--cpu",
                str(cpu_threads),
                "--domtblout",
                domtbl_name,
                str(profile),
                fasta_name,
            )
            completed = subprocess.run(
                command,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if completed.returncode != 0:
                raise subprocess.CalledProcessError(completed.returncode, command)
            with open(domtbl_name, encoding="ascii") as stream:
                for line in stream:
                    if line.startswith("#"):
                        continue
                    columns = line.strip().split()
                    if len(columns) >= 19:
                        return int(columns[17]), int(columns[18])
            return None, None
        finally:
            if fasta_descriptor >= 0:
                os.close(fasta_descriptor)
            for temporary in (fasta_name, domtbl_name):
                if temporary:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass

    infer_multi._run_hmmsearch = safe_run_hmmsearch

del sys.argv[1:8]
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
    multi: bool = False
    profiles_dir: Path | None = None
    hmmsearch_executable: Path | None = None
    max_output_bytes: int = MAX_OUTPUT_BYTES

    @classmethod
    def from_execution_plan(
        cls,
        *,
        config: DeepKoalaRuntimeConfig,
        job_directory: Path,
        plan: ExecutionPlan,
    ) -> RunnerPlan:
        """Bind a public effective plan to deployment-private runner paths."""
        profiles_dir = None
        hmmsearch_executable = None
        if plan.multi:
            if config.profiles_dir is None or config.hmmsearch_executable is None:
                raise RuntimeError("multi-domain dependencies are not configured")
            profiles_dir = config.profiles_dir.resolve(strict=True)
            hmmsearch_executable = config.hmmsearch_executable.resolve(strict=True)
        return cls(
            python_executable=config.python_executable,
            checkout=config.checkout,
            job_directory=job_directory,
            model=plan.model,
            resolved_date=plan.resolved_model_date,
            batch_size=plan.batch_size,
            topk=plan.topk,
            timeout_seconds=plan.timeout_seconds,
            cpu_threads=plan.cpu_threads,
            multi=plan.multi,
            profiles_dir=profiles_dir,
            hmmsearch_executable=hmmsearch_executable,
            max_output_bytes=config.max_output_bytes,
        )

    def __post_init__(self) -> None:
        if self.multi and (self.profiles_dir is None or self.hmmsearch_executable is None):
            raise ValueError("multi-domain runner plan requires local dependencies")
        for path in (self.profiles_dir, self.hmmsearch_executable):
            if path is not None and (not path.is_absolute() or ".." in path.parts):
                raise ValueError("multi-domain runner paths must be absolute and traversal-free")

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
    control = (
        str(plan.python_executable),
        "-c",
        _CHILD_LAUNCHER,
        str(os.getpid()),
        str(plan.max_output_bytes),
        "1" if plan.multi else "0",
        str(plan.hmmsearch_executable) if plan.hmmsearch_executable is not None else "-",
        str(plan.profiles_dir) if plan.profiles_dir is not None else "-",
        str(plan.cpu_threads),
        str(plan.job_directory),
    )
    arguments = (
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
    if plan.multi:
        return control + arguments + ("--multi", "--profiles_dir", str(plan.profiles_dir))
    return control + arguments


def build_child_environment(plan: RunnerPlan) -> dict[str, str]:
    """Build a small environment that preserves deployment GPU visibility and bounds threads."""
    environment = build_runtime_environment(plan.checkout, plan.cpu_threads)
    if plan.multi:
        if plan.hmmsearch_executable is None:
            raise ValueError("multi-domain runner plan has no hmmsearch executable")
        environment["PATH"] = str(plan.hmmsearch_executable.resolve(strict=True).parent)
        environment["TMPDIR"] = str(plan.job_directory.resolve(strict=True))
    return environment


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
