"""Canonical annotation evidence and derived KO view contracts."""

import re
from collections import defaultdict
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id

KNumber = Annotated[str, Field(pattern=r"^K[0-9]{5}$")]
RecordIdentifier = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
FiniteFloat = Annotated[float, Field(strict=True, allow_inf_nan=False)]
MachineReason = Annotated[
    str,
    Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100),
]
JsonInteger = Annotated[int, Field(strict=True, ge=-(2**63), le=2**63 - 1)]
JsonFloat = Annotated[
    float,
    Field(strict=True, allow_inf_nan=False, ge=-(2**63), le=2**63 - 1),
]
MAX_EVIDENCE_STRING_CHARACTERS = 5_000_000
JsonString = Annotated[str, Field(max_length=MAX_EVIDENCE_STRING_CHARACTERS)]
JsonScalar = JsonString | JsonInteger | JsonFloat | bool | None
SourceColumnName = Annotated[str, Field(min_length=1, max_length=256)]
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")
_LOGICAL_INPUT_URI_SCHEMES = frozenset({"inline", "mcp", "resource", "urn"})


def validate_utf8_text(value: str, *, field_name: str) -> str:
    """Reject Python strings that cannot be represented in UTF-8 JSON."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from error
    return value


def normalize_identifier_label(value: str, *, field_name: str) -> str:
    """Normalize a non-empty identifier label and reject unsafe text."""
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be blank")
    validate_utf8_text(normalized, field_name=field_name)
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError(f"{field_name} must not contain control characters")
    return normalized


def validate_logical_input_uri(value: str | None) -> str | None:
    """Reject local paths, traversal, NULs, and URI credentials from provenance."""
    if value is None:
        return None
    validate_utf8_text(value, field_name="input_uri")
    if not value or value != value.strip():
        raise ValueError("input_uri must be a non-empty logical source without outer whitespace")
    if "%" in value:
        raise ValueError("input_uri must not contain percent-encoded components")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("input_uri must not contain whitespace or control characters")
    if "\\" in value:
        raise ValueError("input_uri must not contain backslash path separators")
    if value.startswith(("/", "\\", "~")) or _WINDOWS_ABSOLUTE_PATH.match(value):
        raise ValueError("input_uri must be a logical source, not an absolute local path")
    normalized_segments = value.replace("\\", "/").split("/")
    if any(segment in {".", ".."} for segment in normalized_segments):
        raise ValueError("input_uri must not contain path traversal segments")
    parsed = urlsplit(value)
    if parsed.scheme and parsed.scheme.lower() not in _LOGICAL_INPUT_URI_SCHEMES:
        raise ValueError("input_uri uses an unsupported logical-source scheme")
    if parsed.scheme.lower() == "urn":
        if not parsed.path or ":" not in parsed.path or "/" in parsed.path:
            raise ValueError("input_uri is not a supported logical URN")
    elif parsed.scheme and not parsed.netloc:
        raise ValueError("hierarchical logical-source URIs require an authority")
    elif parsed.scheme and re.fullmatch(r"[A-Za-z]:", parsed.netloc):
        raise ValueError("logical-source URI authority must not be a Windows drive")
    if not parsed.scheme and ("/" in value or "\\" in value):
        raise ValueError("input_uri must be a logical name, not a relative local path")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("input_uri must not contain URI credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("input_uri must not contain query parameters or fragments")
    return value


def validate_absolute_input_path(value: str | None) -> str | None:
    """Validate an absolute local path retained as explicit user-facing provenance."""
    if value is None:
        return None
    validate_utf8_text(value, field_name="input_path")
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError("input_path must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("input_path must be absolute and contain no traversal components")
    return str(path)


class NormalizedStatus(StrEnum):
    """Decision status produced by a named normalization policy."""

    ACCEPTED = "accepted"
    UNCERTAIN = "uncertain"
    REJECTED = "rejected"
    UNCLASSIFIED = "unclassified"
    INVALID = "invalid"


class ScoreType(StrEnum):
    """Semantics of a source-provided score."""

    PROBABILITY = "probability"
    BITSCORE = "bitscore"
    E_VALUE = "e_value"
    SOURCE_SPECIFIC = "source_specific"


class ThresholdRule(StrEnum):
    """Relationship between a score and its source threshold."""

    GTE = "gte"
    LTE = "lte"
    SOURCE_SPECIFIC = "source_specific"


class AnalysisUnit(StrEnum):
    """Biological unit represented by one imported dataset."""

    ISOLATE_GENOME = "isolate_genome"
    ISOLATE_PROTEOME = "isolate_proteome"
    MAG = "MAG"
    PANGENOME = "pangenome"
    METAGENOMIC_COMMUNITY = "metagenomic_community"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class EvidenceMode(StrEnum):
    """Evidence sets supported by downstream analyses."""

    STRICT = "strict"
    LENIENT = "lenient"


class InputFormat(StrEnum):
    """Importer formats implemented in Milestone 1."""

    PLAIN_KO = "plain_ko"
    GENERIC_CSV = "generic_csv"
    GENERIC_TSV = "generic_tsv"
    DEEPKOALA_DETAILED = "deepkoala_detailed"


class DiagnosticCode(StrEnum):
    """Stable row-level importer diagnostic codes."""

    EMPTY_INPUT = "EMPTY_INPUT"
    INVALID_KO_IDENTIFIER = "INVALID_KO_IDENTIFIER"
    INVALID_FIELD_VALUE = "INVALID_FIELD_VALUE"
    UNRECOGNIZED_SOURCE_DECISION = "UNRECOGNIZED_SOURCE_DECISION"
    SOURCE_DECISION_CONFLICT = "SOURCE_DECISION_CONFLICT"
    DUPLICATE_ROW = "DUPLICATE_ROW"
    CONFLICTING_ASSIGNMENT = "CONFLICTING_ASSIGNMENT"
    ROW_SKIPPED = "ROW_SKIPPED"


class FrozenModel(BaseModel):
    """Base configuration for immutable, strict public contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
    )


