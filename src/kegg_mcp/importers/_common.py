"""Shared, deterministic importer mechanics."""

import csv
import io
import math
import uuid
from collections.abc import Callable, Hashable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Iterator

from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    AnnotationDataset,
    AnnotationRecord,
    ColumnBinding,
    DecisionPolicyReference,
    DiagnosticCode,
    EvidenceField,
    ImportDiagnostic,
    ImportReport,
    InputFormat,
    NormalizedStatus,
    RowEvidence,
    SourceProvenance,
    StatusCount,
    normalize_identifier_label,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.importers.contracts import (
    ImportLimits,
    ProjectionImportLimits,
    SourceProvenanceInput,
)

IMPORTER_VERSION = "2"
MAX_EVIDENCE_FIELD_NAME_LENGTH = 256
MAX_AUXILIARY_METADATA_FIELDS = 128
MAX_INTEGER_TEXT_DIGITS = 18
_CSV_FIELD_SIZE_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class DecodedInput:
    """One bounded UTF-8 input preserving the exact supplied bytes."""

    content: bytes
    text: str


@dataclass(frozen=True, slots=True)
class ParsedTable:
    """A bounded table with raw logical rows retained as immutable evidence."""

    header: tuple[str, ...]
    rows: tuple[RowEvidence, ...]
    unparsed_rows: tuple[RowEvidence, ...]
    diagnostics: tuple[ImportDiagnostic, ...]
    input_rows: int


def decode_payload(payload: object, limits: ImportLimits) -> DecodedInput:
    """Decode one bounded UTF-8 payload without changing the supplied bytes."""
    if isinstance(payload, bytes):
        content = payload
    elif isinstance(payload, str):
        try:
            content = payload.encode("utf-8")
        except UnicodeEncodeError:
            fail(
                ErrorCode.UNSUPPORTED_INPUT_FORMAT,
                "The annotation input cannot be encoded as valid UTF-8 text.",
                suggested_action="Replace invalid Unicode surrogate characters and try again.",
            )
    else:
        fail(
            ErrorCode.UNSUPPORTED_INPUT_FORMAT,
            "The annotation input must be UTF-8 text or bytes.",
            suggested_action="Provide the input as a str or bytes value.",
            safe_details=(SafeDetail(name="input_type", value=type(payload).__name__[:100]),),
        )
    if len(content) > limits.max_bytes:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The annotation input exceeds the configured byte limit.",
            suggested_action="Reduce the input size or use an explicitly larger safe limit.",
            safe_details=(
                SafeDetail(name="max_bytes", value=str(limits.max_bytes)),
                SafeDetail(name="actual_bytes", value=str(len(content))),
            ),
        )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        fail(
            ErrorCode.UNSUPPORTED_INPUT_FORMAT,
            "The annotation input is not valid UTF-8 text.",
            suggested_action="Convert the input to UTF-8 and try again.",
        )
    if "\x00" in text:
        fail(
            ErrorCode.UNSUPPORTED_INPUT_FORMAT,
            "The annotation input contains NUL characters.",
            suggested_action="Provide plain UTF-8 text without binary content.",
        )
    return DecodedInput(
        content=content,
        text=text,
    )


def validate_auxiliary_evidence(
    metadata: Sequence[EvidenceField],
    source_input: SourceProvenanceInput | None,
    limits: ImportLimits | ProjectionImportLimits,
) -> None:
    """Apply caller-selected field and byte bounds to non-payload metadata."""
    if len(metadata) > MAX_AUXILIARY_METADATA_FIELDS:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Dataset metadata exceeds the supported field-count limit.",
            suggested_action="Reduce the number of dataset metadata fields.",
            safe_details=(
                SafeDetail(
                    name="max_metadata_fields",
                    value=str(MAX_AUXILIARY_METADATA_FIELDS),
                ),
            ),
        )
    if (
        source_input is not None
        and len(source_input.source_metadata) > MAX_AUXILIARY_METADATA_FIELDS
    ):
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Source metadata exceeds the supported field-count limit.",
            suggested_action="Reduce the number of source metadata fields.",
            safe_details=(
                SafeDetail(
                    name="max_source_metadata_fields",
                    value=str(MAX_AUXILIARY_METADATA_FIELDS),
                ),
            ),
        )
    fields = tuple(metadata) + (() if source_input is None else source_input.source_metadata)
    auxiliary_values = [
        (field.name, "" if field.value is None else str(field.value)) for field in fields
    ]
    if source_input is not None:
        auxiliary_values.extend(
            (name, str(value))
            for name, value in (
                ("source_name", source_input.source_name),
                ("source_version", source_input.source_version),
                ("model_name", source_input.model_name),
                ("model_version", source_input.model_version),
                ("annotation_date", source_input.annotation_date),
                ("input_uri", source_input.input_uri),
                ("input_path", source_input.input_path),
            )
            if value is not None
        )
    total_bytes = 0
    for name, value_text in auxiliary_values:
        if max(len(name), len(value_text)) > limits.max_field_length:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "An auxiliary metadata field exceeds the configured length limit.",
                suggested_action="Shorten metadata fields or use a larger safe limit.",
                safe_details=(
                    SafeDetail(name="max_field_length", value=str(limits.max_field_length)),
                ),
            )
        try:
            total_bytes += len(name.encode("utf-8")) + len(value_text.encode("utf-8"))
        except UnicodeEncodeError:
            fail(
                ErrorCode.UNSUPPORTED_INPUT_FORMAT,
                "Auxiliary metadata cannot be encoded as valid UTF-8 text.",
                suggested_action="Replace invalid Unicode surrogate characters and try again.",
            )
    if total_bytes > limits.max_bytes:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Auxiliary metadata exceeds the configured byte limit.",
            suggested_action="Reduce metadata size or use a larger safe limit.",
            safe_details=(SafeDetail(name="max_bytes", value=str(limits.max_bytes)),),
        )


