"""Strict public contracts for the local DeepKOALA MCP companion."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Generic, Literal, NoReturn, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SERVER_NAME = "deepkoala-mcp"
UPDATED_WEIGHTS_URL = "https://www.genome.jp/ftp/db/deepkoala/"

MAX_FASTA_BYTES = 5_000_000
MAX_OUTPUT_BYTES = 5_000_000
MAX_DIAGNOSTIC_BYTES = 65_536
MAX_QUEUE_SIZE = 32
MAX_SEQUENCE_COUNT = 100_000
MAX_SEQUENCE_LENGTH = 100_000
MAX_HEADER_LENGTH = 1_024
MAX_WEIGHT_ARTIFACTS = 64
MAX_SOURCE_METADATA_FIELDS = 32
MAX_STATUS_MODEL_DATES = 128

PlanId = Annotated[str, Field(pattern=r"^plan_[A-Za-z0-9_-]{32}$", max_length=37)]
JobId = Annotated[str, Field(pattern=r"^job_[A-Za-z0-9_-]{32}$", max_length=36)]
Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", max_length=64)]
ModelName = Literal["full", "frag"]
ModelDateSelector = Annotated[
    str,
    Field(pattern=r"^(?:latest|[0-9]{4}(?:0[1-9]|1[0-2]))$", max_length=6),
]
ResolvedModelDate = Annotated[
    str,
    Field(pattern=r"^[0-9]{4}(?:0[1-9]|1[0-2])$", max_length=6),
]
RequestedDevice = Literal["auto", "cpu", "cuda", "mps"]
ResolvedDevice = Literal["cpu", "cuda", "mps"]
WeightSource = Literal["github_bundled", "user_provided"]
BoundedMessage = Annotated[str, Field(min_length=1, max_length=1_000)]
BoundedLabel = Annotated[str, Field(min_length=1, max_length=256)]
ResourceUri = Annotated[
    str,
    Field(
        pattern=(
            r"^deepkoala-job://jobs/job_[A-Za-z0-9_-]{32}/"
            r"(?:output|provenance|diagnostics)$"
        ),
        max_length=128,
    ),
]
LogicalInputUri = Annotated[
    str,
    Field(
        pattern=r"^mcp://deepkoala-mcp/jobs/job_[A-Za-z0-9_-]{32}/output$",
        max_length=128,
    ),
]


class FrozenModel(BaseModel):
    """Shared immutable and strict configuration for every public model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={"$schema": JSON_SCHEMA_DIALECT},
    )


