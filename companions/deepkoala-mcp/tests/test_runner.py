"""Subprocess, diagnostics, and detailed-output safety tests."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from deepkoala_mcp.diagnostics import SanitizedDiagnosticTail
from deepkoala_mcp.output import OutputValidationError, read_artifact_range, validate_detailed_csv
from deepkoala_mcp.runner import (
    DeepKoalaProcessRunner,
    RunnerPlan,
    RunnerTimedOutError,
    build_argv,
    build_child_environment,
)


def _plan(checkout: Path, job: Path, **overrides: object) -> RunnerPlan:
    values: dict[str, object] = {
        "python_executable": Path(sys.executable).resolve(),
        "checkout": checkout,
        "job_directory": job,
        "model": "full",
        "resolved_date": "202502",
        "resolved_device": "cpu",
        "batch_size_override": None,
        "num_workers_override": None,
        "topk_override": None,
        "timeout_seconds": 10,
        "cpu_threads": 2,
        "diagnostic_max_bytes": 2_048,
        "max_output_bytes": 5_000_000,
    }
    values.update(overrides)
    return RunnerPlan(**values)  # pyright: ignore[reportArgumentType]


def _fake_checkout(tmp_path: Path, body: str) -> Path:
    checkout = tmp_path / "checkout"
    package = checkout / "deepkoala"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(body, encoding="utf-8")
    return checkout


def test_default_argv_preserves_upstream_defaults_and_forces_detail(tmp_path: Path) -> None:
    plan = _plan(tmp_path / "checkout", tmp_path / "job")

    argv = build_argv(plan)

    assert argv[0] == str(Path(sys.executable).resolve())
    assert argv[1] == "-c"
    assert "resource.setrlimit(resource.RLIMIT_FSIZE" in argv[2]
    assert 'runpy.run_module("deepkoala.cli"' in argv[2]
    assert str(plan.input_path) not in argv[2]
    assert str(plan.output_path) not in argv[2]
    assert argv[3] == "5000000"
    assert "--detail" in argv
    assert "--multi" not in argv
    assert "--batch_size" not in argv
    assert "--num_workers" not in argv
    assert "--topk" not in argv
    assert argv[argv.index("--device") + 1] == "cpu"


def test_explicit_inference_overrides_are_bounded_argv_values(tmp_path: Path) -> None:
    plan = _plan(
        tmp_path / "checkout",
        tmp_path / "job",
        batch_size_override=8,
        num_workers_override=0,
        topk_override=3,
    )

    argv = build_argv(plan)

    assert argv[-6:] == ("--batch_size", "8", "--num_workers", "0", "--topk", "3")


def test_child_environment_is_allowlisted_and_thread_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KEGG_MCP_LICENSED_ENDPOINT", "secret-value")
    monkeypatch.setenv("SOME_API_TOKEN", "secret-token")
    monkeypatch.setenv("HIP_VISIBLE_DEVICES", "0")
    monkeypatch.setenv("ROCR_VISIBLE_DEVICES", "1")
    plan = _plan(tmp_path / "checkout", tmp_path / "job")

    environment = build_child_environment(plan)

    assert "KEGG_MCP_LICENSED_ENDPOINT" not in environment
    assert "SOME_API_TOKEN" not in environment
    assert environment["HIP_VISIBLE_DEVICES"] == "0"
    assert environment["ROCR_VISIBLE_DEVICES"] == "1"
    assert environment["PYTHONPATH"] == str(plan.checkout)
    assert environment["OMP_NUM_THREADS"] == "2"
    assert environment["MKL_NUM_THREADS"] == "2"


def test_diagnostic_tail_omits_sequences_paths_and_secrets(tmp_path: Path) -> None:
    sink = SanitizedDiagnosticTail(max_bytes=256, redacted_paths=(tmp_path,))
    sink.feed(f"reading {tmp_path}/input.fasta\n".encode())
    sink.feed(b">private-header\n")
    sink.feed(b"MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQLR\n")
    sink.feed(b"MPEPTIDE\n")
    sink.feed(b"token=super-secret\n")
    sink.feed(b"Authorization: Bearer abc.def.ghi\n")
    sink.feed(b'{"api_key": "json-secret"}\n')
    sink.feed(b"sequence=MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQLR\n")
    sink.finish()

    result = sink.text()

    assert str(tmp_path) not in result
    assert "private-header" not in result
    assert "MKTAYIA" not in result
    assert "MPEPTIDE" not in result
    assert "super-secret" not in result
    assert "abc.def.ghi" not in result
    assert "json-secret" not in result
    assert "sequence-like diagnostic omitted" in result


@pytest.mark.asyncio
async def test_runner_captures_diagnostics_without_polluting_output(tmp_path: Path) -> None:
    checkout = _fake_checkout(
        tmp_path,
        """
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--input_path')
p.add_argument('--output_path')
p.add_argument('--model')
p.add_argument('--date')
p.add_argument('--device')
p.add_argument('--detail', action='store_true')
args = p.parse_args()
Path(args.output_path).write_text(
    'name,predict_label,probability,threshold,annotate\\nseq1,K00001,0.9,0.5,*\\n',
    encoding='utf-8',
)
print('Processed 1 sequences, annotated 1.')
""",
    )
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    (job / "input.fasta").write_text(">seq1\nMKTAYIAK\n", encoding="utf-8")

    outcome = await DeepKoalaProcessRunner().run(_plan(checkout, job))

    assert outcome.return_code == 0
    assert "Processed 1 sequences" in outcome.diagnostic_text
    summary = validate_detailed_csv(job / "deepkoala-output.csv", max_rows=1)
    assert summary.row_count == 1


@pytest.mark.asyncio
async def test_combined_diagnostics_respect_the_exact_byte_limit(tmp_path: Path) -> None:
    checkout = _fake_checkout(
        tmp_path,
        "print('bounded stdout'); import sys; print('bounded stderr', file=sys.stderr)\n",
    )
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    (job / "input.fasta").write_text(">seq1\nMKTAYIAK\n", encoding="utf-8")

    outcome = await DeepKoalaProcessRunner().run(_plan(checkout, job, diagnostic_max_bytes=8))

    assert len(outcome.diagnostic_text.encode("utf-8")) <= 8
    assert outcome.diagnostics_truncated is True


@pytest.mark.asyncio
async def test_runner_timeout_reaps_process_and_returns_safe_diagnostics(tmp_path: Path) -> None:
    checkout = _fake_checkout(
        tmp_path,
        """
