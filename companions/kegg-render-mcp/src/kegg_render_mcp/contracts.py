"""Strict public contracts for renderer tools, results, and safe errors."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Generic, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kegg_render_mcp.config import RendererLimits

MAX_TARGETS = 32
MAX_FORMATS = 2
MAX_WARNINGS = 32
MAX_ARTIFACTS = MAX_TARGETS * MAX_FORMATS + 1
MAX_SAFE_DETAILS = 8
MAX_INLINE_INPUT_CHARACTERS = 50_000_000
RENDER_ID_PATTERN = r"render_[A-Za-z0-9_-]{32}"
ARTIFACT_NAME_PATTERN = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
REQUIRED_RENDER_INPUT_SCHEMA_VERSION: Literal["4"] = "4"

_RenderTargetId = Annotated[str, Field(pattern=r"^(?:ko[0-9]{5}|M[0-9]{5})$")]
_RenderId = Annotated[str, Field(pattern=rf"^{RENDER_ID_PATTERN}$")]
_ArtifactName = Annotated[str, Field(pattern=rf"^{ARTIFACT_NAME_PATTERN}$")]


class _Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class RenderFormat(StrEnum):
    SVG = "svg"
    PNG = "png"


class ConnectivityStatus(StrEnum):
    REACHABLE = "reachable"
    NOT_CONFIGURED = "not_configured"
    OFFLINE_CACHE = "offline_cache"
    DNS_FAILURE = "dns_failure"
    CONNECTION_FAILURE = "connection_failure"
    TIMEOUT = "timeout"
    TLS_FAILURE = "tls_failure"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    ENDPOINT_REJECTED = "endpoint_rejected"
    UNKNOWN_FAILURE = "unknown_failure"


class ArtifactKind(StrEnum):
    IMAGE = "image"
    MANIFEST = "manifest"


class ErrorCode(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INPUT_PATH_REJECTED = "INPUT_PATH_REJECTED"
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_NOT_RENDERABLE = "TARGET_NOT_RENDERABLE"
    ASSET_UNAVAILABLE = "ASSET_UNAVAILABLE"
    ASSET_INVALID = "ASSET_INVALID"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    OUTPUT_ALREADY_EXISTS = "OUTPUT_ALREADY_EXISTS"
    OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"
    RESULT_NOT_FOUND = "RESULT_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class SafeDetail(_Model):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    value: str = Field(min_length=1, max_length=160)


class ErrorDetail(_Model):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=320)
    recoverable: bool = True
    suggested_action: str = Field(min_length=1, max_length=320)
    safe_details: tuple[SafeDetail, ...] = Field(default=(), max_length=MAX_SAFE_DETAILS)


class RenderMcpError(Exception):
    """Expected error carrying only bounded, redacted public details."""

    def __init__(self, detail: ErrorDetail) -> None:
        super().__init__(detail.message)
        self.detail = detail


class EmptyInput(_Model):
    pass


class _RenderInputSource(_Model):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {"required": ["render_input_path"]},
                {"required": ["render_input_json"]},
            ]
        }
    )

    render_input_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description=(
            "Allowed local path to a current render_input.json handoff. Provide exactly one "
            "of render_input_path or render_input_json."
        ),
    )
    render_input_json: str | None = Field(
        default=None,
        min_length=2,
        max_length=MAX_INLINE_INPUT_CHARACTERS,
        repr=False,
        description=(
            "Bounded inline contents of a current render_input.json handoff. Provide exactly "
            "one of render_input_path or render_input_json."
        ),
    )

    @model_validator(mode="after")
    def exactly_one_render_input(self) -> Self:
        if (self.render_input_path is None) == (self.render_input_json is None):
            raise ValueError("provide exactly one of render_input_path or render_input_json")
        return self


class _RenderOutputInput(_RenderInputSource):
    output_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description=(
            "Allowed local directory for published render artifacts. Omit to allocate a fresh "
            "directory beneath the deployment's default output root."
        ),
    )
    formats: tuple[RenderFormat, ...] = Field(
        default=(RenderFormat.SVG,),
        min_length=1,
        max_length=MAX_FORMATS,
        description="One or two unique static output formats; defaults to SVG.",
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("formats")
    @classmethod
    def unique_formats(cls, value: tuple[RenderFormat, ...]) -> tuple[RenderFormat, ...]:
        if len(value) != len(set(value)):
            raise ValueError("formats must be unique")
        return value


class RenderAnalysisBundleInput(_RenderOutputInput):
    target_ids: tuple[_RenderTargetId, ...] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_TARGETS,
        description=(
            "Optional unique canonical ko pathway or MODULE IDs; omit to render every target "
            "from the handoff, up to the renderer limit."
        ),
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("target_ids")
    @classmethod
    def validate_targets(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        if len(value) != len(set(value)):
            raise ValueError("target_ids must be unique")
        return value


class RenderOneInput(_RenderOutputInput):
    target_id: str = Field(
        min_length=6,
        max_length=7,
        description="One canonical ko pathway or MODULE identifier, as constrained by the tool.",
    )


class RenderPathwayInput(RenderOneInput):
    target_id: str = Field(pattern=r"^ko[0-9]{5}$")


class RenderModuleInput(RenderOneInput):
    target_id: str = Field(pattern=r"^M[0-9]{5}$")


class DeleteRenderResultInput(_Model):
    render_id: _RenderId


class RendererStatus(_Model):
    server_name: Literal["kegg-render-mcp"] = "kegg-render-mcp"
    server_version: str = Field(min_length=1, max_length=32)
    ready: bool
    render_input_schema_version: Literal["4"]
    output_formats: tuple[RenderFormat, ...] = (RenderFormat.SVG, RenderFormat.PNG)
    pathway_access_configured: bool
    access_mode: Literal["public_academic", "licensed", "offline_cache", "unconfigured"]
    allowed_root_count: int = Field(ge=0, le=64)
    retention_seconds: int = Field(ge=1)
    retained_result_count: int = Field(ge=0)
    cleanup_pending_result_count: int = Field(ge=0)
    retained_bytes: int = Field(ge=0)
    retained_storage_bytes: int = Field(ge=0)
    max_targets: int = Field(ge=1)
    bounds: RendererLimits


class ConnectivityResult(_Model):
    reachable: bool
    classification: ConnectivityStatus
    operation: Literal["info"] = "info"
    request_count: int = Field(ge=0, le=1)
    message: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def classification_matches_reachability(self) -> Self:
        if self.reachable != (self.classification is ConnectivityStatus.REACHABLE):
            raise ValueError("reachable must match the connectivity classification")
        zero_request_classifications = {
            ConnectivityStatus.NOT_CONFIGURED,
            ConnectivityStatus.OFFLINE_CACHE,
        }
        if (self.classification in zero_request_classifications) != (self.request_count == 0):
            raise ValueError("only unconfigured and offline probes may report request_count zero")
        return self


class ArtifactMetadata(_Model):
    name: _ArtifactName
    kind: ArtifactKind
    mime_type: Literal["image/svg+xml", "image/png", "application/json"]
    byte_size: int = Field(ge=1)
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    resource_uri: str = Field(pattern=rf"^kegg-render://results/{RENDER_ID_PATTERN}/")
    output_path: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def dimensions_match_kind(self) -> ArtifactMetadata:
        if (self.width is None) != (self.height is None):
            raise ValueError("artifact dimensions must be both present or both absent")
        if self.kind is ArtifactKind.IMAGE and self.width is None:
            raise ValueError("image artifacts require dimensions")
        if self.kind is ArtifactKind.MANIFEST and self.width is not None:
            raise ValueError("manifest artifacts must not have dimensions")
        return self


class RenderResult(_Model):
    render_id: _RenderId
    created_at: datetime
    expires_at: datetime
    target_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_TARGETS)
    artifacts: tuple[ArtifactMetadata, ...] = Field(min_length=1, max_length=MAX_ARTIFACTS)
    warnings: tuple[str, ...] = Field(default=(), max_length=MAX_WARNINGS)
    result_uri: str = Field(pattern=rf"^kegg-render://results/{RENDER_ID_PATTERN}$")
    output_directory: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def output_paths_match_directory(self) -> RenderResult:
        exported = self.output_directory is not None
        if any((item.output_path is not None) != exported for item in self.artifacts):
            raise ValueError("artifact output paths must match output_directory availability")
        return self


class DeleteRenderResult(_Model):
    render_id: _RenderId
    deleted: Literal[True] = True


T = TypeVar("T", bound=BaseModel)


class SuccessPayload(_Model, Generic[T]):
    data: T


class ToolEnvelope(_Model, Generic[T]):
    ok: bool
    result: SuccessPayload[T] | None
    error: ErrorDetail | None

    @model_validator(mode="after")
    def exactly_one_branch(self) -> ToolEnvelope[T]:
        if self.ok != (self.result is not None and self.error is None):
            raise ValueError("envelope must contain exactly one matching branch")
        return self