def build_source(
    source_input: SourceProvenanceInput | None,
    *,
    default_source_name: str,
    importer_name: str,
) -> SourceProvenance:
    """Create canonical source provenance without guessing absent facts."""
    supplied = source_input or SourceProvenanceInput(source_name=default_source_name)
    return SourceProvenance(
        source_name=supplied.source_name,
        source_version=supplied.source_version,
        model_name=supplied.model_name,
        model_version=supplied.model_version,
        annotation_date=supplied.annotation_date,
        input_uri=supplied.input_uri,
        input_path=supplied.input_path,
        importer_name=importer_name,
        importer_version=IMPORTER_VERSION,
        source_metadata=supplied.source_metadata,
    )


def parse_table(
    decoded: DecodedInput,
    *,
    delimiter: str,
    limits: ImportLimits,
) -> ParsedTable:
    """Parse a table while coordinating Python's process-wide CSV field limit."""
    with configured_csv_field_limit(limits):
        return _parse_table(decoded, delimiter=delimiter, limits=limits)


@contextmanager
def configured_csv_field_limit(
    limits: ImportLimits | ProjectionImportLimits,
) -> Iterator[None]:
    """Temporarily coordinate Python's process-wide CSV field-size setting."""
    with _CSV_FIELD_SIZE_LOCK:
        previous_limit = csv.field_size_limit()
        configured_limit = max(
            previous_limit,
            min(limits.max_field_length, limits.max_bytes),
        )
        csv.field_size_limit(configured_limit)
        try:
            yield
        finally:
            csv.field_size_limit(previous_limit)


def _parse_table(
    decoded: DecodedInput,
    *,
    delimiter: str,
    limits: ImportLimits,
) -> ParsedTable:
    """Parse a bounded CSV/TSV payload with exact header semantics."""
    try:
        reader = csv.reader(io.StringIO(decoded.text, newline=""), delimiter=delimiter, strict=True)
        header = read_table_header(reader, limits)

        rows: list[RowEvidence] = []
        unparsed: list[RowEvidence] = []
        diagnostics: list[ImportDiagnostic] = []
        input_rows = 0
        for cells in reader:
            if not cells:
                continue
            input_rows += 1
            if input_rows > limits.max_rows:
                fail(
                    ErrorCode.INPUT_LIMIT_EXCEEDED,
                    "The annotation table exceeds the configured row limit.",
                    suggested_action="Reduce the row count or use an explicitly larger safe limit.",
                    safe_details=(SafeDetail(name="max_rows", value=str(limits.max_rows)),),
                )
            check_table_columns(cells, limits)
            row_number = reader.line_num
            evidence = build_row_evidence(header, cells, row_number)
            if len(cells) != len(header):
                unparsed.append(evidence)
                diagnostics.append(
                    ImportDiagnostic(
                        code=DiagnosticCode.ROW_SKIPPED,
                        message="The row has a different number of fields than the header.",
                        row_number=row_number,
                        field=None,
                        safe_details=(
                            EvidenceField(name="expected_columns", value=len(header)),
                            EvidenceField(name="actual_columns", value=len(cells)),
                        ),
                    )
                )
                continue
            rows.append(evidence)
    except csv.Error:
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The annotation table is not well-formed delimited text.",
            suggested_action="Repair quoting or delimiters and try again.",
        )

    return ParsedTable(
        header=header,
        rows=tuple(rows),
        unparsed_rows=tuple(unparsed),
        diagnostics=tuple(diagnostics),
        input_rows=input_rows,
    )


