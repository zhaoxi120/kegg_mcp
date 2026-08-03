"""Atomic run, stable handoff, policy, cancellation, and cleanup tests."""

from __future__ import annotations

import asyncio
import stat
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import deepkoala_mcp.fasta as fasta_module
import deepkoala_mcp.jobs as jobs_module
from conftest import DETAILED_CSV, ready_probe
from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.contracts import (
    ANNOTATIONS_FILENAME,
    RUN_REPORT_FILENAME,
    DeepKoalaMcpError,
    ErrorCode,
    JobState,
    RunDeepKoalaInput,
    RunDeepKoalaResult,
)
from deepkoala_mcp.fasta import StagedFasta
from deepkoala_mcp.installation import RuntimeProbeResult
from deepkoala_mcp.job_storage import OutputValidationError
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan, RunnerTimedOutError


class SuccessfulRunner:
    def __init__(self, payload: bytes = DETAILED_CSV) -> None:
        self.payload = payload
        self.calls: list[RunnerPlan] = []

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        self.calls.append(plan)
        plan.output_path.write_bytes(self.payload)
        return ProcessOutcome(return_code=0)


class ExitRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        del plan
        return ProcessOutcome(return_code=7)


class TimeoutRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        del plan
        raise RunnerTimedOutError


class BlockingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        plan.output_path.write_bytes(DETAILED_CSV)
        return ProcessOutcome(return_code=0)


class SymlinkOutputRunner:
    def __init__(self, target: Path) -> None:
        self.target = target

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        plan.output_path.symlink_to(self.target)
        return ProcessOutcome(return_code=0)


@asynccontextmanager
async def _manager(
    config: DeepKoalaRuntimeConfig,
    runner: object,
    *,
    runtime_probe: object = ready_probe,
) -> AsyncGenerator[DeepKoalaJobManager]:
    manager = DeepKoalaJobManager(
        config,
        runner=runner,  # pyright: ignore[reportArgumentType]
        runtime_probe=runtime_probe,  # pyright: ignore[reportArgumentType]
    )
    await manager.open()
    try:
        yield manager
    finally:
        await manager.close()


def _request(
    config: DeepKoalaRuntimeConfig,
    *,
    name: str = "run-1",
    fasta: str = ">protein-1\nMPEPTIDE\n",
    **updates: object,
) -> RunDeepKoalaInput:
    input_path = config.input_roots[0] / f"{name}.faa"
    input_path.write_text(fasta, encoding="ascii")
    values: dict[str, object] = {
        "fasta_path": str(input_path),
        "output_directory": str(config.output_roots[0] / name),
    }
    values.update(updates)
    return RunDeepKoalaInput(**values)  # pyright: ignore[reportArgumentType]


def _explicit_output_path(request: RunDeepKoalaInput) -> Path:
    assert request.output_directory is not None
    return Path(request.output_directory)


def _cuda_ready_probe(
    *,
    checkout: Path,
    python_executable: Path,
    cpu_threads: int,
) -> RuntimeProbeResult:
    del checkout, python_executable, cpu_threads
    return RuntimeProbeResult(runtime_ready=True, cuda_available=True)


def _mps_ready_probe(
    *,
    checkout: Path,
    python_executable: Path,
    cpu_threads: int,
) -> RuntimeProbeResult:
    del checkout, python_executable, cpu_threads
    return RuntimeProbeResult(
        runtime_ready=True,
        cuda_available=False,
        mps_available=True,
    )


