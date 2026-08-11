"""Annotation import, normalization, and retained dataset creation."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from kegg_mcp.domain.analysis_view import KoAnalysisView, build_ko_analysis_view
from kegg_mcp.domain.annotations import AnnotationDataset, ScoreType, ThresholdRule
from kegg_mcp.domain.decisions import CANONICAL_SOURCE_STATUS, USER_SUPPLIED_KO
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.importers import (
    GenericColumnMapping,
    TableDialect,
    import_deepkoala_detailed,
    import_generic_table,
    import_plain_ko,
)
from kegg_mcp.services.models import (
    DATASET_SECTION,
    AnnotationInputFormat,
    GenericDecisionPolicy,
    NormalizeAnnotationsRequest,
    NormalizeAnnotationsResult,
)
from kegg_mcp.services.output_bundle import write_normalization_bundle
from kegg_mcp.services.previews import (
    _annotation_provenance,
    _annotation_record_preview,
    _import_summary,
)
from kegg_mcp.services.result_builders import _artifact_metadata
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    SQLiteResultStore,
    create_retained_result,
)


def normalize_annotations(
    request: NormalizeAnnotationsRequest,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
    output_directory: Path | None = None,
    remove_created_output_on_failure: bool = False,
) -> NormalizeAnnotationsResult:
    """Normalize one inline payload and retain its complete typed dataset."""
    dataset = _import_dataset(request)
    content = dataset.model_dump_json().encode("utf-8")
    with create_retained_result(
        result_store,
        scope_id,
        (
            ResultArtifactInput(
                section=DATASET_SECTION, mime_type="application/json", content=content
            ),
        ),
    ) as metadata:
        output_bundle = (
            write_normalization_bundle(
                dataset,
                output_directory,
                manifest_path_mode=request.manifest_path_mode,
                remove_created_directory_on_failure=remove_created_output_on_failure,
            )
            if output_directory is not None
            else None
        )
    artifact = _artifact_metadata(DATASET_SECTION, "application/json", content)
    preview = tuple(
        _annotation_record_preview(record) for record in dataset.records[: request.preview_limit]
    )
    diagnostics = dataset.import_report.diagnostics[: request.diagnostic_preview_limit]
    return NormalizeAnnotationsResult(
        result=metadata,
        artifact=artifact,
        import_summary=_import_summary(dataset),
        provenance=_annotation_provenance(dataset),
        record_preview=preview,
        preview_truncated=len(preview) < len(dataset.records),
        diagnostic_count=len(dataset.import_report.diagnostics),
        diagnostic_preview=diagnostics,
        diagnostics_truncated=len(diagnostics) < len(dataset.import_report.diagnostics),
        column_mapping=dataset.import_report.column_mapping,
        column_mapping_inferred=(
            request.input_format
            in {AnnotationInputFormat.GENERIC_CSV, AnnotationInputFormat.GENERIC_TSV}
            and request.column_mapping is None
        ),
        output_bundle=output_bundle,
    )


def build_analysis_view(request: NormalizeAnnotationsRequest) -> KoAnalysisView:
    """Import one bounded materialized request and immediately discard record-level evidence."""
    if request.text is None:
        raise AssertionError("analysis-view requests must be materialized before service execution")
    return build_ko_analysis_view(
        _import_dataset(request),
        input_bytes=len(request.text.encode()),
    )


def _import_dataset(request: NormalizeAnnotationsRequest) -> AnnotationDataset:
    if request.text is None:
        raise AssertionError("file-backed requests must be materialized before service execution")
    if request.input_format is AnnotationInputFormat.PLAIN_KO:
        return import_plain_ko(
            request.text,
            limits=request.import_limits,
            analysis_unit=request.analysis_unit,
            sample_id=request.sample_id,
            taxon_id=request.taxon_id,
            kegg_organism_code=request.kegg_organism_code,
            source=request.source,
        )
    if request.input_format is AnnotationInputFormat.DEEPKOALA_DETAILED:
        return import_deepkoala_detailed(
            request.text,
            limits=request.import_limits,
            analysis_unit=request.analysis_unit,
            sample_id=request.sample_id,
            taxon_id=request.taxon_id,
            kegg_organism_code=request.kegg_organism_code,
            source=request.source,
        )
    mapping = request.column_mapping or _infer_generic_column_mapping(
        request.text,
        delimiter=("," if request.input_format is AnnotationInputFormat.GENERIC_CSV else "\t"),
    )
    selected_policy = request.decision_policy
    if selected_policy is None:
        selected_policy = (
            GenericDecisionPolicy.CANONICAL_SOURCE_STATUS
            if mapping.raw_decision is not None
            else GenericDecisionPolicy.USER_SUPPLIED_KO
        )
    policy = (
        USER_SUPPLIED_KO
        if selected_policy is GenericDecisionPolicy.USER_SUPPLIED_KO
        else CANONICAL_SOURCE_STATUS
    )
    dialect = (
        TableDialect.CSV
        if request.input_format is AnnotationInputFormat.GENERIC_CSV
        else TableDialect.TSV
    )
    return import_generic_table(
        request.text,
        dialect=dialect,
        mapping=mapping,
        policy=policy,
        limits=request.import_limits,
        analysis_unit=request.analysis_unit,
        default_sample_id=request.sample_id,
        taxon_id=request.taxon_id,
        kegg_organism_code=request.kegg_organism_code,
        source=request.source,
    )


def _infer_generic_column_mapping(text: str, *, delimiter: str) -> GenericColumnMapping:
    """Infer unambiguous common columns with explicitly source-specific numeric semantics."""
    try:
        header = next(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
    except (StopIteration, csv.Error):
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The generic annotation table does not contain a readable header.",
            suggested_action="Provide a UTF-8 CSV or TSV file with one header row.",
        )
    normalized: dict[str, list[str]] = {}
    for column in header:
        normalized.setdefault(column.strip().casefold(), []).append(column)

    def select(logical_name: str, aliases: tuple[str, ...], *, required: bool) -> str | None:
        matches = [value for alias in aliases for value in normalized.get(alias, ())]
        if len(matches) > 1:
            fail(
                ErrorCode.AMBIGUOUS_COLUMN_MAPPING,
                f"More than one common column matches {logical_name}.",
                suggested_action=f"Supply column_mapping.{logical_name} explicitly.",
            )
        if not matches and required:
            fail(
                ErrorCode.MISSING_REQUIRED_COLUMN,
                f"No common column name matches {logical_name}.",
                suggested_action=f"Supply column_mapping.{logical_name} explicitly.",
            )
        return matches[0] if matches else None

    sequence_id = select(
        "sequence_id",
        ("sequence_id", "protein_id", "seq_id", "query_id", "gene_id"),
        required=True,
    )
    ko_id = select("ko_id", ("ko_id", "ko", "k_number", "kegg_orthology"), required=True)
    if sequence_id is None or ko_id is None:
        raise AssertionError("required generic-column inference returned no column")
    score = select("score", ("score",), required=False)
    threshold = select("threshold", ("threshold",), required=False)
    return GenericColumnMapping(
        sequence_id=sequence_id,
        ko_id=ko_id,
        protein_name=select(
            "protein_name", ("protein_name", "protein", "description"), required=False
        ),
        sample_id=select("sample_id", ("sample_id", "sample"), required=False),
        raw_decision=select(
            "raw_decision", ("raw_decision", "decision", "status", "annotate"), required=False
        ),
        score=score,
        score_type=ScoreType.SOURCE_SPECIFIC if score is not None else None,
        threshold=threshold,
        threshold_rule=ThresholdRule.SOURCE_SPECIFIC if threshold is not None else None,
    )


__all__ = ["build_analysis_view", "normalize_annotations"]