def read_table_header(
    reader: Iterator[list[str]],
    limits: ImportLimits | ProjectionImportLimits,
) -> tuple[str, ...]:
    """Read and validate one exact delimited-table header without retaining payload rows."""
    raw_header = next(reader, None)
    if raw_header is None:
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The annotation table has no header row.",
            suggested_action="Provide a table with an explicit header row.",
        )
    check_table_columns(raw_header, limits)
    header = tuple(raw_header)
    if any(not name for name in header):
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The annotation table contains an empty column name.",
            suggested_action="Give every source column a unique non-empty name.",
        )
    if any(len(name) > MAX_EVIDENCE_FIELD_NAME_LENGTH for name in header):
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The annotation table contains an oversized column name.",
            suggested_action=(
                f"Shorten column names to {MAX_EVIDENCE_FIELD_NAME_LENGTH} characters or fewer."
            ),
            safe_details=(
                SafeDetail(
                    name="max_column_name_length",
                    value=str(MAX_EVIDENCE_FIELD_NAME_LENGTH),
                ),
            ),
        )
    if len(header) != len(set(header)):
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The annotation table contains duplicate column names.",
            suggested_action="Rename duplicate columns before importing the table.",
        )
    return header


def require_columns(header: Sequence[str], required: Sequence[str]) -> None:
    """Fail with a repairable error when exact required columns are absent."""
    missing = tuple(column for column in required if column not in header)
    if missing:
        missing_preview = ",".join(missing)
        if len(missing_preview) > 900:
            missing_preview = f"{missing_preview[:897]}..."
        fail(
            ErrorCode.MISSING_REQUIRED_COLUMN,
            "The annotation table is missing required columns.",
            suggested_action=(
                "Provide the missing columns or use the generic importer with explicit mapping."
            ),
            safe_details=(
                SafeDetail(name="missing_column_count", value=str(len(missing))),
                SafeDetail(name="missing_columns_preview", value=missing_preview),
            ),
        )


def parse_optional_float(
    raw: str,
    *,
    row_number: int,
    field: str,
    diagnostics: list[ImportDiagnostic],
) -> float | None:
    """Parse a finite float while retaining malformed text in row evidence."""
    if not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.INVALID_FIELD_VALUE,
                message="The numeric field is not a finite number.",
                row_number=row_number,
                field=field,
            )
        )
        return None
    return value


def constrain_probability(
    value: float | None,
    *,
    row_number: int,
    field: str,
    diagnostics: list[ImportDiagnostic],
) -> float | None:
    """Reject finite numeric values outside the probability interval."""
    if value is not None and not 0.0 <= value <= 1.0:
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.INVALID_FIELD_VALUE,
                message="The probability field must be between zero and one.",
                row_number=row_number,
                field=field,
            )
        )
        return None
    return value


def parse_optional_positive_int(
    raw: str,
    *,
    row_number: int,
    field: str,
    diagnostics: list[ImportDiagnostic],
) -> int | None:
    """Parse a strict positive base-10 integer without accepting booleans or floats."""
    candidate = raw.strip()
    if not candidate:
        return None
    value = (
        0
        if (
            not candidate.isascii()
            or not candidate.isdecimal()
            or len(candidate) > MAX_INTEGER_TEXT_DIGITS
        )
        else int(candidate)
    )
    if value <= 0:
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.INVALID_FIELD_VALUE,
                message="The field must be a positive integer.",
                row_number=row_number,
                field=field,
            )
        )
        return None
    return value


def parse_domain(
    raw_start: str | None,
    raw_end: str | None,
    *,
    row_number: int,
    diagnostics: list[ImportDiagnostic],
) -> tuple[int | None, int | None]:
    """Parse one-based inclusive coordinates without inventing missing bounds."""
    if raw_start is None and raw_end is None:
        return None, None
    assert raw_start is not None and raw_end is not None
    start = parse_optional_positive_int(
        raw_start,
        row_number=row_number,
        field="domain_start",
        diagnostics=diagnostics,
    )
    end = parse_optional_positive_int(
        raw_end,
        row_number=row_number,
        field="domain_end",
        diagnostics=diagnostics,
    )
    if (start is None) != (end is None):
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.INVALID_FIELD_VALUE,
                message="Both domain coordinates are required when either is present.",
                row_number=row_number,
                field="domain_start,domain_end",
            )
        )
        return None, None
    if start is not None and end is not None and end < start:
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.INVALID_FIELD_VALUE,
                message="The domain end coordinate precedes the start coordinate.",
                row_number=row_number,
                field="domain_start,domain_end",
            )
        )
        return None, None
    return start, end


