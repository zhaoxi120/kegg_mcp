"""Typed public contracts for the local DeepKOALA companion."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Generic, Literal, NoReturn, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_FASTA_BYTES = 5_000_000
MAX_OUTPUT_BYTES = 5_000_000
MAX_SEQUENCE_COUNT = 100_000
MAX_SEQUENCE_LENGTH = 100_000
MAX_HEADER_BYTES = 1_024
MAX_QUEUE_SIZE = 8

JobId = Annotated[str, Field(pattern=r"^job_[a-f0-9]{32}$", max_length=36)]
ModelName = Literal["full", "frag"]
ModelDate = Annotated[
    str,
    Field(pattern=r"^(?:latest|[0-9]{4}(?:0[1-9]|1[0-2]))$", max_length=6),
]
ResolvedModelDate = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}(?:0[1-9]|1[0-2])$", max_length=6),
]
BoundedText = Annotated[str, Field(min_length=1, max_length=1_000)]


class FrozenModel(BaseModel):
    """Strict immutable base for public contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        hide_input_in_errors=True,
    )


class ErrorCode(StrEnum):
    """Stable companion-owned error codes."""

    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_FASTA = "INVALID_FASTA"
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    DEEPKOALA_UNAVAILABLE = "DEEPKOALA_UNAVAILABLE"
    WEIGHTS_NOT_FOUND = "WEIGHTS_NOT_FOUND"
    QUEUE_FULL = "QUEUE_FULL"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"
    NOT_TERMINAL = "NOT_TERMINAL"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SafeDetail(FrozenModel):
    """One bounded non-sensitive error fact."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    value: str = Field(max_length=1_000)


class ErrorDetail(FrozenModel):
    """Serializable failure without FASTA, environment, or private paths."""

    code: ErrorCode
    message: BoundedText
    recoverable: bool = True
    suggested_action: BoundedText
    safe_details: Annotated[tuple[SafeDetail, ...], Field(max_length=16)] = ()


class DeepKoalaMcpError(Exception):
    """Exception carrying a schema-conforming public error."""

    def __init__(self, detail: ErrorDetail) -> None:
        super().__init__(detail.message)
        self.detail = detail


def fail(
    code: ErrorCode,
    message: str,
    *,
    suggested_action: str,
    safe_details: tuple[SafeDetail, ...] = (),
) -> NoReturn:
    """Raise a bounded recoverable companion error."""
    raise DeepKoalaMcpError(
        ErrorDetail(
            code=code,
            message=message,
            suggested_action=suggested_action,
            safe_details=safe_details,
        )
    )


T = TypeVar("T")


class ToolPayload(FrozenModel, Generic[T]):
    """Typed successful data returned by one tool."""

    data: T


class ToolEnvelope(FrozenModel, Generic[T]):
    """One output schema for successful and repairable tool results."""

    ok: bool
    result: ToolPayload[T] | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("successful envelopes require only result")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("failed envelopes require only error")
        return self


class JobState(StrEnum):
    """Complete lifecycle of one opaque job identifier."""

    PREPARED = "prepared"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class PrepareDeepKoalaInput(FrozenModel):
    """Bounded input used to prepare, but not launch, one local CPU job."""

    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "required": ["fasta_text"],
                    "properties": {"fasta_path": {"type": "null"}},
                },
                {
                    "required": ["fasta_path"],
                    "properties": {"fasta_text": {"type": "null"}},
                },
            ]
        }
    )

    fasta_text: str | None = Field(default=None, min_length=1, max_length=MAX_FASTA_BYTES)
    fasta_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    model: ModelName = "full"
    model_date: ModelDate = "latest"
    batch_size: int = Field(default=1, strict=True, ge=1, le=64)
    topk: int = Field(default=1, strict=True, ge=1, le=10)
    timeout_seconds: int | None = Field(default=None, strict=True, ge=1, le=86_400)

    @field_validator("fasta_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("fasta_path must not contain NUL")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("fasta_path must be absolute and traversal-free")
        return value

    @model_validator(mode="after")
    def require_one_source(self) -> Self:
        if (self.fasta_text is None) == (self.fasta_path is None):
            raise ValueError("provide exactly one of fasta_text or fasta_path")
        return self


class SubmitDeepKoalaInput(FrozenModel):
    """Explicit acknowledgement for one server-retained prepared job."""

    job_id: JobId
    acknowledged: Literal[True]


class GetDeepKoalaJobInput(FrozenModel):
    """Identify one process-scoped job."""

    job_id: JobId


class CancelDeepKoalaJobInput(GetDeepKoalaJobInput):
    """Identify one prepared, queued, or running job to cancel."""


class DeleteDeepKoalaJobInput(GetDeepKoalaJobInput):
    """Identify one terminal job to delete."""


class GetDeepKoalaStatusInput(FrozenModel):
    """Empty status input."""


class FastaSummary(FrozenModel):
    """Aggregate FASTA facts that contain no headers or sequences."""

    sequence_count: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_COUNT)
    total_residues: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_sequence_length: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_LENGTH)
    input_bytes: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)


class ExecutionPlan(FrozenModel):
    """Effective CPU-only execution settings retained by the server."""

    model: ModelName
    requested_model_date: ModelDate
    resolved_model_date: ResolvedModelDate
    device: Literal["cpu"] = "cpu"
    detail: Literal[True] = True
    batch_size: int = Field(strict=True, ge=1, le=64)
    num_workers: Literal[0] = 0
    topk: int = Field(strict=True, ge=1, le=10)
    multi: Literal[False] = False
    cpu_threads: int = Field(strict=True, ge=1, le=4)
    timeout_seconds: int = Field(strict=True, ge=1, le=86_400)


class ExecutionNotice(FrozenModel):
    """Facts shown before any DeepKOALA process starts."""

    plan: ExecutionPlan
    fasta: FastaSummary
    deepkoala_version: str | None = Field(default=None, max_length=128)
    queued_jobs_ahead: int = Field(strict=True, ge=0, le=MAX_QUEUE_SIZE)
    cpu_only: Literal[True] = True
    downloads_enabled: Literal[False] = False
    companion_network_requests: Literal[False] = False
    warning: BoundedText = (
        "DeepKOALA will run locally on CPU with existing installed resources; the companion "
        "does not download or update weights."
    )


class PrepareDeepKoalaResult(FrozenModel):
    """One retained plan awaiting explicit acknowledgement."""

    job_id: JobId
    state: Literal["prepared"] = "prepared"
    prepared_at: datetime
    expires_at: datetime
    notice: ExecutionNotice

    @model_validator(mode="after")
    def validate_times(self) -> Self:
        if self.prepared_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        if self.expires_at <= self.prepared_at:
            raise ValueError("expires_at must follow prepared_at")
        return self


class JobSummary(FrozenModel):
    """Bounded lifecycle facts for one process-scoped job."""

    job_id: JobId
    state: JobState
    prepared_at: datetime
    submitted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = Field(default=None, strict=True, ge=-255, le=255)
    failure_reason: BoundedText | None = None
    output_bytes: int | None = Field(default=None, strict=True, ge=1, le=MAX_OUTPUT_BYTES)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        terminal = {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }
        if any(
            value is not None and value.utcoffset() is None
            for value in (
                self.prepared_at,
                self.submitted_at,
                self.started_at,
                self.completed_at,
            )
        ):
            raise ValueError("timestamps must be timezone-aware")
        if (self.state in terminal) != (self.completed_at is not None):
            raise ValueError("only terminal jobs have completed_at")
        if self.state is JobState.SUCCEEDED and self.output_bytes is None:
            raise ValueError("successful jobs require output_bytes")
        if self.state is not JobState.SUCCEEDED and self.output_bytes is not None:
            raise ValueError("only successful jobs expose output_bytes")
        if self.state in {JobState.FAILED, JobState.TIMED_OUT} and self.failure_reason is None:
            raise ValueError("failed and timed-out jobs require failure_reason")
        return self


MetadataValue = str | int | float | bool | None


class SourceMetadataField(FrozenModel):
    """One source metadata field accepted by the core importer."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    value: MetadataValue


