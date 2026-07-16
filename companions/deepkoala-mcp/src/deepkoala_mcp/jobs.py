"""Single-record local job lifecycle for the DeepKOALA companion."""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

from deepkoala_mcp import __version__
from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.contracts import (
    CompanionStatus,
    DeleteDeepKoalaJobResult,
    ErrorCode,
    ExecutionNotice,
    ExecutionPlan,
    GetDeepKoalaJobResult,
    ImportHandoff,
    JobState,
    JobSummary,
    PrepareDeepKoalaInput,
    PrepareDeepKoalaResult,
    SourceMetadataField,
    SourceProvenance,
    fail,
)
from deepkoala_mcp.fasta import (
    INPUT_FILENAME,
    FastaLimitError,
    FastaValidationError,
    InputPathError,
    stage_fasta,
)
from deepkoala_mcp.installation import (
    InstallationError,
    fail_installation,
    inspect_installation,
    select_installation,
)
from deepkoala_mcp.job_storage import (
    prepare_state_root,
    remove_job_directory,
    remove_session_directory,
    validate_output,
)
from deepkoala_mcp.runner import (
    OUTPUT_FILENAME,
    DeepKoalaProcessRunner,
    ProcessOutcome,
    RunnerPlan,
    RunnerTimedOutError,
)
from deepkoala_mcp.scheduler import JobScheduler

_TERMINAL = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.TIMED_OUT})


class Runner(Protocol):
    """Small injectable boundary used by offline job-manager tests."""

    async def run(self, plan: RunnerPlan) -> ProcessOutcome: ...


@dataclass(slots=True)
class _JobRecord:
    job_id: str
    directory: Path
    notice: ExecutionNotice
    prepared_at: datetime
    expires_at: datetime
    source_version: str
    original_input_path: str | None
    state: JobState = JobState.PREPARED
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    output_bytes: int | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False


