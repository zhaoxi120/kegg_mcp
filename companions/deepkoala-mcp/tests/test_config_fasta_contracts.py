"""Configuration, public schema, and FASTA boundary tests."""

from __future__ import annotations

import stat
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from deepkoala_mcp.config import (
    ALLOWED_DEVICES_ENV,
    ALLOWED_MODELS_ENV,
    CHECKOUT_ENV,
    CPU_THREADS_ENV,
    INPUT_ROOTS_ENV,
    MAX_TIMEOUT_SECONDS_ENV,
    OUTPUT_ROOTS_ENV,
    PYTHON_ENV,
    STATE_ROOT_ENV,
    DeepKoalaRuntimeConfig,
    load_runtime_config,
)
from deepkoala_mcp.contracts import (
    ImportHandoff,
    RunDeepKoalaInput,
    SourceProvenance,
)
from deepkoala_mcp.fasta import (
    FastaLimitError,
    FastaValidationError,
    InputPathError,
    stage_fasta,
    validate_fasta_bytes,
)


def _environment(tmp_path: Path, checkout: Path) -> dict[str, str]:
    inputs = tmp_path / "inputs"
    outputs = tmp_path / "outputs"
    inputs.mkdir()
    outputs.mkdir()
    return {
        CHECKOUT_ENV: str(checkout),
        PYTHON_ENV: str(Path(sys.executable).resolve()),
        STATE_ROOT_ENV: str(tmp_path / "state"),
        INPUT_ROOTS_ENV: str(inputs),
        OUTPUT_ROOTS_ENV: str(outputs),
    }


def test_load_runtime_config_has_bounded_single_runner_defaults(
    tmp_path: Path,
    checkout: Path,
) -> None:
    config = load_runtime_config(_environment(tmp_path, checkout))
    assert config.allowed_models == ("full", "frag")
    assert config.allowed_devices == ("auto",)
    assert config.max_concurrent_jobs == 1
    assert config.allow_multi is False
    assert config.max_timeout_seconds == 3_600


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (CPU_THREADS_ENV, "0"),
        (CPU_THREADS_ENV, "5"),
        (MAX_TIMEOUT_SECONDS_ENV, "0"),
        (MAX_TIMEOUT_SECONDS_ENV, "1.5"),
        (ALLOWED_MODELS_ENV, "full,other"),
        (ALLOWED_DEVICES_ENV, "cuda"),
    ],
)
def test_load_runtime_config_rejects_out_of_policy_values(
    tmp_path: Path,
    checkout: Path,
    name: str,
    value: str,
) -> None:
    environment = _environment(tmp_path, checkout)
    environment[name] = value
    with pytest.raises(ValueError, match=name):
        load_runtime_config(environment)


def test_load_runtime_config_requires_both_shared_root_sets(
    tmp_path: Path,
    checkout: Path,
) -> None:
    environment = _environment(tmp_path, checkout)
    environment.pop(OUTPUT_ROOTS_ENV)
    with pytest.raises(ValueError, match=OUTPUT_ROOTS_ENV):
        load_runtime_config(environment)


def test_runtime_config_rejects_state_overlap(
    tmp_path: Path,
    checkout: Path,
) -> None:
    root = tmp_path / "private"
    output = tmp_path / "output"
    root.mkdir()
    output.mkdir()
    with pytest.raises(ValidationError, match="overlap"):
        DeepKoalaRuntimeConfig(
            checkout=checkout,
            python_executable=Path(sys.executable).resolve(),
            state_root=root / "state",
            input_roots=(root,),
            output_roots=(output,),
        )


def test_run_contract_requires_absolute_paths_and_service_owned_device() -> None:
    request = RunDeepKoalaInput(
        fasta_path="/allowed/proteins.faa",
        output_directory="/allowed/run-1",
    )
    assert request.device == "auto"
    with pytest.raises(ValidationError):
        RunDeepKoalaInput(
            fasta_path="proteins.faa",
            output_directory="/allowed/run-1",
        )
    with pytest.raises(ValidationError):
        RunDeepKoalaInput.model_validate(
            {
                "fasta_path": "/allowed/proteins.faa",
                "output_directory": "/allowed/run-1",
                "device": "cuda",
            },
            strict=True,
        )


