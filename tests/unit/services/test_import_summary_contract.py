"""Tests for concise service-level preview contracts."""

import pytest
from pydantic import ValidationError

from kegg_mcp.domain import AnalysisUnit, NormalizedStatus, StatusCount
from kegg_mcp.services.contracts import ImportSummary


def _status_counts(*, accepted: int) -> tuple[StatusCount, ...]:
    return tuple(
        StatusCount(
            status=status,
            count=accepted if status is NormalizedStatus.ACCEPTED else 0,
        )
        for status in NormalizedStatus
    )


def test_import_summary_allows_multiple_records_from_one_source_row() -> None:
    summary = ImportSummary(
        dataset_id="dataset-1",
        analysis_unit=AnalysisUnit.UNKNOWN,
        input_rows=1,
        emitted_records=2,
        skipped_rows=0,
        duplicate_count=0,
        conflict_count=0,
        status_counts=_status_counts(accepted=2),
    )

    assert summary.input_rows == 1
    assert summary.emitted_records == 2


def test_import_summary_rejects_records_without_a_parsed_source_row() -> None:
    with pytest.raises(ValidationError, match="require at least one parsed input row"):
        ImportSummary(
            dataset_id="dataset-1",
            analysis_unit=AnalysisUnit.UNKNOWN,
            input_rows=0,
            emitted_records=1,
            skipped_rows=0,
            duplicate_count=0,
            conflict_count=0,
            status_counts=_status_counts(accepted=1),
        )