async def _wait_terminal(manager: DeepKoalaJobManager, job_id: str) -> JobState:
    async with asyncio.timeout(5):
        while True:
            state = (await manager.get_job(job_id)).job.state
            if state is not JobState.RUNNING:
                return state
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_run_starts_directly_and_publishes_stable_validated_handoff(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = SuccessfulRunner()
    request = _request(runtime_config, model="frag", model_date="202401", topk=2)
    async with _manager(runtime_config, runner) as manager:
        started = await manager.run(request)
        assert started.job.state is JobState.RUNNING
        assert started.plan.device == "cpu"
        assert started.plan.num_workers == 0
        assert started.plan.multi is False
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
        result = await manager.get_job(started.job.job_id)
        assert result.handoff is not None
        handoff = result.handoff
        annotations = Path(handoff.annotations_path)
        report = Path(handoff.report_path)
        assert annotations.name == ANNOTATIONS_FILENAME
        assert report.name == RUN_REPORT_FILENAME
        assert annotations.read_bytes() == DETAILED_CSV
        report_text = report.read_text(encoding="utf-8")
        assert str(Path(request.fasta_path).resolve()) in report_text
        assert "- Device policy: `cpu`" in report_text
        assert stat.S_IMODE(annotations.stat().st_mode) == 0o600
        assert handoff.schema_version == "1"
        assert handoff.tool_version == "0.4.0"
        assert handoff.input_path == request.fasta_path
        assert handoff.source.input_path == request.fasta_path
        assert handoff.source.model_name == "frag"
        assert handoff.source.model_version == "202401"
        assert handoff.source.annotation_date.utcoffset() is not None
        metadata = {field.name: field.value for field in handoff.source.source_metadata}
        assert metadata["device_requested"] == "cpu"
        assert metadata["cuda_available_at_preflight"] is False
        assert metadata["mps_available_at_preflight"] is False
        assert "sha" not in handoff.model_dump_json().lower()


@pytest.mark.asyncio
async def test_run_accepts_valid_fasta_larger_than_the_removed_byte_cap(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    fasta = "".join(f">protein-{index}\n{'M' * 100_000}\n" for index in range(51))
    runner = SuccessfulRunner()
    request = _request(runtime_config, name="large-fasta", fasta=fasta)

    async with _manager(runtime_config, runner) as manager:
        started = await manager.run(request)
        assert started.fasta.input_bytes > 5_000_000
        assert started.fasta.total_residues == 5_100_000
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED

    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_cancelled_intake_joins_worker_before_cleanup_and_next_run(
    runtime_config: DeepKoalaRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    intake_started = threading.Event()
    release_intake = threading.Event()
    call_lock = threading.Lock()
    call_count = 0
    real_stage = fasta_module.stage_fasta

    def blocking_stage(
        *,
        fasta_path: str,
        input_roots: tuple[Path, ...],
        job_directory: Path,
        max_sequences: int,
    ) -> StagedFasta:
        nonlocal call_count
        with call_lock:
            call_count += 1
        intake_started.set()
        if not release_intake.wait(timeout=5):
            raise RuntimeError("test intake release timed out")
        return real_stage(
            fasta_path=fasta_path,
            input_roots=input_roots,
            job_directory=job_directory,
            max_sequences=max_sequences,
        )

    monkeypatch.setattr(fasta_module, "stage_fasta", blocking_stage)
    runner = SuccessfulRunner()
    first_request = _request(runtime_config, name="cancelled-intake")
    second_request = _request(runtime_config, name="after-cancelled-intake")

    async with _manager(runtime_config, runner) as manager:
        first_task = asyncio.create_task(manager.run(first_request))
        second_task: asyncio.Task[RunDeepKoalaResult] | None = None
        try:
            assert await asyncio.to_thread(intake_started.wait, 2)
            first_task.cancel()
            await asyncio.sleep(0.05)
            assert not first_task.done()
            first_task.cancel()
            second_task = asyncio.create_task(manager.run(second_request))
            await asyncio.sleep(0.05)
            with call_lock:
                assert call_count == 1

            release_intake.set()
            with pytest.raises(asyncio.CancelledError):
                await first_task
            second = await asyncio.wait_for(second_task, timeout=5)
            assert await _wait_terminal(manager, second.job.job_id) is JobState.SUCCEEDED
            assert not _explicit_output_path(first_request).exists()
            with call_lock:
                assert call_count == 2
        finally:
            release_intake.set()
            pending = tuple(
                task for task in (first_task, second_task) if task is not None and not task.done()
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_explicit_cuda_request_reaches_the_execution_plan(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"allowed_devices": ("cpu", "cuda")})
    runner = SuccessfulRunner()
    async with _manager(config, runner, runtime_probe=_cuda_ready_probe) as manager:
        started = await manager.run(_request(config, name="cuda", device="cuda"))
        assert started.plan.device == "cuda"
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
        assert runner.calls[0].device == "cuda"
        result = await manager.get_job(started.job.job_id)
        assert result.handoff is not None
        metadata = {field.name: field.value for field in result.handoff.source.source_metadata}
        assert metadata["device_requested"] == "cuda"
        assert metadata["cuda_available_at_preflight"] is True
        assert metadata["mps_available_at_preflight"] is False
        assert "- Device policy: `cuda`" in Path(result.handoff.report_path).read_text(
            encoding="utf-8"
        )


@pytest.mark.asyncio
async def test_cuda_request_requires_available_runtime_without_staging(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"allowed_devices": ("cpu", "cuda")})
    runner = SuccessfulRunner()
    request = _request(config, name="cuda-unavailable", device="cuda")
    async with _manager(config, runner) as manager:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(request)

    assert captured.value.detail.code is ErrorCode.RUNTIME_UNAVAILABLE
    assert not _explicit_output_path(request).exists()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_cuda_is_rechecked_before_runner_execution(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"allowed_devices": ("cpu", "cuda")})
    runner = SuccessfulRunner()
    probes = iter(
        (
            RuntimeProbeResult(runtime_ready=True, cuda_available=True),
            RuntimeProbeResult(runtime_ready=True, cuda_available=False),
        )
    )

    def changing_cuda_probe(**_: object) -> RuntimeProbeResult:
        return next(probes)

    async with _manager(config, runner, runtime_probe=changing_cuda_probe) as manager:
        started = await manager.run(_request(config, name="cuda-disappears", device="cuda"))
        assert await _wait_terminal(manager, started.job.job_id) is JobState.FAILED
        result = await manager.get_job(started.job.job_id)

    assert result.job.failure_reason == "CUDA became unavailable before DeepKOALA started."
    assert runner.calls == []


@pytest.mark.asyncio
async def test_explicit_mps_request_is_preflighted_rechecked_and_reported(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"allowed_devices": ("cpu", "mps")})
    runner = SuccessfulRunner()
    async with _manager(config, runner, runtime_probe=_mps_ready_probe) as manager:
        started = await manager.run(_request(config, name="mps", device="mps"))
        assert started.plan.device == "mps"
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
        result = await manager.get_job(started.job.job_id)

    assert runner.calls[0].device == "mps"
    assert result.handoff is not None
    metadata = {field.name: field.value for field in result.handoff.source.source_metadata}
    assert metadata["device_requested"] == "mps"
    assert metadata["cuda_available_at_preflight"] is False
    assert metadata["mps_available_at_preflight"] is True
    report = Path(result.handoff.report_path).read_text(encoding="utf-8")
    assert "- Device policy: `mps`" in report
    assert "- MPS available at preflight: `true`" in report


