"""Fixed argv, device policy, output cap, and process-group lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from deepkoala_mcp import runner as runner_module
from deepkoala_mcp.runner import (
    DeepKoalaProcessRunner,
    RunnerPlan,
    RunnerTimedOutError,
    build_argv,
    build_child_environment,
)

_ARGPARSE = """\
import argparse
p = argparse.ArgumentParser()
p.add_argument('--input_path', required=True)
p.add_argument('--output_path', required=True)
p.add_argument('--model', required=True)
p.add_argument('--date', required=True)
p.add_argument('--device', required=True)
p.add_argument('--detail', action='store_true')
p.add_argument('--batch_size', type=int, required=True)
p.add_argument('--num_workers', type=int, required=True)
p.add_argument('--topk', type=int, required=True)
args = p.parse_args()
"""


def _plan(checkout: Path, tmp_path: Path, **updates: object) -> RunnerPlan:
    job = tmp_path / "job"
    job.mkdir(exist_ok=True)
    (job / "input.fasta").write_text(">p\nM\n", encoding="ascii")
    values: dict[str, object] = {
        "python_executable": Path(sys.executable).resolve(),
        "checkout": checkout,
        "job_directory": job,
        "model": "full",
        "resolved_date": "202502",
        "batch_size": 1,
        "topk": 1,
        "timeout_seconds": 5,
        "cpu_threads": 2,
    }
    values.update(updates)
    return RunnerPlan(**values)  # pyright: ignore[reportArgumentType]


def _write_cli(checkout: Path, body: str) -> None:
    (checkout / "deepkoala" / "cli.py").write_text(_ARGPARSE + body, encoding="utf-8")


def test_build_argv_is_fixed_auto_device_and_worker_free(checkout: Path, tmp_path: Path) -> None:
    plan = _plan(checkout, tmp_path, model="frag", batch_size=4, topk=3)
    argv = build_argv(plan)
    assert argv[0] == str(plan.python_executable)
    assert argv[1] == "-c"
    assert argv[3] == str(os.getpid())
    assert argv[argv.index("--device") + 1] == "auto"
    assert argv[argv.index("--num_workers") + 1] == "0"
    assert argv[argv.index("--model") + 1] == "frag"
    assert "--detail" in argv
    assert len(argv) == 22


def test_child_environment_inherits_gpu_visibility_and_bounds_threads(
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("HIP_VISIBLE_DEVICES", "1")
    monkeypatch.setenv("ROCR_VISIBLE_DEVICES", "2")
    environment = build_child_environment(_plan(checkout, tmp_path, cpu_threads=3))
    assert environment["CUDA_VISIBLE_DEVICES"] == "0"
    assert environment["HIP_VISIBLE_DEVICES"] == "1"
    assert environment["ROCR_VISIBLE_DEVICES"] == "2"
    for name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
    ):
        assert environment[name] == "3"


@pytest.mark.asyncio
async def test_runner_executes_local_cli_with_auto_device_contract(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    _write_cli(
        checkout,
        """
import json
import os
from pathlib import Path
Path(args.output_path).write_text(json.dumps({
    'device': args.device,
    'workers': args.num_workers,
    'threads': os.environ['OMP_NUM_THREADS'],
    'cuda': os.environ['CUDA_VISIBLE_DEVICES'],
}), encoding='utf-8')
""",
    )
    plan = _plan(checkout, tmp_path)
    outcome = await DeepKoalaProcessRunner().run(plan)
    payload = json.loads(plan.output_path.read_text(encoding="utf-8"))
    assert outcome.return_code == 0
    assert payload == {"device": "auto", "workers": 0, "threads": "2", "cuda": "0"}


@pytest.mark.asyncio
async def test_runner_installs_hard_output_file_limit(checkout: Path, tmp_path: Path) -> None:
    _write_cli(
        checkout,
        """
