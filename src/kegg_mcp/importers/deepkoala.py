"""Import-only support for documented DeepKOALA detailed CSV output."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    AnnotationDataset,
    AnnotationRecord,
    ColumnBinding,
    DiagnosticCode,
    EvidenceField,
    ImportDiagnostic,
    InputFormat,
    RowEvidence,
    ScoreType,
    ThresholdRule,
)
from kegg_mcp.domain.decisions import (
    DEEPKOALA_DETAILED,
    DecisionEvidence,
    DecisionOutcome,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.importers._common import (
    build_source,
    constrain_probability,
    decode_payload,
    exact_record_key,
    finalize_dataset,
    is_valid_record_label,
    parse_domain,
    parse_optional_float,
    parse_table,
    require_columns,
    require_non_empty_label,
    sequence_or_domain_assignment_slot,
    validate_auxiliary_evidence,
)
from kegg_mcp.importers.contracts import ImportLimits, SourceProvenanceInput

_REQUIRED_COLUMNS = ("name", "predict_label", "probability", "threshold", "annotate")
_COMPOSITE_KO_LABEL = re.compile(r"K[0-9]{5}(?:[ \t]*\+[ \t]*K[0-9]{5})+\Z")


@dataclass(frozen=True, slots=True)
class DeepKoalaAssignment:
    """One normalized component emitted from a DeepKOALA prediction cell."""

    raw_ko: str
    ko_id: str | None
    outcome: DecisionOutcome


@dataclass(frozen=True, slots=True)
class DeepKoalaParsedRow:
    """One classified source row shared by full and streaming importers."""

    evidence: RowEvidence
    sequence_id: str
    raw_decision: str
    score: float | None
    threshold: float | None
    domain_start: int | None
    domain_end: int | None
    assignments: tuple[DeepKoalaAssignment, ...]


def validate_deepkoala_header(header: Sequence[str]) -> bool:
    """Validate the documented columns and report whether domain coordinates are present."""
    require_columns(header, _REQUIRED_COLUMNS)
    has_start = "start" in header
    has_end = "end" in header
    if has_start != has_end:
        fail(
            ErrorCode.MISSING_REQUIRED_COLUMN,
            "DeepKOALA domain coordinates require both start and end columns.",
            suggested_action=(
                "Provide both coordinate columns or remove the incomplete coordinate field."
            ),
            safe_details=(
                SafeDetail(name="missing_columns", value="end" if has_start else "start"),
            ),
        )
    return has_start


def parse_deepkoala_row(
    evidence: RowEvidence,
    *,
    has_domain_coordinates: bool,
    diagnostics: list[ImportDiagnostic],
    remaining_assignment_capacity: int | None = None,
    max_assignment_count: int | None = None,
    assignment_limit_name: Literal["max_records", "max_expanded_assignments"] = "max_records",
) -> DeepKoalaParsedRow | None:
    """Classify one validated-width source row without retaining cross-row state."""
    values = {field.name: field.value for field in evidence.fields}
    sequence_id = _text(values["name"]).strip()
    if not is_valid_record_label(sequence_id):
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.ROW_SKIPPED,
                message="The DeepKOALA name field is empty or oversized.",
                row_number=evidence.row_number,
                field="name",
            )
        )
        return None

    source_raw_ko = _text(values["predict_label"])
    raw_decision = _text(values["annotate"])
    raw_ko_components = _split_composite_ko_label(source_raw_ko)
    if (
        remaining_assignment_capacity is not None
        and len(raw_ko_components) > remaining_assignment_capacity
    ):
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Expanded DeepKOALA assignments exceed the configured assignment limit.",
            suggested_action="Reduce the input or use a larger safe assignment limit.",
            safe_details=(
                SafeDetail(
                    name=assignment_limit_name,
                    value=str(max_assignment_count or remaining_assignment_capacity),
                ),
            ),
        )
    score = parse_optional_float(
        _text(values["probability"]),
        row_number=evidence.row_number,
        field="probability",
        diagnostics=diagnostics,
    )
    threshold = parse_optional_float(
        _text(values["threshold"]),
        row_number=evidence.row_number,
        field="threshold",
        diagnostics=diagnostics,
    )
    score = constrain_probability(
        score,
        row_number=evidence.row_number,
        field="probability",
        diagnostics=diagnostics,
    )
    threshold = constrain_probability(
        threshold,
        row_number=evidence.row_number,
        field="threshold",
        diagnostics=diagnostics,
    )
    domain_start, domain_end = parse_domain(
        _text(values["start"]) if has_domain_coordinates else None,
        _text(values["end"]) if has_domain_coordinates else None,
        row_number=evidence.row_number,
        diagnostics=diagnostics,
    )
    normalized_components = tuple(
        (raw_ko, try_normalize_ko_id(raw_ko)[0]) for raw_ko in raw_ko_components
    )
    outcomes = tuple(
        DEEPKOALA_DETAILED.classify(
            DecisionEvidence(
                raw_ko=raw_ko,
                ko_id=ko_id,
                raw_decision=raw_decision,
                score=score,
                score_type=ScoreType.PROBABILITY,
                threshold=threshold,
                threshold_rule=ThresholdRule.GTE if threshold is not None else None,
            )
        )
        for raw_ko, ko_id in normalized_components
    )
    if normalized_components[0][1] is None and source_raw_ko.strip():
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.INVALID_KO_IDENTIFIER,
                message="The DeepKOALA prediction is not a valid K number.",
                row_number=evidence.row_number,
                field="predict_label",
            )
        )
    if outcomes[0].reason == "unrecognized_source_decision":
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.UNRECOGNIZED_SOURCE_DECISION,
                message="The DeepKOALA annotate field uses an unsupported marker.",
                row_number=evidence.row_number,
                field="annotate",
            )
        )
    if raw_decision == "*" and score is not None and threshold is not None and score < threshold:
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.SOURCE_DECISION_CONFLICT,
                message="The source acceptance marker and numeric threshold comparison disagree.",
                row_number=evidence.row_number,
                field="annotate,probability,threshold",
            )
        )
    assignments = tuple(
        DeepKoalaAssignment(raw_ko=raw_ko, ko_id=ko_id, outcome=outcome)
        for (raw_ko, ko_id), outcome in zip(normalized_components, outcomes, strict=True)
    )
    return DeepKoalaParsedRow(
        evidence=evidence,
        sequence_id=sequence_id,
        raw_decision=raw_decision,
        score=score,
        threshold=threshold,
        domain_start=domain_start,
        domain_end=domain_end,
        assignments=assignments,
    )


def import_deepkoala_detailed(
    payload: str | bytes,
    *,
    limits: ImportLimits,
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN,
    sample_id: str = "sample-1",
    taxon_id: int | None = None,
    kegg_organism_code: str | None = None,
    metadata: tuple[EvidenceField, ...] = (),
    source: SourceProvenanceInput | None = None,
) -> AnnotationDataset:
    """Import documented detailed output without executing DeepKOALA."""
    sample_id = require_non_empty_label(sample_id, field="sample_id")
    validate_auxiliary_evidence(metadata, source, limits)
    if source is not None and source.source_name != "deepkoala":
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "DeepKOALA detailed input requires source_name='deepkoala'.",
            suggested_action="Correct the source provenance or use the generic table importer.",
            safe_details=(SafeDetail(name="source_name", value=source.source_name),),
        )
    decoded = decode_payload(payload, limits)
    table = parse_table(decoded, delimiter=",", limits=limits)
    has_domain_coordinates = validate_deepkoala_header(table.header)
    provenance = build_source(
        source,
        default_source_name="deepkoala",
        importer_name="deepkoala_detailed",
    )
    diagnostics = list(table.diagnostics)
    records: list[AnnotationRecord] = []
    skipped_rows = len(table.unparsed_rows)
    unparsed_rows = list(table.unparsed_rows)

    for evidence in table.rows:
        parsed = parse_deepkoala_row(
            evidence,
            has_domain_coordinates=has_domain_coordinates,
            diagnostics=diagnostics,
            remaining_assignment_capacity=limits.max_rows - len(records),
            max_assignment_count=limits.max_rows,
        )
        if parsed is None:
            skipped_rows += 1
            unparsed_rows.append(evidence)
            continue
        for assignment in parsed.assignments:
            records.append(
                AnnotationRecord(
                    record_id=f"record-{len(records) + 1:06d}",
                    sample_id=sample_id,
                    sequence_id=parsed.sequence_id,
                    ko_id=assignment.ko_id,
                    raw_ko=assignment.raw_ko,
                    raw_decision=parsed.raw_decision,
                    normalized_status=assignment.outcome.status,
                    status_reason=assignment.outcome.reason,
                    decision_policy=DEEPKOALA_DETAILED.reference,
                    score=parsed.score,
                    score_type=ScoreType.PROBABILITY,
                    threshold=parsed.threshold,
                    threshold_rule=(ThresholdRule.GTE if parsed.threshold is not None else None),
                    rank=None,
                    domain_start=parsed.domain_start,
                    domain_end=parsed.domain_end,
                    evidence=parsed.evidence,
                    source=provenance,
                )
            )

    mapped = _REQUIRED_COLUMNS + (("start", "end") if has_domain_coordinates else ())
    bindings = tuple(ColumnBinding(logical_field=column, source_column=column) for column in mapped)
    return finalize_dataset(
        records,
        input_format=InputFormat.DEEPKOALA_DETAILED,
        input_rows=table.input_rows,
        skipped_rows=skipped_rows,
        source_columns=table.header,
        delimiter=",",
        column_mapping=bindings,
        policy=DEEPKOALA_DETAILED.reference,
        diagnostics=diagnostics,
        unparsed_rows=unparsed_rows,
        duplicate_key=exact_record_key,
        conflict_key=sequence_or_domain_assignment_slot,
        analysis_unit=analysis_unit,
        taxon_id=taxon_id,
        kegg_organism_code=kegg_organism_code,
        metadata=metadata,
        source=provenance,
    )


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("DeepKOALA evidence values must be text")
    return value


def _split_composite_ko_label(raw_ko: str) -> tuple[str, ...]:
    """Split only an exact plus-joined sequence of canonical K numbers."""
    candidate = raw_ko.strip()
    if _COMPOSITE_KO_LABEL.fullmatch(candidate) is None:
        return (raw_ko,)
    return tuple(component.strip() for component in candidate.split("+"))
