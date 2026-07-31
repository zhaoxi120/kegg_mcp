from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn

from deepkoala_mcp import __version__
from deepkoala_mcp._job_types import JobRecord as _JobRecord
from deepkoala_mcp._job_types import Runner, RuntimeProbe
from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.contracts import (
    ANNOTATIONS_FILENAME,
    MAX_RESOURCE_PAGE_BYTES,
    MAX_RETAINED_JOBS,
    RUN_REPORT_FILENAME,
    CompanionStatus,
    DeleteDeepKoalaJobResult,
    ErrorCode,
    ExecutionPlan,
    GetDeepKoalaJobResult,
    ImportHandoff,
    JobState,
    JobSummary,
    RunDeepKoalaInput,
    RunDeepKoalaResult,
    fail,
)
from deepkoala_mcp.fasta import (
    FastaLimitError,
    FastaValidationError,
    InputPathError,
    stage_fasta,
)
from deepkoala_mcp.installation import (
    Installation,
    InstallationError,
    RuntimeProbeResult,
    classify_readiness_route,
    fail_installation,
    fail_multi_unavailable,
    fail_runtime_unavailable,
    inspect_installation,
    probe_multi_dependencies_async,
    probe_runtime_async,
    select_installation,
)
from deepkoala_mcp.job_storage import (
    ArtifactSlice,
    ControlledOutputDirectory,
    OutputAlreadyExistsError,
    OutputPathError,
    OutputValidationError,
    StateSession,
    artifact_size,
    cleanup_output_directory,
    close_output_directory,
    close_state_session,
    create_output_directory,
    open_state_session,
    publish_artifacts,
    read_artifact_slice,
    release_runner_lock,
    remove_job_directory,
    try_acquire_runner_lock,
    validate_delivered_artifacts,
)
from deepkoala_mcp.reporting import build_handoff, build_run_report
from deepkoala_mcp.runner import (
    DeepKoalaProcessRunner,
    RunnerPlan,
    RunnerTimedOutError,
)

ArtifactName = Literal["annotations", "report"]
_TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.TIMED_OUT})