@pytest.mark.asyncio
async def test_mps_request_requires_available_runtime_without_staging(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"allowed_devices": ("cpu", "mps")})
    runner = SuccessfulRunner()
    request = _request(config, name="mps-unavailable", device="mps")
    async with _manager(config, runner) as manager:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(request)

    assert captured.value.detail.code is ErrorCode.RUNTIME_UNAVAILABLE
    assert captured.value.detail.message == (
        "The requested MPS device is unavailable in the configured runtime."
    )
    assert not _explicit_output_path(request).exists()
    assert runner.calls == []


@pytest.mark.asyncio
async def test_mps_is_rechecked_before_runner_execution(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"allowed_devices": ("cpu", "mps")})
    runner = SuccessfulRunner()
    probes = iter(
        (
            RuntimeProbeResult(
                runtime_ready=True,
                cuda_available=False,
                mps_available=True,
            ),
            RuntimeProbeResult(
                runtime_ready=True,
                cuda_available=False,
                mps_available=False,
            ),
        )
    )

    def changing_mps_probe(**_: object) -> RuntimeProbeResult:
        return next(probes)

    async with _manager(config, runner, runtime_probe=changing_mps_probe) as manager:
        started = await manager.run(_request(config, name="mps-disappears", device="mps"))
        assert await _wait_terminal(manager, started.job.job_id) is JobState.FAILED
        result = await manager.get_job(started.job.job_id)

    assert result.job.failure_reason == "MPS became unavailable before DeepKOALA started."
    assert runner.calls == []


