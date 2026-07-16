"""Plain K-number list importer."""

from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    AnnotationDataset,
    AnnotationRecord,
    DiagnosticCode,
    EvidenceField,
    ImportDiagnostic,
    InputFormat,
    RowEvidence,
)
from kegg_mcp.domain.decisions import USER_SUPPLIED_KO, DecisionEvidence
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.importers._common import (
    build_source,
    decode_payload,
    finalize_dataset,
    no_assignment_slot,
    require_non_empty_label,
    validate_auxiliary_evidence,
)
from kegg_mcp.importers.contracts import ImportLimits, SourceProvenanceInput


def import_plain_ko(
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
    """Import newline-delimited K numbers while preserving every non-empty row."""
    sample_id = require_non_empty_label(sample_id, field="sample_id")
    validate_auxiliary_evidence(metadata, source, limits)
    decoded = decode_payload(payload, limits)
    provenance = build_source(
        decoded,
        source,
        default_source_name="manual",
        importer_name="plain_ko",
    )
    records: list[AnnotationRecord] = []
    diagnostics: list[ImportDiagnostic] = []
    input_rows = 0

    for row_number, raw_ko in enumerate(decoded.text.splitlines(), start=1):
        if not raw_ko.strip():
            continue
        input_rows += 1
        if input_rows > limits.max_rows:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The KO list exceeds the configured row limit.",
                suggested_action="Reduce the row count or use an explicitly larger safe limit.",
                safe_details=(SafeDetail(name="max_rows", value=str(limits.max_rows)),),
            )
        if len(raw_ko) > limits.max_field_length:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "A KO-list field exceeds the configured length limit.",
                suggested_action="Shorten the field or use a larger safe limit.",
                safe_details=(
                    SafeDetail(name="max_field_length", value=str(limits.max_field_length)),
                ),
            )
        ko_id, _ = try_normalize_ko_id(raw_ko)
        outcome = USER_SUPPLIED_KO.classify(
            DecisionEvidence(
                raw_ko=raw_ko,
                ko_id=ko_id,
                raw_decision=None,
                score=None,
                score_type=None,
                threshold=None,
                threshold_rule=None,
            )
        )
        evidence = RowEvidence(
            row_number=row_number,
            fields=(EvidenceField(name="raw_ko", value=raw_ko),),
        )
        record = AnnotationRecord(
            record_id=f"record-{len(records) + 1:06d}",
            sample_id=sample_id,
            sequence_id=None,
            ko_id=ko_id,
            raw_ko=raw_ko,
            raw_decision=None,
            normalized_status=outcome.status,
            status_reason=outcome.reason,
            decision_policy=USER_SUPPLIED_KO.reference,
            score=None,
            score_type=None,
            threshold=None,
            threshold_rule=None,
            rank=None,
            domain_start=None,
            domain_end=None,
            evidence=evidence,
            source=provenance,
        )
        records.append(record)
        if ko_id is None:
            diagnostics.append(
                ImportDiagnostic(
                    code=DiagnosticCode.INVALID_KO_IDENTIFIER,
                    message="The row does not contain a valid K number.",
                    row_number=row_number,
                    field="raw_ko",
                )
            )

    if not records:
        diagnostics.append(
            ImportDiagnostic(
                code=DiagnosticCode.EMPTY_INPUT,
                message="The input contains no non-empty KO rows.",
                row_number=None,
                field=None,
            )
        )

    def plain_duplicate_key(record: AnnotationRecord) -> tuple[str, str]:
        if record.ko_id is not None:
            return "ko_id", record.ko_id
        return "invalid", record.raw_ko.strip()

    return finalize_dataset(
        records,
        input_format=InputFormat.PLAIN_KO,
        input_rows=input_rows,
        skipped_rows=0,
        source_columns=(),
        delimiter=None,
        column_mapping=(),
        policy=USER_SUPPLIED_KO.reference,
        diagnostics=diagnostics,
        unparsed_rows=(),
        duplicate_key=plain_duplicate_key,
        conflict_key=no_assignment_slot,
        analysis_unit=analysis_unit,
        taxon_id=taxon_id,
        kegg_organism_code=kegg_organism_code,
        metadata=metadata,
        source=provenance,
    )