class ErrorCode(StrEnum):
    """Stable errors owned by the companion rather than the core server."""

    CONFIGURATION_INVALID = "CONFIGURATION_INVALID"
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_FASTA = "INVALID_FASTA"
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
    PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
    RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
    DEEPKOALA_UNAVAILABLE = "DEEPKOALA_UNAVAILABLE"
    WEIGHTS_NOT_FOUND = "WEIGHTS_NOT_FOUND"
    DEVICE_UNAVAILABLE = "DEVICE_UNAVAILABLE"
    PLAN_NOT_FOUND = "PLAN_NOT_FOUND"
    PLAN_EXPIRED = "PLAN_EXPIRED"
    NOTICE_STALE = "NOTICE_STALE"
    QUEUE_FULL = "QUEUE_FULL"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    JOB_NOT_CANCELLABLE = "JOB_NOT_CANCELLABLE"
    NOT_TERMINAL = "NOT_TERMINAL"
    PROCESS_LAUNCH_FAILED = "PROCESS_LAUNCH_FAILED"
    PROCESS_FAILED = "PROCESS_FAILED"
    OUTPUT_INVALID = "OUTPUT_INVALID"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    RESULT_NOT_FOUND = "RESULT_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SafeDetail(FrozenModel):
    """One explicitly bounded and non-sensitive error detail."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    value: str = Field(max_length=1_000)


class ErrorDetail(FrozenModel):
    """Serializable companion error that never carries FASTA or local paths."""

    code: ErrorCode
    message: BoundedMessage
    recoverable: bool
    suggested_action: BoundedMessage | None = None
    safe_details: Annotated[tuple[SafeDetail, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def require_recovery_action(self) -> Self:
        if self.recoverable and self.suggested_action is None:
            raise ValueError("recoverable errors require a suggested_action")
        return self


class DeepKoalaMcpError(Exception):
    """Exception carrying one schema-conforming companion error."""

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
    """Raise a recoverable error without embedding raw input or local locations."""
    raise DeepKoalaMcpError(
        ErrorDetail(
            code=code,
            message=message,
            recoverable=True,
            suggested_action=suggested_action,
            safe_details=safe_details,
        )
    )


T = TypeVar("T")


class ToolPayload(FrozenModel, Generic[T]):
    """Typed successful data returned by one companion tool."""

    data: T


class ToolEnvelope(FrozenModel, Generic[T]):
    """Discriminated success or failure envelope shared by companion tools."""

    model_config = ConfigDict(
        json_schema_extra={
            "$schema": JSON_SCHEMA_DIALECT,
            "oneOf": [
                {
                    "properties": {
                        "ok": {"const": True},
                        "result": {"not": {"type": "null"}},
                        "error": {"type": "null"},
                    },
                    "required": ["ok", "result", "error"],
                },
                {
                    "properties": {
                        "ok": {"const": False},
                        "result": {"type": "null"},
                        "error": {"not": {"type": "null"}},
                    },
                    "required": ["ok", "result", "error"],
                },
            ],
        }
    )

    ok: bool
    result: ToolPayload[T] | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("successful tool envelopes require only result")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("failed tool envelopes require only error")
        return self


class JobState(StrEnum):
    """Complete companion plan and job lifecycle."""

    PREPARED = "prepared"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class PrepareDeepKoalaInput(FrozenModel):
    """Bounded FASTA request used to prepare, but not launch, one execution."""

    model_config = ConfigDict(
        json_schema_extra={
            "$schema": JSON_SCHEMA_DIALECT,
            "oneOf": [
                {
                    "properties": {
                        "fasta_text": {"type": "string"},
                        "fasta_path": {"type": "null"},
                    },
                    "required": ["fasta_text"],
                },
                {
                    "properties": {
                        "fasta_text": {"type": "null"},
                        "fasta_path": {"type": "string"},
                    },
                    "required": ["fasta_path"],
                },
            ],
        }
    )

    fasta_text: str | None = Field(default=None, min_length=1, max_length=MAX_FASTA_BYTES)
    fasta_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    model: ModelName = "full"
    model_date: ModelDateSelector = "latest"
    device: RequestedDevice = "auto"
    batch_size: int | None = Field(default=None, strict=True, ge=1, le=1_024)
    num_workers: int | None = Field(default=None, strict=True, ge=0, le=16)
    topk: int | None = Field(default=None, strict=True, ge=1, le=10)
    multi: Literal[False] = False
    timeout_seconds: int | None = Field(default=None, strict=True, ge=1, le=86_400)

    @field_validator("fasta_path")
    @classmethod
    def validate_absolute_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "\x00" in value:
            raise ValueError("fasta_path must not contain a NUL character")
        from pathlib import Path

        path = Path(value)
        if not path.is_absolute():
            raise ValueError("fasta_path must be absolute")
        if ".." in path.parts:
            raise ValueError("fasta_path must not contain traversal segments")
        return value

    @model_validator(mode="after")
    def require_one_fasta_source(self) -> Self:
        if (self.fasta_text is None) == (self.fasta_path is None):
            raise ValueError("provide exactly one of fasta_text or fasta_path")
        return self


class SubmitDeepKoalaInput(FrozenModel):
    """Acknowledgement required to submit the exact reviewed plan."""

    plan_id: PlanId
    notice_sha256: Sha256
    acknowledged: Literal[True]


class GetDeepKoalaJobInput(FrozenModel):
    """Identify one session-scoped job for a status read."""

    job_id: JobId


class CancelDeepKoalaJobInput(FrozenModel):
    """Identify one queued or running job for cancellation."""

    job_id: JobId


class DeleteDeepKoalaJobInput(FrozenModel):
    """Identify one terminal job for controlled artifact deletion."""

    job_id: JobId


class GetDeepKoalaStatusInput(FrozenModel):
    """Empty input for the read-only companion status tool."""


class FastaSummary(FrozenModel):
    """Non-sequence FASTA facts safe to display and retain."""

    sequence_count: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_COUNT)
    total_residues: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    min_length: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_length: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_LENGTH)
    input_bytes: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    input_sha256: Sha256

    @model_validator(mode="after")
    def validate_lengths(self) -> Self:
        if self.min_length > self.max_length:
            raise ValueError("min_length must not exceed max_length")
        if self.max_length > self.total_residues:
            raise ValueError("max_length must not exceed total_residues")
        return self


class WeightArtifact(FrozenModel):
    """Path-free identity for one installed weight or threshold artifact."""

    name: BoundedLabel
    source: WeightSource
    resolved_version: BoundedLabel | None = None
    sha256: Sha256 | None = None
    size_bytes: int | None = Field(default=None, strict=True, ge=1, le=10_000_000_000)


class ExecutionArtifact(FrozenModel):
    """Path-free identity for code, interpreter, model, or threshold input."""

    kind: Literal["configured_python", "deepkoala_source", "model_weights", "model_config"]
    name: BoundedLabel
    sha256: Sha256
    size_bytes: int = Field(strict=True, ge=1, le=10_000_000_000)


class ExecutionSettings(FrozenModel):
    """Effective immutable settings shared by the notice and provenance."""

    model: ModelName
    requested_model_date: ModelDateSelector
    resolved_model_date: ResolvedModelDate
    requested_device: RequestedDevice
    resolved_device: ResolvedDevice | None = None
    detail: Literal[True] = True
    batch_size: int = Field(strict=True, ge=1, le=1_024)
    num_workers: int = Field(strict=True, ge=0, le=16)
    topk: int = Field(strict=True, ge=1, le=10)
    multi: Literal[False] = False
    cpu_threads: int = Field(strict=True, ge=1, le=32)
    timeout_seconds: int = Field(strict=True, ge=1, le=86_400)


class QueueSnapshot(FrozenModel):
    """Bounded scheduler state with job concurrency kept distinct from batching."""

    max_concurrent_jobs: Literal[1] = 1
    queue_capacity: int = Field(strict=True, ge=1, le=MAX_QUEUE_SIZE)
    running_jobs: int = Field(strict=True, ge=0, le=1)
    queued_jobs: int = Field(strict=True, ge=0, le=MAX_QUEUE_SIZE)
    planned_disposition: Literal["running", "queued"] | None = None
    queue_position: int | None = Field(default=None, strict=True, ge=1, le=MAX_QUEUE_SIZE)

    @model_validator(mode="after")
    def validate_queue(self) -> Self:
        if self.queued_jobs > self.queue_capacity:
            raise ValueError("queued_jobs must not exceed queue_capacity")
        if self.planned_disposition == "running" and self.running_jobs != 0:
            raise ValueError("a planned running job requires an available runner slot")
        if self.planned_disposition == "queued" and self.queue_position is None:
            raise ValueError("a planned queued job requires queue_position")
        if self.planned_disposition != "queued" and self.queue_position is not None:
            raise ValueError("queue_position is valid only for a planned queued job")
        return self


class ExecutionNotice(FrozenModel):
    """Structured user-visible facts that must be acknowledged before launch."""

    settings: ExecutionSettings
    fasta: FastaSummary
    weight_source: WeightSource
    resolved_weight_version: BoundedLabel | None = None
    execution_artifacts: Annotated[tuple[ExecutionArtifact, ...], Field(min_length=4, max_length=4)]
    queue: QueueSnapshot
    warnings: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=500)], ...],
        Field(min_length=1, max_length=4),
    ]
    updated_weights_url: Literal["https://www.genome.jp/ftp/db/deepkoala/"] = UPDATED_WEIGHTS_URL

    @model_validator(mode="after")
    def require_auto_device_warning(self) -> Self:
        if self.settings.requested_device == "auto" and not any(
            "GPU" in warning and "auto" in warning for warning in self.warnings
        ):
            raise ValueError("device=auto notices must warn that an available GPU may be used")
        artifact_kinds = tuple(artifact.kind for artifact in self.execution_artifacts)
        if set(artifact_kinds) != {
            "configured_python",
            "deepkoala_source",
            "model_weights",
            "model_config",
        }:
            raise ValueError("execution notice requires one identity for each execution artifact")
        return self


class PrepareDeepKoalaResult(FrozenModel):
    """Prepared plan and digest returned before any DeepKOALA process starts."""

    plan_id: PlanId
    state: Literal["prepared"] = "prepared"
    prepared_at: datetime
    expires_at: datetime
    notice_sha256: Sha256
    notice: ExecutionNotice
    fasta: FastaSummary
    weight_artifacts: Annotated[
        tuple[WeightArtifact, ...], Field(min_length=1, max_length=MAX_WEIGHT_ARTIFACTS)
    ]

    @field_validator("prepared_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_expiry(self) -> Self:
        if self.expires_at <= self.prepared_at:
            raise ValueError("expires_at must be later than prepared_at")
        return self


class JobSummary(FrozenModel):
    """Path-free job lifecycle, safe diagnostics, and bounded result identity."""

    job_id: JobId
    plan_id: PlanId
    state: JobState
    created_at: datetime
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    exit_code: int | None = Field(default=None, strict=True, ge=-255, le=255)
    failure_reason: BoundedMessage | None = None
    cleanup_pending: bool = False
    result_uri: ResourceUri | None = None
    provenance_uri: ResourceUri | None = None
    diagnostic_uri: ResourceUri | None = None
    diagnostics_truncated: bool = False
    output_sha256: Sha256 | None = None
    output_bytes: int | None = Field(default=None, strict=True, ge=1, le=MAX_OUTPUT_BYTES)
    output_rows: int | None = Field(default=None, strict=True, ge=1, le=MAX_SEQUENCE_COUNT)

    @field_validator("created_at", "queued_at", "started_at", "completed_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_state_details(self) -> Self:
        terminal = {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }
        if self.state in terminal and self.completed_at is None:
            raise ValueError("terminal jobs require completed_at")
        if self.state not in terminal and self.completed_at is not None:
            raise ValueError("non-terminal jobs must not have completed_at")
        states_requiring_start = {
            JobState.RUNNING,
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.TIMED_OUT,
        }
        if self.state in states_requiring_start and self.started_at is None:
            raise ValueError("started or terminal jobs require started_at")
        if self.state in {JobState.FAILED, JobState.TIMED_OUT} and self.failure_reason is None:
            raise ValueError("failed and timed-out jobs require a safe failure_reason")
        output_fields = (
            self.result_uri,
            self.provenance_uri,
            self.output_sha256,
            self.output_bytes,
            self.output_rows,
        )
        if self.state is JobState.SUCCEEDED and any(value is None for value in output_fields):
            raise ValueError("successful jobs require complete output metadata")
        if self.state is not JobState.SUCCEEDED and any(
            value is not None for value in output_fields
        ):
            raise ValueError("only successful jobs may expose output metadata")
        if self.state is JobState.SUCCEEDED and self.exit_code != 0:
            raise ValueError("successful jobs require exit_code=0")
        if self.diagnostic_uri is not None and self.state not in terminal:
            raise ValueError("diagnostic resources are available only for terminal jobs")
        if self.diagnostics_truncated and self.diagnostic_uri is None:
            raise ValueError("truncated diagnostics require a diagnostic resource")
        if self.failure_reason is not None and (
            "/" in self.failure_reason or "\\" in self.failure_reason
        ):
            raise ValueError("failure_reason must not contain local path separators")
        return self


JsonMetadataValue = (
    Annotated[str, Field(max_length=1_000)]
    | Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
    | Annotated[float, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
    | bool
    | None
)


class SourceMetadataField(FrozenModel):
    """One JSON-compatible provenance field for the core importer template."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    value: JsonMetadataValue