@pytest.mark.asyncio
async def test_delivered_files_survive_delete_and_server_close(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    manager = DeepKoalaJobManager(
        runtime_config,
        runner=SuccessfulRunner(),
        runtime_probe=ready_probe,
    )
    await manager.open()
    started = await manager.run(_request(runtime_config))
    assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
    result = await manager.get_job(started.job.job_id)
    assert result.handoff is not None
    annotations = Path(result.handoff.annotations_path)
    report = Path(result.handoff.report_path)
    deleted = await manager.delete(started.job.job_id)
    assert deleted.delivered_files_retained is True
    assert annotations.is_file() and report.is_file()
    with pytest.raises(DeepKoalaMcpError) as captured:
        await manager.get_job(started.job.job_id)
    assert captured.value.detail.code is ErrorCode.JOB_NOT_FOUND
    await manager.close()
    assert annotations.is_file() and report.is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_state"),
    [
        (b"not,a,detailed,csv\n1,2,3,4\n", JobState.FAILED),
        (b"name,predict_label,probability,threshold,annotate\n", JobState.FAILED),
    ],
)
async def test_malformed_or_empty_detailed_output_fails_and_cleans_stable_directory(
    runtime_config: DeepKoalaRuntimeConfig,
    payload: bytes,
    expected_state: JobState,
) -> None:
    request = _request(runtime_config)
    async with _manager(runtime_config, SuccessfulRunner(payload)) as manager:
        started = await manager.run(request)
        assert await _wait_terminal(manager, started.job.job_id) is expected_state
        assert not _explicit_output_path(request).exists()