class DeepKoalaJobManager:
    """Own one bounded CPU runner, one queue, and one opaque job namespace."""

    def __init__(
        self,
        config: DeepKoalaRuntimeConfig,
        *,
        runner: Runner | None = None,
    ) -> None:
        self.config = config
        self._runner = runner or DeepKoalaProcessRunner()
        self._lifecycle_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._prepare_lock = asyncio.Lock()
        self._jobs: dict[str, _JobRecord] = {}
        self._scheduler = JobScheduler(config.max_queue_size)
        self._session_directory: Path | None = None
        self._sweeper: asyncio.Task[None] | None = None
        self._opened = False
        self._closing = False

    async def open(self) -> None:
        """Create one owner-only process scope without loading a model."""
        async with self._lifecycle_lock:
            if self._opened:
                return
            root = prepare_state_root(self.config.state_root)
            session = root / f"session_{secrets.token_hex(16)}"
            session.mkdir(mode=0o700)
            os.chmod(session, 0o700)
            self._session_directory = session
            self._opened = True
            self._closing = False
            self._sweeper = asyncio.create_task(self._sweep_expired())

    async def close(self) -> None:
        """Cancel the owned child and remove the complete process scope."""
        async with self._lifecycle_lock:
            await self._close_owned_state()

    async def _close_owned_state(self) -> None:
        async with self._lock:
            if not self._opened:
                return
            self._closing = True
            active: list[asyncio.Task[None]] = []
            for record in self._jobs.values():
                task = record.task
                if task is None or task.done():
                    continue
                active.append(task)
                if not record.cancel_requested:
                    record.cancel_requested = True
                    task.cancel()
            tasks = tuple(active)
            sweeper = self._sweeper
            if sweeper is not None:
                sweeper.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if sweeper is not None:
            await asyncio.gather(sweeper, return_exceptions=True)
        async with self._prepare_lock:
            session = self._session_directory
            if session is not None:
                try:
                    remove_session_directory(session)
                except (OSError, ValueError) as error:
                    self._sweeper = None
                    raise RuntimeError("private session cleanup failed") from error
        async with self._lock:
            self._jobs.clear()
            self._scheduler.clear()
            self._session_directory = None
            self._sweeper = None
            self._opened = False
            self._closing = False

    async def prepare(self, request: PrepareDeepKoalaInput) -> PrepareDeepKoalaResult:
        """Stage one FASTA and return an execution notice without launching DeepKOALA."""
        self._require_open()
        await self._cleanup_expired()
        async with self._prepare_lock:
            self._require_open()
            async with self._lock:
                self._require_capacity_locked()
                queued_ahead = self._scheduler.queued_count
            session = self._session_directory
            if session is None:
                raise RuntimeError("job manager has no session")
            job_id = f"job_{secrets.token_hex(16)}"
            directory = session / job_id
            directory.mkdir(mode=0o700)
            os.chmod(directory, 0o700)
            try:
                staged = stage_fasta(
                    fasta_text=request.fasta_text,
                    fasta_path=request.fasta_path,
                    allowed_roots=self.config.allowed_roots,
                    job_directory=directory,
                )
                installation = select_installation(
                    self.config.checkout,
                    request.model,
                    request.model_date,
                )
            except FastaLimitError:
                remove_job_directory(directory, session)
                fail(
                    ErrorCode.INPUT_LIMIT_EXCEEDED,
                    "The protein FASTA exceeds a hard companion limit.",
                    suggested_action="Reduce the FASTA to the documented input bounds.",
                )
            except FastaValidationError:
                remove_job_directory(directory, session)
                fail(
                    ErrorCode.INVALID_FASTA,
                    "The supplied input is not an accepted protein FASTA.",
                    suggested_action="Correct the FASTA syntax and protein residues.",
                )
            except InputPathError:
                remove_job_directory(directory, session)
                fail(
                    ErrorCode.PATH_NOT_ALLOWED,
                    "The FASTA path is outside the configured local input boundary.",
                    suggested_action=(
                        "Use inline FASTA or a direct regular file under an allowed root."
                    ),
                )
            except InstallationError as error:
                remove_job_directory(directory, session)
                fail_installation(error)
            except OSError:
                remove_job_directory(directory, session)
                fail(
                    ErrorCode.INTERNAL_ERROR,
                    "The companion could not create private job state.",
                    suggested_action="Check the owner-only state root and retry.",
                )

            plan = ExecutionPlan(
                model=request.model,
                requested_model_date=request.model_date,
                resolved_model_date=installation.resource.model_date,
                batch_size=request.batch_size,
                topk=request.topk,
                cpu_threads=self.config.cpu_threads,
                timeout_seconds=request.timeout_seconds or self.config.default_timeout_seconds,
            )
            notice = ExecutionNotice(
                plan=plan,
                fasta=staged.summary,
                deepkoala_version=installation.source_version,
                queued_jobs_ahead=queued_ahead,
            )
            prepared_at = _now()
            record = _JobRecord(
                job_id=job_id,
                directory=directory,
                notice=notice,
                prepared_at=prepared_at,
                expires_at=prepared_at + timedelta(seconds=self.config.plan_ttl_seconds),
                source_version=installation.source_version,
                original_input_path=staged.original_input_path,
            )
            async with self._lock:
                if self._closing:
                    remove_job_directory(directory, session)
                    raise RuntimeError("job manager is closing")
                self._require_capacity_locked()
                self._jobs[job_id] = record
            return self._prepare_result(record)

    async def submit(self, job_id: str) -> JobSummary:
        """Idempotently submit one explicitly acknowledged retained plan."""
        self._require_open()
        await self._cleanup_expired()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            assert record is not None
            if record.state is not JobState.PREPARED:
                return self._summary(record)
            if self._scheduler.is_full:
                fail(
                    ErrorCode.QUEUE_FULL,
                    "The local DeepKOALA queue is full.",
                    suggested_action="Wait for or cancel a queued job before retrying.",
                )
            else:
                record.state = JobState.QUEUED
                record.submitted_at = _now()
                self._scheduler.enqueue(job_id)
                self._schedule_locked()
                return self._summary(record)

    async def get_job(self, job_id: str) -> GetDeepKoalaJobResult:
        """Return current state and a file handoff only after success."""
        self._require_open()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or _record_expired(
                record,
                _now(),
                self.config.retention_seconds,
            ):
                _fail_job_not_found()
            assert record is not None
            return GetDeepKoalaJobResult(
                job=self._summary(record),
                handoff=self._handoff(record) if record.state is JobState.SUCCEEDED else None,
            )

    async def cancel(self, job_id: str) -> JobSummary:
        """Cancel a prepared/queued job or terminate one running process group."""
        self._require_open()
        await self._cleanup_expired()
        task: asyncio.Task[None] | None = None
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            assert record is not None
            if record.state is JobState.CANCELLED:
                return self._summary(record)
            if record.state is JobState.PREPARED:
                record.state = JobState.CANCELLED
                record.completed_at = _now()
                remove_job_directory(record.directory, self._require_session())
                return self._summary(record)
            elif record.state is JobState.QUEUED:
                self._scheduler.remove(job_id)
                record.state = JobState.CANCELLED
                record.completed_at = _now()
                remove_job_directory(record.directory, self._require_session())
                self._schedule_locked()
                return self._summary(record)
            elif record.state is JobState.RUNNING:
                task = record.task
                if task is not None and not record.cancel_requested:
                    record.cancel_requested = True
                    task.cancel()
            else:
                fail(
                    ErrorCode.JOB_NOT_CANCELLABLE,
                    "The job is already terminal and cannot be cancelled.",
                    suggested_action="Read or delete the terminal job instead.",
                )
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is None:
                _fail_job_not_found()
            return self._summary(cast(_JobRecord, current))

    async def delete(self, job_id: str) -> DeleteDeepKoalaJobResult:
        """Delete one terminal job and its retained local files."""
        self._require_open()
        await self._cleanup_expired()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            assert record is not None
            if record.state not in _TERMINAL:
                fail(
                    ErrorCode.NOT_TERMINAL,
                    "Only a terminal DeepKOALA job can be deleted.",
                    suggested_action="Cancel the job or wait for it to finish.",
                )
            self._jobs.pop(job_id)
        try:
            remove_job_directory(record.directory, self._require_session())
        except (OSError, ValueError):
            async with self._lock:
                self._jobs.setdefault(job_id, record)
            fail(
                ErrorCode.INTERNAL_ERROR,
                "The terminal job directory could not be removed.",
                suggested_action="Repair the owner-only state root and retry.",
            )
        return DeleteDeepKoalaJobResult(job_id=job_id)

    async def status(self) -> CompanionStatus:
        """Return structural readiness without loading a model or exposing paths."""
        self._require_open()
        try:
            version, resources = inspect_installation(self.config.checkout)
        except InstallationError:
            version, resources = None, ()
        async with self._lock:
            prepared = sum(record.state is JobState.PREPARED for record in self._jobs.values())
            queued = self._scheduler.queued_count
            running = int(self._scheduler.running_job_id is not None)
        return CompanionStatus(
            server_version=__version__,
            ready=bool(resources),
            deepkoala_version=version,
            installed_resources=resources,
            cpu_threads=self.config.cpu_threads,
            max_queue_size=self.config.max_queue_size,
            prepared_jobs=prepared,
            queued_jobs=queued,
            running_jobs=running,
        )

    def _schedule_locked(self) -> None:
        if self._closing:
            return
        while True:
            job_id = self._scheduler.start_next()
            if job_id is None:
                return
            record = self._jobs.get(job_id)
            if record is None or record.state is not JobState.QUEUED:
                self._scheduler.finish(job_id)
                continue
            record.state = JobState.RUNNING
            record.started_at = _now()
            record.task = asyncio.create_task(self._execute(record))
            return

    async def _execute(self, record: _JobRecord) -> None:
        state = JobState.FAILED
        reason: str | None = "The local DeepKOALA process or output failed safely."
        output_bytes: int | None = None
        try:
            installation = select_installation(
                self.config.checkout,
                record.notice.plan.model,
                record.notice.plan.resolved_model_date,
            )
            record.source_version = installation.source_version
            outcome = await self._runner.run(self._runner_plan(record))
            record.exit_code = outcome.return_code
            if outcome.return_code != 0:
                reason = "DeepKOALA exited without a successful result."
            else:
                output_path = record.directory / OUTPUT_FILENAME
                output_bytes = validate_output(output_path)
                os.chmod(output_path, 0o600, follow_symlinks=False)
                (record.directory / INPUT_FILENAME).unlink()
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
        except Exception:
            pass
        finally:
            if state is not JobState.SUCCEEDED:
                with contextlib.suppress(OSError, ValueError):
                    remove_job_directory(record.directory, self._require_session())
            async with self._lock:
                record.state = state
                record.failure_reason = reason
                record.output_bytes = output_bytes
                record.completed_at = _now()
                record.task = None
                if self._scheduler.running_job_id == record.job_id:
                    self._scheduler.finish(record.job_id)
                self._schedule_locked()

    async def _cleanup_expired(self) -> None:
        now = _now()
        expired: list[Path] = []
        async with self._lock:
            for job_id, record in tuple(self._jobs.items()):
                if _record_expired(record, now, self.config.retention_seconds):
                    self._jobs.pop(job_id, None)
                    expired.append(record.directory)
        session = self._session_directory
        if session is not None:
            for directory in expired:
                with contextlib.suppress(OSError, ValueError):
                    remove_job_directory(directory, session)

    async def _sweep_expired(self) -> None:
        interval = max(1, min(60, self.config.plan_ttl_seconds, self.config.retention_seconds))
        while True:
            await asyncio.sleep(interval)
            await self._cleanup_expired()

    def _require_capacity_locked(self) -> None:
        if len(self._jobs) >= self.config.max_queue_size + 1:
            fail(
                ErrorCode.QUEUE_FULL,
                "The bounded local job capacity is full.",
                suggested_action=(
                    "Delete, cancel, or wait for existing jobs before preparing another."
                ),
            )

    def _runner_plan(self, record: _JobRecord) -> RunnerPlan:
        plan = record.notice.plan
        return RunnerPlan(
            python_executable=self.config.python_executable,
            checkout=self.config.checkout,
            job_directory=record.directory,
            model=plan.model,
            resolved_date=plan.resolved_model_date,
            batch_size=plan.batch_size,
            topk=plan.topk,
            timeout_seconds=plan.timeout_seconds,
            cpu_threads=plan.cpu_threads,
        )

    def _prepare_result(self, record: _JobRecord) -> PrepareDeepKoalaResult:
        return PrepareDeepKoalaResult(
            job_id=record.job_id,
            prepared_at=record.prepared_at,
            expires_at=record.expires_at,
            notice=record.notice,
        )

    def _summary(self, record: _JobRecord) -> JobSummary:
        return JobSummary(
            job_id=record.job_id,
            state=record.state,
            prepared_at=record.prepared_at,
            submitted_at=record.submitted_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            exit_code=record.exit_code,
            failure_reason=record.failure_reason,
            output_bytes=record.output_bytes if record.state is JobState.SUCCEEDED else None,
        )

    def _handoff(self, record: _JobRecord) -> ImportHandoff:
        if record.completed_at is None or record.state is not JobState.SUCCEEDED:
            raise AssertionError("handoff requires a successful job")
        plan = record.notice.plan
        output_path = record.directory / OUTPUT_FILENAME
        if validate_output(output_path) != record.output_bytes:
            raise RuntimeError("retained DeepKOALA output changed after completion")
        resolved_directory = record.directory.resolve(strict=True)
        resolved_output = output_path.resolve(strict=True)
        if resolved_output.parent != resolved_directory or resolved_output != output_path:
            raise RuntimeError("retained DeepKOALA output escaped private state")
        metadata = (
            SourceMetadataField(name="runner_version", value=__version__),
            SourceMetadataField(name="device", value="cpu"),
            SourceMetadataField(name="detail", value=True),
            SourceMetadataField(name="batch_size", value=plan.batch_size),
            SourceMetadataField(name="num_workers", value=0),
            SourceMetadataField(name="topk", value=plan.topk),
            SourceMetadataField(name="multi", value=False),
            SourceMetadataField(name="cpu_threads", value=plan.cpu_threads),
        )
        source = SourceProvenance(
            source_version=record.source_version,
            model_name=plan.model,
            model_version=plan.resolved_model_date,
            annotation_date=record.completed_at,
            input_uri=f"mcp://deepkoala-mcp/jobs/{record.job_id}/output",
            input_path=record.original_input_path,
            source_metadata=metadata,
        )
        return ImportHandoff(output_path=str(resolved_output), source=source)

    def _require_open(self) -> None:
        if not self._opened or self._closing:
            raise RuntimeError("job manager is unavailable")

    def _require_session(self) -> Path:
        session = self._session_directory
        if session is None:
            raise RuntimeError("job manager has no session")
        return session


def _fail_job_not_found() -> None:
    fail(
        ErrorCode.JOB_NOT_FOUND,
        "The job is unavailable in this companion process.",
        suggested_action="Use a current job identifier or prepare a new job.",
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _record_expired(record: _JobRecord, now: datetime, retention_seconds: int) -> bool:
    if record.state is JobState.PREPARED:
        return record.expires_at <= now
    return (
        record.state in _TERMINAL
        and record.completed_at is not None
        and record.completed_at + timedelta(seconds=retention_seconds) <= now
    )


__all__ = ["DeepKoalaJobManager", "Runner"]
