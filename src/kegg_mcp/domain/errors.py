"""Structured domain errors safe for later MCP serialization."""

from enum import StrEnum
from typing import Annotated, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErrorCode(StrEnum):
    """Stable error codes defined by the reviewed development plan."""

    INVALID_KO_IDENTIFIER = "INVALID_KO_IDENTIFIER"
    INVALID_ANNOTATION_TABLE = "INVALID_ANNOTATION_TABLE"
    AMBIGUOUS_COLUMN_MAPPING = "AMBIGUOUS_COLUMN_MAPPING"
    MISSING_REQUIRED_COLUMN = "MISSING_REQUIRED_COLUMN"
    UNSUPPORTED_INPUT_FORMAT = "UNSUPPORTED_INPUT_FORMAT"
    INPUT_LIMIT_EXCEEDED = "INPUT_LIMIT_EXCEEDED"
    KEGG_REQUEST_FAILED = "KEGG_REQUEST_FAILED"
    KEGG_RATE_LIMITED = "KEGG_RATE_LIMITED"
    KEGG_ENTRY_NOT_FOUND = "KEGG_ENTRY_NOT_FOUND"
    KEGG_PARSE_FAILED = "KEGG_PARSE_FAILED"
    CACHE_FAILED = "CACHE_FAILED"
    CACHE_ENTRY_NOT_FOUND = "CACHE_ENTRY_NOT_FOUND"
    ANALYSIS_CONFIGURATION_INVALID = "ANALYSIS_CONFIGURATION_INVALID"
    MODULE_DEFINITION_INVALID = "MODULE_DEFINITION_INVALID"
    MODULE_REFERENCE_CYCLE = "MODULE_REFERENCE_CYCLE"
    MODULE_NOT_EVALUABLE = "MODULE_NOT_EVALUABLE"
    PATHWAY_NAMESPACE_MISMATCH = "PATHWAY_NAMESPACE_MISMATCH"
    INCOMPATIBLE_ANALYSIS_PROVENANCE = "INCOMPATIBLE_ANALYSIS_PROVENANCE"
    RESULT_NOT_FOUND = "RESULT_NOT_FOUND"
    RESULT_STORE_FAILED = "RESULT_STORE_FAILED"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    OUTPUT_ALREADY_EXISTS = "OUTPUT_ALREADY_EXISTS"
    OUTPUT_WRITE_FAILED = "OUTPUT_WRITE_FAILED"


class SafeDetail(BaseModel):
    """One non-sensitive key/value detail attached to a domain error."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=1_000)


class ErrorDetail(BaseModel):
    """Serializable error information that never contains raw input payloads."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:error-detail:1",
            "$schema": "https://json-schema.org/draft/2020-12/schema",
        },
    )

    code: ErrorCode
    message: str = Field(min_length=1, max_length=1_000)
    recoverable: bool
    suggested_action: str | None = Field(default=None, max_length=1_000)
    safe_details: Annotated[tuple[SafeDetail, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def require_action_for_recoverable_error(self) -> Self:
        """Ensure callers receive a concrete repair path when one exists."""
        if self.recoverable and not self.suggested_action:
            raise ValueError("recoverable errors require a suggested_action")
        return self


class KeggMcpError(Exception):
    """Base exception carrying a schema-conforming domain error."""

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
    """Raise a recoverable domain error without leaking unsafe context."""
    raise KeggMcpError(
        ErrorDetail(
            code=code,
            message=message,
            recoverable=True,
            suggested_action=suggested_action,
            safe_details=safe_details,
        )
    )
