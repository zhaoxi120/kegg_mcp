"""Process-local job lifecycle and core-import handoff tests."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

import deepkoala_mcp.jobs as jobs_module
from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.contracts import DeepKoalaMcpError, JobState, PrepareDeepKoalaInput
from deepkoala_mcp.deepkoala import DeepKoalaInstallation
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan

_FAKE_CLI = """\
import argparse
import csv
import sys
import time
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--input_path', required=True)
p.add_argument('--output_path', required=True)
p.add_argument('--model', choices=['full', 'frag'], default='full')
p.add_argument('--date', default='latest')
p.add_argument('--device', choices=['auto', 'cpu', 'cuda', 'mps'], default='auto')
p.add_argument('--detail', action='store_true')
p.add_argument('--batch_size', type=int, default=32)
p.add_argument('--num_workers', type=int, default=2)
p.add_argument('--topk', type=int, default=1)
args = p.parse_args()

text = Path(args.input_path).read_text(encoding='utf-8')
names = [line[1:].split()[0] for line in text.splitlines() if line.startswith('>')]
if any(name.startswith('SLOW') for name in names):
    print('runner is waiting', flush=True)
    time.sleep(30)
if any(name.startswith('FAIL') for name in names):
    print('safe synthetic failure')
    sys.exit(1)
if any(name.startswith('SIMPLE') for name in names):
    Path(args.output_path).write_text('name,predict_label\\nSIMPLE,K00001\\n', encoding='utf-8')
    sys.exit(0)

with Path(args.output_path).open('w', newline='', encoding='utf-8') as stream:
    writer = csv.writer(stream)
    writer.writerow(['name', 'predict_label', 'probability', 'threshold', 'annotate'])
    for name in names:
        for _rank in range(args.topk):
            writer.writerow([name, 'K00001', '0.9', '0.5', '*'])