class SourceProvenanceTemplate(FrozenModel):
    """Fields accepted by the core source-agnostic DeepKOALA importer boundary."""

    source_name: Literal["deepkoala"] = "deepkoala"
    source_version: BoundedLabel | None = None
    model_name: ModelName
    model_version: ResolvedModelDate
    annotation_date: datetime
    input_uri: LogicalInputUri
    source_metadata: Annotated[
        tuple[SourceMetadataField, ...], Field(max_length=MAX_SOURCE_METADATA_FIELDS)
    ] = ()

    @field_validator("annotation_date")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("annotation_date must include a timezone")
        return value

    @model_validator(mode="after")
    def require_unique_metadata_names(self) -> Self:
        names = tuple(field.name for field in self.source_metadata)
        if len(names) != len(set(names)):
            raise ValueError("source_metadata names must be unique")
        return self


class ImportHandoff(FrozenModel):
    """Lossless bridge from a successful job to the existing core importer."""

    payload_resource_uri: ResourceUri
    input_format: Literal["deepkoala_detailed"] = "deepkoala_detailed"
    source_provenance_template: SourceProvenanceTemplate
    core_max_payload_bytes: Literal[5_000_000] = MAX_OUTPUT_BYTES


class SubmitDeepKoalaResult(FrozenModel):
    """Idempotent submission response for a reviewed plan."""

    job: JobSummary
    idempotent_replay: bool