class EvidenceField(FrozenModel):
    """One immutable raw field, preserving source column order and text."""

    name: str = Field(min_length=1, max_length=256)
    value: JsonScalar

    @field_validator("name")
    @classmethod
    def require_utf8_name(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="evidence field name")

    @field_validator("value", mode="before")
    @classmethod
    def reject_out_of_range_number(cls, value: object) -> object:
        if isinstance(value, str):
            validate_utf8_text(value, field_name="string evidence value")
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and not -(2**63) <= value <= 2**63 - 1
        ):
            raise ValueError("integer evidence values must fit in signed 64-bit JSON range")
        if isinstance(value, float) and (value >= 2**63 or value < -(2**63)):
            raise ValueError("numeric evidence values must fit in signed 64-bit JSON range")
        return value


class RowEvidence(FrozenModel):
    """Immutable logical source row retained with an annotation record."""

    row_number: PositiveInt
    fields: tuple[EvidenceField, ...]

    @model_validator(mode="after")
    def require_unique_field_names(self) -> Self:
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("evidence field names must be unique")
        return self

    def get(self, name: str) -> JsonScalar:
        """Return one raw value by field name, or ``None`` when absent."""
        for field in self.fields:
            if field.name == name:
                return field.value
        return None


class DecisionPolicyReference(FrozenModel):
    """Serializable identity of a normalization policy."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)

    @property
    def identifier(self) -> str:
        """Return the compact policy identifier used in reports."""
        return f"{self.name}_v{self.version}"


class SourceProvenance(FrozenModel):
    """Immutable provenance for one imported annotation source."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:source-provenance:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    source_name: str = Field(min_length=1, max_length=100)
    source_version: str | None = Field(max_length=256)
    model_name: str | None = Field(max_length=256)
    model_version: str | None = Field(max_length=256)
    annotation_date: datetime | None
    input_uri: str | None = Field(max_length=2_048)
    input_path: str | None = Field(default=None, max_length=4_096)
    importer_name: str = Field(min_length=1, max_length=100)
    importer_version: str = Field(min_length=1, max_length=32)
    source_metadata: Annotated[tuple[EvidenceField, ...], Field(max_length=128)] = ()

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

    @field_validator(
        "source_version",
        "model_name",
        "model_version",
        "importer_name",
        "importer_version",
    )
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


