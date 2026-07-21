"""Stable DeepKOALA handoff and run-report construction."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from deepkoala_mcp import __version__
from deepkoala_mcp.contracts import (
    ANNOTATIONS_FILENAME,
    HANDOFF_SCHEMA_VERSION,
    ExecutionPlan,
    FastaSummary,
    ImportHandoff,
    SourceMetadataField,
    SourceProvenance,
)
from deepkoala_mcp.installation import RuntimeProbeResult


def build_handoff(
    *,
    job_id: str,
    input_path: Path,
    source_version: str,
    plan: ExecutionPlan,
    annotations_path: Path,
    report_path: Path,
    completed_at: datetime,
) -> ImportHandoff:
    """Build the versioned stable file handoff without a digest or private result ID."""
    metadata = (
        SourceMetadataField(name="device_requested", value="auto"),
        SourceMetadataField(name="detail", value=True),
        SourceMetadataField(name="batch_size", value=plan.batch_size),
        SourceMetadataField(name="num_workers", value=0),
        SourceMetadataField(name="topk", value=plan.topk),
        SourceMetadataField(name="multi", value=plan.multi),
    )
    source = SourceProvenance(
        source_version=source_version,
        model_name=plan.model,
        model_version=plan.resolved_model_date,
        annotation_date=completed_at,
        input_path=str(input_path),
        source_metadata=metadata,
    )
    base = f"deepkoala://jobs/{job_id}"
    return ImportHandoff(
        schema_version=HANDOFF_SCHEMA_VERSION,
        tool_version=__version__,
        input_path=str(input_path),
        annotations_path=str(annotations_path),
        report_path=str(report_path),
        input_format="deepkoala_detailed",
        annotations_resource_uri=f"{base}/annotations",
        report_resource_uri=f"{base}/report",
        source=source,
    )


def build_run_report(
    *,
    input_path: Path,
    source_version: str,
    plan: ExecutionPlan,
    fasta: FastaSummary,
    started_at: datetime,
    completed_at: datetime,
    runtime: RuntimeProbeResult,
) -> str:
    """Build a bounded human-readable record of the completed local run."""
    return "\n".join(
        (
            "# DeepKOALA Run Report",
            "",
            f"- Input FASTA absolute path (JSON): {json.dumps(str(input_path))}",
            f"- Annotation file: `{ANNOTATIONS_FILENAME}`",
            f"- Handoff schema version: `{HANDOFF_SCHEMA_VERSION}`",
            f"- Companion version: `{__version__}`",
            f"- DeepKOALA version: `{source_version}`",
            f"- Model: `{plan.model}`",
            f"- Model date: `{plan.resolved_model_date}`",
            "- Device policy: `auto`",
            f"- CUDA available at preflight: `{str(runtime.cuda_available).lower()}`",
            "- Detailed output: `true`",
            f"- Batch size: `{plan.batch_size}`",
            "- Worker processes: `0`",
            f"- Top-k: `{plan.topk}`",
            f"- Multi-domain mode: `{str(plan.multi).lower()}`",
            f"- Timeout seconds: `{plan.timeout_seconds}`",
            f"- Sequence count: `{fasta.sequence_count}`",
            f"- Started at: `{_iso(started_at)}`",
            f"- Completed at: `{_iso(completed_at)}`",
            "",
            "K number assignments are computational annotations, not experimental validation.",
            "",
        )
    )


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


__all__ = ["build_handoff", "build_run_report"]
