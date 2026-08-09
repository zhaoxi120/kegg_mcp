"""Internal typing and mutable job state for the DeepKOALA job manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

from deepkoala_mcp.contracts import ExecutionPlan, FastaSummary, ImportHandoff, JobState
from deepkoala_mcp.installation import RuntimeProbeResult
from deepkoala_mcp.job_storage import (
    ControlledOutputDirectory,
    _validate_detailed_csv,  # pyright: ignore[reportPrivateUsage]
    _ValidatedDetailedCsv,  # pyright: ignore[reportPrivateUsage]
)
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