class AnnotationRecord(FrozenModel):
    """One preserved source assignment and its normalized decision."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:annotation-record:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    record_id: RecordIdentifier
    sample_id: str = Field(min_length=1, max_length=256)
    sequence_id: str | None = Field(max_length=256)
    protein_name: str | None = Field(default=None, max_length=1_000)
    ko_id: KNumber | None
    raw_ko: str
    raw_decision: str | None
    normalized_status: NormalizedStatus
    status_reason: MachineReason
    decision_policy: DecisionPolicyReference
    score: FiniteFloat | None
    score_type: ScoreType | None
    threshold: FiniteFloat | None
    threshold_rule: ThresholdRule | None
    rank: PositiveInt | None
    domain_start: PositiveInt | None
    domain_end: PositiveInt | None
    evidence: RowEvidence
    source: SourceProvenance

    @field_validator("sample_id")
    @classmethod
    def normalize_sample_id(cls, value: str) -> str:
        return normalize_identifier_label(value, field_name="sample_id")

    @field_validator("sequence_id")
    @classmethod
    def normalize_sequence_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_identifier_label(value, field_name="sequence_id")

    @field_validator("protein_name")
    @classmethod
    def validate_protein_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_identifier_label(value, field_name="protein_name")

    @field_validator("raw_ko", "raw_decision")
    @classmethod
    def require_utf8_raw_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name=info.field_name or "raw field")

    @model_validator(mode="after")
    def validate_evidence_consistency(self) -> Self:
        normalized_raw_ko, _ = try_normalize_ko_id(self.raw_ko)
        if normalized_raw_ko != self.ko_id:
            raise ValueError("ko_id must be the exact normalization of raw_ko")
        status_needs_ko = self.normalized_status in {
            NormalizedStatus.ACCEPTED,
            NormalizedStatus.UNCERTAIN,
            NormalizedStatus.REJECTED,
        }
        if status_needs_ko and self.ko_id is None:
            raise ValueError(f"{self.normalized_status.value} records require ko_id")
        if self.normalized_status is NormalizedStatus.INVALID and self.ko_id is not None:
            raise ValueError("invalid records must not contain a normalized ko_id")
        if self.sequence_id is None and self.source.importer_name != "plain_ko":
            raise ValueError("sequence_id may be null only for plain KO input")
        if (self.domain_start is None) != (self.domain_end is None):
            raise ValueError("domain_start and domain_end must be provided together")
        if (
            self.domain_start is not None
            and self.domain_end is not None
            and self.domain_end < self.domain_start
        ):
            raise ValueError("domain_end must be greater than or equal to domain_start")
        if self.score is not None and self.score_type is None:
            raise ValueError("score_type is required when score is present")
        if (self.threshold is None) != (self.threshold_rule is None):
            raise ValueError("threshold and threshold_rule must be provided together")
        if self.score_type is ScoreType.PROBABILITY:
            for name, value in (("score", self.score), ("threshold", self.threshold)):
                if value is not None and not 0.0 <= value <= 1.0:
                    raise ValueError(f"probability {name} must be between zero and one")
        return self


class StatusCount(FrozenModel):
    """Deterministic record count for one normalized status."""

    status: NormalizedStatus
    count: Annotated[int, Field(strict=True, ge=0)]


class ColumnBinding(FrozenModel):
    """One logical-to-source column binding recorded in an import report."""

    logical_field: str = Field(min_length=1, max_length=100)
    source_column: str = Field(min_length=1, max_length=256)

    @field_validator("logical_field", "source_column")
    @classmethod
    def require_utf8_binding(cls, value: str, info: ValidationInfo) -> str:
        return validate_utf8_text(value, field_name=info.field_name or "column binding")


class ImportDiagnostic(FrozenModel):
    """Repairable row-level issue that does not expose an entire payload."""

    code: DiagnosticCode
    message: str = Field(min_length=1, max_length=1_000)
    row_number: PositiveInt | None
    field: str | None = Field(default=None, max_length=256)
    safe_details: Annotated[tuple[EvidenceField, ...], Field(max_length=8)] = ()

    @field_validator("message", "field")
    @classmethod
    def require_utf8_diagnostic_text(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name=info.field_name or "diagnostic field")


class ImportReport(FrozenModel):
    """Deterministic summary of preserved, skipped, and classified rows."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:import-report:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    input_format: InputFormat
    input_rows: Annotated[int, Field(strict=True, ge=0)]
    emitted_records: Annotated[int, Field(strict=True, ge=0)]
    skipped_rows: Annotated[int, Field(strict=True, ge=0)]
    source_columns: tuple[SourceColumnName, ...]
    status_counts: tuple[StatusCount, ...]
    duplicate_count: Annotated[int, Field(strict=True, ge=0)]
    conflict_count: Annotated[int, Field(strict=True, ge=0)]
    diagnostics: tuple[ImportDiagnostic, ...] = ()
    unparsed_rows: tuple[RowEvidence, ...] = ()
    delimiter: str | None
    column_mapping: tuple[ColumnBinding, ...] = ()
    decision_policy: DecisionPolicyReference

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if self.input_rows != self.emitted_records + self.skipped_rows:
            raise ValueError("input_rows must equal emitted_records plus skipped_rows")
        if self.skipped_rows != len(self.unparsed_rows):
            raise ValueError("skipped_rows must equal the number of retained unparsed rows")
        if sum(item.count for item in self.status_counts) != self.emitted_records:
            raise ValueError("status counts must equal emitted_records")
        statuses = [item.status for item in self.status_counts]
        if len(statuses) != len(set(statuses)):
            raise ValueError("status_counts must contain unique statuses")
        if set(statuses) != set(NormalizedStatus):
            raise ValueError("status_counts must contain every normalized status")
        diagnostic_duplicate_count = sum(
            diagnostic.code is DiagnosticCode.DUPLICATE_ROW for diagnostic in self.diagnostics
        )
        if self.duplicate_count != diagnostic_duplicate_count:
            raise ValueError("duplicate_count must match duplicate diagnostics")
        diagnostic_conflict_count = sum(
            diagnostic.code is DiagnosticCode.CONFLICTING_ASSIGNMENT
            for diagnostic in self.diagnostics
        )
        if self.conflict_count != diagnostic_conflict_count:
            raise ValueError("conflict_count must match assignment-conflict diagnostics")
        if len(self.source_columns) != len(set(self.source_columns)):
            raise ValueError("source_columns must be unique")
        for source_column in self.source_columns:
            validate_utf8_text(source_column, field_name="source column")
        if self.delimiter is not None:
            validate_utf8_text(self.delimiter, field_name="delimiter")
        logical_fields = tuple(binding.logical_field for binding in self.column_mapping)
        bound_source_columns = tuple(binding.source_column for binding in self.column_mapping)
        if len(logical_fields) != len(set(logical_fields)):
            raise ValueError("column_mapping logical fields must be unique")
        if len(bound_source_columns) != len(set(bound_source_columns)):
            raise ValueError("column_mapping source columns must be unique")
        if any(column not in self.source_columns for column in bound_source_columns):
            raise ValueError("column_mapping must reference retained source columns")
        if self.input_format is InputFormat.PLAIN_KO and self.source_columns:
            raise ValueError("plain KO reports must not declare table source columns")
        if self.input_format is InputFormat.PLAIN_KO and self.column_mapping:
            raise ValueError("plain KO reports must not declare column mappings")
        if self.input_format is not InputFormat.PLAIN_KO and not self.source_columns:
            raise ValueError("table import reports must retain source columns")
        return self

    def count_for(self, status: NormalizedStatus) -> int:
        """Return the record count for one status."""
        for item in self.status_counts:
            if item.status is status:
                return item.count
        return 0


