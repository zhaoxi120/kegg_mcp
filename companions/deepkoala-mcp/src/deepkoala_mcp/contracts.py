"""Typed public contracts for bounded local DeepKOALA execution."""

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
MAX_RETAINED_JOBS = 32
MAX_RESOURCE_PAGE_BYTES = 65_536
HANDOFF_SCHEMA_VERSION = "1"
ANNOTATIONS_FILENAME = "deepkoala_annotations.csv"
RUN_REPORT_FILENAME = "deepkoala_run_report.md"
DEFAULT_MODEL_DATE = "202502"
JOB_ID_PATTERN = r"job_[a-f0-9]{32}"

JobId = Annotated[str, Field(pattern=rf"^{JOB_ID_PATTERN}$", max_length=36)]
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
    """Stable companion-owned technical error codes."""

    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_FASTA = "INVALID_FASTA"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    OUTPUT_NOT_ALLOWED = "OUTPUT_NOT_ALLOWED"
    OUTPUT_ALREADY_EXISTS = "OUTPUT_ALREADY_EXISTS"
    POLICY_DENIED = "POLICY_DENIED"
    DEEPKOALA_UNAVAILABLE = "DEEPKOALA_UNAVAILABLE"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    WEIGHTS_NOT_FOUND = "WEIGHTS_NOT_FOUND"
    RUNNER_BUSY = "RUNNER_BUSY"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"
    NOT_TERMINAL = "NOT_TERMINAL"
    ARTIFACT_NOT_FOUND = "ARTIFACT_NOT_FOUND"
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
    """Observable states after an atomic run request."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class CompanionRouteState(StrEnum):
    """Stable redacted deployment routes reported by status and doctor."""

    LOCAL_READY = "local_ready"
    DEEPKOALA_CHECKOUT_UNAVAILABLE = "deepkoala_checkout_unavailable"
    DEEPKOALA_RUNTIME_UNAVAILABLE = "deepkoala_runtime_unavailable"
    MODEL_RESOURCES_UNAVAILABLE = "model_resources_unavailable"
    MULTI_DEPENDENCIES_UNAVAILABLE = "multi_dependencies_unavailable"


class RunDeepKoalaInput(FrozenModel):
    """One policy-bounded local run using explicit shared filesystem paths."""

    fasta_path: str = Field(min_length=1, max_length=4_096)
    output_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_096,
        description=(
            "New or empty owner-only directory beneath an allowed output root. Omit to let the "
            "companion allocate a fresh directory beneath its configured output root."
        ),
    )
    model: ModelName = "full"
    model_date: ModelDate = DEFAULT_MODEL_DATE
    device: Literal["cpu", "cuda", "mps"] = "cpu"
    batch_size: int = Field(default=1, strict=True, ge=1, le=64)
    topk: int = Field(default=1, strict=True, ge=1, le=10)
    multi: bool = Field(default=False, strict=True)
    timeout_seconds: int | None = Field(default=None, strict=True, ge=1, le=86_400)

    @field_validator("fasta_path", "output_directory")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value or any(ord(character) < 32 for character in value):
            raise ValueError("filesystem paths must not contain control characters")
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("filesystem paths must be absolute and traversal-free")
        return value


class GetDeepKoalaJobInput(FrozenModel):
    """Identify one process-scoped job."""

    job_id: JobId


class CancelDeepKoalaJobInput(GetDeepKoalaJobInput):
    """Identify one running job to cancel."""


class DeleteDeepKoalaJobInput(GetDeepKoalaJobInput):
    """Identify one terminal job record to forget without deleting delivered files."""


class GetDeepKoalaStatusInput(FrozenModel):
    """Empty status input."""


class FastaSummary(FrozenModel):
    """Aggregate FASTA facts that contain no headers or sequences."""

    sequence_count: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_COUNT)
    total_residues: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_sequence_length: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_LENGTH)
    input_bytes: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)


class ExecutionPlan(FrozenModel):
    """Effective deployment-approved execution settings."""

    model: ModelName
    requested_model_date: ModelDate
    resolved_model_date: ResolvedModelDate
    device: Literal["cpu", "cuda", "mps"] = "cpu"
    detail: Literal[True] = True
    batch_size: int = Field(strict=True, ge=1, le=64)
    num_workers: Literal[0] = 0
    topk: int = Field(strict=True, ge=1, le=10)
    multi: bool = Field(default=False, strict=True)
    cpu_threads: int = Field(strict=True, ge=1, le=4)
    timeout_seconds: int = Field(strict=True, ge=1, le=86_400)


class JobSummary(FrozenModel):
    """Bounded lifecycle facts for one process-scoped job."""

    job_id: JobId
    state: JobState
    started_at: datetime
    completed_at: datetime | None = None
    exit_code: int | None = Field(default=None, strict=True, ge=-255, le=255)
    failure_reason: BoundedText | None = None
    correlation_id: str | None = Field(
        default=None,
        pattern=r"^joberr_[A-Za-z0-9_-]{12}$",
        max_length=19,
    )
    output_bytes: int | None = Field(default=None, strict=True, ge=1, le=MAX_OUTPUT_BYTES)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        terminal = {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }
        if self.started_at.utcoffset() is None or (
            self.completed_at is not None and self.completed_at.utcoffset() is None
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
        if self.correlation_id is not None and self.state is not JobState.FAILED:
            raise ValueError("only failed jobs may expose a correlation identifier")
        return self


class RunDeepKoalaResult(FrozenModel):
    """Immediate result for one validated local run."""

    job: JobSummary
    plan: ExecutionPlan
    fasta: FastaSummary


MetadataValue = str | int | float | bool | None


class SourceMetadataField(FrozenModel):
    """One source metadata field accepted by the core importer."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    value: MetadataValue