def test_handoff_rejects_version_and_path_mismatch_and_round_trips_timezones() -> None:
    job_id = "job_" + "a" * 32
    offset = timezone(timedelta(hours=5, minutes=30))
    source = SourceProvenance(
        source_version="0.1-test",
        model_name="full",
        model_version="202502",
        annotation_date=datetime(2026, 7, 16, 12, 30, tzinfo=offset),
        input_path="/allowed/original.faa",
    )
    handoff = ImportHandoff(
        schema_version="1",
        tool_version="0.3.0",
        input_path="/allowed/original.faa",
        annotations_path="/outputs/run/deepkoala_annotations.csv",
        report_path="/outputs/run/deepkoala_run_report.md",
        input_format="deepkoala_detailed",
        annotations_resource_uri=f"deepkoala://jobs/{job_id}/annotations",
        report_resource_uri=f"deepkoala://jobs/{job_id}/report",
        source=source,
    )
    assert "+05:30" in handoff.model_dump_json()
    payload = handoff.model_dump(mode="python")
    del payload["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        ImportHandoff.model_validate(payload, strict=True)
    payload = handoff.model_dump(mode="python")
    payload["schema_version"] = "2"
    with pytest.raises(ValidationError, match="schema_version"):
        ImportHandoff.model_validate(payload, strict=True)
    payload = handoff.model_dump(mode="python")
    del payload["input_format"]
    with pytest.raises(ValidationError, match="input_format"):
        ImportHandoff.model_validate(payload, strict=True)
    payload = handoff.model_dump(mode="python")
    payload["input_path"] = "/allowed/different.faa"
    with pytest.raises(ValidationError, match="must match"):
        ImportHandoff.model_validate(payload, strict=True)


def test_utc_timestamp_serializes_with_z() -> None:
    source = SourceProvenance(
        source_version="0.1-test",
        model_name="frag",
        model_version="202401",
        annotation_date=datetime(2026, 7, 16, tzinfo=UTC),
        input_path="/allowed/input.faa",
    )
    assert '"annotation_date":"2026-07-16T00:00:00Z"' in source.model_dump_json()


def test_validate_fasta_normalizes_and_honors_deployment_limits() -> None:
    summary, canonical = validate_fasta_bytes(
        b">p1 note\r\nmpep\r\n>p2\rW\r",
        max_bytes=100,
        max_sequences=2,
    )
    assert canonical == b">p1 note\nMPEP\n>p2\nW\n"
    assert summary.sequence_count == 2
    with pytest.raises(FastaLimitError):
        validate_fasta_bytes(b">p1\nM\n>p2\nW\n", max_sequences=1)
    with pytest.raises(FastaLimitError):
        validate_fasta_bytes(b">p\n" + b"M" * 20 + b"\n", max_bytes=10)


@pytest.mark.parametrize(
    "content",
    [b"", b"MPEPTIDE\n", b">p\n", b">p\nM  P\n", b">p\nM?P\n", b">p\nM\n>p\nW\n"],
)
def test_validate_fasta_rejects_invalid_documents(content: bytes) -> None:
    with pytest.raises(FastaValidationError):
        validate_fasta_bytes(content)


def test_stage_path_is_allowlisted_and_owner_only(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    job = tmp_path / "job"
    allowed.mkdir()
    job.mkdir(mode=0o700)
    source = allowed / "proteins.faa"
    source.write_text(">p\nMPEPTIDE\n", encoding="ascii")
    staged_result = stage_fasta(
        fasta_path=str(source),
        input_roots=(allowed.resolve(),),
        job_directory=job,
        max_bytes=1_000,
        max_sequences=10,
    )
    staged = job / "input.fasta"
    assert staged_result.input_path == source.resolve()
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600


def test_stage_path_rejects_escape_and_symlink(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    job = tmp_path / "job"
    allowed.mkdir()
    outside.mkdir()
    job.mkdir()
    source = outside / "private.faa"
    source.write_text(">private\nM\n", encoding="ascii")
    link = allowed / "link.faa"
    link.symlink_to(source)
    for path in (source, link):
        with pytest.raises(InputPathError):
            stage_fasta(
                fasta_path=str(path),
                input_roots=(allowed.resolve(),),
                job_directory=job,
                max_bytes=1_000,
                max_sequences=10,
            )
