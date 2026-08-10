"""Internal typing and mutable job state for the DeepKOALA job manager."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from deepkoala_mcp.contracts import (
    AnnotationOutputCoverage,
    ExecutionPlan,
    FastaSummary,
    ImportHandoff,
    JobState,
)
from deepkoala_mcp.fasta import (
    _run_sync_in_joined_worker,  # pyright: ignore[reportPrivateUsage]
)
from deepkoala_mcp.installation import RuntimeProbeResult
from deepkoala_mcp.job_storage import (
    ControlledOutputDirectory,
    _validate_detailed_csv,  # pyright: ignore[reportPrivateUsage]
    _ValidatedDetailedCsv,  # pyright: ignore[reportPrivateUsage]
    publish_artifacts,
)
from deepkoala_mcp.reporting import build_run_report
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan

ArtifactName = Literal["annotations", "report"]


class Runner(Protocol):
    async def run(self, plan: RunnerPlan) -> ProcessOutcome: ...


class RuntimeProbe(Protocol):
    def __call__(
        self,
        *,
        checkout: Path,
        python_executable: Path,
        cpu_threads: int,
    ) -> RuntimeProbeResult: ...


@dataclass(frozen=True, slots=True)
class _PublishedOutput:
    """Private stable publication facts returned to the event-loop orchestrator."""

    annotations_path: Path
    report_path: Path
    output_bytes: int
    coverage: AnnotationOutputCoverage
    completed_at: datetime


@dataclass(slots=True)
class JobRecord:
    job_id: str
    directory: Path
    output_directory: ControlledOutputDirectory
    input_path: Path
    source_version: str
    plan: ExecutionPlan
    fasta: FastaSummary
    input_sequence_ids: frozenset[str] = field(repr=False)
    started_at: datetime
    runner_lock_fd: int | None
    state: JobState = JobState.RUNNING
    completed_at: datetime | None = None
    exit_code: int | None = None
    failure_reason: str | None = None
    correlation_id: str | None = None
    output_bytes: int | None = None
    handoff: ImportHandoff | None = None
    task: asyncio.Task[None] | None = None
    cancel_requested: bool = False

    def validate_output(self, max_output_bytes: int) -> _ValidatedDetailedCsv:
        if len(self.input_sequence_ids) != self.fasta.sequence_count:
            raise RuntimeError("staged FASTA identifier accounting is inconsistent")
        return _validate_detailed_csv(
            self.directory / "output.csv",
            max_output_bytes,
            expected_sequence_ids=self.input_sequence_ids,
            multi=self.plan.multi,
            topk=self.plan.topk,
        )

    async def publish_output_in_worker(
        self,
        *,
        max_output_bytes: int,
        runtime: RuntimeProbeResult,
        completion_clock: Callable[[], datetime],
    ) -> _PublishedOutput:
        """Validate and publish off-loop, joining the worker before cancellation cleanup."""

        def validate_and_publish() -> _PublishedOutput:
            validated_output = self.validate_output(max_output_bytes)
            completed_at: datetime | None = None

            def build_report_after_csv_copy() -> str:
                nonlocal completed_at
                completed_at = completion_clock()
                return build_run_report(
                    input_path=self.input_path,
                    source_version=self.source_version,
                    plan=self.plan,
                    fasta=self.fasta,
                    started_at=self.started_at,
                    completed_at=completed_at,
                    runtime=runtime,
                    output_coverage=validated_output.coverage,
                )

            annotations, report, output_bytes = publish_artifacts(
                validated_output=validated_output,
                output_directory=self.output_directory,
                report_builder=build_report_after_csv_copy,
                max_output_bytes=max_output_bytes,
            )
            if completed_at is None:
                raise RuntimeError("successful publication did not build its run report")
            return _PublishedOutput(
                annotations_path=annotations,
                report_path=report,
                output_bytes=output_bytes,
                coverage=validated_output.coverage,
                completed_at=completed_at,
            )

        return await _run_sync_in_joined_worker(validate_and_publish)
