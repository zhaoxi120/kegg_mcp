"""Process-local DeepKOALA plan, queue, job, retention, and handoff supervision."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
import tomllib
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast

from deepkoala_mcp import __version__
from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.contracts import (
    MAX_STATUS_MODEL_DATES,
    UPDATED_WEIGHTS_URL,
    CancelDeepKoalaJobResult,
    CompanionDefaults,
    CompanionLimits,
    CompanionStatus,
    DeepKoalaMcpError,
    DeleteDeepKoalaJobResult,
    ErrorCode,
    ErrorDetail,
    ExecutionArtifact,
    ExecutionNotice,
    ExecutionSettings,
    FastaSummary,
    GetDeepKoalaJobResult,
    ImportHandoff,
    JobState,
    JobSummary,
    PrepareDeepKoalaInput,
    PrepareDeepKoalaResult,
    QueueSnapshot,
    SafeDetail,
    SourceMetadataField,
    SourceProvenanceTemplate,
    SubmitDeepKoalaResult,
    WeightArtifact,
    fail,
)
from deepkoala_mcp.deepkoala import (
    DeepKoalaInstallation,
    DeepKoalaProbeError,
    DeepKoalaProbeResult,
    probe_deepkoala_installation,
    recheck_artifact_identities,
)
from deepkoala_mcp.fasta import (
    FastaLimits,
    FastaValidationError,
    ingest_inline_fasta,
    ingest_path_fasta,
    validate_stored_fasta,
)
from deepkoala_mcp.fasta import (
    FastaSummary as IntakeFastaSummary,
)
from deepkoala_mcp.filesystem import (
    FilesystemSecurityError,
    cleanup_job_directory,
    cleanup_session_directory,
    create_job_directory,
    create_session_directory,
    prepare_state_root,
    remove_controlled_file,
    secure_write_bytes,
)
from deepkoala_mcp.output import (
    DetailedCsvSummary,
    OutputValidationError,
    read_hashed_artifact_range,
    validate_detailed_csv,
)
from deepkoala_mcp.runner import (
    DeepKoalaProcessRunner,
    RunnerPlan,
    RunnerTimedOutError,
    build_argv,
)

_DEFAULT_BATCH_SIZE: Final = 32
_DEFAULT_NUM_WORKERS: Final = 2
_DEFAULT_TOPK: Final = 1
_PROBE_TIMEOUT_SECONDS: Final = 30.0
_MAX_SOURCE_VERSION_BYTES: Final = 256 * 1024
_MAX_SESSION_RECORDS: Final = 128
_MAX_DELETED_TOMBSTONES: Final = 1_024
_MAX_STATUS_RESOURCE_ENTRIES: Final = 1_024
_MODEL_DATE_PATTERN: Final = re.compile(r"^[0-9]{6}$")
_SAFE_SOURCE_VERSION: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_TERMINAL_STATES: Final = frozenset(
    {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.TIMED_OUT}
)


@dataclass(slots=True)
class _JobRecord:
    plan_id: str
    private_job_id: str
    directory: Path
    request: PrepareDeepKoalaInput
    fasta: FastaSummary
    probe: DeepKoalaProbeResult
    settings: ExecutionSettings
    notice: ExecutionNotice
    notice_sha256: str
    prepared_at: datetime
    expires_at: datetime
    job_id: str | None = None
    state: JobState = JobState.PREPARED
    created_at: datetime | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    output: DetailedCsvSummary | None = None
    provenance_sha256: str | None = None
    diagnostic_text: str = ""
    diagnostics_truncated: bool = False
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False
    artifacts_deleted: bool = False
    cleanup_pending: bool = False


@dataclass(frozen=True, slots=True)
class ArtifactRange:
    """One bounded artifact range returned without exposing its private path."""

    content: bytes
    mime_type: str
    sha256: str
    total_bytes: int
    next_offset: int | None


class _StagedInputChangedError(RuntimeError):
    pass


class DeepKoalaJobManager:
    """Own every external process and artifact in one opaque stdio process scope."""

    def __init__(
        self,
        config: DeepKoalaRuntimeConfig,
        *,
        runner: DeepKoalaProcessRunner | None = None,
    ) -> None:
        self.config = config
        self._runner = runner or DeepKoalaProcessRunner()
        self._installation = DeepKoalaInstallation(
            python_executable=config.python_executable,
            checkout=config.checkout,
        )
        self._lock = asyncio.Lock()
        self._preflight_lock = asyncio.Lock()
        self._plans: dict[str, _JobRecord] = {}
        self._jobs: dict[str, _JobRecord] = {}
        self._queue: deque[str] = deque()
        self._deleted: set[str] = set()
        self._deleted_order: deque[str] = deque()
        self._pending_cleanup: set[Path] = set()
        self._active_prepares: set[asyncio.Task[PrepareDeepKoalaResult]] = set()
        self._session_directory: Path | None = None
        self._sweeper_task: asyncio.Task[None] | None = None
        self._opened = False
        self._closing = False

    async def open(self) -> None:
        """Create this process scope without probing or launching DeepKOALA."""
        if self._opened:
            return
        root = prepare_state_root(self.config.state_root)
        session_name = f"session_{secrets.token_hex(16)}"
        self._session_directory = create_session_directory(root, session_name)
        self._closing = False
        self._opened = True
        self._sweeper_task = asyncio.create_task(self._sweep_expired())

    async def close(self) -> None:
        """Cancel and reap children, then remove every artifact in this process scope."""
        async with self._lock:
            if not self._opened or self._closing:
                return
            self._closing = True
            tasks = tuple(
                record.task
                for record in self._jobs.values()
                if record.task is not None and not record.task.done()
            )
            current_task = asyncio.current_task()
            active_prepares = tuple(
                task
                for task in self._active_prepares
                if task is not current_task and not task.done()
            )
            for record in self._jobs.values():
                record.cancel_requested = True
            for task in (*tasks, *active_prepares):
                task.cancel()
            sweeper = self._sweeper_task
            if sweeper is not None:
                sweeper.cancel()
        if tasks or active_prepares:
            await asyncio.gather(*tasks, *active_prepares, return_exceptions=True)
        if sweeper is not None:
            await asyncio.gather(sweeper, return_exceptions=True)
        async with self._preflight_lock:
            records = tuple({id(record): record for record in self._plans.values()}.values())
            for record in records:
                if not record.artifacts_deleted:
                    self._cleanup_or_defer(record.directory)
            for directory in tuple(self._pending_cleanup):
                self._cleanup_or_defer(directory)
            session = self._session_directory
            if session is not None:
                with contextlib.suppress(OSError, FilesystemSecurityError):
                    cleanup_session_directory(session)
            async with self._lock:
                self._plans.clear()
                self._jobs.clear()
                self._queue.clear()
                self._deleted.clear()
                self._deleted_order.clear()
                self._pending_cleanup.clear()
                self._active_prepares.clear()
                self._session_directory = None
                self._sweeper_task = None
                self._opened = False
                self._closing = False

    async def prepare(self, request: PrepareDeepKoalaInput) -> PrepareDeepKoalaResult:
        """Validate and stage one exact plan without starting DeepKOALA inference."""
        self._require_open()
        await self._cleanup_expired()
        current_task = cast(
            asyncio.Task[PrepareDeepKoalaResult] | None,
            asyncio.current_task(),
        )
        if current_task is None:
            raise RuntimeError("prepare requires an asyncio task")
        async with self._lock:
            if not self._opened or self._closing:
                raise RuntimeError("job manager is not available")
            self._require_execution_queue_capacity_locked()
            if (
                len(self._plans) + len(self._pending_cleanup) + len(self._active_prepares)
                >= _MAX_SESSION_RECORDS
            ):
                fail(
                    ErrorCode.QUEUE_FULL,
                    "The bounded plan capacity is full.",
                    suggested_action="Delete or wait for existing plans before preparing another.",
                )
            self._active_prepares.add(current_task)
        try:
            return await self._prepare_reserved(request, reservation=current_task)
        finally:
            async with self._lock:
                self._active_prepares.discard(current_task)

    async def _prepare_reserved(
        self,
        request: PrepareDeepKoalaInput,
        *,
        reservation: asyncio.Task[PrepareDeepKoalaResult],
    ) -> PrepareDeepKoalaResult:
        """Complete one capacity-reserved preparation and release no inference process."""
        plan_id = f"plan_{secrets.token_hex(16)}"
        private_job_id = f"job_{secrets.token_hex(16)}"
        session = self._session_directory
        if session is None:
            raise RuntimeError("job manager has no process scope")
        registered = False
        try:
            directory = create_job_directory(session, private_job_id)
            intake = self._ingest_fasta(request, directory)
            async with self._preflight_lock:
                self._require_open()
                probe = await probe_deepkoala_installation(
                    self._installation,
                    model=request.model,
                    date=request.model_date,
                    device=request.device,
                    weight_source=self.config.weight_source,
                    timeout_seconds=_PROBE_TIMEOUT_SECONDS,
                )
        except asyncio.CancelledError:
            self._cleanup_if_created(locals().get("directory"))
            raise
        except FastaValidationError as error:
            if "limit" in str(error) or "exceeds" in str(error):
                code = ErrorCode.INPUT_LIMIT_EXCEEDED
            else:
                code = ErrorCode.INVALID_FASTA
            self._cleanup_if_created(locals().get("directory"))
            fail(
                code,
                "The supplied input is not an accepted bounded protein FASTA.",
                suggested_action="Correct the FASTA content or reduce it to the documented limits.",
            )
        except FilesystemSecurityError:
            self._cleanup_if_created(locals().get("directory"))
            fail(
                ErrorCode.PATH_NOT_ALLOWED,
                "The FASTA path could not be read within the configured filesystem boundary.",
                suggested_action="Use inline FASTA or a regular file under an allowed root.",
            )
        except DeepKoalaProbeError as error:
            self._cleanup_if_created(locals().get("directory"))
            raise _map_probe_error(error) from error
        except (OSError, RuntimeError):
            self._cleanup_if_created(locals().get("directory"))
            fail(
                ErrorCode.INTERNAL_ERROR,
                "The companion could not stage the job safely.",
                suggested_action="Check private state storage and retry.",
            )

        try:
            fasta = _contract_fasta(intake)
            settings = _settings(request, probe, self.config)
            async with self._lock:
                queue = self._queue_snapshot_locked(planned=True)
            notice = _notice(settings, fasta, probe, self.config, queue)
            digest = _notice_digest(notice)
            prepared_at = _now()
            record = _JobRecord(
                plan_id=plan_id,
                private_job_id=private_job_id,
                directory=directory,
                request=request,
                fasta=fasta,
                probe=probe,
                settings=settings,
                notice=notice,
                notice_sha256=digest,
                prepared_at=prepared_at,
                expires_at=prepared_at + timedelta(seconds=self.config.plan_ttl_seconds),
            )
            async with self._lock:
                if not self._opened or self._closing:
                    manager_unavailable = True
                    capacity_full = False
                elif (
                    len(self._plans) + len(self._pending_cleanup) + len(self._active_prepares)
                    > _MAX_SESSION_RECORDS
                ):
                    manager_unavailable = False
                    capacity_full = True
                else:
                    manager_unavailable = False
                    capacity_full = False
                    self._active_prepares.discard(reservation)
                    self._plans[plan_id] = record
                    registered = True
            if manager_unavailable:
                fail(
                    ErrorCode.INTERNAL_ERROR,
                    "The companion began closing before the prepared plan could be retained.",
                    suggested_action="Restart the companion and prepare the input again.",
                )
            if capacity_full:
                fail(
                    ErrorCode.QUEUE_FULL,
                    "The bounded plan capacity became full during preflight.",
                    suggested_action="Wait for existing plans before preparing another.",
                )
            return self._prepare_result(record)
        except asyncio.CancelledError:
            if registered:
                async with self._lock:
                    self._plans.pop(plan_id, None)
            self._cleanup_or_defer(directory)
            raise
        except DeepKoalaMcpError:
            if registered:
                async with self._lock:
                    self._plans.pop(plan_id, None)
            self._cleanup_or_defer(directory)
            raise
        except Exception:
            if registered:
                async with self._lock:
                    self._plans.pop(plan_id, None)
            self._cleanup_or_defer(directory)
            fail(
                ErrorCode.INTERNAL_ERROR,
                "The companion could not retain the prepared job safely.",
                suggested_action="Retry after checking the private state and queue bounds.",
            )

    async def submit(
        self,
        *,
        plan_id: str,
        notice_sha256: str,
    ) -> SubmitDeepKoalaResult:
        """Idempotently queue the exact acknowledged plan after artifact revalidation."""
        self._require_open()
        await self._cleanup_expired()
        async with self._lock:
            record = self._plans.get(plan_id)
            if record is None:
                _fail_plan_not_found()
            assert record is not None
            if not secrets.compare_digest(notice_sha256, record.notice_sha256):
                fail(
                    ErrorCode.NOTICE_STALE,
                    "The execution notice digest does not match the prepared plan.",
                    suggested_action="Review the current notice and submit its exact digest.",
                )
            if record.job_id is not None:
                return SubmitDeepKoalaResult(job=self._job_summary(record), idempotent_replay=True)
            if record.expires_at <= _now():
                self._plans.pop(plan_id, None)
                expired_directory = record.directory
            else:
                expired_directory = None
        if expired_directory is not None:
            self._cleanup_or_defer(expired_directory)
            fail(
                ErrorCode.PLAN_EXPIRED,
                "The prepared execution plan has expired.",
                suggested_action="Prepare the FASTA again and review a new execution notice.",
            )
        try:
            async with self._preflight_lock:
                self._require_open()
                await recheck_artifact_identities(self._installation, record.probe)
        except DeepKoalaProbeError as error:
            raise _map_probe_error(error, stale=True) from error
        try:
            self._require_staged_input_unchanged(record)
        except _StagedInputChangedError:
            fail(
                ErrorCode.NOTICE_STALE,
                "The staged FASTA changed after the execution notice.",
                suggested_action="Prepare the input again and review the new notice.",
            )

        async with self._lock:
            if not self._opened or self._closing:
                raise RuntimeError("job manager is not available")
            if record.job_id is not None:
                return SubmitDeepKoalaResult(job=self._job_summary(record), idempotent_replay=True)
            if len(self._queue) >= self.config.max_queue_size:
                fail(
                    ErrorCode.QUEUE_FULL,
                    "The DeepKOALA execution queue is full.",
                    suggested_action=(
                        "Wait for a queued job to finish or cancel one before retrying."
                    ),
                )
            submitted_at = _now()
            record.job_id = record.private_job_id
            record.state = JobState.QUEUED
            record.created_at = submitted_at
            record.queued_at = submitted_at
            self._jobs[record.job_id] = record
            self._queue.append(record.job_id)
            self._schedule_locked()
            return SubmitDeepKoalaResult(job=self._job_summary(record), idempotent_replay=False)

    async def get_job(self, job_id: str) -> GetDeepKoalaJobResult:
        """Return one scoped job and its successful core-import handoff."""
        self._require_open()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None or _record_retention_expired(record, _now(), self.config):
                _fail_job_not_found()
            assert record is not None
            return GetDeepKoalaJobResult(
                job=self._job_summary(record),
                handoff=self._handoff(record) if record.state is JobState.SUCCEEDED else None,
            )

    async def cancel(self, job_id: str) -> CancelDeepKoalaJobResult:
        """Cancel one queued or running job without accepting a client-supplied PID."""
        self._require_open()
        await self._cleanup_expired()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            assert record is not None
            if record.state is JobState.CANCELLED:
                directory = None if record.artifacts_deleted else record.directory
                task = None
            elif record.state is JobState.QUEUED:
                self._queue.remove(job_id)
                record.state = JobState.CANCELLED
                record.completed_at = _now()
                directory = record.directory
                self._schedule_locked()
                task = None
            elif record.state is JobState.RUNNING:
                record.cancel_requested = True
                task = record.task
                directory = None
            else:
                fail(
                    ErrorCode.JOB_NOT_CANCELLABLE,
                    "The job is no longer cancellable.",
                    suggested_action=(
                        "Read the terminal job result or delete its retained artifacts."
                    ),
                )
        if directory is not None:
            try:
                cleanup_job_directory(directory)
                record.artifacts_deleted = True
                record.cleanup_pending = False
            except (OSError, FilesystemSecurityError):
                record.cleanup_pending = True
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            async with self._lock:
                cancellation_needs_finalize = record.state is JobState.RUNNING
                if cancellation_needs_finalize:
                    record.state = JobState.CANCELLED
                    record.completed_at = _now()
                    self._schedule_locked()
            if not record.artifacts_deleted:
                try:
                    cleanup_job_directory(record.directory)
                    record.artifacts_deleted = True
                    record.cleanup_pending = False
                except (OSError, FilesystemSecurityError):
                    record.cleanup_pending = True
        async with self._lock:
            return CancelDeepKoalaJobResult(job=self._job_summary(record))

    async def delete(self, job_id: str) -> DeleteDeepKoalaJobResult:
        """Delete a terminal job and its private retained artifacts."""
        self._require_open()
        await self._cleanup_expired()
        async with self._lock:
            if job_id in self._deleted:
                return DeleteDeepKoalaJobResult(job_id=job_id)
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            assert record is not None
            if record.state not in _TERMINAL_STATES:
                fail(
                    ErrorCode.NOT_TERMINAL,
                    "Only terminal jobs can be deleted.",
                    suggested_action="Cancel the job or wait for it to reach a terminal state.",
                )
        if not record.artifacts_deleted:
            try:
                cleanup_job_directory(record.directory)
            except (OSError, FilesystemSecurityError):
                record.cleanup_pending = True
                fail(
                    ErrorCode.INTERNAL_ERROR,
                    "The terminal job artifacts could not be deleted safely.",
                    suggested_action=(
                        "Restore the private state directory boundary and retry deletion."
                    ),
                )
            record.artifacts_deleted = True
            record.cleanup_pending = False
        async with self._lock:
            self._jobs.pop(job_id, None)
            self._plans.pop(record.plan_id, None)
            self._remember_deleted_locked(job_id)
        return DeleteDeepKoalaJobResult(job_id=job_id)

    async def status(self) -> CompanionStatus:
        """Return redacted readiness, defaults, bounds, and scheduler counts."""
        self._require_open()
        dates, source_version, deepkoala_available = _inspect_installation(self.config)
        async with self._lock:
            queue = self._queue_snapshot_locked(planned=False)
            cleanup_pending_jobs = len(self._pending_cleanup) + sum(
                record.cleanup_pending for record in self._plans.values()
            )
        weights_available = bool(dates)
        return CompanionStatus(
            server_version=__version__,
            ready=deepkoala_available and weights_available,
            deepkoala_available=deepkoala_available,
            weights_available=weights_available,
            deepkoala_version=source_version,
            weight_source=self.config.weight_source,
            available_model_dates=dates,
            supported_models=("full", "frag"),
            supported_devices=("auto", "cpu", "cuda", "mps"),
            defaults=CompanionDefaults(cpu_threads=self.config.cpu_threads),
            limits=CompanionLimits(
                max_input_bytes=self.config.max_input_bytes,
                max_output_bytes=self.config.max_output_bytes,
                max_diagnostic_bytes=self.config.diagnostic_max_bytes,
                max_sequences=self.config.max_sequences,
                max_residues=self.config.max_residues,
                max_sequence_length=self.config.max_sequence_length,
                max_header_length=self.config.max_header_length,
                max_queue_size=self.config.max_queue_size,
                default_timeout_seconds=self.config.default_timeout_seconds,
                plan_ttl_seconds=self.config.plan_ttl_seconds,
                retention_seconds=self.config.retention_seconds,
            ),
            queue=queue,
            cleanup_pending_jobs=cleanup_pending_jobs,
        )

    async def read_artifact(
        self,
        job_id: str,
        section: str,
        *,
        offset: int,
        limit: int,
    ) -> ArtifactRange:
        """Read a bounded range of one authorized job artifact."""
        self._require_open()
        await self._cleanup_expired()
        async with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                _fail_job_not_found()
            assert record is not None
            path, mime_type, expected_sha256 = _artifact_path(record, section)
        try:
            content, total, next_offset, digest = read_hashed_artifact_range(
                path,
                offset=offset,
                limit=limit,
            )
            if not secrets.compare_digest(digest, expected_sha256):
                raise OutputValidationError(
                    "OUTPUT_INVALID",
                    "Retained artifact identity changed.",
                )
        except (OSError, ValueError, OutputValidationError):
            fail(
                ErrorCode.RESULT_NOT_FOUND,
                "The requested scoped result artifact is unavailable.",
                suggested_action="Refresh the job state and use a declared artifact resource.",
            )
        return ArtifactRange(
            content=content,
            mime_type=mime_type,
            sha256=digest,
            total_bytes=total,
            next_offset=next_offset,
        )

    def _ingest_fasta(
        self,
        request: PrepareDeepKoalaInput,
        directory: Path,
    ) -> IntakeFastaSummary:
        limits = FastaLimits(
            max_input_bytes=self.config.max_input_bytes,
            max_sequences=self.config.max_sequences,
            max_total_residues=self.config.max_residues,
            max_residues_per_sequence=self.config.max_sequence_length,
            max_header_bytes=self.config.max_header_length,
        )
        if request.fasta_text is not None:
            return ingest_inline_fasta(
                request.fasta_text,
                job_directory=directory,
                limits=limits,
            )
        if request.fasta_path is None:
            raise AssertionError("validated request omitted its FASTA source")
        return ingest_path_fasta(
            request.fasta_path,
            allowed_roots=self.config.allowed_roots,
            job_directory=directory,
            limits=limits,
        )

    def _require_staged_input_unchanged(self, record: _JobRecord) -> None:
        limits = FastaLimits(
            max_input_bytes=self.config.max_input_bytes,
            max_sequences=self.config.max_sequences,
            max_total_residues=self.config.max_residues,
            max_residues_per_sequence=self.config.max_sequence_length,
            max_header_bytes=self.config.max_header_length,
        )
        try:
            current = _contract_fasta(validate_stored_fasta(record.directory, limits))
        except (FastaValidationError, FilesystemSecurityError, OSError):
            raise _StagedInputChangedError from None
        if current != record.fasta or not secrets.compare_digest(
            current.input_sha256,
            record.fasta.input_sha256,
        ):
            raise _StagedInputChangedError

    def _schedule_locked(self) -> None:
        if self._closing:
            return
        running = sum(record.state is JobState.RUNNING for record in self._jobs.values())
        while running < self.config.max_concurrent_jobs and self._queue:
            job_id = self._queue.popleft()
            record = self._jobs.get(job_id)
            if record is None or record.state is not JobState.QUEUED:
                continue
            record.state = JobState.RUNNING
            record.started_at = _now()
            task = asyncio.create_task(self._execute(record))
            task.add_done_callback(_consume_task_exception)
            record.task = task
            running += 1

    async def _execute(self, record: _JobRecord) -> None:
        try:
            try:
                await self._run_job(record)
            except RunnerTimedOutError as error:
                record.diagnostic_text = error.diagnostic_text
                record.diagnostics_truncated = error.diagnostics_truncated
                await self._finish_timed_out(record)
            except asyncio.CancelledError:
                await self._finish_cancelled(record)
            except OutputValidationError as error:
                await self._finish_failed(
                    record,
                    "DeepKOALA output failed the bounded detailed CSV contract.",
                    output_code=error.code,
                )
            except DeepKoalaProbeError:
                await self._finish_failed(
                    record,
                    "Installed DeepKOALA artifacts changed after confirmation.",
                )
            except _StagedInputChangedError:
                await self._finish_failed(
                    record,
                    "The staged FASTA changed after confirmation.",
                )
            except (OSError, RuntimeError, ValueError):
                await self._finish_failed(
                    record,
                    "The DeepKOALA process or private artifact operation failed safely.",
                )
            except Exception:
                await self._finish_failed(
                    record,
                    "DeepKOALA encountered an unexpected internal execution failure.",
                )
        except asyncio.CancelledError:
            await self._finish_cancelled(record)
        except Exception:
            await self._finish_failed(
                record,
                "DeepKOALA encountered an unexpected internal execution failure.",
            )
        finally:
            await self._finalize_execution(record)

    async def _run_job(self, record: _JobRecord) -> None:
        """Run one already scheduled job while leaving slot release to the task wrapper."""
        async with self._preflight_lock:
            self._require_open()
            await recheck_artifact_identities(self._installation, record.probe)
        self._require_staged_input_unchanged(record)
        outcome = await self._runner.run(_runner_plan(record, self.config))
        record.diagnostic_text = outcome.diagnostic_text
        record.diagnostics_truncated = outcome.diagnostics_truncated
        record.exit_code = outcome.return_code
        if record.cancel_requested:
            await self._finish_cancelled(record)
            return
        if outcome.return_code != 0:
            await self._finish_failed(
                record,
                "DeepKOALA exited without producing a successful result.",
            )
            return
        async with self._preflight_lock:
            self._require_open()
            await recheck_artifact_identities(self._installation, record.probe)
        self._require_staged_input_unchanged(record)
        maximum_rows = min(
            self.config.max_sequences,
            record.fasta.sequence_count * record.settings.topk,
        )
        output = validate_detailed_csv(
            record.directory / "deepkoala-output.csv",
            max_rows=maximum_rows,
            max_bytes=self.config.max_output_bytes,
        )
        if record.cancel_requested:
            await self._finish_cancelled(record)
            return
        completed_at = self._retain_success(record, output)
        async with self._lock:
            record.output = output
            record.state = JobState.SUCCEEDED
            record.completed_at = completed_at

    async def _finalize_execution(self, record: _JobRecord) -> None:
        """Guarantee a terminal state and release the single runner slot."""
        cleaned = True
        if record.state is JobState.RUNNING:
            try:
                cleaned = _remove_sensitive_job_files(record)
            except Exception:
                cleaned = False
        async with self._lock:
            if record.state is JobState.RUNNING:
                record.state = JobState.FAILED
                record.cleanup_pending = not cleaned
                record.failure_reason = (
                    "DeepKOALA stopped before a terminal execution result could be retained."
                )
                record.completed_at = _now()
            self._schedule_locked()

    def _retain_success(self, record: _JobRecord, output: DetailedCsvSummary) -> datetime:
        source = record.directory / "deepkoala-output.csv"
        destination = record.directory / "detailed.csv"
        if destination.exists() or destination.is_symlink():
            raise OSError("private retained output already exists")
        os.rename(source, destination)
        completion = _now()
        provenance = _provenance_document(
            record,
            output,
            completion,
            __version__,
            self.config,
        )
        provenance_bytes = json.dumps(
            provenance,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        secure_write_bytes(
            record.directory,
            "provenance.json",
            provenance_bytes,
        )
        record.provenance_sha256 = hashlib.sha256(provenance_bytes).hexdigest()
        if record.diagnostic_text:
            secure_write_bytes(
                record.directory,
                "diagnostics.txt",
                record.diagnostic_text.encode("utf-8"),
            )
        removed = remove_controlled_file(record.directory, "input.fasta")
        if not removed:
            raise OSError("private FASTA copy could not be removed")
        return completion

    async def _finish_failed(
        self,
        record: _JobRecord,
        reason: str,
        *,
        output_code: str | None = None,
    ) -> None:
        try:
            cleaned = _remove_sensitive_job_files(record)
        except Exception:
            cleaned = False
        async with self._lock:
            record.state = JobState.FAILED
            record.cleanup_pending = not cleaned
            record.failure_reason = reason
            record.completed_at = _now()
            if output_code == "OUTPUT_LIMIT_EXCEEDED":
                record.failure_reason = "DeepKOALA output exceeded the core import handoff limits."

    async def _finish_timed_out(self, record: _JobRecord) -> None:
        try:
            cleaned = _remove_sensitive_job_files(record)
        except Exception:
            cleaned = False
        async with self._lock:
            record.state = JobState.TIMED_OUT
            record.cleanup_pending = not cleaned
            record.failure_reason = "DeepKOALA exceeded the configured execution timeout."
            record.completed_at = _now()

    async def _finish_cancelled(self, record: _JobRecord) -> None:
        async with self._lock:
            record.state = JobState.CANCELLED
            record.failure_reason = None
            record.diagnostic_text = ""
            record.diagnostics_truncated = False
            record.completed_at = _now()
        try:
            cleanup_job_directory(record.directory)
            record.artifacts_deleted = True
            record.cleanup_pending = False
        except Exception:
            record.cleanup_pending = True

    async def _cleanup_expired(self) -> None:
        self._retry_pending_cleanup()
        now = _now()
        expired: list[_JobRecord] = []
        async with self._lock:
            for plan_id, record in tuple(self._plans.items()):
                plan_expired = record.job_id is None and record.expires_at <= now
                result_expired = _record_retention_expired(record, now, self.config)
                if not (plan_expired or result_expired):
                    continue
                self._plans.pop(plan_id, None)
                if record.job_id is not None:
                    self._jobs.pop(record.job_id, None)
                expired.append(record)
        for record in expired:
            if record.artifacts_deleted:
                continue
            self._cleanup_or_defer(record.directory)

    async def _sweep_expired(self) -> None:
        interval = max(
            1,
            min(60, self.config.plan_ttl_seconds, self.config.retention_seconds),
        )
        while True:
            await asyncio.sleep(interval)
            await self._cleanup_expired()

    def _retry_pending_cleanup(self) -> None:
        for directory in tuple(self._pending_cleanup):
            try:
                cleanup_job_directory(directory)
            except (OSError, FilesystemSecurityError):
                continue
            self._pending_cleanup.discard(directory)

    def _cleanup_or_defer(self, directory: Path) -> None:
        try:
            cleanup_job_directory(directory)
        except (OSError, FilesystemSecurityError):
            self._pending_cleanup.add(directory)
        else:
            self._pending_cleanup.discard(directory)

    def _cleanup_if_created(self, value: object) -> None:
        if isinstance(value, Path):
            self._cleanup_or_defer(value)

    def _queue_snapshot_locked(self, *, planned: bool) -> QueueSnapshot:
        running = sum(record.state is JobState.RUNNING for record in self._jobs.values())
        queued = len(self._queue)
        if not planned:
            return QueueSnapshot(
                queue_capacity=self.config.max_queue_size,
                running_jobs=running,
                queued_jobs=queued,
            )
        self._require_execution_queue_capacity_locked()
        if running == 0:
            disposition = "running"
            position = None
        else:
            disposition = "queued"
            position = queued + 1
        return QueueSnapshot(
            queue_capacity=self.config.max_queue_size,
            running_jobs=running,
            queued_jobs=queued,
            planned_disposition=disposition,
            queue_position=position,
        )

    def _require_execution_queue_capacity_locked(self) -> None:
        if len(self._queue) >= self.config.max_queue_size:
            fail(
                ErrorCode.QUEUE_FULL,
                "The DeepKOALA execution queue is full.",
                suggested_action="Wait for or cancel a queued job before preparing another.",
            )

    def _remember_deleted_locked(self, job_id: str) -> None:
        if len(self._deleted_order) >= _MAX_DELETED_TOMBSTONES:
            expired = self._deleted_order.popleft()
            self._deleted.discard(expired)
        self._deleted.add(job_id)
        self._deleted_order.append(job_id)

    def _prepare_result(self, record: _JobRecord) -> PrepareDeepKoalaResult:
        return PrepareDeepKoalaResult(
            plan_id=record.plan_id,
            prepared_at=record.prepared_at,
            expires_at=record.expires_at,
            notice_sha256=record.notice_sha256,
            notice=record.notice,
            fasta=record.fasta,
            weight_artifacts=_weight_artifacts(record.probe, self.config),
        )

    def _job_summary(self, record: _JobRecord) -> JobSummary:
        if record.job_id is None or record.created_at is None:
            raise AssertionError("prepared plan does not have a public job summary")
        succeeded = record.state is JobState.SUCCEEDED
        terminal = record.state in _TERMINAL_STATES
        diagnostic_uri = (
            _resource_uri(record.job_id, "diagnostics")
            if terminal and record.diagnostic_text
            else None
        )
        return JobSummary(
            job_id=record.job_id,
            plan_id=record.plan_id,
            state=record.state,
            created_at=record.created_at,
            queued_at=record.queued_at,
            started_at=record.started_at,
            completed_at=record.completed_at,
            exit_code=record.exit_code,
            failure_reason=record.failure_reason,
            cleanup_pending=record.cleanup_pending,
            result_uri=_resource_uri(record.job_id, "output") if succeeded else None,
            provenance_uri=_resource_uri(record.job_id, "provenance") if succeeded else None,
            diagnostic_uri=diagnostic_uri,
            diagnostics_truncated=(
                terminal and bool(record.diagnostic_text) and record.diagnostics_truncated
            ),
            output_sha256=record.output.sha256 if succeeded and record.output else None,
            output_bytes=record.output.byte_size if succeeded and record.output else None,
            output_rows=record.output.row_count if succeeded and record.output else None,
        )

    def _handoff(self, record: _JobRecord) -> ImportHandoff:
        if record.job_id is None or record.completed_at is None:
            raise AssertionError("successful job omitted handoff identity")
        metadata = (
            SourceMetadataField(name="runner_version", value=__version__),
            SourceMetadataField(name="input_sha256", value=record.fasta.input_sha256),
            SourceMetadataField(name="requested_device", value=record.settings.requested_device),
            SourceMetadataField(name="resolved_device", value=record.settings.resolved_device),
            SourceMetadataField(name="weight_source", value=self.config.weight_source),
            SourceMetadataField(name="python_sha256", value=record.probe.python_artifact.sha256),
            SourceMetadataField(name="source_sha256", value=record.probe.source_artifact.sha256),
            SourceMetadataField(name="weight_sha256", value=record.probe.weight_artifact.sha256),
            SourceMetadataField(name="config_sha256", value=record.probe.config_artifact.sha256),
            SourceMetadataField(name="batch_size", value=record.settings.batch_size),
            SourceMetadataField(name="num_workers", value=record.settings.num_workers),
            SourceMetadataField(name="topk", value=record.settings.topk),
            SourceMetadataField(name="cpu_threads", value=record.settings.cpu_threads),
            SourceMetadataField(name="detail", value=True),
            SourceMetadataField(name="multi", value=False),
        )
        return ImportHandoff(
            payload_resource_uri=_resource_uri(record.job_id, "output"),
            source_provenance_template=SourceProvenanceTemplate(
                source_version=record.probe.source_version,
                model_name=record.settings.model,
                model_version=record.settings.resolved_model_date,
                annotation_date=record.completed_at,
                input_uri=f"mcp://deepkoala-mcp/jobs/{record.job_id}/output",
                source_metadata=metadata,
            ),
        )

    def _require_open(self) -> None:
        if not self._opened or self._closing:
            raise RuntimeError("job manager is not available")


def _settings(
    request: PrepareDeepKoalaInput,
    probe: DeepKoalaProbeResult,
    config: DeepKoalaRuntimeConfig,
) -> ExecutionSettings:
    return ExecutionSettings(
        model=request.model,
        requested_model_date=request.model_date,
        resolved_model_date=probe.resolved_date,
        requested_device=request.device,
        resolved_device=cast(Literal["cpu", "cuda", "mps"], probe.resolved_device),
        batch_size=request.batch_size or _DEFAULT_BATCH_SIZE,
        num_workers=(
            request.num_workers if request.num_workers is not None else _DEFAULT_NUM_WORKERS
        ),
        topk=request.topk or _DEFAULT_TOPK,
        cpu_threads=config.cpu_threads,
        timeout_seconds=request.timeout_seconds or config.default_timeout_seconds,
    )


def _notice(
    settings: ExecutionSettings,
    fasta: FastaSummary,
    probe: DeepKoalaProbeResult,
    config: DeepKoalaRuntimeConfig,
    queue: QueueSnapshot,
) -> ExecutionNotice:
    warnings: list[str] = []
    if settings.requested_device == "auto":
        warnings.append(
            "device=auto may select an available GPU; this plan resolved and pinned the reported "
            "device before submission."
        )
    warnings.append(
        "Installed model artifacts will be used as-is; the companion will not download, replace, "
        "or update weights."
    )
    return ExecutionNotice(
        settings=settings,
        fasta=fasta,
        weight_source=config.weight_source,
        resolved_weight_version=probe.resolved_date,
        execution_artifacts=_execution_artifacts(probe),
        queue=queue,
        warnings=tuple(warnings),
        updated_weights_url=UPDATED_WEIGHTS_URL,
    )


def _execution_artifacts(probe: DeepKoalaProbeResult) -> tuple[ExecutionArtifact, ...]:
    return (
        ExecutionArtifact(
            kind="configured_python",
            name=probe.python_artifact.basename,
            sha256=probe.python_artifact.sha256,
            size_bytes=probe.python_artifact.size_bytes,
        ),
        ExecutionArtifact(
            kind="deepkoala_source",
            name=probe.source_artifact.basename,
            sha256=probe.source_artifact.sha256,
            size_bytes=probe.source_artifact.size_bytes,
        ),
        ExecutionArtifact(
            kind="model_weights",
            name=probe.weight_artifact.basename,
            sha256=probe.weight_artifact.sha256,
            size_bytes=probe.weight_artifact.size_bytes,
        ),
        ExecutionArtifact(
            kind="model_config",
            name=probe.config_artifact.basename,
            sha256=probe.config_artifact.sha256,
            size_bytes=probe.config_artifact.size_bytes,
        ),
    )


def _weight_artifacts(
    probe: DeepKoalaProbeResult,
    config: DeepKoalaRuntimeConfig,
) -> tuple[WeightArtifact, ...]:
    return (
        WeightArtifact(
            name=probe.weight_artifact.basename,
            source=config.weight_source,
            resolved_version=probe.resolved_date,
            sha256=probe.weight_artifact.sha256,
            size_bytes=probe.weight_artifact.size_bytes,
        ),
        WeightArtifact(
            name=probe.config_artifact.basename,
            source=config.weight_source,
            resolved_version=probe.resolved_date,
            sha256=probe.config_artifact.sha256,
            size_bytes=probe.config_artifact.size_bytes,
        ),
    )


def _contract_fasta(summary: IntakeFastaSummary) -> FastaSummary:
    return FastaSummary(
        sequence_count=summary.sequence_count,
        total_residues=summary.total_residues,
        min_length=summary.min_length,
        max_length=summary.max_length,
        input_bytes=summary.input_bytes,
        input_sha256=summary.input_sha256,
    )


def _notice_digest(notice: ExecutionNotice) -> str:
    canonical = json.dumps(
        notice.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _resource_uri(job_id: str, section: str) -> str:
    return f"deepkoala-job://jobs/{job_id}/{section}"


def _provenance_document(
    record: _JobRecord,
    output: DetailedCsvSummary,
    completed_at: datetime,
    runner_version: str,
    config: DeepKoalaRuntimeConfig,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "runner": {"name": "deepkoala-mcp", "version": runner_version},
        "source": {
            "name": "deepkoala",
            "version": record.probe.source_version,
            "artifact": {
                "name": record.probe.source_artifact.basename,
                "size_bytes": record.probe.source_artifact.size_bytes,
                "sha256": record.probe.source_artifact.sha256,
            },
            "configured_python": {
                "name": record.probe.python_artifact.basename,
                "size_bytes": record.probe.python_artifact.size_bytes,
                "sha256": record.probe.python_artifact.sha256,
            },
        },
        "model": {
            "name": record.settings.model,
            "requested_date": record.settings.requested_model_date,
            "resolved_date": record.settings.resolved_model_date,
            "weight_source": record.notice.weight_source,
            "artifacts": [
                {
                    "name": record.probe.weight_artifact.basename,
                    "size_bytes": record.probe.weight_artifact.size_bytes,
                    "sha256": record.probe.weight_artifact.sha256,
                },
                {
                    "name": record.probe.config_artifact.basename,
                    "size_bytes": record.probe.config_artifact.size_bytes,
                    "sha256": record.probe.config_artifact.sha256,
                },
            ],
        },
        "execution": {
            "requested_device": record.settings.requested_device,
            "resolved_device": record.settings.resolved_device,
            "detail": True,
            "batch_size": record.settings.batch_size,
            "num_workers": record.settings.num_workers,
            "topk": record.settings.topk,
            "multi": False,
            "cpu_threads": record.settings.cpu_threads,
            "timeout_seconds": record.settings.timeout_seconds,
            "argv_template": _sanitized_argv(record, config),
            "started_at": record.started_at.isoformat() if record.started_at else None,
            "completed_at": completed_at.isoformat(),
            "exit_code": record.exit_code,
        },
        "diagnostics": {
            "truncated": record.diagnostics_truncated,
            "retained_bytes": len(record.diagnostic_text.encode("utf-8")),
        },
        "input": {
            "sha256": record.fasta.input_sha256,
            "bytes": record.fasta.input_bytes,
            "sequence_count": record.fasta.sequence_count,
            "total_residues": record.fasta.total_residues,
            "maximum_sequence_length": record.fasta.max_length,
        },
        "output": {
            "format": "deepkoala_detailed",
            "sha256": output.sha256,
            "bytes": output.byte_size,
            "rows": output.row_count,
            "columns": output.column_count,
        },
        "updated_weights_url": UPDATED_WEIGHTS_URL,
    }


def _runner_plan(record: _JobRecord, config: DeepKoalaRuntimeConfig) -> RunnerPlan:
    return RunnerPlan(
        python_executable=config.python_executable,
        checkout=config.checkout,
        job_directory=record.directory,
        model=record.settings.model,
        resolved_date=record.settings.resolved_model_date,
        resolved_device=record.settings.resolved_device or "cpu",
        batch_size_override=record.request.batch_size,
        num_workers_override=record.request.num_workers,
        topk_override=record.request.topk,
        timeout_seconds=record.settings.timeout_seconds,
        cpu_threads=record.settings.cpu_threads,
        diagnostic_max_bytes=config.diagnostic_max_bytes,
        max_output_bytes=config.max_output_bytes,
    )


def _sanitized_argv(record: _JobRecord, config: DeepKoalaRuntimeConfig) -> list[str]:
    plan = _runner_plan(record, config)
    argv = build_argv(plan)
    replacements = {
        str(plan.python_executable): "<configured-python>",
        str(plan.input_path): "<private-input>",
        str(plan.output_path): "<private-output>",
    }
    return [
        "<bounded-launcher>" if index == 2 and argv[1] == "-c" else replacements.get(value, value)
        for index, value in enumerate(argv)
    ]


def _remove_sensitive_job_files(record: _JobRecord) -> bool:
    cleaned = True
    for filename in ("input.fasta", "deepkoala-output.csv", "detailed.csv", "provenance.json"):
        try:
            remove_controlled_file(record.directory, filename)
        except (OSError, FilesystemSecurityError):
            cleaned = False
    if record.diagnostic_text:
        try:
            secure_write_bytes(
                record.directory,
                "diagnostics.txt",
                record.diagnostic_text.encode("utf-8"),
            )
        except (OSError, FilesystemSecurityError):
            cleaned = False
    return cleaned


def _artifact_path(record: _JobRecord, section: str) -> tuple[Path, str, str]:
    if section == "output" and record.state is JobState.SUCCEEDED and record.output is not None:
        return (
            record.directory / "detailed.csv",
            "text/csv; charset=utf-8",
            record.output.sha256,
        )
    if (
        section == "provenance"
        and record.state is JobState.SUCCEEDED
        and record.provenance_sha256 is not None
    ):
        return record.directory / "provenance.json", "application/json", record.provenance_sha256
    if section == "diagnostics" and record.diagnostic_text:
        return (
            record.directory / "diagnostics.txt",
            "text/plain; charset=utf-8",
            hashlib.sha256(record.diagnostic_text.encode("utf-8")).hexdigest(),
        )
    fail(
        ErrorCode.RESULT_NOT_FOUND,
        "The requested scoped result artifact is unavailable.",
        suggested_action="Refresh the job state and use a declared artifact resource.",
    )


def _inspect_installation(
    config: DeepKoalaRuntimeConfig,
) -> tuple[tuple[str, ...], str | None, bool]:
    package = config.checkout / "deepkoala"
    cli = package / "cli.py"
    source_version = _read_project_version(config.checkout / "pyproject.toml")
    deepkoala_available = (
        _direct_directory(package)
        and source_version is not None
        and _regular_file(package / "__init__.py")
        and _nonempty_regular_file(cli)
        and _nonempty_regular_file(package / "utils.py")
        and _regular_file(config.python_executable)
        and os.access(config.python_executable, os.X_OK)
    )
    dates: list[str] = []
    resources = config.checkout / "resources"
    inspected_entries = 0
    try:
        if not _direct_directory(resources):
            raise OSError
        for candidate in resources.iterdir():
            inspected_entries += 1
            if inspected_entries > _MAX_STATUS_RESOURCE_ENTRIES:
                dates = []
                break
            if not _MODEL_DATE_PATTERN.fullmatch(candidate.name):
                continue
            metadata = candidate.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                continue
            if any(
                _nonempty_regular_file(candidate / f"weights_{model}.pt")
                and _nonempty_regular_file(candidate / f"ko_config_{model}.json")
                for model in ("full", "frag")
            ):
                dates.append(candidate.name)
                if len(dates) > MAX_STATUS_MODEL_DATES:
                    dates = []
                    break
    except OSError:
        dates = []
    return tuple(sorted(dates)), source_version, deepkoala_available


def _regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _nonempty_regular_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_size > 0
    )


def _direct_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode)


def _read_project_version(path: Path) -> str | None:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
            or before.st_size > _MAX_SOURCE_VERSION_BYTES
        ):
            return None
        flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            return None
        raw = bytearray()
        while len(raw) <= _MAX_SOURCE_VERSION_BYTES:
            chunk = os.read(descriptor, min(65_536, _MAX_SOURCE_VERSION_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        final = path.lstat()
        if (
            len(raw) != before.st_size
            or len(raw) > _MAX_SOURCE_VERSION_BYTES
            or (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
            != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            or (after.st_dev, after.st_ino) != (final.st_dev, final.st_ino)
        ):
            return None
        document = cast(dict[str, object], tomllib.loads(raw.decode("utf-8")))
        project = document.get("project")
        if not isinstance(project, dict):
            return None
        project_table = cast(dict[str, object], project)
        version = project_table.get("version")
        if project_table.get("name") != "deepkoala":
            return None
        return (
            version
            if isinstance(version, str) and _SAFE_SOURCE_VERSION.fullmatch(version)
            else None
        )
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _map_probe_error(
    error: DeepKoalaProbeError,
    *,
    stale: bool = False,
) -> DeepKoalaMcpError:
    if stale or error.code == "artifacts_changed":
        code = ErrorCode.NOTICE_STALE
        message = "Installed DeepKOALA artifacts changed after the execution notice."
        action = "Prepare the job again and review the new artifact identities."
    elif "device" in error.code:
        code = ErrorCode.DEVICE_UNAVAILABLE
        message = "The configured DeepKOALA environment cannot use the requested device."
        action = "Select an available device or repair the configured DeepKOALA environment."
    elif "resource" in error.code or "artifact" in error.code:
        code = ErrorCode.WEIGHTS_NOT_FOUND
        message = "The requested installed DeepKOALA model resources are unavailable."
        action = "Install or select an existing local model date and prepare the job again."
    else:
        code = ErrorCode.DEEPKOALA_UNAVAILABLE
        message = "The configured DeepKOALA installation could not pass preflight."
        action = "Check the configured checkout and Python environment, then retry."
    return DeepKoalaMcpError(
        detail=ErrorDetail(
            code=code,
            message=message,
            recoverable=True,
            suggested_action=action,
            safe_details=(SafeDetail(name="preflight_stage", value=error.code),),
        )
    )


def _fail_plan_not_found() -> None:
    fail(
        ErrorCode.PLAN_NOT_FOUND,
        "The prepared plan is unavailable in this process scope.",
        suggested_action="Prepare a new job in the current companion session.",
    )


def _fail_job_not_found() -> None:
    fail(
        ErrorCode.JOB_NOT_FOUND,
        "The job is unavailable in this process scope.",
        suggested_action="Use a current job identifier from this companion session.",
    )


def _record_retention_expired(
    record: _JobRecord,
    now: datetime,
    config: DeepKoalaRuntimeConfig,
) -> bool:
    return (
        record.state in _TERMINAL_STATES
        and record.completed_at is not None
        and record.completed_at + timedelta(seconds=config.retention_seconds) <= now
    )


def _consume_task_exception(task: asyncio.Task[None]) -> None:
    """Retrieve any terminal task exception so it cannot become an event-loop warning."""
    with contextlib.suppress(asyncio.CancelledError):
        task.exception()


def _now() -> datetime:
    return datetime.now(UTC)


__all__ = ["ArtifactRange", "DeepKoalaJobManager"]