class GetDeepKoalaJobResult(FrozenModel):
    """Current job state and importer handoff after successful completion."""

    job: JobSummary
    handoff: ImportHandoff | None = None

    @model_validator(mode="after")
    def validate_handoff(self) -> Self:
        if self.job.state is JobState.SUCCEEDED and self.handoff is None:
            raise ValueError("successful jobs require an import handoff")
        if self.job.state is not JobState.SUCCEEDED and self.handoff is not None:
            raise ValueError("only successful jobs may expose an import handoff")
        return self


class CancelDeepKoalaJobResult(FrozenModel):
    """Job state after a cancellation request."""

    job: JobSummary


class DeleteDeepKoalaJobResult(FrozenModel):
    """Confirmation that one terminal job and its retained artifacts were removed."""

    job_id: JobId
    deleted: Literal[True] = True


class CompanionLimits(FrozenModel):
    """Public hard bounds without local paths or environment values."""

    max_input_bytes: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_output_bytes: int = Field(strict=True, ge=1, le=MAX_OUTPUT_BYTES)
    max_diagnostic_bytes: int = Field(strict=True, ge=1, le=MAX_DIAGNOSTIC_BYTES)
    max_sequences: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_COUNT)
    max_residues: int = Field(strict=True, ge=1, le=MAX_FASTA_BYTES)
    max_sequence_length: int = Field(strict=True, ge=1, le=MAX_SEQUENCE_LENGTH)
    max_header_length: int = Field(strict=True, ge=1, le=MAX_HEADER_LENGTH)
    max_queue_size: int = Field(strict=True, ge=1, le=MAX_QUEUE_SIZE)
    max_concurrent_jobs: Literal[1] = 1
    default_timeout_seconds: int = Field(strict=True, ge=1, le=86_400)
    plan_ttl_seconds: int = Field(strict=True, ge=1, le=86_400)
    retention_seconds: int = Field(strict=True, ge=1, le=2_592_000)

    @model_validator(mode="after")
    def validate_sequence_limits(self) -> Self:
        if self.max_sequence_length > self.max_residues:
            raise ValueError("max_sequence_length must not exceed max_residues")
        return self