class AnnotationDataset(FrozenModel):
    """One immutable imported annotation dataset."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:annotation-dataset:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    records: tuple[AnnotationRecord, ...]
    sources: Annotated[tuple[SourceProvenance, ...], Field(min_length=1)]
    analysis_unit: AnalysisUnit
    taxon_id: PositiveInt | None
    kegg_organism_code: str | None = Field(pattern=r"^[a-z][a-z0-9]{1,7}$")
    metadata: Annotated[tuple[EvidenceField, ...], Field(max_length=128)] = ()
    import_report: ImportReport

    @model_validator(mode="after")
    def validate_dataset_contract(self) -> Self:
        record_ids = [record.record_id for record in self.records]
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("record_id values must be unique within a dataset")
        if self.import_report.emitted_records != len(self.records):
            raise ValueError("import report record count does not match dataset records")
        if any(record.source not in self.sources for record in self.records):
            raise ValueError("every record source must appear in dataset sources")
        record_counts = {status: 0 for status in NormalizedStatus}
        for record in self.records:
            record_counts[record.normalized_status] += 1
        report_counts = {
            status_count.status: status_count.count
            for status_count in self.import_report.status_counts
        }
        if record_counts != report_counts:
            raise ValueError("import report status counts do not match dataset records")
        policy_ids = {record.decision_policy.identifier for record in self.records}
        if policy_ids and policy_ids != {self.import_report.decision_policy.identifier}:
            raise ValueError("one imported dataset must use one decision policy")
        return self


class KORecordIndexEntry(FrozenModel):
    """Record identifiers associated with one normalized K number."""

    ko_id: KNumber
    record_ids: Annotated[tuple[RecordIdentifier, ...], Field(min_length=1)]


class SequenceRecordIndexEntry(FrozenModel):
    """Record identifiers associated with one sample-scoped sequence."""

    sample_id: str = Field(min_length=1, max_length=256)
    sequence_id: str = Field(min_length=1, max_length=256)
    record_ids: Annotated[tuple[RecordIdentifier, ...], Field(min_length=1)]

    @field_validator("sample_id", "sequence_id")
    @classmethod
    def normalize_index_identifier(cls, value: str, info: ValidationInfo) -> str:
        field_name = info.field_name or "identifier"
        return normalize_identifier_label(value, field_name=field_name)


class KOEvidenceView(FrozenModel):
    """Deterministic KO-set views derived from immutable source records."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:ko-evidence-view:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    accepted_kos: tuple[KNumber, ...]
    uncertain_kos: tuple[KNumber, ...]
    rejected_kos: tuple[KNumber, ...]
    records_by_ko: tuple[KORecordIndexEntry, ...]
    records_by_sequence: tuple[SequenceRecordIndexEntry, ...]
    status_counts: tuple[StatusCount, ...]
    policy: DecisionPolicyReference

    @model_validator(mode="after")
    def validate_deterministic_view(self) -> Self:
        for field_name, ko_ids in (
            ("accepted_kos", self.accepted_kos),
            ("uncertain_kos", self.uncertain_kos),
            ("rejected_kos", self.rejected_kos),
        ):
            if ko_ids != tuple(sorted(set(ko_ids))):
                raise ValueError(f"{field_name} must be a sorted tuple of unique K numbers")

        ko_keys = tuple(entry.ko_id for entry in self.records_by_ko)
        if ko_keys != tuple(sorted(set(ko_keys))):
            raise ValueError("records_by_ko must have sorted unique K-number keys")
        sequence_keys = tuple(
            (entry.sample_id, entry.sequence_id) for entry in self.records_by_sequence
        )
        if sequence_keys != tuple(sorted(set(sequence_keys))):
            raise ValueError("records_by_sequence must have sorted unique sequence keys")
        for entry in (*self.records_by_ko, *self.records_by_sequence):
            if len(entry.record_ids) != len(set(entry.record_ids)):
                raise ValueError("evidence-view index entries must contain unique record IDs")
        ko_record_ids = tuple(
            record_id for entry in self.records_by_ko for record_id in entry.record_ids
        )
        if len(ko_record_ids) != len(set(ko_record_ids)):
            raise ValueError("records_by_ko must not index one record under multiple K numbers")
        sequence_record_ids = tuple(
            record_id for entry in self.records_by_sequence for record_id in entry.record_ids
        )
        if len(sequence_record_ids) != len(set(sequence_record_ids)):
            raise ValueError("records_by_sequence must not index one record under multiple keys")
        status_ko_ids = set(self.accepted_kos) | set(self.uncertain_kos) | set(self.rejected_kos)
        if not status_ko_ids.issubset(ko_keys):
            raise ValueError("status-specific KO sets must be represented in records_by_ko")

        statuses = tuple(item.status for item in self.status_counts)
        if len(statuses) != len(set(statuses)) or set(statuses) != set(NormalizedStatus):
            raise ValueError("status_counts must contain every normalized status exactly once")
        return self


