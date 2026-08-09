"""Public importer configuration contracts."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, ValidationInfo, field_validator, model_validator

from kegg_mcp.domain.analysis_view import (
    MAX_ANALYSIS_VIEW_COLUMNS,
    MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW,
    MAX_ANALYSIS_VIEW_EXPANDED_ASSIGNMENTS,
    MAX_ANALYSIS_VIEW_FIELD_LENGTH,
    MAX_ANALYSIS_VIEW_INPUT_BYTES,
    MAX_ANALYSIS_VIEW_INPUT_ROWS,
    MAX_ANALYSIS_VIEW_UNIQUE_KO_IDS,
)
from kegg_mcp.domain.annotations import (
    MAX_EVIDENCE_STRING_CHARACTERS,
    EvidenceField,
    FrozenModel,
    ScoreType,
    ThresholdRule,
    normalize_identifier_label,
    validate_absolute_input_path,
    validate_logical_input_uri,
    validate_utf8_text,
)

MAX_IMPORT_LIMIT_VALUE = 2_147_483_647
MAX_IMPORT_FIELD_LENGTH = MAX_EVIDENCE_STRING_CHARACTERS
MAX_ANNOTATION_DATE_CHARACTERS = 64
ImportLimitValue = Annotated[
    int,
    Field(strict=True, gt=0, le=MAX_IMPORT_LIMIT_VALUE),
]
ImportFieldLength = Annotated[
    int,
    Field(strict=True, gt=0, le=MAX_IMPORT_FIELD_LENGTH),
]
ColumnName = Annotated[str, Field(min_length=1, max_length=256)]


class TableDialect(StrEnum):
    """Explicit generic table delimiters supported by the importers."""

    CSV = "csv"
    TSV = "tsv"

    @property
    def delimiter(self) -> str:
        """Return the exact delimiter for this dialect."""
        return "," if self is TableDialect.CSV else "\t"


class ImportLimits(FrozenModel):
    """Caller-selected input bounds pending the high-level configuration milestone."""

    max_bytes: ImportLimitValue
    max_rows: ImportLimitValue
    max_columns: ImportLimitValue
    max_field_length: ImportFieldLength


class AnalysisViewImportLimits(FrozenModel):
    """Deployment-owned bounds for streaming compact analysis-view intake."""

    max_bytes: int = Field(
        default=MAX_ANALYSIS_VIEW_INPUT_BYTES,
        strict=True,
        gt=0,
        le=MAX_ANALYSIS_VIEW_INPUT_BYTES,
    )
    max_rows: int = Field(
        default=MAX_ANALYSIS_VIEW_INPUT_ROWS,
        strict=True,
        gt=0,
        le=MAX_ANALYSIS_VIEW_INPUT_ROWS,
    )
    max_expanded_assignments: int = Field(
        default=MAX_ANALYSIS_VIEW_EXPANDED_ASSIGNMENTS,
        strict=True,
        gt=0,
        le=MAX_ANALYSIS_VIEW_EXPANDED_ASSIGNMENTS,
    )
    max_unique_ko_ids: int = Field(
        default=MAX_ANALYSIS_VIEW_UNIQUE_KO_IDS,
        strict=True,
        gt=0,
        le=MAX_ANALYSIS_VIEW_UNIQUE_KO_IDS,
    )
    max_columns: int = Field(
        default=MAX_ANALYSIS_VIEW_COLUMNS,
        strict=True,
        gt=0,
        le=MAX_ANALYSIS_VIEW_COLUMNS,
    )
    max_field_length: int = Field(
        default=MAX_ANALYSIS_VIEW_FIELD_LENGTH,
        strict=True,
        gt=0,
        le=MAX_ANALYSIS_VIEW_FIELD_LENGTH,
    )
    max_diagnostic_preview: int = Field(
        default=MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW,
        strict=True,
        ge=0,
        le=MAX_ANALYSIS_VIEW_DIAGNOSTIC_PREVIEW,
    )


class SourceProvenanceInput(FrozenModel):
    """Optional source facts supplied by the caller before import."""

    source_name: str = Field(min_length=1, max_length=100)
    source_version: str | None = Field(default=None, max_length=256)
    model_name: str | None = Field(default=None, max_length=256)
    model_version: str | None = Field(default=None, max_length=256)
    annotation_date: datetime | None = None
    input_uri: str | None = Field(default=None, max_length=2_048)
    input_path: str | None = Field(default=None, max_length=4_096)
    source_metadata: tuple[EvidenceField, ...] = Field(default=(), max_length=128)

    @field_validator("annotation_date", mode="before")
    @classmethod
    def bound_annotation_date_text(cls, value: object) -> object:
        if isinstance(value, str):
            if len(value) > MAX_ANNOTATION_DATE_CHARACTERS:
                raise ValueError("annotation_date exceeds the bounded timestamp length")
            normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
            try:
                return datetime.fromisoformat(normalized)
            except ValueError:
                raise ValueError("annotation_date must use ISO 8601 syntax") from None
        return value

    @field_validator("source_name")
    @classmethod
    def normalize_source_name(cls, value: str) -> str:
        return normalize_identifier_label(value, field_name="source_name")

    @field_validator("input_uri")
    @classmethod
    def validate_input_uri(cls, value: str | None) -> str | None:
        return validate_logical_input_uri(value)

    @field_validator("input_path")
    @classmethod
    def validate_input_path(cls, value: str | None) -> str | None:
        return validate_absolute_input_path(value)

    @field_validator("source_version", "model_name", "model_version")
    @classmethod
    def require_utf8_source_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name=info.field_name or "source field")

    @model_validator(mode="after")
    def require_timezone_when_date_is_known(self) -> Self:
        if self.annotation_date is not None and self.annotation_date.utcoffset() is None:
            raise ValueError("annotation_date must include a timezone")
        return self


class GenericColumnMapping(FrozenModel):
    """Explicit mapping from generic table columns to annotation fields."""

    sequence_id: ColumnName
    ko_id: ColumnName
    protein_name: ColumnName | None = None
    sample_id: ColumnName | None = None
    raw_decision: ColumnName | None = None
    score: ColumnName | None = None
    score_type: ScoreType | None = None
    threshold: ColumnName | None = None
    threshold_rule: ThresholdRule | None = None
    rank: ColumnName | None = None
    domain_start: ColumnName | None = None
    domain_end: ColumnName | None = None

    @field_validator(
        "sequence_id",
        "ko_id",
        "protein_name",
        "sample_id",
        "raw_decision",
        "score",
        "threshold",
        "rank",
        "domain_start",
        "domain_end",
    )
    @classmethod
    def require_utf8_column_name(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name=info.field_name or "column name")

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        bound_columns = [
            value
            for value in (
                self.sequence_id,
                self.ko_id,
                self.protein_name,
                self.sample_id,
                self.raw_decision,
                self.score,
                self.threshold,
                self.rank,
                self.domain_start,
                self.domain_end,
            )
            if value is not None
        ]
        if len(bound_columns) != len(set(bound_columns)):
            raise ValueError("one source column cannot bind to multiple logical fields")
        if (self.score is None) != (self.score_type is None):
            raise ValueError("score column and score_type must be declared together")
        if (self.threshold is None) != (self.threshold_rule is None):
            raise ValueError("threshold column and threshold_rule must be declared together")
        if (self.domain_start is None) != (self.domain_end is None):
            raise ValueError("domain_start and domain_end columns must be declared together")
        return self

    def bindings(self) -> tuple[tuple[str, str], ...]:
        """Return declared logical/source bindings in canonical order."""
        pairs = (
            ("sequence_id", self.sequence_id),
            ("ko_id", self.ko_id),
            ("protein_name", self.protein_name),
            ("sample_id", self.sample_id),
            ("raw_decision", self.raw_decision),
            ("score", self.score),
            ("threshold", self.threshold),
            ("rank", self.rank),
            ("domain_start", self.domain_start),
            ("domain_end", self.domain_end),
        )
        return tuple((logical, source) for logical, source in pairs if source is not None)
