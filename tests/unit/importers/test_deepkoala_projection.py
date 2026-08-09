"""Tests for the explicitly lossy streaming DeepKOALA unique-KO projection."""

from io import BytesIO

import pytest

from kegg_mcp.analysis import rank_pathways
from kegg_mcp.domain import (
    AnalysisUnit,
    ErrorCode,
    KeggMcpError,
    KoAnalysisProjection,
    NormalizedStatus,
    analysis_accepted_ko_ids,
)
from kegg_mcp.importers import (
    ImportLimits,
    ProjectionImportLimits,
    SourceProvenanceInput,
    import_deepkoala_detailed,
    project_deepkoala_detailed,
)
from kegg_mcp.kegg.contracts import KeggPairRow


class _BoundedReadStream(BytesIO):
    """Fail if the projection attempts one unbounded or oversized source read."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.max_requested_bytes = 0

    def read(self, size: int = -1, /) -> bytes:
        if size < 0 or size > 65_536:
            raise AssertionError("projection source reads must remain bounded")
        self.max_requested_bytes = max(self.max_requested_bytes, size)
        return super().read(size)


def _project(
    payload: bytes,
    *,
    limits: ProjectionImportLimits | None = None,
) -> KoAnalysisProjection:
    return project_deepkoala_detailed(
        BytesIO(payload),
        input_bytes=len(payload),
        limits=limits,
        analysis_unit=AnalysisUnit.ISOLATE_PROTEOME,
        source=SourceProvenanceInput(
            source_name="deepkoala",
            source_version="1.2.3",
            input_uri="inline://deepkoala-projection",
        ),
    )


def _status_count(projection: KoAnalysisProjection, status: NormalizedStatus) -> int:
    return next(item.count for item in projection.status_counts if item.status is status)


def test_projection_streams_sorted_unique_accepted_kos_with_exact_accounting() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate,note\n"
        "p1,K00002,0.9,0.5,*,accepted\n"
        'p2,"K00001+K00002",0.9,0.5,*,composite\n'
        "p3,K00003,0.2,0.5,,rejected\n"
        "p4,,0.9,0.5,,missing\n"
        "p5,BAD,0.9,0.5,*,invalid\n"
    ).encode()

    projection = _project(payload)

    assert projection.accepted_ko_ids == ("K00001", "K00002")
    assert analysis_accepted_ko_ids(projection) == projection.accepted_ko_ids
    assert projection.input_bytes == len(payload)
    assert projection.input_rows == 5
    assert projection.expanded_assignments == 6
    assert projection.skipped_rows == 0
    assert _status_count(projection, NormalizedStatus.ACCEPTED) == 3
    assert _status_count(projection, NormalizedStatus.REJECTED) == 1
    assert _status_count(projection, NormalizedStatus.UNCLASSIFIED) == 1
    assert _status_count(projection, NormalizedStatus.INVALID) == 1
    assert projection.record_level_evidence_retained is False
    assert projection.protein_ko_mapping_available is False
    assert projection.duplicate_conflict_accounting == "not_evaluated"
    assert projection.sources[0].source_version == "1.2.3"
    assert projection.analysis_unit is AnalysisUnit.ISOLATE_PROTEOME
    assert KoAnalysisProjection.model_validate_json(projection.model_dump_json()) == projection
    assert KoAnalysisProjection.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:ko-analysis-projection:1"
    )


def test_projection_matches_full_import_classification_and_aggregate_counts() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        'p1,"K00001+K00002",0.9,0.5,*\n'
        "p2,K00003,0.2,0.5,\n"
        "p3,K00004,0.9,0.5,unsupported\n"
        "p4,BAD,0.9,0.5,*\n"
        "p5,K00005,0.9,0.5,*,extra\n"
    ).encode()
    full = import_deepkoala_detailed(
        payload,
        limits=ImportLimits(
            max_bytes=len(payload),
            max_rows=100,
            max_columns=10,
            max_field_length=1_000,
        ),
    )

    projection = project_deepkoala_detailed(
        BytesIO(payload),
        input_bytes=len(payload),
    )

    assert projection.accepted_ko_ids == analysis_accepted_ko_ids(full)
    assert projection.input_rows == full.import_report.input_rows
    assert projection.expanded_assignments == full.import_report.emitted_records
    assert projection.skipped_rows == full.import_report.skipped_rows
    assert projection.status_counts == full.import_report.status_counts
    assert projection.diagnostic_count == len(full.import_report.diagnostics)


def test_projection_counts_all_diagnostics_but_bounds_the_retained_preview() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.9,0.5,yes\n"
        "p2,K00002,0.9,0.5,yes\n"
        "p3,K00003,0.9,0.5,yes\n"
    ).encode()
    limits = ProjectionImportLimits(max_diagnostic_preview=1)

    projection = _project(payload, limits=limits)

    assert projection.accepted_ko_ids == ()
    assert _status_count(projection, NormalizedStatus.UNCLASSIFIED) == 3
    assert projection.diagnostic_count == 3
    assert len(projection.diagnostic_preview) == 1
    assert projection.diagnostics_truncated is True


def test_projection_never_requests_the_whole_source_stream_at_once() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.9,0.5,*\n"
        "p2,K00002,0.9,0.5,*\n"
    ).encode()
    stream = _BoundedReadStream(payload)

    projection = project_deepkoala_detailed(stream, input_bytes=len(payload))

    assert projection.accepted_ko_ids == ("K00001", "K00002")
    assert 0 < stream.max_requested_bytes <= 65_536


def test_projection_feeds_unique_ko_ranking_without_materialized_records() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.9,0.5,*\n"
        "p2,K00001,0.9,0.5,*\n"
        "p3,K00002,0.9,0.5,yes\n"
    ).encode()
    projection = _project(payload)
    rows = (
        KeggPairRow(
            batch_index=0,
            line_number=1,
            source_id="ko:K00001",
            target_id="path:ko00010",
        ),
        KeggPairRow(
            batch_index=0,
            line_number=2,
            source_id="ko:K00002",
            target_id="path:ko00020",
        ),
    )

    ranking = rank_pathways(projection, rows)

    assert ranking.selected_ko_ids == ("K00001",)
    assert [item.pathway_id for item in ranking.rows] == ["ko00010"]


def test_projection_enforces_the_unique_ko_bound_during_streaming() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.9,0.5,*\n"
        "p2,K00002,0.9,0.5,*\n"
    ).encode()

    with pytest.raises(KeggMcpError) as caught:
        _project(payload, limits=ProjectionImportLimits(max_unique_ko_ids=1))

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_projection_rejects_a_declared_byte_count_that_does_not_match_the_stream() -> None:
    payload = (
        "name,predict_label,probability,threshold,annotate\n"
        "p1,K00001,0.9,0.5,*\n"
    ).encode()

    with pytest.raises(KeggMcpError) as caught:
        project_deepkoala_detailed(
            BytesIO(payload),
            input_bytes=len(payload) - 1,
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