class SourceProvenance(FrozenModel):
    """Readable fields aligned with the core SourceProvenanceInput contract."""

    source_name: Literal["deepkoala"] = "deepkoala"
    source_version: str | None = Field(default=None, max_length=256)
    model_name: ModelName
    model_version: ResolvedModelDate
    annotation_date: datetime
    input_uri: str = Field(pattern=r"^mcp://deepkoala-mcp/jobs/job_[a-f0-9]{32}/output$")
    input_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    source_metadata: Annotated[tuple[SourceMetadataField, ...], Field(max_length=16)] = ()

    @field_validator("input_path")
    @classmethod
    def validate_original_input_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("input_path must be a safe absolute path")
        return value


class ImportHandoff(FrozenModel):
    """File handoff consumed by the current core normalization tool."""

    output_path: str = Field(min_length=1, max_length=4_096)
    input_format: Literal["deepkoala_detailed"] = "deepkoala_detailed"
    source: SourceProvenance

    @field_validator("output_path")
    @classmethod
    def validate_output_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("output_path must be a safe absolute path")
        return value


class GetDeepKoalaJobResult(FrozenModel):
    """Current state and a core-import handoff after successful execution."""

    job: JobSummary
    handoff: ImportHandoff | None = None

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        if (self.job.state is JobState.SUCCEEDED) != (self.handoff is not None):
            raise ValueError("only successful jobs expose a handoff")
        return self