class CompanionDefaults(FrozenModel):
    """Execution defaults visible during capability discovery."""

    model: Literal["full"] = "full"
    model_date: Literal["latest"] = "latest"
    device: Literal["auto"] = "auto"
    detail: Literal[True] = True
    batch_size: Literal[32] = 32
    num_workers: Literal[2] = 2
    topk: Literal[1] = 1
    multi: Literal[False] = False
    cpu_threads: int = Field(strict=True, ge=1, le=32)


class CompanionStatus(FrozenModel):
    """Path-free companion readiness and scheduler status."""

    server_name: Literal["deepkoala-mcp"] = SERVER_NAME
    server_version: str = Field(
        pattern=r"^[0-9]+(?:\.[0-9]+){2}(?:[.-][A-Za-z0-9.]+)?$",
        max_length=64,
    )
    ready: bool
    deepkoala_available: bool
    weights_available: bool
    deepkoala_version: BoundedLabel | None = None
    weight_source: WeightSource
    available_model_dates: Annotated[
        tuple[ResolvedModelDate, ...], Field(max_length=MAX_STATUS_MODEL_DATES)
    ]
    supported_models: Annotated[tuple[ModelName, ...], Field(min_length=2, max_length=2)]
    supported_devices: Annotated[tuple[RequestedDevice, ...], Field(min_length=1, max_length=4)]
    defaults: CompanionDefaults
    limits: CompanionLimits
    queue: QueueSnapshot
    cleanup_pending_jobs: int = Field(strict=True, ge=0, le=256)

    @model_validator(mode="after")
    def validate_capabilities(self) -> Self:
        if len(self.available_model_dates) != len(set(self.available_model_dates)):
            raise ValueError("available_model_dates must be unique")
        if set(self.supported_models) != {"full", "frag"}:
            raise ValueError("supported_models must contain full and frag")
        if len(self.supported_devices) != len(set(self.supported_devices)):
            raise ValueError("supported_devices must be unique")
        if self.ready and not (self.deepkoala_available and self.weights_available):
            raise ValueError("ready status requires DeepKOALA and installed weights")
        if self.queue.planned_disposition is not None:
            raise ValueError("status queue snapshots must not contain a planned disposition")
        return self