from pathlib import Path
Path(args.output_path).write_bytes(b'x' * 10000)
""",
    )
    plan = _plan(checkout, tmp_path, max_output_bytes=64)
    outcome = await DeepKoalaProcessRunner().run(plan)
    assert outcome.return_code != 0
    assert not plan.output_path.exists() or plan.output_path.stat().st_size <= 64


@pytest.mark.asyncio
async def test_runner_times_out_and_reaps_process_group(checkout: Path, tmp_path: Path) -> None:
    _write_cli(checkout, "\nimport time\ntime.sleep(30)\n")
    plan = _plan(checkout, tmp_path, timeout_seconds=1)
    started = time.monotonic()
    with pytest.raises(RunnerTimedOutError):
        await DeepKoalaProcessRunner().run(plan)
    assert time.monotonic() - started < 4


@pytest.mark.asyncio
async def test_runner_cancellation_terminates_descendants(checkout: Path, tmp_path: Path) -> None:
    _write_cli(
        checkout,
        """
import subprocess
from pathlib import Path
child = subprocess.Popen(['sleep', '30'])
Path(args.output_path).write_text(str(child.pid), encoding='ascii')
child.wait()
""",
    )
    plan = _plan(checkout, tmp_path, timeout_seconds=30)
    task = asyncio.create_task(DeepKoalaProcessRunner().run(plan))
    async with asyncio.timeout(5):
        while not plan.output_path.exists():
            await asyncio.sleep(0.01)
    child_pid = int(plan.output_path.read_text(encoding="ascii"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(3):
        while _pid_exists(child_pid):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runner_repeated_cancellation_still_reaps_group(
    checkout: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "_TERMINATION_GRACE_SECONDS", 0.05)
    _write_cli(
        checkout,
        """
import signal
import subprocess
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen(['sleep', '30'])
Path(args.output_path).write_text(str(child.pid), encoding='ascii')
child.wait()
""",
    )
    plan = _plan(checkout, tmp_path, timeout_seconds=30)
    task = asyncio.create_task(DeepKoalaProcessRunner().run(plan))
    async with asyncio.timeout(5):
        while not plan.output_path.exists():
            await asyncio.sleep(0.01)
    child_pid = int(plan.output_path.read_text(encoding="ascii"))
    task.cancel()
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(3):
        while _pid_exists(child_pid):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runner_reaps_descendant_after_leader_exit(checkout: Path, tmp_path: Path) -> None:
    _write_cli(
        checkout,
        """
import subprocess
from pathlib import Path
child = subprocess.Popen(['sleep', '30'])
Path(args.output_path).write_text(str(child.pid), encoding='ascii')
""",
    )
    plan = _plan(checkout, tmp_path)
    outcome = await DeepKoalaProcessRunner().run(plan)
    child_pid = int(plan.output_path.read_text(encoding="ascii"))
    assert outcome.return_code == 0
    async with asyncio.timeout(3):
        while _pid_exists(child_pid):
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_parent_sigkill_terminates_deepkoala_child(checkout: Path, tmp_path: Path) -> None:
    _write_cli(
        checkout,
        """
import os
import time
from pathlib import Path
Path(args.output_path).write_text(str(os.getpid()), encoding='ascii')
time.sleep(30)
""",
    )
    plan = _plan(checkout, tmp_path, timeout_seconds=30)
    parent_code = """
import asyncio
import sys
from pathlib import Path
from deepkoala_mcp.runner import DeepKoalaProcessRunner, RunnerPlan

plan = RunnerPlan(
    python_executable=Path(sys.argv[1]),
    checkout=Path(sys.argv[2]),
    job_directory=Path(sys.argv[3]),
    model='full',
    resolved_date='202502',
    batch_size=1,
    topk=1,
    timeout_seconds=30,
    cpu_threads=2,
)
asyncio.run(DeepKoalaProcessRunner().run(plan))
"""
    parent = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        str(plan.python_executable),
        str(plan.checkout),
        str(plan.job_directory),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(5):
            while not plan.output_path.exists():
                await asyncio.sleep(0.01)
        child_pid = int(plan.output_path.read_text(encoding="ascii"))
        parent.kill()
        await parent.wait()
        async with asyncio.timeout(3):
            while _pid_exists(child_pid):
                await asyncio.sleep(0.01)
    finally:
        if parent.returncode is None:
            parent.kill()
            await parent.wait()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