print(f'Processed {len(names)} sequences, annotated {len(names)}.')
"""


def _fake_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "deepkoala"
    package = checkout / "deepkoala"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "utils.py").write_text(
        "def resolve_device(requested):\n    return 'cpu' if requested == 'auto' else requested\n",
        encoding="utf-8",
    )
    (package / "cli.py").write_text(_FAKE_CLI, encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "deepkoala"\nversion = "0.1-beta"\n',
        encoding="utf-8",
    )
    resources = checkout / "resources" / "202502"
    resources.mkdir(parents=True)
    for model in ("full", "frag"):
        (resources / f"weights_{model}.pt").write_bytes(f"{model}-weights".encode())
        (resources / f"ko_config_{model}.json").write_text(
            '{"K00001":{"index":0,"threshold":0.5}}', encoding="utf-8"
        )
    return checkout.resolve()


def _config(
    tmp_path: Path,
    *,
    max_queue_size: int = 4,
    retention_seconds: int = 86_400,
) -> DeepKoalaRuntimeConfig:
    return DeepKoalaRuntimeConfig(
        checkout=_fake_checkout(tmp_path),
        python_executable=Path(sys.executable).resolve(),
        state_root=(tmp_path / "state").resolve(),
        max_queue_size=max_queue_size,
        cpu_threads=2,
        retention_seconds=retention_seconds,
    )


async def _wait_terminal(
    manager: DeepKoalaJobManager,
    job_id: str,
    *,
    attempts: int = 200,
) -> JobState:
    for _ in range(attempts):
        current = await manager.get_job(job_id)
        if current.job.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }:
            return current.job.state
        await asyncio.sleep(0.02)
    raise AssertionError("job did not reach a terminal state")


@pytest.mark.asyncio
async def test_prepare_does_not_run_inference_then_success_exposes_handoff(
    tmp_path: Path,
) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        prepared = await manager.prepare(PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n"))

        assert prepared.state == "prepared"
        assert prepared.notice.settings.requested_device == "auto"
        assert prepared.notice.settings.resolved_device == "cpu"
        assert prepared.notice.settings.resolved_model_date == "202502"
        assert prepared.notice.settings.batch_size == 32
        assert prepared.notice.settings.num_workers == 2
        assert prepared.notice.settings.topk == 1
        assert prepared.notice.settings.multi is False
        assert prepared.notice.fasta == prepared.fasta
        assert {artifact.kind for artifact in prepared.notice.execution_artifacts} == {
            "configured_python",
            "deepkoala_source",
            "model_weights",
            "model_config",
        }
        assert any("GPU" in warning for warning in prepared.notice.warnings)
        assert not tuple(manager.config.state_root.rglob("detailed.csv"))
        status = await manager.status()
        assert status.limits.max_sequences == manager.config.max_sequences
        assert status.limits.max_residues == manager.config.max_residues
        assert status.limits.max_sequence_length == manager.config.max_sequence_length
        assert status.limits.max_header_length == manager.config.max_header_length

        submitted = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )
        replay = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )
        assert submitted.job.job_id == replay.job.job_id
        assert replay.idempotent_replay is True
        with pytest.raises(DeepKoalaMcpError) as stale_replay:
            await manager.submit(
                plan_id=prepared.plan_id,
                notice_sha256="0" * 64,
            )
        assert stale_replay.value.detail.code.value == "NOTICE_STALE"
        assert await _wait_terminal(manager, submitted.job.job_id) is JobState.SUCCEEDED

        result = await manager.get_job(submitted.job.job_id)
        assert result.handoff is not None
        assert result.handoff.input_format == "deepkoala_detailed"
        assert result.handoff.source_provenance_template.source_name == "deepkoala"
        assert result.handoff.source_provenance_template.model_version == "202502"
        assert result.job.output_rows == 1
        assert result.job.diagnostics_truncated is False
        artifact = await manager.read_artifact(
            submitted.job.job_id, "output", offset=0, limit=1_024
        )
        output = artifact.content.decode("utf-8")
        assert "name,predict_label,probability,threshold,annotate" in output
        assert result.job.output_sha256 == artifact.sha256

        provenance = await manager.read_artifact(
            submitted.job.job_id, "provenance", offset=0, limit=65_536
        )
        provenance_data = json.loads(provenance.content)
        assert provenance_data["execution"]["resolved_device"] == "cpu"
        assert provenance_data["model"]["resolved_date"] == "202502"
        assert provenance_data["source"]["artifact"]["sha256"]
        assert provenance_data["source"]["configured_python"]["sha256"]
        assert provenance_data["execution"]["argv_template"] == [
            "<configured-python>",
            "-c",
            "<bounded-launcher>",
            "5000000",
            "--input_path",
            "<private-input>",
            "--output_path",
            "<private-output>",
            "--model",
            "full",
            "--date",
            "202502",
            "--device",
            "cpu",
            "--detail",
        ]
        assert provenance_data["diagnostics"]["truncated"] is False
        assert 0 < provenance_data["diagnostics"]["retained_bytes"] <= 65_536
        assert set(provenance_data["diagnostics"]) == {"retained_bytes", "truncated"}
        assert str(tmp_path) not in provenance.content.decode("utf-8")

        deleted = await manager.delete(submitted.job.job_id)
        replayed_delete = await manager.delete(submitted.job.job_id)
        assert deleted.deleted is True
        assert replayed_delete == deleted
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_submit_rejects_wrong_notice_digest_without_starting_job(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )

        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.submit(plan_id=prepared.plan_id, notice_sha256="0" * 64)

        assert captured.value.detail.code.value == "NOTICE_STALE"
        assert not tuple(manager.config.state_root.rglob("detailed.csv"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_status_lists_date_with_one_complete_model_resource_pair(tmp_path: Path) -> None:
    config = _config(tmp_path)
    resources = config.checkout / "resources" / "202502"
    (resources / "weights_frag.pt").unlink()
    (resources / "ko_config_frag.json").unlink()
    manager = DeepKoalaJobManager(config)
    await manager.open()
    try:
        status = await manager.status()

        assert status.available_model_dates == ("202502",)
        assert status.weights_available is True
        assert status.ready is True
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_cancelled_prepare_removes_private_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_probe(*_args: object, **_kwargs: object) -> None:
        started.set()
        await never.wait()

    monkeypatch.setattr(jobs_module, "probe_deepkoala_installation", blocking_probe)
    await manager.open()
    try:
        task = asyncio.create_task(
            manager.prepare(PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n"))
        )
        await started.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert not tuple(manager.config.state_root.rglob("input.fasta"))
        assert not tuple(manager.config.state_root.rglob("job_*"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_close_cancels_inflight_prepare_and_removes_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_probe(*_args: object, **_kwargs: object) -> None:
        started.set()
        await never.wait()

    monkeypatch.setattr(jobs_module, "probe_deepkoala_installation", blocking_probe)
    await manager.open()
    prepare_task = asyncio.create_task(
        manager.prepare(PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n"))
    )
    await started.wait()

    await asyncio.wait_for(manager.close(), timeout=2)

    with pytest.raises(asyncio.CancelledError):
        await prepare_task
    assert not tuple(manager.config.state_root.rglob("session_*"))
    assert not tuple(manager.config.state_root.rglob("job_*"))


@pytest.mark.asyncio
async def test_close_rejects_prepare_that_suppresses_probe_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    manager = DeepKoalaJobManager(config)
    installation = DeepKoalaInstallation(
        python_executable=config.python_executable,
        checkout=config.checkout,
    )
    probe_result = await jobs_module.probe_deepkoala_installation(
        installation,
        model="full",
        date="latest",
        device="cpu",
        weight_source=config.weight_source,
    )
    started = asyncio.Event()
    never = asyncio.Event()

    async def cancellation_resistant_probe(*_args: object, **_kwargs: object) -> object:
        started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            return probe_result
        return probe_result

    monkeypatch.setattr(
        jobs_module,
        "probe_deepkoala_installation",
        cancellation_resistant_probe,
    )
    await manager.open()
    prepare_task = asyncio.create_task(
        manager.prepare(PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu"))
    )
    await started.wait()

    await asyncio.wait_for(manager.close(), timeout=2)

    with pytest.raises(DeepKoalaMcpError) as captured:
        await prepare_task
    assert captured.value.detail.code.value == "INTERNAL_ERROR"
    assert not tuple(config.state_root.rglob("session_*"))


@pytest.mark.asyncio
async def test_concurrent_prepare_reservations_enforce_session_capacity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jobs_module, "_MAX_SESSION_RECORDS", 2)
    manager = DeepKoalaJobManager(_config(tmp_path))
    started = asyncio.Event()
    never = asyncio.Event()

    async def blocking_probe(*_args: object, **_kwargs: object) -> None:
        started.set()
        await never.wait()

    monkeypatch.setattr(jobs_module, "probe_deepkoala_installation", blocking_probe)
    await manager.open()
    first = asyncio.create_task(
        manager.prepare(PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n"))
    )
    await started.wait()
    second = asyncio.create_task(
        manager.prepare(PrepareDeepKoalaInput(fasta_text=">seq2\nMKTAYIAK\n"))
    )
    for _ in range(100):
        if len(tuple(manager.config.state_root.rglob("job_*"))) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(tuple(manager.config.state_root.rglob("job_*"))) == 2

    with pytest.raises(DeepKoalaMcpError) as captured:
        await manager.prepare(PrepareDeepKoalaInput(fasta_text=">seq3\nMKTAYIAK\n"))
    assert captured.value.detail.code.value == "QUEUE_FULL"
    assert len(tuple(manager.config.state_root.rglob("job_*"))) <= 2

    await asyncio.wait_for(manager.close(), timeout=2)
    await asyncio.gather(first, second, return_exceptions=True)
    assert not tuple(manager.config.state_root.rglob("session_*"))


@pytest.mark.asyncio
async def test_prepare_rejects_a_full_execution_queue_without_staging(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path, max_queue_size=1))
    await manager.open()
    try:
        running_plan = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">SLOW_QUEUE\nMKTAYIAK\n", device="cpu")
        )
        queued_plan = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">queued\nMKTAYIAK\n", device="cpu")
        )
        running = await manager.submit(
            plan_id=running_plan.plan_id,
            notice_sha256=running_plan.notice_sha256,
        )
        queued = await manager.submit(
            plan_id=queued_plan.plan_id,
            notice_sha256=queued_plan.notice_sha256,
        )
        assert running.job.state is JobState.RUNNING
        assert queued.job.state is JobState.QUEUED
        before = tuple(manager.config.state_root.rglob("job_*"))

        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.prepare(
                PrepareDeepKoalaInput(fasta_text=">rejected\nMKTAYIAK\n", device="cpu")
            )

        assert captured.value.detail.code.value == "QUEUE_FULL"
        assert tuple(manager.config.state_root.rglob("job_*")) == before
        await manager.cancel(queued.job.job_id)
        await manager.cancel(running.job.job_id)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_prepare_cleans_staging_after_unexpected_notice_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))

    def fail_notice(*_args: object, **_kwargs: object) -> None:
        raise LookupError("synthetic notice failure")

    monkeypatch.setattr(jobs_module, "_notice", fail_notice)
    await manager.open()
    try:
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.prepare(
                PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
            )

        assert captured.value.detail.code.value == "INTERNAL_ERROR"
        assert not tuple(manager.config.state_root.rglob("job_*"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_submit_rejects_source_changed_after_notice(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        (manager.config.checkout / "deepkoala" / "cli.py").write_text(
            "raise RuntimeError('changed')\n",
            encoding="utf-8",
        )

        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.submit(
                plan_id=prepared.plan_id,
                notice_sha256=prepared.notice_sha256,
            )

        assert captured.value.detail.code.value == "NOTICE_STALE"
        assert not tuple(manager.config.state_root.rglob("detailed.csv"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_submit_rejects_staged_fasta_changed_after_notice(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        staged = next(manager.config.state_root.rglob("input.fasta"))
        staged.write_text(">seq1\nMPEPTIDE\n", encoding="ascii")

        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.submit(
                plan_id=prepared.plan_id,
                notice_sha256=prepared.notice_sha256,
            )

        assert captured.value.detail.code.value == "NOTICE_STALE"
        assert not tuple(manager.config.state_root.rglob("detailed.csv"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_queued_launch_rechecks_staged_fasta(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        submitted = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )
        staged = next(manager.config.state_root.rglob(f"{submitted.job.job_id}/input.fasta"))
        staged.write_text(">seq1\nMPEPTIDE\n", encoding="ascii")

        assert await _wait_terminal(manager, submitted.job.job_id) is JobState.FAILED
        result = await manager.get_job(submitted.job.job_id)
        assert result.handoff is None
        assert result.job.failure_reason == "The staged FASTA changed after confirmation."
    finally:
        await manager.close()


class _MutatingSuccessfulRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        plan.output_path.write_text(
            "name,predict_label,probability,threshold,annotate\nseq1,K00001,0.9,0.5,*\n",
            encoding="utf-8",
        )
        (plan.checkout / "deepkoala" / "cli.py").write_text(
            "raise RuntimeError('changed during run')\n",
            encoding="utf-8",
        )
        return ProcessOutcome(return_code=0, diagnostic_text="", diagnostics_truncated=False)


class _InputMutatingSuccessfulRunner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        plan.output_path.write_text(
            "name,predict_label,probability,threshold,annotate\nseq1,K00001,0.9,0.5,*\n",
            encoding="utf-8",
        )
        plan.input_path.write_text(">seq1\nMPEPTIDE\n", encoding="ascii")
        return ProcessOutcome(return_code=0, diagnostic_text="", diagnostics_truncated=False)


class _UnexpectedThenSuccessfulRunner:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        self.calls += 1
        if self.calls == 1:
            raise LookupError("synthetic unexpected runner failure")
        plan.output_path.write_text(
            "name,predict_label,probability,threshold,annotate\nseq2,K00001,0.9,0.5,*\n",
            encoding="utf-8",
        )
        return ProcessOutcome(return_code=0, diagnostic_text="", diagnostics_truncated=False)


@pytest.mark.asyncio
async def test_post_run_identity_change_cannot_produce_handoff(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(
        _config(tmp_path),
        runner=_MutatingSuccessfulRunner(),  # type: ignore[arg-type]
    )
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        submitted = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )

        assert await _wait_terminal(manager, submitted.job.job_id) is JobState.FAILED
        result = await manager.get_job(submitted.job.job_id)
        assert result.handoff is None
        assert result.job.result_uri is None
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_post_run_input_change_cannot_produce_handoff(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(
        _config(tmp_path),
        runner=_InputMutatingSuccessfulRunner(),  # type: ignore[arg-type]
    )
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        submitted = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )

        assert await _wait_terminal(manager, submitted.job.job_id) is JobState.FAILED
        result = await manager.get_job(submitted.job.job_id)
        assert result.handoff is None
        assert result.job.result_uri is None
        assert result.job.failure_reason == "The staged FASTA changed after confirmation."
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_unexpected_job_exception_fails_safely_and_releases_runner_slot(
    tmp_path: Path,
) -> None:
    runner = _UnexpectedThenSuccessfulRunner()
    manager = DeepKoalaJobManager(
        _config(tmp_path),
        runner=runner,  # type: ignore[arg-type]
    )
    await manager.open()
    try:
        first_plan = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        second_plan = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq2\nMKTAYIAK\n", device="cpu")
        )
        first = await manager.submit(
            plan_id=first_plan.plan_id,
            notice_sha256=first_plan.notice_sha256,
        )
        second = await manager.submit(
            plan_id=second_plan.plan_id,
            notice_sha256=second_plan.notice_sha256,
        )

        assert await _wait_terminal(manager, first.job.job_id) is JobState.FAILED
        assert await _wait_terminal(manager, second.job.job_id) is JobState.SUCCEEDED
        first_result = await manager.get_job(first.job.job_id)
        assert first_result.job.failure_reason == (
            "DeepKOALA encountered an unexpected internal execution failure."
        )
        assert first_result.handoff is None
        status = await manager.status()
        assert status.queue.running_jobs == 0
        assert status.queue.queued_jobs == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_invalid_detailed_output_and_nonzero_exit_fail_safely(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        for sequence_id in ("SIMPLE", "FAIL"):
            prepared = await manager.prepare(
                PrepareDeepKoalaInput(
                    fasta_text=f">{sequence_id}\nMKTAYIAK\n",
                    device="cpu",
                )
            )
            submitted = await manager.submit(
                plan_id=prepared.plan_id,
                notice_sha256=prepared.notice_sha256,
            )
            assert await _wait_terminal(manager, submitted.job.job_id) is JobState.FAILED
            result = await manager.get_job(submitted.job.job_id)
            assert result.handoff is None
            assert result.job.result_uri is None
            assert result.job.failure_reason is not None
            assert str(tmp_path) not in result.job.failure_reason
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_running_and_queued_jobs_cancel_without_orphaning_queue(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path, max_queue_size=1))
    await manager.open()
    try:
        slow = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">SLOW1\nMKTAYIAK\n", device="cpu")
        )
        queued = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq2\nMKTAYIAK\n", device="cpu")
        )
        slow_job = await manager.submit(plan_id=slow.plan_id, notice_sha256=slow.notice_sha256)
        queued_job = await manager.submit(
            plan_id=queued.plan_id, notice_sha256=queued.notice_sha256
        )
        assert slow_job.job.state is JobState.RUNNING
        assert queued_job.job.state is JobState.QUEUED

        cancelled_queue = await manager.cancel(queued_job.job.job_id)
        cancelled_running = await manager.cancel(slow_job.job.job_id)

        assert cancelled_queue.job.state is JobState.CANCELLED
        assert cancelled_queue.job.started_at is None
        assert cancelled_running.job.state is JobState.CANCELLED
        assert cancelled_running.job.diagnostic_uri is None
        assert cancelled_running.job.diagnostics_truncated is False
        status = await manager.status()
        assert status.queue.running_jobs == 0
        assert status.queue.queued_jobs == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_timeout_is_distinct_from_failure_and_cleans_input(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(
                fasta_text=">SLOW_TIMEOUT\nMKTAYIAK\n",
                device="cpu",
                timeout_seconds=1,
            )
        )
        submitted = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )

        assert await _wait_terminal(manager, submitted.job.job_id) is JobState.TIMED_OUT
        result = await manager.get_job(submitted.job.job_id)
        assert result.job.failure_reason is not None
        assert "timeout" in result.job.failure_reason
        assert not tuple(manager.config.state_root.rglob("input.fasta"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_direct_resource_read_rejects_expired_result(tmp_path: Path) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path, retention_seconds=1))
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        submitted = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )
        assert await _wait_terminal(manager, submitted.job.job_id) is JobState.SUCCEEDED
        await asyncio.sleep(1.05)

        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.read_artifact(
                submitted.job.job_id,
                "output",
                offset=0,
                limit=1_024,
            )

        assert captured.value.detail.code.value == "JOB_NOT_FOUND"
        assert not tuple(manager.config.state_root.rglob("detailed.csv"))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_delete_failure_is_retryable_and_never_reports_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = DeepKoalaJobManager(_config(tmp_path))
    await manager.open()
    try:
        prepared = await manager.prepare(
            PrepareDeepKoalaInput(fasta_text=">seq1\nMKTAYIAK\n", device="cpu")
        )
        submitted = await manager.submit(
            plan_id=prepared.plan_id,
            notice_sha256=prepared.notice_sha256,
        )
        assert await _wait_terminal(manager, submitted.job.job_id) is JobState.SUCCEEDED
        real_cleanup = jobs_module.cleanup_job_directory

        def fail_cleanup(_directory: Path) -> None:
            raise OSError("synthetic cleanup failure")

        monkeypatch.setattr(jobs_module, "cleanup_job_directory", fail_cleanup)
        with pytest.raises(DeepKoalaMcpError) as captured:
            await manager.delete(submitted.job.job_id)
        assert captured.value.detail.code.value == "INTERNAL_ERROR"
        retained = await manager.get_job(submitted.job.job_id)
        assert retained.job.cleanup_pending is True

        monkeypatch.setattr(jobs_module, "cleanup_job_directory", real_cleanup)
        deleted = await manager.delete(submitted.job.job_id)
        assert deleted.deleted is True
    finally:
        await manager.close()