import time
print('starting slow runner', flush=True)
time.sleep(30)
""",
    )
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    (job / "input.fasta").write_text(">seq1\nMKTAYIAK\n", encoding="utf-8")

    with pytest.raises(RunnerTimedOutError) as error:
        await DeepKoalaProcessRunner().run(_plan(checkout, job, timeout_seconds=1))

    assert "starting slow runner" in error.value.diagnostic_text


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are unavailable")
@pytest.mark.asyncio
async def test_runner_reaps_inherited_pipe_descendant_after_leader_exit(
    tmp_path: Path,
) -> None:
    checkout = _fake_checkout(
        tmp_path,
        """
import argparse
import subprocess
import sys
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--input_path')
p.add_argument('--output_path')
p.add_argument('--model')
p.add_argument('--date')
p.add_argument('--device')
p.add_argument('--detail', action='store_true')
args = p.parse_args()
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])
Path(args.output_path).with_name('descendant.pid').write_text(str(child.pid), encoding='ascii')
Path(args.output_path).write_text(
    'name,predict_label,probability,threshold,annotate\\nseq1,K00001,0.9,0.5,*\\n',
    encoding='utf-8',
)
""",
    )
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    (job / "input.fasta").write_text(">seq1\nMKTAYIAK\n", encoding="utf-8")

    async with asyncio.timeout(2):
        outcome = await DeepKoalaProcessRunner().run(_plan(checkout, job, timeout_seconds=1))

    descendant_pid = int((job / "descendant.pid").read_text(encoding="ascii"))
    assert outcome.return_code == 0
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.skipif(os.name != "posix", reason="POSIX file-size limits are unavailable")
@pytest.mark.asyncio
async def test_runner_enforces_output_file_hard_limit_before_validation(tmp_path: Path) -> None:
    checkout = _fake_checkout(
        tmp_path,
        """
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--input_path')
p.add_argument('--output_path')
p.add_argument('--model')
p.add_argument('--date')
p.add_argument('--device')
p.add_argument('--detail', action='store_true')
args = p.parse_args()
Path(args.output_path).write_bytes(b'X' * 1_000_000)
""",
    )
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    (job / "input.fasta").write_text(">seq1\nMKTAYIAK\n", encoding="utf-8")
    maximum_output_bytes = 4_096

    async with asyncio.timeout(3):
        outcome = await DeepKoalaProcessRunner().run(
            _plan(
                checkout,
                job,
                timeout_seconds=2,
                max_output_bytes=maximum_output_bytes,
            )
        )

    assert outcome.return_code != 0
    assert (job / "deepkoala-output.csv").stat().st_size <= maximum_output_bytes


def test_detailed_output_rejects_simple_or_oversized_content(tmp_path: Path) -> None:
    simple = tmp_path / "simple.csv"
    simple.write_text("name,predict_label\nseq1,K00001\n", encoding="utf-8")
    oversized = tmp_path / "oversized.csv"
    oversized.write_bytes(b"x" * 101)

    with pytest.raises(OutputValidationError) as simple_error:
        validate_detailed_csv(simple, max_rows=1)
    with pytest.raises(OutputValidationError) as size_error:
        validate_detailed_csv(oversized, max_rows=1, max_bytes=100)

    assert simple_error.value.code == "OUTPUT_INVALID"
    assert size_error.value.code == "OUTPUT_LIMIT_EXCEEDED"


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink checks are unavailable")
def test_detailed_output_rejects_symlink_and_non_regular_files(tmp_path: Path) -> None:
    target = tmp_path / "target.csv"
    target.write_text(
        "name,predict_label,probability,threshold,annotate\nseq1,K00001,0.9,0.5,*\n",
        encoding="utf-8",
    )
    link = tmp_path / "output.csv"
    link.symlink_to(target)
    fifo = tmp_path / "output.fifo"
    os.mkfifo(fifo)

    with pytest.raises(OutputValidationError):
        validate_detailed_csv(link, max_rows=1)
    with pytest.raises(OutputValidationError):
        validate_detailed_csv(fifo, max_rows=1)


def test_artifact_ranges_are_bounded_and_deterministic(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_bytes(b"abcdefghij")
    artifact.chmod(stat.S_IRUSR | stat.S_IWUSR)

    first, total, continuation = read_artifact_range(artifact, offset=0, limit=4)
    second, _, done = read_artifact_range(artifact, offset=continuation or 0, limit=10)

    assert first == b"abcd"
    assert total == 10
    assert continuation == 4
    assert second == b"efghij"
    assert done is None
