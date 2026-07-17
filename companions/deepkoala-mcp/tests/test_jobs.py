"""Single-state job lifecycle, queue, handoff, and cleanup tests."""

from __future__ import annotations

import asyncio
import stat
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.contracts import (
    MAX_OUTPUT_BYTES,
    DeepKoalaMcpError,
    ErrorCode,
    JobState,
    PrepareDeepKoalaInput,
)
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan, RunnerTimedOutError


class SuccessfulRunner:
    def __init__(self, payload: bytes = b"opaque output for the core importer\n") -> None:
        self.payload = payload
        self.calls: list[RunnerPlan] = []

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        self.calls.append(plan)
        plan.output_path.write_bytes(self.payload)
        return ProcessOutcome(return_code=0)


class ExitRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        return ProcessOutcome(return_code=7)


class EmptyRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        plan.output_path.write_bytes(b"")
        plan.output_path.chmod(0o644)
        return ProcessOutcome(return_code=0)


class OversizedRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        plan.output_path.write_bytes(b"x" * (MAX_OUTPUT_BYTES + 1))
        return ProcessOutcome(return_code=0)


class TimeoutRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        raise RunnerTimedOutError


class UnexpectedRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        raise ValueError(f"private runner failure at {plan.checkout}")


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
        plan.output_path.write_bytes(b"bounded output\n")
        return ProcessOutcome(return_code=0)


class SlowCancellationRunner(BlockingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.cleanup_started = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_finished = asyncio.Event()

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cleanup_started.set()
            await self.cleanup_release.wait()
            self.cleanup_finished.set()
            raise
        raise AssertionError("runner should have been cancelled")


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
) -> AsyncGenerator[DeepKoalaJobManager]:
    manager = DeepKoalaJobManager(config, runner=runner)  # pyright: ignore[reportArgumentType]
    await manager.open()
    try:
        yield manager
    finally:
        await manager.close()


async def _wait_terminal(manager: DeepKoalaJobManager, job_id: str) -> JobState:
    async with asyncio.timeout(5):
        while True:
            state = (await manager.get_job(job_id)).job.state
            if state in {
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.TIMED_OUT,
            }:
                return state
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_prepare_stages_without_running_or_retaining_raw_request(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = SuccessfulRunner()
    secret = "private-header-never-retained"
    async with _manager(runtime_config, runner) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=f">{secret}\nMPEPTIDE\n"))
        assert prepared.state == "prepared"
        assert prepared.notice.plan.device == "auto"
        assert prepared.notice.gpu_visibility_inherited is True
        assert prepared.notice.plan.num_workers == 0
        assert prepared.notice.plan.resolved_model_date == "202502"
        assert prepared.notice.downloads_enabled is False
        assert runner.calls == []
        assert secret not in repr(vars(manager))


@pytest.mark.asyncio
async def test_success_returns_current_core_file_handoff_without_csv_interpretation(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    payload = b"this is deliberately not parsed by the companion\n"
    runner = SuccessfulRunner(payload)
    async with _manager(runtime_config, runner) as manager:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(
                fasta_text=">p\nMPEPTIDE\n",
                model="frag",
                model_date="202401",
                batch_size=3,
                topk=2,
            )
        )
        submitted = await manager.submit(prepared.job_id)
        assert submitted.state in {JobState.RUNNING, JobState.QUEUED}
        assert await _wait_terminal(manager, prepared.job_id) is JobState.SUCCEEDED

        result = await manager.get_job(prepared.job_id)
        assert result.handoff is not None
        handoff = result.handoff
        output = Path(handoff.output_path)
        assert output.read_bytes() == payload
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert handoff.input_format == "deepkoala_detailed"
        assert handoff.source.source_name == "deepkoala"
        assert handoff.source.model_name == "frag"
        assert handoff.source.model_version == "202401"
        assert handoff.source.input_path is None
        assert handoff.source.input_uri == (f"mcp://deepkoala-mcp/jobs/{prepared.job_id}/output")
        assert not (output.parent / "input.fasta").exists()
        assert str(output.parent / "input.fasta") not in handoff.model_dump_json()

        expected_core_fields = {
            "source_name",
            "source_version",
            "model_name",
            "model_version",
            "annotation_date",
            "input_uri",
            "input_path",
            "source_metadata",
        }
        assert set(handoff.source.model_dump(mode="json")) == expected_core_fields
        assert all(
            set(item.model_dump()) == {"name", "value"} for item in handoff.source.source_metadata
        )
        assert {item.name: item.value for item in handoff.source.source_metadata}[
            "device_requested"
        ] == "auto"