def require_non_empty_label(value: str, *, field: str) -> str:
    """Validate a caller-supplied label as a repairable configuration error."""
    try:
        normalized = normalize_identifier_label(value, field_name=field)
    except ValueError:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            f"{field} is not a valid identifier label.",
            suggested_action=(f"Provide a non-empty UTF-8 {field} without control characters."),
            safe_details=(SafeDetail(name="field", value=field),),
        )
    if len(normalized) > 256:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            f"{field} exceeds the supported label length.",
            suggested_action=f"Shorten {field} to 256 characters or fewer.",
            safe_details=(
                SafeDetail(name="field", value=field),
                SafeDetail(name="max_length", value="256"),
            ),
        )
    return normalized


def is_valid_record_label(value: str) -> bool:
    """Return whether a row-derived sample or sequence label is safe to emit."""
    try:
        normalized = normalize_identifier_label(value, field_name="identifier")
    except ValueError:
        return False
    return len(normalized) <= 256


def finalize_dataset(
    records: Sequence[AnnotationRecord],
    *,
    input_format: InputFormat,
    input_rows: int,
    skipped_rows: int,
    source_columns: tuple[str, ...],
    delimiter: str | None,
    column_mapping: tuple[ColumnBinding, ...],
    policy: DecisionPolicyReference,
    diagnostics: Sequence[ImportDiagnostic],
    unparsed_rows: Sequence[RowEvidence],
    duplicate_key: Callable[[AnnotationRecord], Hashable],
    conflict_key: Callable[[AnnotationRecord], Hashable | None],
    analysis_unit: AnalysisUnit,
    taxon_id: int | None,
    kegg_organism_code: str | None,
    metadata: tuple[EvidenceField, ...],
    source: SourceProvenance,
) -> AnnotationDataset:
    """Report duplicates/conflicts without collapsing any source records."""
    all_diagnostics = list(diagnostics)
    seen_duplicates: dict[Hashable, str] = {}
    duplicate_count = 0
    conflict_count = 0
    slot_rows: dict[Hashable, set[int]] = {}
    slot_assignment_rows: dict[
        Hashable,
        dict[tuple[str | None, NormalizedStatus], set[int]],
    ] = {}
    slot_first_record_ids: dict[Hashable, str] = {}

    for record in records:
        key = duplicate_key(record)
        first_record_id = seen_duplicates.get(key)
        if first_record_id is None:
            seen_duplicates[key] = record.record_id
        else:
            duplicate_count += 1
            all_diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.DUPLICATE_ROW,
                    message="The normalized assignment duplicates an earlier source row.",
                    row_number=record.evidence.row_number,
                    field=None,
                    safe_details=(EvidenceField(name="first_record_id", value=first_record_id),),
                )
            )

        slot = conflict_key(record)
        if slot is None:
            continue
        row_number = record.evidence.row_number
        previous_rows = slot_rows.setdefault(slot, set())
        assignment_rows = slot_assignment_rows.setdefault(slot, {}).setdefault(
            (record.ko_id, record.normalized_status),
            set(),
        )
        has_other_source_row = bool(previous_rows) and (
            len(previous_rows) > 1 or row_number not in previous_rows
        )
        assignment_matches_other_row = bool(assignment_rows) and (
            len(assignment_rows) > 1 or row_number not in assignment_rows
        )
        if has_other_source_row and not assignment_matches_other_row:
            conflict_count += 1
            all_diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.CONFLICTING_ASSIGNMENT,
                    message=(
                        "The same explicit assignment slot contains conflicting source records."
                    ),
                    row_number=record.evidence.row_number,
                    field=None,
                    safe_details=(
                        EvidenceField(
                            name="first_record_id",
                            value=slot_first_record_ids[slot],
                        ),
                    ),
                )
            )
        previous_rows.add(row_number)
        assignment_rows.add(row_number)
        slot_first_record_ids.setdefault(slot, record.record_id)

    seen_unparsed_rows: dict[Hashable, int] = {}
    for evidence in unparsed_rows:
        key = tuple((field.name, field.value) for field in evidence.fields)
        first_row_number = seen_unparsed_rows.get(key)
        if first_row_number is None:
            seen_unparsed_rows[key] = evidence.row_number
            continue
        duplicate_count += 1
        all_diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.DUPLICATE_ROW,
                message="The skipped logical row duplicates an earlier skipped source row.",
                row_number=evidence.row_number,
                field=None,
                safe_details=(EvidenceField(name="first_row_number", value=first_row_number),),
            )
        )

    counts = {status: 0 for status in NormalizedStatus}
    for record in records:
        counts[record.normalized_status] += 1
    status_counts = tuple(
        StatusCount(status=status, count=counts[status]) for status in NormalizedStatus
    )
    report = ImportReport(
        input_format=input_format,
        input_rows=input_rows,
        emitted_records=len(records),
        skipped_rows=skipped_rows,
        source_columns=source_columns,
        status_counts=status_counts,
        duplicate_count=duplicate_count,
        conflict_count=conflict_count,
        diagnostics=tuple(all_diagnostics),
        unparsed_rows=tuple(unparsed_rows),
        delimiter=delimiter,
        column_mapping=column_mapping,
        decision_policy=policy,
    )
    return AnnotationDataset(
        dataset_id=f"dataset-{uuid.uuid4().hex}",
        records=tuple(records),
        sources=(source,),
        analysis_unit=analysis_unit,
        taxon_id=taxon_id,
        kegg_organism_code=kegg_organism_code,
        metadata=metadata,
        import_report=report,
    )


