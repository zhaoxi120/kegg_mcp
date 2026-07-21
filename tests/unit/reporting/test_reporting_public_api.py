"""Tests for the stable Milestone 5 reporting import surface."""

import kegg_mcp.reporting as reporting
from kegg_mcp.reporting import contracts, render


def test_reporting_package_exports_contracts_and_renderer() -> None:
    expected = {
        "REPORT_FORMAT_NAME",
        "REPORT_FORMAT_VERSION",
        "REPORT_RENDERER_NAME",
        "REPORT_RENDERER_VERSION",
        "AnalysisExecutionProvenance",
        "RenderedReport",
        "ReportArtifact",
        "ReportInput",
        "ReportLimits",
        "ReportSection",
        "StructuredReport",
        "render_report",
    }

    assert expected == set(reporting.__all__)
    assert all(hasattr(reporting, name) for name in expected)
    assert reporting.ReportInput is contracts.ReportInput
    assert reporting.RenderedReport is contracts.RenderedReport
    assert reporting.render_report is render.render_report


def test_reporting_result_schemas_have_stable_identifiers() -> None:
    assert reporting.ReportInput.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:report-input:3"
    )
    assert reporting.StructuredReport.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:structured-report:3"
    )
    assert reporting.ReportArtifact.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:report-artifact:1"
    )
    assert reporting.RenderedReport.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:rendered-report:2"
    )