def build_ko_evidence_view(dataset: AnnotationDataset) -> KOEvidenceView:
    """Build deterministic status sets and indexes without changing evidence."""
    accepted: set[str] = set()
    uncertain: set[str] = set()
    rejected: set[str] = set()
    by_ko: dict[str, list[str]] = defaultdict(list)
    by_sequence: dict[tuple[str, str], list[str]] = defaultdict(list)

    for record in dataset.records:
        if record.ko_id is not None:
            by_ko[record.ko_id].append(record.record_id)
            if record.normalized_status is NormalizedStatus.ACCEPTED:
                accepted.add(record.ko_id)
            elif record.normalized_status is NormalizedStatus.UNCERTAIN:
                uncertain.add(record.ko_id)
            elif record.normalized_status is NormalizedStatus.REJECTED:
                rejected.add(record.ko_id)
        if record.sequence_id is not None:
            by_sequence[(record.sample_id, record.sequence_id)].append(record.record_id)

    return KOEvidenceView(
        accepted_kos=tuple(sorted(accepted)),
        uncertain_kos=tuple(sorted(uncertain)),
        rejected_kos=tuple(sorted(rejected)),
        records_by_ko=tuple(
            KORecordIndexEntry(ko_id=ko_id, record_ids=tuple(by_ko[ko_id]))
            for ko_id in sorted(by_ko)
        ),
        records_by_sequence=tuple(
            SequenceRecordIndexEntry(
                sample_id=sample_id,
                sequence_id=sequence_id,
                record_ids=tuple(by_sequence[(sample_id, sequence_id)]),
            )
            for sample_id, sequence_id in sorted(by_sequence)
        ),
        status_counts=tuple(
            StatusCount(
                status=status,
                count=sum(record.normalized_status is status for record in dataset.records),
            )
            for status in NormalizedStatus
        ),
        policy=dataset.import_report.decision_policy,
    )


def select_ko_ids(view: KOEvidenceView, mode: EvidenceMode) -> tuple[str, ...]:
    """Select strict or policy-defined lenient K numbers."""
    if mode is EvidenceMode.STRICT:
        return tuple(sorted(set(view.accepted_kos)))
    if mode is EvidenceMode.LENIENT:
        return tuple(sorted(set(view.accepted_kos) | set(view.uncertain_kos)))
    fail(
        ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
        "The evidence mode is not supported.",
        suggested_action="Use the strict or lenient evidence mode.",
        safe_details=(SafeDetail(name="mode_type", value=type(mode).__name__),),
    )