@pytest.mark.asyncio
async def test_path_input_preserves_original_fasta_provenance_separately(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    original = runtime_config.allowed_roots[0] / "proteins.faa"
    original.write_text(">p\nMPEPTIDE\n", encoding="ascii")
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_path=str(original)))
        await manager.submit(prepared.job_id)
        assert await _wait_terminal(manager, prepared.job_id) is JobState.SUCCEEDED

        result = await manager.get_job(prepared.job_id)
        assert result.handoff is not None
        handoff = result.handoff
        assert handoff.source.input_path == str(original.resolve())
        assert handoff.source.input_path != handoff.output_path
        assert handoff.source.input_uri == (f"mcp://deepkoala-mcp/jobs/{prepared.job_id}/output")
        assert (
            str(Path(handoff.output_path).parent / "input.fasta") not in handoff.model_dump_json()
        )


@pytest.mark.asyncio
async def test_status_is_redacted_and_reports_structural_resources(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        status = await manager.status()
        assert status.ready is True
        assert len(status.installed_resources) == 4
        assert status.cpu_threads == 2
        assert status.max_concurrent_jobs == 1
        assert status.max_input_bytes == 5_000_000
        assert status.max_output_bytes == 5_000_000
        assert status.device_policy == "auto"
        assert status.gpu_visibility_inherited is True
        serialized = status.model_dump_json()
        assert str(runtime_config.checkout) not in serialized
        assert str(runtime_config.state_root) not in serialized


@pytest.mark.asyncio
async def test_status_fails_closed_before_unbounded_resource_enumeration(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    resources = runtime_config.checkout / "resources"
    for index in range(127):
        (resources / f"entry-{index:03d}").mkdir()
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        status = await manager.status()
        assert status.ready is False
        assert status.installed_resources == ()


@pytest.mark.asyncio
async def test_expired_get_is_read_only_and_next_mutation_sweeps_state(
    runtime_config: DeepKoalaRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [datetime(2026, 7, 16, tzinfo=UTC)]
    monkeypatch.setattr("deepkoala_mcp.jobs._now", lambda: clock[0])
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">old\nM\n"))
        staged = next(runtime_config.state_root.rglob("input.fasta"))
        clock[0] += timedelta(seconds=runtime_config.plan_ttl_seconds + 1)
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.get_job(prepared.job_id)
        assert captured.value.detail.code is ErrorCode.JOB_NOT_FOUND
        assert staged.is_file()

        await manager.prepare(PrepareDeepKoalaInput(fasta_text=">new\nM\n"))
        assert not staged.exists()


@pytest.mark.asyncio
async def test_missing_selected_weights_fails_before_retaining_job(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    (runtime_config.checkout / "resources" / "202502" / "weights_full.pt").unlink()
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.prepare(
                PrepareDeepKoalaInput(
                    fasta_text=">p\nM\n",
                    model="full",
                    model_date="202502",
                )
            )
        assert captured.value.detail.code is ErrorCode.WEIGHTS_NOT_FOUND


@pytest.mark.asyncio
async def test_disallowed_path_returns_bounded_error(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.faa"
    outside.write_text(">private\nM\n", encoding="ascii")
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.prepare(PrepareDeepKoalaInput(fasta_path=str(outside)))
        assert captured.value.detail.code is ErrorCode.PATH_NOT_ALLOWED
        assert "private" not in captured.value.detail.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner", "expected"),
    [
        (ExitRunner(), JobState.FAILED),
        (EmptyRunner(), JobState.FAILED),
        (OversizedRunner(), JobState.FAILED),
        (TimeoutRunner(), JobState.TIMED_OUT),
    ],
)
async def test_process_and_output_failures_are_terminal_and_cleaned(
    runtime_config: DeepKoalaRuntimeConfig,
    runner: object,
    expected: JobState,
) -> None:
    async with _manager(runtime_config, runner) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        await manager.submit(prepared.job_id)
        assert await _wait_terminal(manager, prepared.job_id) is expected
        result = await manager.get_job(prepared.job_id)
        assert result.handoff is None
        assert result.job.failure_reason is not None


@pytest.mark.asyncio
async def test_unexpected_background_failure_has_safe_correlation(
    runtime_config: DeepKoalaRuntimeConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with _manager(runtime_config, UnexpectedRunner()) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        await manager.submit(prepared.job_id)
        assert await _wait_terminal(manager, prepared.job_id) is JobState.FAILED
        result = await manager.get_job(prepared.job_id)
        assert result.job.correlation_id is not None
        assert result.job.failure_reason == (
            "The DeepKOALA process could not be started or completed safely."
        )
    diagnostic = capsys.readouterr().err
    assert result.job.correlation_id in diagnostic
    assert "stage=runner_execution" in diagnostic
    assert "type=ValueError" in diagnostic
    assert str(runtime_config.checkout) not in diagnostic


@pytest.mark.asyncio
async def test_symlink_output_is_rejected_without_deleting_target(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    async with _manager(runtime_config, SymlinkOutputRunner(target)) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        await manager.submit(prepared.job_id)
        assert await _wait_terminal(manager, prepared.job_id) is JobState.FAILED
        assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.asyncio
async def test_successful_output_replacement_is_rejected_at_handoff(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside.csv"
    target.write_text("outside", encoding="utf-8")
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        await manager.submit(prepared.job_id)
        assert await _wait_terminal(manager, prepared.job_id) is JobState.SUCCEEDED
        result = await manager.get_job(prepared.job_id)
        assert result.handoff is not None
        output = Path(result.handoff.output_path)
        output.unlink()
        output.symlink_to(target)
        with pytest.raises(RuntimeError, match="output"):
            await manager.get_job(prepared.job_id)
        assert target.read_text(encoding="utf-8") == "outside"


@pytest.mark.asyncio
async def test_queue_is_single_runner_and_bounded_before_new_staging(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    config = runtime_config.model_copy(update={"max_queue_size": 1})
    runner = BlockingRunner()
    async with _manager(config, runner) as manager:
        first = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">first\nM\n"))
        await manager.submit(first.job_id)
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        second = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">second\nM\n"))
        assert (await manager.submit(second.job_id)).state is JobState.QUEUED
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.prepare(PrepareDeepKoalaInput(fasta_text=">third\nM\n"))
        assert captured.value.detail.code is ErrorCode.QUEUE_FULL
        runner.release.set()
        assert await _wait_terminal(manager, first.job_id) is JobState.SUCCEEDED
        assert await _wait_terminal(manager, second.job_id) is JobState.SUCCEEDED


@pytest.mark.asyncio
async def test_cancel_running_job_waits_for_runner_cleanup(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = BlockingRunner()
    async with _manager(runtime_config, runner) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        await manager.submit(prepared.job_id)
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        cancelled = await manager.cancel(prepared.job_id)
        assert cancelled.state is JobState.CANCELLED
        assert runner.cancelled.is_set()
        assert (await manager.cancel(prepared.job_id)).state is JobState.CANCELLED


@pytest.mark.asyncio
async def test_concurrent_cancel_does_not_interrupt_owned_cleanup(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = SlowCancellationRunner()
    async with _manager(runtime_config, runner) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        await manager.submit(prepared.job_id)
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        first = asyncio.create_task(manager.cancel(prepared.job_id))
        await asyncio.wait_for(runner.cleanup_started.wait(), timeout=2)
        second = asyncio.create_task(manager.cancel(prepared.job_id))
        await asyncio.sleep(0)
        runner.cleanup_release.set()
        results = await asyncio.gather(first, second)
        assert {result.state for result in results} == {JobState.CANCELLED}
        assert runner.cleanup_finished.is_set()


@pytest.mark.asyncio
async def test_cancel_prepared_then_delete_and_second_delete_is_not_idempotent(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        assert (await manager.cancel(prepared.job_id)).state is JobState.CANCELLED
        assert (await manager.delete(prepared.job_id)).deleted is True
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.delete(prepared.job_id)
        assert captured.value.detail.code is ErrorCode.JOB_NOT_FOUND


@pytest.mark.asyncio
async def test_concurrent_delete_has_one_atomic_winner(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        await manager.cancel(prepared.job_id)
        outcomes = await asyncio.gather(
            manager.delete(prepared.job_id),
            manager.delete(prepared.job_id),
            return_exceptions=True,
        )
        assert sum(not isinstance(item, BaseException) for item in outcomes) == 1
        errors = [item for item in outcomes if isinstance(item, DeepKoalaMcpError)]
        assert len(errors) == 1
        assert errors[0].detail.code is ErrorCode.JOB_NOT_FOUND


@pytest.mark.asyncio
async def test_delete_rejects_nonterminal_job(runtime_config: DeepKoalaRuntimeConfig) -> None:
    async with _manager(runtime_config, SuccessfulRunner()) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.delete(prepared.job_id)
        assert captured.value.detail.code is ErrorCode.NOT_TERMINAL


@pytest.mark.asyncio
async def test_submit_is_idempotent_for_one_retained_job(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = BlockingRunner()
    async with _manager(runtime_config, runner) as manager:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
        first = await manager.submit(prepared.job_id)
        second = await manager.submit(prepared.job_id)
        assert first.job_id == second.job_id
        assert second.state is JobState.RUNNING
        await manager.cancel(prepared.job_id)


@pytest.mark.asyncio
async def test_state_root_is_exclusive_and_recovers_after_close(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    first = DeepKoalaJobManager(runtime_config, runner=SuccessfulRunner())
    second = DeepKoalaJobManager(runtime_config, runner=SuccessfulRunner())
    await first.open()
    try:
        with pytest.raises(ValueError, match="already active"):
            await second.open()
    finally:
        await first.close()
    await second.open()
    await second.close()


@pytest.mark.asyncio
async def test_open_cleans_only_strict_abandoned_sessions(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runtime_config.state_root.mkdir(mode=0o700)
    abandoned = runtime_config.state_root / ("session_" + "a" * 32)
    abandoned.mkdir(mode=0o700)
    job = abandoned / ("job_" + "b" * 32)
    job.mkdir(mode=0o700)
    staged = job / "input.fasta"
    staged.write_text(">p\nM\n", encoding="ascii")
    staged.chmod(0o600)
    unrelated = runtime_config.state_root / "operator-note"
    unrelated.write_text("keep", encoding="utf-8")

    manager = DeepKoalaJobManager(runtime_config, runner=SuccessfulRunner())
    await manager.open()
    try:
        assert not abandoned.exists()
        assert unrelated.read_text(encoding="utf-8") == "keep"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_open_rejects_symlinked_abandoned_session_without_touching_target(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    runtime_config.state_root.mkdir(mode=0o700)
    target = tmp_path / "outside"
    target.mkdir()
    marker = target / "keep"
    marker.write_text("private", encoding="utf-8")
    (runtime_config.state_root / ("session_" + "a" * 32)).symlink_to(
        target, target_is_directory=True
    )

    manager = DeepKoalaJobManager(runtime_config, runner=SuccessfulRunner())
    with pytest.raises(ValueError, match="abandoned session"):
        await manager.open()
    assert marker.read_text(encoding="utf-8") == "private"


@pytest.mark.asyncio
async def test_close_cancels_runner_and_removes_complete_session(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = BlockingRunner()
    manager = DeepKoalaJobManager(runtime_config, runner=runner)
    await manager.open()
    prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))
    await manager.submit(prepared.job_id)
    await asyncio.wait_for(runner.started.wait(), timeout=2)
    assert any(runtime_config.state_root.iterdir())
    await manager.close()
    assert runner.cancelled.is_set()
    assert {path.name for path in runtime_config.state_root.iterdir()} == {".deepkoala.lock"}


@pytest.mark.asyncio
async def test_close_reports_cleanup_failure_and_retains_state_for_retry(
    runtime_config: DeepKoalaRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeepKoalaJobManager(runtime_config, runner=SuccessfulRunner())
    await manager.open()
    await manager.prepare(PrepareDeepKoalaInput(fasta_text=">p\nM\n"))

    def fail_cleanup(_: Path) -> None:
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr("deepkoala_mcp.jobs.remove_session_directory", fail_cleanup)
    with pytest.raises(RuntimeError, match="session cleanup"):
        await manager.close()
    assert any(runtime_config.state_root.iterdir())

    monkeypatch.undo()
    await manager.close()
    assert {path.name for path in runtime_config.state_root.iterdir()} == {".deepkoala.lock"}