class SourceProvenance(FrozenModel):
    """Readable DeepKOALA source facts without digest or private job identifiers."""

    source_name: Literal["deepkoala"] = "deepkoala"
    source_version: str | None = Field(default=None, max_length=256)
    model_name: ModelName
    model_version: ResolvedModelDate
    annotation_date: datetime
    input_path: str = Field(min_length=1, max_length=4_096)
    source_metadata: Annotated[tuple[SourceMetadataField, ...], Field(max_length=16)] = ()

    @field_validator("input_path")
    @classmethod
    def validate_original_input_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("input_path must be a safe absolute path")
        return value

    @model_validator(mode="after")
    def validate_annotation_time(self) -> Self:
        if self.annotation_date.utcoffset() is None:
            raise ValueError("annotation_date must include a timezone")
        return self


class ImportHandoff(FrozenModel):
    """Versioned stable file handoff consumed by core normalization."""

    schema_version: Literal["1"]
    tool_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    input_path: str = Field(min_length=1, max_length=4_096)
    annotations_path: str = Field(min_length=1, max_length=4_096)
    report_path: str = Field(min_length=1, max_length=4_096)
    input_format: Literal["deepkoala_detailed"]
    annotations_resource_uri: str = Field(
        pattern=rf"^deepkoala://jobs/{JOB_ID_PATTERN}/annotations$",
        max_length=80,
    )
    report_resource_uri: str = Field(
        pattern=rf"^deepkoala://jobs/{JOB_ID_PATTERN}/report$",
        max_length=80,
    )
    source: SourceProvenance

    @field_validator("input_path", "annotations_path", "report_path")
    @classmethod
    def validate_absolute_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("handoff paths must be safe absolute paths")
        return value

    @model_validator(mode="after")
    def validate_paths(self) -> Self:
        if self.input_path != self.source.input_path:
            raise ValueError("handoff and source input paths must match")
        if self.annotations_path == self.report_path:
            raise ValueError("annotation and report paths must be distinct")
        return self


class GetDeepKoalaJobResult(FrozenModel):
    """Current state and a stable core-import handoff after successful execution."""

    job: JobSummary
    handoff: ImportHandoff | None = None

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        if (self.job.state is JobState.SUCCEEDED) != (self.handoff is not None):
            raise ValueError("only successful jobs expose a handoff")
        return self


class DeleteDeepKoalaJobResult(FrozenModel):
    """Confirmation that a terminal process record was removed."""

    job_id: JobId
    deleted: Literal[True] = True
    delivered_files_retained: Literal[True] = True


