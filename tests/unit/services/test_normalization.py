"""Tests for service-level annotation normalization."""

from pathlib import Path

from kegg_mcp.domain import (
    AnnotationDataset,
    ColumnBinding,
    NormalizedStatus,
    ScoreType,
    ThresholdRule,
)
from kegg_mcp.importers import GenericColumnMapping
from kegg_mcp.services.models import (
    AnnotationInputFormat,
    GenericDecisionPolicy,
    NormalizeAnnotationsRequest,
)
from kegg_mcp.services.normalization import normalize_annotations
from kegg_mcp.services.result_store import SQLiteResultStore

_GENERIC_SCORE_CSV = (
    "sequence_id,ko_id,status,score,threshold\n"
    "prot1,K00844,accepted,0.95,0.50\n"
    "prot2,K01810,rejected,0.20,0.50\n"
    "prot3,,unclassified,,\n"
    "prot4,BAD,accepted,0.90,0.50\n"
    "prot5,K01623,accepted,0.80,0.50\n"
)


def _request(column_mapping: GenericColumnMapping | None = None) -> NormalizeAnnotationsRequest:
    return NormalizeAnnotationsRequest(
        text=_GENERIC_SCORE_CSV,
        input_format=AnnotationInputFormat.GENERIC_CSV,
        decision_policy=GenericDecisionPolicy.CANONICAL_SOURCE_STATUS,
        column_mapping=column_mapping,
        preview_limit=5,
    )


def test_inferred_generic_numeric_columns_preserve_source_specific_evidence_and_mapping(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")

    inferred = normalize_annotations(
        _request(),
        result_store=store,
        scope_id="inferred",
        output_directory=tmp_path / "inferred-output",
    )
    explicit_mapping = GenericColumnMapping(
        sequence_id="sequence_id",
        ko_id="ko_id",
        raw_decision="status",
        score="score",
        score_type=ScoreType.PROBABILITY,
        threshold="threshold",
        threshold_rule=ThresholdRule.GTE,
    )
    explicit = normalize_annotations(
        _request(explicit_mapping),
        result_store=store,
        scope_id="explicit",
        output_directory=tmp_path / "explicit-output",
    )

    assert inferred.column_mapping_inferred is True
    assert inferred.column_mapping == (
        ColumnBinding(logical_field="sequence_id", source_column="sequence_id"),
        ColumnBinding(logical_field="ko_id", source_column="ko_id"),
        ColumnBinding(logical_field="raw_decision", source_column="status"),
        ColumnBinding(logical_field="score", source_column="score"),
        ColumnBinding(logical_field="threshold", source_column="threshold"),
    )
    assert explicit.column_mapping_inferred is False
    assert explicit.column_mapping == inferred.column_mapping
    retained = AnnotationDataset.model_validate_json(
        store.read_artifact(
            "inferred",
            inferred.result.result_id,
            inferred.artifact.section,
        ).content
    )
    assert retained.import_report.column_mapping == inferred.column_mapping

    assert [record.score for record in inferred.record_preview] == [0.95, 0.2, None, 0.9, 0.8]
    assert [record.threshold for record in inferred.record_preview] == [
        0.5,
        0.5,
        None,
        0.5,
        0.5,
    ]
    assert [record.score for record in inferred.record_preview] == [
        record.score for record in explicit.record_preview
    ]
    assert [record.threshold for record in inferred.record_preview] == [
        record.threshold for record in explicit.record_preview
    ]
    assert {record.score_type for record in inferred.record_preview} == {ScoreType.SOURCE_SPECIFIC}
    assert {
        record.threshold_rule for record in inferred.record_preview if record.threshold is not None
    } == {ThresholdRule.SOURCE_SPECIFIC}
    assert {record.score_type for record in explicit.record_preview} == {ScoreType.PROBABILITY}
    assert {
        record.threshold_rule for record in explicit.record_preview if record.threshold is not None
    } == {ThresholdRule.GTE}
    assert [record.normalized_status for record in inferred.record_preview] == [
        NormalizedStatus.ACCEPTED,
        NormalizedStatus.REJECTED,
        NormalizedStatus.UNCLASSIFIED,
        NormalizedStatus.INVALID,
        NormalizedStatus.ACCEPTED,
    ]
    assert [record.normalized_status for record in inferred.record_preview] == [
        record.normalized_status for record in explicit.record_preview
    ]

    inferred_tsv = (tmp_path / "inferred-output" / "normalized_annotations.tsv").read_text(
        encoding="utf-8"
    )
    explicit_tsv = (tmp_path / "explicit-output" / "normalized_annotations.tsv").read_text(
        encoding="utf-8"
    )
    assert inferred_tsv == explicit_tsv
    assert "prot1\t\tK00844\taccepted\tsource_accepted\t0.95\t0.5" in inferred_tsv