class DeepKoalaJobManager:
    def __init__(
        self,
        config: DeepKoalaRuntimeConfig,
        *,
        runner: Runner | None = None,
        runtime_probe: RuntimeProbe | None = None,
    ) -> None:
        self.config = config
        self._runner = runner or DeepKoalaProcessRunner()
        self._runtime_probe = runtime_probe
        self._lifecycle_lock = asyncio.Lock()
        self._run_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._jobs: dict[str, _JobRecord] = {}
        self._state_session: StateSession | None = None
        self._opened = False
        self._closing = False

    async def open(self) -> None:
        async with self._lifecycle_lock:
            if self._opened:
                return
            self._state_session = open_state_session(self.config.state_root)
            self._opened = True
            self._closing = False

    async def close(self) -> None:
        async with self._lifecycle_lock, self._run_lock:
            async with self._lock:
                if not self._opened:
                    return
                self._closing = True
                tasks = tuple(
                    record.task
                    for record in self._jobs.values()
                    if record.task is not None and not record.task.done()
                )
                for record in self._jobs.values():
                    if record.task is not None and not record.task.done():
                        record.cancel_requested = True
                        record.task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            async with self._lock:
                for record in self._jobs.values():
                    self._release_record_runner(record)
                    close_output_directory(record.output_directory)
            state_session = self._state_session
            cleanup_error: Exception | None = None
            try:
                if state_session is None:
                    raise RuntimeError("private state session is unavailable during close")
                close_state_session(state_session)
            except (OSError, ValueError, OutputValidationError) as error:
                cleanup_error = error
            async with self._lock:
                self._jobs.clear()
                self._state_session = None
                self._opened = False
                self._closing = False
            if cleanup_error is not None:
                raise RuntimeError("private session cleanup failed") from cleanup_error

    async def run(self, request: RunDeepKoalaInput) -> RunDeepKoalaResult:
        self._require_open()
        async with self._run_lock:
            self._require_open()
            async with self._lock:
                if self._closing:
                    raise RuntimeError("job manager is closing")
                if any(record.state is JobState.RUNNING for record in self._jobs.values()):
                    fail(
                        ErrorCode.RUNNER_BUSY,
                        "The deployment-wide DeepKOALA runner is already active.",
                        suggested_action="Wait for the running job to finish or cancel it.",
                    )
                self._prune_terminal_locked()
            self.config.validate_run_request(request)
            installation = self._select_installation(request.model, request.model_date)
            runtime = await self._probe_runtime()
            if not runtime.runtime_ready:
                fail_runtime_unavailable()
            if request.device == "cuda" and not runtime.cuda_available:
                fail_runtime_unavailable(cuda_requested=True)
            if request.device == "mps" and not runtime.mps_available:
                fail_runtime_unavailable(mps_requested=True)
            if request.multi and not await self._multi_ready(runtime):
                fail_multi_unavailable()

            timeout = request.timeout_seconds or self.config.max_timeout_seconds
            plan = ExecutionPlan(
                model=request.model,
                requested_model_date=request.model_date,
                resolved_model_date=installation.resource.model_date,
                device=request.device,
                batch_size=request.batch_size,
                topk=request.topk,
                multi=request.multi,
                cpu_threads=self.config.cpu_threads,
                timeout_seconds=timeout,
            )
            state_session = self._require_state_session()
            job_id = f"job_{secrets.token_hex(16)}"
            session = state_session.session
            directory = session / job_id
            try:
                runner_lock_fd = try_acquire_runner_lock(state_session)
            except (OSError, ValueError) as error:
                _raise_internal("runner_lock_acquisition", error)
            if runner_lock_fd is None:
                fail(
                    ErrorCode.RUNNER_BUSY,
                    "The deployment-wide DeepKOALA runner is already active.",
                    suggested_action="Wait for the running job to finish or cancel it.",
                )
            output_directory: ControlledOutputDirectory | None = None
            directory_created = False
            started = False
            try:
                os.mkdir(directory, mode=0o700)
                os.chmod(directory, 0o700)
                directory_created = True
                staged = stage_fasta(
                    fasta_path=request.fasta_path,
                    input_roots=self.config.input_roots,
                    job_directory=directory,
                    max_bytes=self.config.max_fasta_bytes,
                    max_sequences=self.config.max_sequences,
                )
                requested_output = (
                    Path(request.output_directory)
                    if request.output_directory is not None
                    else self.config.output_roots[-1] / f"deepkoala-{job_id.removeprefix('job_')}"
                )
                output_directory = create_output_directory(
                    requested_output,
                    self.config.output_roots,
                )
                record = _JobRecord(
                    job_id=job_id,
                    directory=directory,
                    output_directory=output_directory,
                    input_path=staged.input_path,
                    source_version=installation.source_version,
                    plan=plan,
                    fasta=staged.summary,
                    started_at=_now(),
                    runner_lock_fd=runner_lock_fd,
                )
                async with self._lock:
                    if self._closing:
                        raise RuntimeError("job manager is closing")
                    self._jobs[job_id] = record
                    record.task = asyncio.create_task(self._execute(record))
                    started = True
                    runner_lock_fd = None
                return RunDeepKoalaResult(
                    job=self._summary(record),
                    plan=record.plan,
                    fasta=record.fasta,
                )
            except OutputAlreadyExistsError:
                fail(
                    ErrorCode.OUTPUT_ALREADY_EXISTS,
                    "The requested output directory exists and is not empty.",
                    suggested_action=(
                        "Choose a new or empty owner-only output directory for this run."
                    ),
                )
            except OutputPathError:
                fail(
                    ErrorCode.OUTPUT_NOT_ALLOWED,
                    "The requested output directory is outside the deployment policy.",
                    suggested_action=(
                        "Choose a new or empty owner-only directory below a configured output root."
                    ),
                )
            except InputPathError:
                fail(
                    ErrorCode.PATH_NOT_ALLOWED,
                    "The FASTA path is unavailable or outside the deployment policy.",
                    suggested_action="Use a direct readable file below a configured input root.",
                )
            except FastaLimitError:
                fail(
                    ErrorCode.INPUT_LIMIT_EXCEEDED,
                    "The FASTA exceeds a configured input limit.",
                    suggested_action=(
                        "Split the input or ask the operator to review deployment bounds."
                    ),
                )
            except FastaValidationError:
                fail(
                    ErrorCode.INVALID_FASTA,
                    "The input is not a valid bounded protein FASTA.",
                    suggested_action="Correct the protein FASTA and retry with the same path.",
                )
            finally:
                if not started:
                    try:
                        async with self._lock:
                            self._jobs.pop(job_id, None)
                        rollback_error: Exception | None = None
                        if output_directory is not None:
                            try:
                                cleanup_output_directory(output_directory)
                            except (OSError, ValueError, OutputValidationError) as error:
                                rollback_error = error
                            finally:
                                close_output_directory(output_directory)
                        if directory_created:
                            try:
                                remove_job_directory(directory, state_session)
                            except (OSError, ValueError) as error:
                                rollback_error = rollback_error or error
                        if rollback_error is not None:
                            _raise_internal("atomic_run_rollback", rollback_error)
                    finally:
                        if runner_lock_fd is not None:
                            release_runner_lock(runner_lock_fd)

    async def get_job(self, job_id: str) -> GetDeepKoalaJobResult:
        self._require_open()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            result = GetDeepKoalaJobResult(job=self._summary(record), handoff=record.handoff)
            if result.handoff is not None:
                try:
                    validate_delivered_artifacts(
                        record.output_directory,
                        max_output_bytes=self.config.max_output_bytes,
                    )
                except OutputValidationError:
                    _fail_artifact_not_found()
        return result

    async def cancel(self, job_id: str) -> JobSummary:
        self._require_open()
        task: asyncio.Task[None] | None = None
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            if record.state is JobState.CANCELLED:
                return self._summary(record)
            if record.state is not JobState.RUNNING:
                fail(
                    ErrorCode.JOB_NOT_CANCELLABLE,
                    "The job is already terminal and cannot be cancelled.",
                    suggested_action="Read or delete the terminal job record instead.",
                )
            task = record.task
            if task is not None and not record.cancel_requested:
                record.cancel_requested = True
                task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                _fail_job_not_found()
            return self._summary(current)

    async def delete(self, job_id: str) -> DeleteDeepKoalaJobResult:
        self._require_open()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            if record.state not in _TERMINAL:
                fail(
                    ErrorCode.NOT_TERMINAL,
                    "Only a terminal DeepKOALA job record can be deleted.",
                    suggested_action="Cancel the job or wait for it to finish.",
                )
            removed = self._jobs.pop(job_id)
            close_output_directory(removed.output_directory)
        return DeleteDeepKoalaJobResult(job_id=job_id)

    async def status(self) -> CompanionStatus:
        self._require_open()
        checkout_ready = False
        try:
            version, installed = inspect_installation(self.config.checkout)
            checkout_ready = True
            resources = tuple(
                item for item in installed if item.model in self.config.allowed_models
            )
        except InstallationError:
            version, resources = None, ()
        try:
            runtime = await self._probe_runtime()
        except Exception:
            runtime = RuntimeProbeResult(
                runtime_ready=False,
                cuda_available=False,
                mps_available=False,
            )
        multi_ready = await self._multi_ready(runtime)
        route = classify_readiness_route(
            checkout_ready=checkout_ready,
            runtime_ready=runtime.runtime_ready,
            model_resources_ready=bool(resources),
            allow_multi=self.config.allow_multi,
            multi_ready=multi_ready,
        )
        async with self._lock:
            running = sum(record.state is JobState.RUNNING for record in self._jobs.values())
            terminal = sum(record.state in _TERMINAL for record in self._jobs.values())
        return CompanionStatus(
            server_version=__version__,
            ready=runtime.runtime_ready and bool(resources),
            runtime_ready=runtime.runtime_ready,
            cuda_available=runtime.cuda_available,
            mps_available=runtime.mps_available,
            deepkoala_version=version,
            installed_resources=resources,
            allowed_models=self.config.allowed_models,
            allowed_devices=self.config.allowed_devices,
            allow_multi=self.config.allow_multi,
            multi_ready=multi_ready,
            route_state=route.route_state,
            issue=route.issue,
            next_action=route.next_action,
            cpu_threads=self.config.cpu_threads,
            running_jobs=running,
            terminal_jobs=terminal,
            max_input_bytes=self.config.max_fasta_bytes,
            max_sequences=self.config.max_sequences,
            max_output_bytes=self.config.max_output_bytes,
            max_timeout_seconds=self.config.max_timeout_seconds,
            input_root_count=len(self.config.input_roots),
            output_root_count=len(self.config.output_roots),
        )

    async def artifact_size(self, job_id: str, artifact: ArtifactName) -> int:
        self._require_open()
        async with self._lock:
            record, name, maximum = self._artifact_access_locked(job_id, artifact)
            try:
                return artifact_size(record.output_directory, name, max_bytes=maximum)
            except OutputValidationError:
                _fail_artifact_not_found()

    async def read_artifact(
        self,
        job_id: str,
        artifact: ArtifactName,
        *,
        offset: int,
        limit: int,
    ) -> ArtifactSlice:
        self._require_open()
        async with self._lock:
            record, name, maximum = self._artifact_access_locked(job_id, artifact)
            try:
                return read_artifact_slice(
                    record.output_directory,
                    name,
                    max_bytes=maximum,
                    offset=offset,
                    limit=limit,
                )
            except OutputValidationError:
                _fail_artifact_not_found()

    def _artifact_access_locked(
        self,
        job_id: str,
        artifact: ArtifactName,
    ) -> tuple[_JobRecord, str, int]:
        record = self._jobs.get(job_id)
        if record is None or record.handoff is None:
            _fail_artifact_not_found()
        if artifact == "annotations":
            return record, ANNOTATIONS_FILENAME, self.config.max_output_bytes
        return record, RUN_REPORT_FILENAME, MAX_RESOURCE_PAGE_BYTES

    async def _execute(self, record: _JobRecord) -> None:
        try:
            await self._execute_job(record)
        finally:
            self._release_record_runner(record)

    async def _execute_job(self, record: _JobRecord) -> None:
        state = JobState.FAILED
        reason: str | None = "The DeepKOALA process did not produce a usable result."
        handoff: ImportHandoff | None = None
        output_bytes: int | None = None
        stage = "runtime_recheck"
        completed_at: datetime | None = None
        try:
            installation = self._select_installation(
                record.plan.model,
                record.plan.resolved_model_date,
            )
            runtime = await self._probe_runtime()
            if not runtime.runtime_ready:
                reason = "The configured DeepKOALA runtime became unavailable."
            elif record.plan.device == "cuda" and not runtime.cuda_available:
                reason = "CUDA became unavailable before DeepKOALA started."
            elif record.plan.device == "mps" and not runtime.mps_available:
                reason = "MPS became unavailable before DeepKOALA started."
            elif record.plan.multi and not await self._multi_ready(runtime):
                reason = "Configured multi-domain dependencies became unavailable."
            else:
                record.source_version = installation.source_version
                stage = "runner_execution"
                outcome = await self._runner.run(
                    RunnerPlan.from_execution_plan(
                        config=self.config,
                        job_directory=record.directory,
                        plan=record.plan,
                    )
                )
                record.exit_code = outcome.return_code
                if outcome.return_code != 0:
                    reason = "DeepKOALA exited without a successful detailed result."
                else:
                    stage = "artifact_publication"
                    completed_at = _now()
                    annotations, report, output_bytes = publish_artifacts(
                        raw_output=record.directory / "output.csv",
                        output_directory=record.output_directory,
                        report=build_run_report(
                            input_path=record.input_path,
                            source_version=record.source_version,
                            plan=record.plan,
                            fasta=record.fasta,
                            started_at=record.started_at,
                            completed_at=completed_at,
                            runtime=runtime,
                        ),
                        max_output_bytes=self.config.max_output_bytes,
                    )
                    handoff = build_handoff(
                        job_id=record.job_id,
                        input_path=record.input_path,
                        source_version=record.source_version,
                        plan=record.plan,
                        annotations_path=annotations,
                        report_path=report,
                        completed_at=completed_at,
                        runtime=runtime,
                    )
                    state = JobState.SUCCEEDED
                    reason = None
        except RunnerTimedOutError:
            state = JobState.TIMED_OUT
            reason = "DeepKOALA exceeded the configured execution timeout."
        except asyncio.CancelledError:
            state = JobState.CANCELLED
            reason = None
        except InstallationError:
            reason = "The configured DeepKOALA installation became unavailable."
        except OutputValidationError:
            reason = "DeepKOALA output was missing or not a valid detailed CSV."
        except Exception as error:
            reason, record.correlation_id = _record_background_failure(stage, error)
        finally:
            if state is not JobState.SUCCEEDED:
                try:
                    cleanup_output_directory(record.output_directory)
                except (OSError, ValueError, OutputValidationError) as error:
                    state = JobState.FAILED
                    if record.correlation_id is None:
                        reason, record.correlation_id = _record_background_failure(
                            "stable_output_cleanup", error
                        )
            try:
                remove_job_directory(record.directory, self._require_state_session())
            except (OSError, ValueError) as error:
                if state is JobState.SUCCEEDED:
                    with contextlib.suppress(OSError, ValueError, OutputValidationError):
                        cleanup_output_directory(record.output_directory)
                state = JobState.FAILED
                handoff = None
                output_bytes = None
                if record.correlation_id is None:
                    reason, record.correlation_id = _record_background_failure(
                        "private_job_cleanup", error
                    )
            if state is not JobState.SUCCEEDED:
                close_output_directory(record.output_directory)
            completed_at = completed_at or _now()
            self._release_record_runner(record)
            async with self._lock:
                record.state = state
                record.failure_reason = reason
                record.output_bytes = output_bytes if state is JobState.SUCCEEDED else None
                record.handoff = handoff if state is JobState.SUCCEEDED else None
                record.completed_at = completed_at
                record.task = None

    @staticmethod
    def _release_record_runner(record: _JobRecord) -> None:
        runner_lock_fd = record.runner_lock_fd
        if runner_lock_fd is None:
            return
        record.runner_lock_fd = None
        release_runner_lock(runner_lock_fd)

    def _select_installation(self, model: str, date: str) -> Installation:
        try:
            return select_installation(self.config.checkout, model, date)
        except InstallationError as error:
            fail_installation(error)

    async def _probe_runtime(self) -> RuntimeProbeResult:
        if self._runtime_probe is not None:
            return self._runtime_probe(
                checkout=self.config.checkout,
                python_executable=self.config.python_executable,
                cpu_threads=self.config.cpu_threads,
            )
        return await probe_runtime_async(
            checkout=self.config.checkout,
            python_executable=self.config.python_executable,
            cpu_threads=self.config.cpu_threads,
        )

    async def _multi_ready(self, runtime: RuntimeProbeResult) -> bool:
        return await probe_multi_dependencies_async(
            allow_multi=self.config.allow_multi,
            profiles_dir=self.config.profiles_dir,
            hmmsearch_executable=self.config.hmmsearch_executable,
            runtime=runtime,
        )

    def _summary(self, record: _JobRecord) -> JobSummary:
        return JobSummary(
            job_id=record.job_id,
            state=record.state,
            started_at=record.started_at,
            completed_at=record.completed_at,
            exit_code=record.exit_code,
            failure_reason=record.failure_reason,
            correlation_id=record.correlation_id,
            output_bytes=record.output_bytes,
        )

    def _prune_terminal_locked(self) -> None:
        while len(self._jobs) >= MAX_RETAINED_JOBS:
            terminal = [record for record in self._jobs.values() if record.state in _TERMINAL]
            if not terminal:
                fail(
                    ErrorCode.RUNNER_BUSY,
                    "The bounded local runner state is full.",
                    suggested_action="Wait for the active job to finish and retry.",
                )
            oldest = min(
                terminal,
                key=lambda record: record.completed_at or record.started_at,
            )
            removed = self._jobs.pop(oldest.job_id)
            close_output_directory(removed.output_directory)

    def _require_open(self) -> None:
        if not self._opened:
            raise RuntimeError("job manager is not open")

    def _require_state_session(self) -> StateSession:
        state_session = self._state_session
        if state_session is None:
            raise RuntimeError("private session is unavailable")
        return state_session


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _fail_job_not_found() -> NoReturn:
    fail(
        ErrorCode.JOB_NOT_FOUND,
        "The process-scoped DeepKOALA job was not found.",
        suggested_action="Use a job ID returned by this active companion process.",
    )


def _fail_artifact_not_found() -> NoReturn:
    fail(
        ErrorCode.ARTIFACT_NOT_FOUND,
        "The requested stable DeepKOALA artifact is unavailable.",
        suggested_action=(
            "Use the returned stable file path or rerun annotation to a new directory."
        ),
    )


def _record_background_failure(stage: str, error: Exception) -> tuple[str, str]:
    correlation_id = f"joberr_{secrets.token_urlsafe(9)}"
    print(
        f"deepkoala-mcp job failure correlation_id={correlation_id} "
        f"stage={stage} type={type(error).__name__}",
        file=sys.stderr,
    )
    return "The DeepKOALA process could not be completed safely.", correlation_id


def _raise_internal(stage: str, error: Exception) -> NoReturn:
    correlation_id = f"err_{secrets.token_urlsafe(9)}"
    print(
        f"deepkoala-mcp internal error correlation_id={correlation_id} "
        f"stage={stage} type={type(error).__name__}",
        file=sys.stderr,
    )
    fail(
        ErrorCode.INTERNAL_ERROR,
        "The companion could not roll back a partially staged local run safely.",
        suggested_action="Check owner-only state and output directories before retrying.",
    )


__all__ = ["ArtifactName", "DeepKoalaJobManager"]