__all__ = [
    "MAX_DIAGNOSTIC_BYTES",
    "MAX_FASTA_BYTES",
    "MAX_HEADER_LENGTH",
    "MAX_OUTPUT_BYTES",
    "MAX_QUEUE_SIZE",
    "MAX_SEQUENCE_COUNT",
    "MAX_SEQUENCE_LENGTH",
    "SERVER_NAME",
    "UPDATED_WEIGHTS_URL",
    "CancelDeepKoalaJobInput",
    "CancelDeepKoalaJobResult",
    "CompanionDefaults",
    "CompanionLimits",
    "CompanionStatus",
    "DeepKoalaMcpError",
    "DeleteDeepKoalaJobInput",
    "DeleteDeepKoalaJobResult",
    "ErrorCode",
    "ErrorDetail",
    "ExecutionArtifact",
    "ExecutionNotice",
    "ExecutionSettings",
    "FastaSummary",
    "FrozenModel",
    "GetDeepKoalaJobInput",
    "GetDeepKoalaJobResult",
    "GetDeepKoalaStatusInput",
    "ImportHandoff",
    "JobState",
    "JobSummary",
    "PrepareDeepKoalaInput",
    "PrepareDeepKoalaResult",
    "QueueSnapshot",
    "SafeDetail",
    "SourceMetadataField",
    "SourceProvenanceTemplate",
    "SubmitDeepKoalaInput",
    "SubmitDeepKoalaResult",
    "ToolEnvelope",
    "ToolPayload",
    "WeightArtifact",
    "WeightSource",
    "fail",
]
