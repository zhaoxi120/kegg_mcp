"""Tests for the compact streaming DeepKOALA accepted-KO analysis view."""

from io import BytesIO

import pytest

from kegg_mcp.analysis import rank_pathways
from kegg_mcp.domain import (
    AnalysisUnit,
    ErrorCode,
    KeggMcpError,
    KoAnalysisView,
    NormalizedStatus,
    build_ko_analysis_view,
)
from kegg_mcp.importers import (
    AnalysisViewImportLimits,
    ImportLimits,
    SourceProvenanceInput,
    import_deepkoala_detailed,
    stream_deepkoala_analysis_view,
)
from kegg_mcp.kegg.contracts import KeggPairRow


class _BoundedReadStream(BytesIO):
    """Fail if streaming intake attempts one unbounded or oversized source read."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.max_requested_bytes = 0

    def read(self, size: int = -1, /) -> bytes:
        if size < 0 or size > 65_536:
            raise AssertionError("analysis source reads must remain bounded")
        self.max_requested_bytes = max(self.max_requested_bytes, size)
        return super().read(size)


def _stream(
    payload: bytes,
    *,
    limits: AnalysisViewImportLimits | None = None,
) -> KoAnalysisView:
    return stream_deepkoala_analysis_view(
        BytesIO(payload),
        input_bytes=len(payload),
        limits=limits,
        analysis_unit=AnalysisUnit.ISOLATE_PROTEOME,
        source=SourceProvenanceInput(
            source_name="deepkoala",
            source_version="1.2.3",
            input_uri="inline://deepkoala-stream",
        ),
    )


def _status_count(view: KoAnalysisView, status: NormalizedStatus) -> int:
    return next(item.count for item in view.status_counts if item.status is status)


def test_streaming_view_uses_sorted_unique_accepted_kos_with_exact_accounting() -> None:
    payload = (
        b"name,predict_label,probability,threshold,annotate,note\n"
        b"p1,K00002,0.9,0.5,*,accepted\n"
        b'p2,"K00001+K00002",0.9,0.5,*,composite\n'
        b"p3,K00003,0.2,0.5,,rejected\n"
        b"p4,,0.9,0.5,,missing\n"
        b"p5,BAD,0.9,0.5,*,invalid\n"
    )

    view = _stream(payload)

    assert view.accepted_ko_ids == ("K00001", "K00002")
    assert view.input_bytes == len(payload)
    assert view.input_rows == 5
    assert view.assignment_count == 6
    assert view.skipped_rows == 0
    assert _status_count(view, NormalizedStatus.ACCEPTED) == 3
    assert _status_count(view, NormalizedStatus.REJECTED) == 1
    assert _status_count(view, NormalizedStatus.UNCLASSIFIED) == 1
    assert _status_count(view, NormalizedStatus.INVALID) == 1
    assert view.sources[0].source_version == "1.2.3"
    assert view.analysis_unit is AnalysisUnit.ISOLATE_PROTEOME
    assert KoAnalysisView.model_validate_json(view.model_dump_json()) == view
    assert KoAnalysisView.model_json_schema()["$id"] == ("urn:kegg-mcp:schema:ko-analysis-view:1")


def test_streaming_view_matches_bounded_import_view_counts() -> None:
    payload = (
        b"name,predict_label,probability,threshold,annotate\n"
        b'p1,"K00001+K00002",0.9,0.5,*\n'
        b"p2,K00003,0.2,0.5,\n"
        b"p3,K00004,0.9,0.5,unsupported\n"
        b"p4,BAD,0.9,0.5,*\n"
        b"p5,K00005,0.9,0.5,*,extra\n"
    )
    full = import_deepkoala_detailed(
        payload,
        limits=ImportLimits(
            max_bytes=len(payload),
            max_rows=100,
            max_columns=10,
            max_field_length=1_000,
        ),
    )

    view = stream_deepkoala_analysis_view(
        BytesIO(payload),
        input_bytes=len(payload),
    )
    imported_view = build_ko_analysis_view(full, input_bytes=len(payload))

    assert view.accepted_ko_ids == imported_view.accepted_ko_ids
    assert view.input_rows == imported_view.input_rows
    assert view.assignment_count == imported_view.assignment_count
    assert view.skipped_rows == imported_view.skipped_rows
    assert view.status_counts == imported_view.status_counts
    assert view.diagnostic_count == imported_view.diagnostic_count


def test_streaming_view_counts_all_diagnostics_but_bounds_the_retained_preview() -> None:
    payload = (
        b"name,predict_label,probability,threshold,annotate\n"
        b"p1,K00001,0.9,0.5,yes\n"
        b"p2,K00002,0.9,0.5,yes\n"
        b"p3,K00003,0.9,0.5,yes\n"
    )
    limits = AnalysisViewImportLimits(max_diagnostic_preview=1)

    view = _stream(payload, limits=limits)

    assert view.accepted_ko_ids == ()
    assert _status_count(view, NormalizedStatus.UNCLASSIFIED) == 3
    assert view.diagnostic_count == 3
    assert len(view.diagnostic_preview) == 1
    assert view.diagnostics_truncated is True


def test_streaming_view_never_requests_the_whole_source_at_once() -> None:
    payload = (
        b"name,predict_label,probability,threshold,annotate\n"
        b"p1,K00001,0.9,0.5,*\n"
        b"p2,K00002,0.9,0.5,*\n"
    )
    stream = _BoundedReadStream(payload)

    view = stream_deepkoala_analysis_view(stream, input_bytes=len(payload))

    assert view.accepted_ko_ids == ("K00001", "K00002")
    assert 0 < stream.max_requested_bytes <= 65_536


def test_streaming_view_feeds_unique_ko_ranking_without_materialized_records() -> None:
    payload = (
        b"name,predict_label,probability,threshold,annotate\n"
        b"p1,K00001,0.9,0.5,*\n"
        b"p2,K00001,0.9,0.5,*\n"
        b"p3,K00002,0.9,0.5,yes\n"
    )
    view = _stream(payload)
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

    ranking = rank_pathways(view, rows)

    assert ranking.selected_ko_ids == ("K00001",)
    assert [item.pathway_id for item in ranking.rows] == ["ko00010"]


def test_streaming_view_enforces_the_unique_ko_bound() -> None:
    payload = (
        b"name,predict_label,probability,threshold,annotate\n"
        b"p1,K00001,0.9,0.5,*\n"
        b"p2,K00002,0.9,0.5,*\n"
    )

    with pytest.raises(KeggMcpError) as caught:
        _stream(payload, limits=AnalysisViewImportLimits(max_unique_ko_ids=1))

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_streaming_view_rejects_a_mismatched_declared_byte_count() -> None:
    payload = b"name,predict_label,probability,threshold,annotate\np1,K00001,0.9,0.5,*\n"

    with pytest.raises(KeggMcpError) as caught:
        stream_deepkoala_analysis_view(
            BytesIO(payload),
            input_bytes=len(payload) - 1,
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
