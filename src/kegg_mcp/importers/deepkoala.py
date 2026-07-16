"""Import-only support for documented DeepKOALA detailed CSV output."""

from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    AnnotationDataset,
    AnnotationRecord,
    ColumnBinding,
    DiagnosticCode,
    EvidenceField,
    ImportDiagnostic,
    InputFormat,
    ScoreType,
    ThresholdRule,
)
from kegg_mcp.domain.decisions import DEEPKOALA_DETAILED, DecisionEvidence
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
    require_columns(table.header, _REQUIRED_COLUMNS)
    has_start = "start" in table.header
    has_end = "end" in table.header
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
    provenance = build_source(
        decoded,
        source,
        default_source_name="deepkoala",
        importer_name="deepkoala_detailed",
    )
    diagnostics = list(table.diagnostics)
    records: list[AnnotationRecord] = []
    skipped_rows = len(table.unparsed_rows)
    unparsed_rows = list(table.unparsed_rows)

    for evidence in table.rows:
        values = {field.name: field.value for field in evidence.fields}
        sequence_id = _text(values["name"]).strip()
        if not is_valid_record_label(sequence_id):
            skipped_rows += 1
            unparsed_rows.append(evidence)
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.ROW_SKIPPED,
                    message="The DeepKOALA name field is empty or oversized.",
                    row_number=evidence.row_number,
                    field="name",
                )
            )
            continue
        raw_ko = _text(values["predict_label"])
        raw_decision = _text(values["annotate"])
        ko_id, _ = try_normalize_ko_id(raw_ko)
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
            _text(values["start"]) if has_start else None,
            _text(values["end"]) if has_end else None,
            row_number=evidence.row_number,
            diagnostics=diagnostics,
        )
        outcome = DEEPKOALA_DETAILED.classify(
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
        if ko_id is None and raw_ko.strip():
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.INVALID_KO_IDENTIFIER,
                    message="The DeepKOALA prediction is not a valid K number.",
                    row_number=evidence.row_number,
                    field="predict_label",
                )
            )
        if outcome.reason == "unrecognized_source_decision":
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.UNRECOGNIZED_SOURCE_DECISION,
                    message="The DeepKOALA annotate field uses an unsupported marker.",
                    row_number=evidence.row_number,
                    field="annotate",
                )
            )
        if (
            raw_decision == "*"
            and score is not None
            and threshold is not None
            and score < threshold
        ):
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.SOURCE_DECISION_CONFLICT,
                    message=(
                        "The source acceptance marker and numeric threshold comparison disagree."
                    ),
                    row_number=evidence.row_number,
                    field="annotate,probability,threshold",
                )
            )
        records.append(
            AnnotationRecord(
                record_id=f"record-{len(records) + 1:06d}",
                sample_id=sample_id,
                sequence_id=sequence_id,
                ko_id=ko_id,
                raw_ko=raw_ko,
                raw_decision=raw_decision,
                normalized_status=outcome.status,
                status_reason=outcome.reason,
                decision_policy=DEEPKOALA_DETAILED.reference,
                score=score,
                score_type=ScoreType.PROBABILITY,
                threshold=threshold,
                threshold_rule=ThresholdRule.GTE if threshold is not None else None,
                rank=None,
                domain_start=domain_start,
                domain_end=domain_end,
                evidence=evidence,
                source=provenance,
            )
        )

    mapped = _REQUIRED_COLUMNS + (("start", "end") if has_start else ())
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
