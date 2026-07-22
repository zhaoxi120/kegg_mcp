"""Internal typing and mutable job state for the DeepKOALA job manager."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from deepkoala_mcp.contracts import ExecutionPlan, FastaSummary, ImportHandoff, JobState
from deepkoala_mcp.installation import RuntimeProbeResult
from deepkoala_mcp.job_storage import ControlledOutputDirectory
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan


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
