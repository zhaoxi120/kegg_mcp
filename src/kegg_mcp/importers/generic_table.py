"""Generic CSV/TSV importer with explicit column and decision contracts."""

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
)
from kegg_mcp.domain.decisions import DecisionEvidence, DecisionPolicy
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.importers._common import (
    build_source,
    constrain_probability,
    decode_payload,
    exact_record_key,
    explicit_assignment_slot,
    finalize_dataset,
    is_valid_record_label,
    parse_domain,
    parse_optional_float,
    parse_optional_positive_int,
    parse_table,
    require_columns,
    require_non_empty_label,
    validate_auxiliary_evidence,
)
from kegg_mcp.importers.contracts import (
    GenericColumnMapping,
    ImportLimits,
    SourceProvenanceInput,
    TableDialect,
)


def import_generic_table(
    payload: str | bytes,
    *,
    dialect: TableDialect,
    mapping: GenericColumnMapping,
    policy: DecisionPolicy,
    limits: ImportLimits,
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN,
    default_sample_id: str = "sample-1",
    taxon_id: int | None = None,
    kegg_organism_code: str | None = None,
    metadata: tuple[EvidenceField, ...] = (),
    source: SourceProvenanceInput | None = None,
) -> AnnotationDataset:
    """Import a generic table without guessing columns or score semantics."""
    default_sample_id = require_non_empty_label(default_sample_id, field="default_sample_id")
    validate_auxiliary_evidence(metadata, source, limits)
    input_format = (
        InputFormat.GENERIC_CSV if dialect is TableDialect.CSV else InputFormat.GENERIC_TSV
    )
    if input_format not in policy.supported_formats:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The selected decision policy does not support this generic table format.",
            suggested_action="Select a policy designed for generic CSV or TSV input.",
            safe_details=(
                SafeDetail(name="policy", value=policy.reference.identifier),
                SafeDetail(name="input_format", value=input_format.value),
            ),
        )
    decoded = decode_payload(payload, limits)
    table = parse_table(decoded, delimiter=dialect.delimiter, limits=limits)
    require_columns(table.header, tuple(source_name for _, source_name in mapping.bindings()))
    provenance = build_source(
        decoded,
        source,
        default_source_name="unknown",
        importer_name="generic_table",
    )
    diagnostics = list(table.diagnostics)
    records: list[AnnotationRecord] = []
    skipped_rows = len(table.unparsed_rows)
    unparsed_rows = list(table.unparsed_rows)

    for evidence in table.rows:
        values = {field.name: field.value for field in evidence.fields}
        sequence_id = _required_text(values[mapping.sequence_id]).strip()
        if not is_valid_record_label(sequence_id):
            skipped_rows += 1
            unparsed_rows.append(evidence)
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.ROW_SKIPPED,
                    message="The required sequence identifier is empty or oversized.",
                    row_number=evidence.row_number,
                    field=mapping.sequence_id,
                )
            )
            continue
        if mapping.sample_id is None:
            sample_id = default_sample_id
        else:
            sample_id = _required_text(values[mapping.sample_id]).strip()
            if not is_valid_record_label(sample_id):
                skipped_rows += 1
                unparsed_rows.append(evidence)
                diagnostics.append(
                    ImportDiagnostic(
                        code=DiagnosticCode.ROW_SKIPPED,
                        message="A mapped sample identifier is empty or oversized.",
                        row_number=evidence.row_number,
                        field=mapping.sample_id,
                    )
                )
                continue

        raw_ko = _required_text(values[mapping.ko_id])
        ko_id, _ = try_normalize_ko_id(raw_ko)
        raw_decision = (
            _required_text(values[mapping.raw_decision])
            if mapping.raw_decision is not None
            else None
        )
        score = (
            parse_optional_float(
                _required_text(values[mapping.score]),
                row_number=evidence.row_number,
                field=mapping.score,
                diagnostics=diagnostics,
            )
            if mapping.score is not None
            else None
        )
        threshold = (
            parse_optional_float(
                _required_text(values[mapping.threshold]),
                row_number=evidence.row_number,
                field=mapping.threshold,
                diagnostics=diagnostics,
            )
            if mapping.threshold is not None
            else None
        )
        if mapping.score_type is ScoreType.PROBABILITY:
            if mapping.score is not None:
                score = constrain_probability(
                    score,
                    row_number=evidence.row_number,
                    field=mapping.score,
                    diagnostics=diagnostics,
                )
            if mapping.threshold is not None:
                threshold = constrain_probability(
                    threshold,
                    row_number=evidence.row_number,
                    field=mapping.threshold,
                    diagnostics=diagnostics,
                )
        rank = (
            parse_optional_positive_int(
                _required_text(values[mapping.rank]),
                row_number=evidence.row_number,
                field=mapping.rank,
                diagnostics=diagnostics,
            )
            if mapping.rank is not None
            else None
        )
        domain_start, domain_end = parse_domain(
            _required_text(values[mapping.domain_start])
            if mapping.domain_start is not None
            else None,
            _required_text(values[mapping.domain_end]) if mapping.domain_end is not None else None,
            row_number=evidence.row_number,
            diagnostics=diagnostics,
        )
        outcome = policy.classify(
            DecisionEvidence(
                raw_ko=raw_ko,
                ko_id=ko_id,
                raw_decision=raw_decision,
                score=score,
                score_type=mapping.score_type,
                threshold=threshold,
                threshold_rule=mapping.threshold_rule if threshold is not None else None,
            )
        )
        if ko_id is None and raw_ko.strip():
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.INVALID_KO_IDENTIFIER,
                    message="The mapped KO field is not a valid K number.",
                    row_number=evidence.row_number,
                    field=mapping.ko_id,
                )
            )
        if outcome.reason == "unrecognized_source_decision":
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.UNRECOGNIZED_SOURCE_DECISION,
                    message="The decision policy does not recognize the source decision.",
                    row_number=evidence.row_number,
                    field=mapping.raw_decision,
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
                decision_policy=policy.reference,
                score=score,
                score_type=mapping.score_type,
                threshold=threshold,
                threshold_rule=mapping.threshold_rule if threshold is not None else None,
                rank=rank,
                domain_start=domain_start,
                domain_end=domain_end,
                evidence=evidence,
                source=provenance,
            )
        )

    bindings = tuple(
        ColumnBinding(logical_field=logical, source_column=source_column)
        for logical, source_column in mapping.bindings()
    )
    return finalize_dataset(
        records,
        input_format=input_format,
        input_rows=table.input_rows,
        skipped_rows=skipped_rows,
        source_columns=table.header,
        delimiter=dialect.delimiter,
        column_mapping=bindings,
        policy=policy.reference,
        diagnostics=diagnostics,
        unparsed_rows=unparsed_rows,
        duplicate_key=exact_record_key,
        conflict_key=explicit_assignment_slot,
        analysis_unit=analysis_unit,
        taxon_id=taxon_id,
        kegg_organism_code=kegg_organism_code,
        metadata=metadata,
        source=provenance,
    )


def _required_text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("table evidence values must be text")
    return value