@pytest.mark.asyncio
async def test_nonzero_exit_and_timeout_are_typed_and_clean_output(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    for name, runner, expected in (
        ("exit", ExitRunner(), JobState.FAILED),
        ("timeout", TimeoutRunner(), JobState.TIMED_OUT),
    ):
        request = _request(runtime_config, name=name)
        async with _manager(runtime_config, runner) as manager:
            started = await manager.run(request)
            assert await _wait_terminal(manager, started.job.job_id) is expected
            assert not _explicit_output_path(request).exists()


@pytest.mark.asyncio
async def test_cleanup_validation_failure_still_makes_the_job_terminal(
    runtime_config: DeepKoalaRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_cleanup(_output: object) -> None:
        raise OutputValidationError("controlled path changed")

    monkeypatch.setattr(jobs_module, "cleanup_output_directory", _fail_cleanup)
    async with _manager(runtime_config, ExitRunner()) as manager:
        first = await manager.run(_request(runtime_config, name="cleanup-failure-1"))
        assert await _wait_terminal(manager, first.job.job_id) is JobState.FAILED
        second = await manager.run(_request(runtime_config, name="cleanup-failure-2"))
        assert await _wait_terminal(manager, second.job.job_id) is JobState.FAILED


@pytest.mark.asyncio
async def test_path_and_nonempty_output_fail_before_runner_start(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    runner = SuccessfulRunner()
    outside = tmp_path / "outside.faa"
    outside.write_text(">p\nM\n", encoding="ascii")
    requests = [
        RunDeepKoalaInput(
            fasta_path=str(outside),
            output_directory=str(runtime_config.output_roots[0] / "outside-input"),
        ),
        _request(
            runtime_config,
            name="outside-output",
            output_directory=str(tmp_path / "unapproved" / "run"),
        ),
    ]
    expected = (ErrorCode.PATH_NOT_ALLOWED, ErrorCode.OUTPUT_NOT_ALLOWED)
    async with _manager(runtime_config, runner) as manager:
        for request, code in zip(requests, expected, strict=True):
            with pytest.raises(DeepKoalaMcpError) as captured:
                await manager.run(request)
            assert captured.value.detail.code is code
        existing = runtime_config.output_roots[0] / "existing"
        existing.mkdir(mode=0o700)
        (existing / "occupied").write_text("x", encoding="ascii")
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(_request(runtime_config, name="existing"))
        assert captured.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
        assert runner.calls == []


@pytest.mark.asyncio
async def test_existing_empty_output_is_accepted_and_retained_on_failure(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    successful_output = runtime_config.output_roots[0] / "existing-empty-success"
    successful_output.mkdir(mode=0o700)
    failed_output = runtime_config.output_roots[0] / "existing-empty-failure"
    failed_output.mkdir(mode=0o700)

    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        started = await manager.run(_request(runtime_config, name="existing-empty-success"))
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
    assert (successful_output / ANNOTATIONS_FILENAME).is_file()
    assert (successful_output / RUN_REPORT_FILENAME).is_file()

    async with _manager(runtime_config, ExitRunner()) as manager:
        started = await manager.run(_request(runtime_config, name="existing-empty-failure"))
        assert await _wait_terminal(manager, started.job.job_id) is JobState.FAILED
    assert failed_output.is_dir()
    assert tuple(failed_output.iterdir()) == ()


@pytest.mark.asyncio
async def test_late_failure_removes_only_published_artifacts_from_adopted_directory(
    runtime_config: DeepKoalaRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = runtime_config.output_roots[0] / "adopted-late-failure"
    output.mkdir(mode=0o700)
    original_remove = jobs_module.remove_job_directory
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("synthetic private cleanup failure")
        original_remove(*args, **kwargs)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(jobs_module, "remove_job_directory", fail_once)
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        started = await manager.run(_request(runtime_config, name="adopted-late-failure"))
        assert await _wait_terminal(manager, started.job.job_id) is JobState.FAILED

    assert output.is_dir()
    assert tuple(output.iterdir()) == ()


@pytest.mark.asyncio
async def test_omitted_output_directory_allocates_a_fresh_configured_child(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    default_output_root = runtime_config.output_roots[0].parent / "default-output"
    default_output_root.mkdir(mode=0o700)
    config = runtime_config.model_copy(
        update={"output_roots": (*runtime_config.output_roots, default_output_root)}
    )
    input_path = runtime_config.input_roots[0] / "allocated.faa"
    input_path.write_text(">protein\nMPEPTIDE\n", encoding="ascii")
    request = RunDeepKoalaInput(fasta_path=str(input_path))

    async with _manager(config, SuccessfulRunner()) as manager:
        started = await manager.run(request)
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
        result = await manager.get_job(started.job.job_id)

    assert result.handoff is not None
    output = Path(result.handoff.annotations_path).parent
    assert output.parent == default_output_root
    assert output.name.startswith("deepkoala-")
    assert (output / ANNOTATIONS_FILENAME).is_file()
    assert (output / RUN_REPORT_FILENAME).is_file()


@pytest.mark.asyncio
async def test_model_timeout_and_device_policy_are_enforced_without_staging(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = SuccessfulRunner()
    config = runtime_config.model_copy(
        update={"allowed_models": ("frag",), "allowed_devices": ("cpu",)}
    )
    async with _manager(config, runner) as manager:
        for request in (
            _request(config, name="model", model="full"),
            _request(config, name="timeout", model="frag", timeout_seconds=31),
            _request(config, name="device", model="frag", device="cuda"),
        ):
            with pytest.raises(DeepKoalaMcpError) as captured:
                await manager.run(request)
            assert captured.value.detail.code is ErrorCode.POLICY_DENIED
        assert runner.calls == []


@pytest.mark.asyncio
async def test_single_runner_busy_then_cancel_waits_for_cleanup(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = BlockingRunner()
    async with _manager(runtime_config, runner) as manager:
        first = await manager.run(_request(runtime_config, name="first"))
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        second = _request(runtime_config, name="second")
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(second)
        assert captured.value.detail.code is ErrorCode.RUNNER_BUSY
        assert not _explicit_output_path(second).exists()
        cancelled = await manager.cancel(first.job.job_id)
        assert cancelled.state is JobState.CANCELLED
        assert runner.cancelled.is_set()
        assert not Path(_request_output(runtime_config, "first")).exists()
        assert (await manager.cancel(first.job.job_id)).state is JobState.CANCELLED


@pytest.mark.asyncio
async def test_shared_state_root_isolates_jobs_and_enforces_one_runner(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    blocking = BlockingRunner()
    first_manager = DeepKoalaJobManager(
        runtime_config,
        runner=blocking,
        runtime_probe=ready_probe,
    )
    second_manager = DeepKoalaJobManager(
        runtime_config,
        runner=SuccessfulRunner(),
        runtime_probe=ready_probe,
    )
    await first_manager.open()
    await second_manager.open()
    first_closed = False
    try:
        first = await first_manager.run(_request(runtime_config, name="shared-first"))
        await asyncio.wait_for(blocking.started.wait(), timeout=2)
        second_request = _request(runtime_config, name="shared-second")
        with pytest.raises(DeepKoalaMcpError) as captured:
            await second_manager.run(second_request)
        assert captured.value.detail.code is ErrorCode.RUNNER_BUSY
        assert not _explicit_output_path(second_request).exists()

        assert (await first_manager.cancel(first.job.job_id)).state is JobState.CANCELLED
        second = await second_manager.run(second_request)
        assert await _wait_terminal(second_manager, second.job.job_id) is JobState.SUCCEEDED

        with pytest.raises(DeepKoalaMcpError) as captured:
            await second_manager.get_job(first.job.job_id)
        assert captured.value.detail.code is ErrorCode.JOB_NOT_FOUND
        with pytest.raises(DeepKoalaMcpError) as captured:
            await first_manager.get_job(second.job.job_id)
        assert captured.value.detail.code is ErrorCode.JOB_NOT_FOUND

        await first_manager.close()
        first_closed = True
        assert (await second_manager.get_job(second.job.job_id)).job.state is JobState.SUCCEEDED
    finally:
        if not first_closed:
            await first_manager.close()
        await second_manager.close()


@pytest.mark.asyncio
async def test_symlink_runner_output_is_rejected_without_touching_target(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target.csv"
    target.write_bytes(DETAILED_CSV)
    request = _request(runtime_config)
    async with _manager(runtime_config, SymlinkOutputRunner(target)) as manager:
        started = await manager.run(request)
        assert await _wait_terminal(manager, started.job.job_id) is JobState.FAILED
        assert target.read_bytes() == DETAILED_CSV
        assert not _explicit_output_path(request).exists()


@pytest.mark.asyncio
async def test_status_is_redacted_and_reports_runtime_and_policy(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        status = await manager.status()
        assert status.ready is True
        assert status.runtime_ready is True
        assert status.cuda_available is False
        assert status.mps_available is False
        assert status.allowed_models == ("full", "frag")
        assert status.device_policy == "cpu"
        assert status.allowed_devices == ("cpu",)
        assert status.max_concurrent_jobs == 1
        assert status.allow_multi is False
        assert status.multi_ready is False
        assert status.route_state == "local_ready"
        assert status.issue is None
        assert status.max_input_bytes is None
        assert status.max_sequences == runtime_config.max_sequences
        serialized = status.model_dump_json()
        assert str(runtime_config.checkout) not in serialized
        assert str(runtime_config.state_root) not in serialized


def _multi_ready_probe(
    *,
    checkout: Path,
    python_executable: Path,
    cpu_threads: int,
) -> RuntimeProbeResult:
    del checkout, python_executable, cpu_threads
    return RuntimeProbeResult(
        runtime_ready=True,
        cuda_available=False,
        multi_adapter_compatible=True,
    )


def _multi_config(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
    *,
    executable_body: str = "#!/bin/sh\nexit 0\n",
) -> DeepKoalaRuntimeConfig:
    profiles = tmp_path / "profiles"
    profiles.mkdir(mode=0o700)
    (profiles / "K00001.hmm").write_text("HMMER3/f\n", encoding="ascii")
    executable = tmp_path / "hmmsearch"
    executable.write_text(executable_body, encoding="utf-8")
    executable.chmod(0o700)
    return runtime_config.model_copy(
        update={
            "allow_multi": True,
            "profiles_dir": profiles,
            "hmmsearch_executable": executable,
        }
    )


@pytest.mark.asyncio
async def test_multi_run_requires_ready_deployment_and_records_effective_mode(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    config = _multi_config(runtime_config, tmp_path)
    payload = (
        b"name,predict_label,probability,threshold,start,end,annotate\n"
        b"protein-1,K00001,0.95,0.50,1,8,*\n"
    )
    runner = SuccessfulRunner(payload)
    async with _manager(config, runner, runtime_probe=_multi_ready_probe) as manager:
        status = await manager.status()
        assert status.ready is True
        assert status.allow_multi is True
        assert status.multi_ready is True
        assert status.route_state == "local_ready"

        started = await manager.run(_request(config, name="multi", multi=True))
        assert started.plan.multi is True
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
        result = await manager.get_job(started.job.job_id)
        assert result.handoff is not None
        metadata = {field.name: field.value for field in result.handoff.source.source_metadata}
        assert metadata["multi"] is True
        assert "Multi-domain mode: `true`" in Path(result.handoff.report_path).read_text(
            encoding="utf-8"
        )
        assert runner.calls[0].multi is True
        assert runner.calls[0].profiles_dir == config.profiles_dir
        assert runner.calls[0].hmmsearch_executable == config.hmmsearch_executable


@pytest.mark.asyncio
async def test_enabled_but_unavailable_multi_has_distinct_route_and_keeps_base_ready(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    config = _multi_config(
        runtime_config,
        tmp_path,
        executable_body="#!/bin/sh\nexit 1\n",
    )
    runner = SuccessfulRunner()
    async with _manager(config, runner, runtime_probe=_multi_ready_probe) as manager:
        status = await manager.status()
        assert status.ready is True
        assert status.allow_multi is True
        assert status.multi_ready is False
        assert status.route_state == "multi_dependencies_unavailable"
        assert status.issue is not None

        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(_request(config, name="unavailable-multi", multi=True))
        assert captured.value.detail.code is ErrorCode.RUNTIME_UNAVAILABLE

        normal = await manager.run(_request(config, name="normal-fallback"))
        assert await _wait_terminal(manager, normal.job.job_id) is JobState.SUCCEEDED


@pytest.mark.asyncio
async def test_multi_request_is_denied_when_deployment_did_not_enable_it(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(_request(runtime_config, name="multi-denied", multi=True))
        assert captured.value.detail.code is ErrorCode.POLICY_DENIED


@pytest.mark.asyncio
async def test_multi_request_rejects_an_ineffective_batch_size(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    config = _multi_config(runtime_config, tmp_path)
    async with _manager(config, SuccessfulRunner(), runtime_probe=_multi_ready_probe) as manager:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(_request(config, name="multi-batch", multi=True, batch_size=2))
        assert captured.value.detail.code is ErrorCode.POLICY_DENIED


@pytest.mark.asyncio
async def test_real_preflight_rejects_bin_false_runtime(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"python_executable": Path("/bin/false")})
    manager = DeepKoalaJobManager(config, runner=SuccessfulRunner())
    await manager.open()
    try:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.run(_request(config))
        assert captured.value.detail.code is ErrorCode.RUNTIME_UNAVAILABLE
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_missing_delivered_file_becomes_artifact_error(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        started = await manager.run(_request(runtime_config))
        assert await _wait_terminal(manager, started.job.job_id) is JobState.SUCCEEDED
        result = await manager.get_job(started.job.job_id)
        assert result.handoff is not None
        Path(result.handoff.annotations_path).unlink()
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.get_job(started.job.job_id)
        assert captured.value.detail.code is ErrorCode.ARTIFACT_NOT_FOUND


def _request_output(config: DeepKoalaRuntimeConfig, name: str) -> Path:
    return config.output_roots[0] / name
