"""Public deterministic reporting contracts and renderers."""

from kegg_mcp.report_limits import ReportLimits
from kegg_mcp.reporting.contracts import (
    REPORT_FORMAT_NAME,
    REPORT_FORMAT_VERSION,
    REPORT_RENDERER_NAME,
    REPORT_RENDERER_VERSION,
    AnalysisExecutionProvenance,
    RenderedReport,
    ReportArtifact,
    ReportInput,
    ReportSection,
    StructuredReport,
)
from kegg_mcp.reporting.render import render_report

__all__ = [
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
]