class DeleteDeepKoalaJobResult(FrozenModel):
    """Confirmation that a terminal job was removed."""

    job_id: JobId
    deleted: Literal[True] = True


class InstalledResource(FrozenModel):
    """One structurally available local model/date pair."""

    model: ModelName
    model_date: ResolvedModelDate


class CompanionStatus(FrozenModel):
    """Redacted CPU-only readiness and scheduler state."""

    server_name: Literal["deepkoala-mcp"] = "deepkoala-mcp"
    server_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    ready: bool
    deepkoala_version: str | None = Field(default=None, max_length=128)
    installed_resources: Annotated[tuple[InstalledResource, ...], Field(max_length=256)]
    cpu_only: Literal[True] = True
    downloads_enabled: Literal[False] = False
    companion_network_requests: Literal[False] = False
    cpu_threads: int = Field(strict=True, ge=1, le=4)
    max_concurrent_jobs: Literal[1] = 1
    max_queue_size: int = Field(strict=True, ge=1, le=MAX_QUEUE_SIZE)
    prepared_jobs: int = Field(strict=True, ge=0, le=MAX_QUEUE_SIZE + 1)
    queued_jobs: int = Field(strict=True, ge=0, le=MAX_QUEUE_SIZE)
    running_jobs: int = Field(strict=True, ge=0, le=1)
    max_input_bytes: Literal[5_000_000] = MAX_FASTA_BYTES
    max_output_bytes: Literal[5_000_000] = MAX_OUTPUT_BYTES

    @model_validator(mode="after")
    def validate_readiness(self) -> Self:
        if self.ready != bool(self.installed_resources):
            raise ValueError("ready must match installed resource availability")
        return self


__all__ = [
    "MAX_FASTA_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_QUEUE_SIZE",
    "MAX_SEQUENCE_COUNT",
    "MAX_SEQUENCE_LENGTH",
    "CancelDeepKoalaJobInput",
    "CompanionStatus",
    "DeepKoalaMcpError",
    "DeleteDeepKoalaJobInput",
    "DeleteDeepKoalaJobResult",
    "ErrorCode",
    "ErrorDetail",
    "ExecutionNotice",
    "ExecutionPlan",
    "FastaSummary",
    "GetDeepKoalaJobInput",
    "GetDeepKoalaJobResult",
    "GetDeepKoalaStatusInput",
    "ImportHandoff",
    "InstalledResource",
    "JobState",
    "JobSummary",
    "ModelName",
    "PrepareDeepKoalaInput",
    "PrepareDeepKoalaResult",
    "SafeDetail",
    "SourceMetadataField",
    "SourceProvenance",
    "SubmitDeepKoalaInput",
    "ToolEnvelope",
    "ToolPayload",
    "fail",
]