def exact_record_key(record: AnnotationRecord) -> Hashable:
    """Return a stable exact-row duplicate key excluding generated identifiers."""
    return (
        record.sample_id,
        record.sequence_id,
        record.ko_id,
        record.raw_ko,
        record.raw_decision,
        record.normalized_status,
        record.score,
        record.score_type,
        record.threshold,
        record.threshold_rule,
        record.rank,
        record.domain_start,
        record.domain_end,
        tuple((field.name, field.value) for field in record.evidence.fields),
    )


def no_assignment_slot(record: AnnotationRecord) -> None:
    """Return no conflict slot for formats without sequence assignment semantics."""
    del record
    return None


def explicit_assignment_slot(record: AnnotationRecord) -> tuple[object, ...] | None:
    """Return a generic slot only when rank or domain coordinates are explicit."""
    if record.sequence_id is None:
        return None
    if record.rank is not None or record.domain_start is not None:
        return (
            record.sample_id,
            record.sequence_id,
            record.rank,
            record.domain_start,
            record.domain_end,
        )
    return None


def sequence_or_domain_assignment_slot(record: AnnotationRecord) -> tuple[object, ...] | None:
    """Use a DeepKOALA domain slot, or its sequence slot when no domain is present."""
    if record.sequence_id is None:
        return None
    if record.domain_start is not None and record.domain_end is not None:
        return (
            record.sample_id,
            record.sequence_id,
            "domain",
            record.domain_start,
            record.domain_end,
        )
    return (record.sample_id, record.sequence_id, "sequence")


def check_table_columns(
    cells: Sequence[str],
    limits: ImportLimits | ProjectionImportLimits,
) -> None:
    """Enforce common column, field-length, and text-safety limits."""
    if len(cells) > limits.max_columns:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The annotation table exceeds the configured column limit.",
            suggested_action="Reduce the number of columns or use a larger safe limit.",
            safe_details=(SafeDetail(name="max_columns", value=str(limits.max_columns)),),
        )
    if any(len(cell) > limits.max_field_length for cell in cells):
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "An annotation field exceeds the configured length limit.",
            suggested_action="Shorten oversized fields or use a larger safe limit.",
            safe_details=(SafeDetail(name="max_field_length", value=str(limits.max_field_length)),),
        )
    if any("\x00" in cell for cell in cells):
        fail(
            ErrorCode.UNSUPPORTED_INPUT_FORMAT,
            "The annotation input contains NUL characters.",
            suggested_action="Provide plain UTF-8 text without binary content.",
        )


def build_row_evidence(
    header: Sequence[str],
    cells: Sequence[str],
    row_number: int,
) -> RowEvidence:
    """Build one immutable logical-row value for immediate classification or retention."""
    fields: list[EvidenceField] = []
    used_names = set(header)
    for index, value in enumerate(cells):
        if index < len(header):
            name = header[index]
        else:
            base_name = f"_extra_{index - len(header) + 1}"
            name = base_name
            suffix = 1
            while name in used_names:
                name = f"{base_name}_{suffix}"
                suffix += 1
            used_names.add(name)
        fields.append(EvidenceField(name=name, value=value))
    return RowEvidence(row_number=row_number, fields=tuple(fields))