class InstalledResource(FrozenModel):
    """One structurally available local model/date pair."""

    model: ModelName
    model_date: ResolvedModelDate


class CompanionStatus(FrozenModel):
    """Redacted deployment policy, runtime readiness, and active state."""

    server_name: Literal["deepkoala-mcp"] = "deepkoala-mcp"
    server_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$", max_length=32)
    ready: bool
    runtime_ready: bool
    cuda_available: bool
    mps_available: bool
    deepkoala_version: str | None = Field(default=None, max_length=128)
    installed_resources: Annotated[tuple[InstalledResource, ...], Field(max_length=256)]
    allowed_models: Annotated[tuple[ModelName, ...], Field(min_length=1, max_length=2)]
    device_policy: Literal["cpu"] = "cpu"
    allowed_devices: Annotated[
        tuple[Literal["cpu", "cuda", "mps"], ...], Field(min_length=1, max_length=2)
    ]
    gpu_visibility_inherited: Literal[True] = True
    downloads_enabled: Literal[False] = False
    companion_network_requests: Literal[False] = False
    allow_multi: bool = Field(default=False, strict=True)
    multi_ready: bool = Field(default=False, strict=True)
    route_state: CompanionRouteState
    issue: BoundedText | None
    next_action: BoundedText
    cpu_threads: int = Field(strict=True, ge=1, le=4)
    max_concurrent_jobs: Literal[1] = 1
    running_jobs: int = Field(strict=True, ge=0, le=1)
    terminal_jobs: int = Field(strict=True, ge=0, le=MAX_RETAINED_JOBS)
    max_input_bytes: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_sequences: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_COUNT)
    max_output_bytes: int = Field(strict=True, ge=1, le=MAX_OUTPUT_BYTES)
    max_timeout_seconds: int = Field(strict=True, ge=1, le=86_400)
    input_root_count: int = Field(strict=True, ge=1)
    output_root_count: int = Field(strict=True, ge=1)
    file_handoff_enabled: Literal[True] = True
    resource_fallback_enabled: Literal[True] = True

    @model_validator(mode="after")
    def validate_readiness(self) -> Self:
        if self.allowed_devices not in {("cpu",), ("cpu", "cuda"), ("cpu", "mps")}:
            raise ValueError("allowed_devices must preserve the CPU default")
        if self.ready != (self.runtime_ready and bool(self.installed_resources)):
            raise ValueError("ready must match runtime and resource availability")
        if self.multi_ready and not self.allow_multi:
            raise ValueError("multi_ready requires deployment authorization")
        if self.route_state is CompanionRouteState.LOCAL_READY:
            if (
                not self.ready
                or (self.allow_multi and not self.multi_ready)
                or self.issue is not None
            ):
                raise ValueError("local_ready must match base and optional multi readiness")
        elif self.issue is None:
            raise ValueError("non-ready routes require one stable issue")
        if self.route_state is CompanionRouteState.MULTI_DEPENDENCIES_UNAVAILABLE and not (
            self.ready and self.allow_multi and not self.multi_ready
        ):
            raise ValueError("multi dependency route must match effective readiness")
        return self


__all__ = [
    "ANNOTATIONS_FILENAME",
    "HANDOFF_SCHEMA_VERSION",
    "MAX_FASTA_BYTES",
    "MAX_HEADER_BYTES",
    "MAX_OUTPUT_BYTES",
    "MAX_RESOURCE_PAGE_BYTES",
    "MAX_RETAINED_JOBS",
    "MAX_SEQUENCE_COUNT",
    "MAX_SEQUENCE_LENGTH",
    "RUN_REPORT_FILENAME",
    "CancelDeepKoalaJobInput",
    "CompanionRouteState",
    "CompanionStatus",
    "DeepKoalaMcpError",
    "DeleteDeepKoalaJobInput",
    "DeleteDeepKoalaJobResult",
    "ErrorCode",
    "ErrorDetail",
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
    "RunDeepKoalaInput",
    "RunDeepKoalaResult",
    "SafeDetail",
    "SourceMetadataField",
    "SourceProvenance",
    "ToolEnvelope",
    "ToolPayload",
    "fail",
]
