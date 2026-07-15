"""Focused tests for companion configuration and boundary contracts."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

import deepkoala_mcp.config as config_module
from deepkoala_mcp.config import (
    ALLOWED_ROOTS_ENV,
    CHECKOUT_ENV,
    CPU_THREADS_ENV,
    DEFAULT_TIMEOUT_SECONDS_ENV,
    MAX_CONCURRENT_JOBS_ENV,
    MAX_DIAGNOSTIC_BYTES_ENV,
    MAX_HEADER_LENGTH_ENV,
    MAX_INPUT_BYTES_ENV,
    MAX_OUTPUT_BYTES_ENV,
    MAX_QUEUE_SIZE_ENV,
    MAX_RESIDUES_ENV,
    MAX_SEQUENCE_LENGTH_ENV,
    MAX_SEQUENCES_ENV,
    PLAN_TTL_SECONDS_ENV,
    PYTHON_ENV,
    RETENTION_SECONDS_ENV,
    STATE_ROOT_ENV,
    WEIGHT_SOURCE_ENV,
    DeepKoalaRuntimeConfig,
    load_runtime_config,
)
from deepkoala_mcp.contracts import (
    CancelDeepKoalaJobInput,
    CompanionDefaults,
    CompanionLimits,
    CompanionStatus,
    ErrorCode,
    ErrorDetail,
    GetDeepKoalaStatusInput,
    JobState,
    JobSummary,
    PrepareDeepKoalaInput,
    QueueSnapshot,
    SafeDetail,
    SubmitDeepKoalaInput,
    ToolEnvelope,
    ToolPayload,
)
from deepkoala_mcp.filesystem import FilesystemSecurityError, prepare_state_root


def _base_environment(tmp_path: Path) -> dict[str, str]:
    checkout = tmp_path / "deepkoala"
    checkout.mkdir()
    return {
        CHECKOUT_ENV: str(checkout),
        PYTHON_ENV: sys.executable,
        STATE_ROOT_ENV: str(tmp_path / "state"),
    }


def test_load_runtime_config_uses_bounded_defaults(tmp_path: Path) -> None:
    environment = _base_environment(tmp_path)

    config = load_runtime_config(environment)

    assert config.checkout == (tmp_path / "deepkoala").resolve()
    assert config.python_executable.is_absolute()
    assert config.state_root == (tmp_path / "state").resolve()
    assert config.allowed_roots == ()
    assert config.weight_source == "github_bundled"
    assert config.max_concurrent_jobs == 1
    assert config.max_queue_size == 32
    assert config.cpu_threads == 2
    assert config.default_timeout_seconds == 3_600
    assert config.plan_ttl_seconds == 600
    assert config.retention_seconds == 86_400
    assert config.diagnostic_max_bytes == 65_536
    assert config.max_input_bytes == 5_000_000
    assert config.max_output_bytes == 5_000_000
    assert config.max_sequences == 100_000
    assert config.max_residues == 5_000_000
    assert config.max_sequence_length == 100_000
    assert config.max_header_length == 1_024


def test_load_runtime_config_rejects_non_posix_before_state_handling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _base_environment(tmp_path)
    state_root = Path(environment[STATE_ROOT_ENV])
    monkeypatch.setattr(config_module, "_supports_required_posix_runtime", lambda: False)

    with pytest.raises(
        ValueError,
        match="POSIX runtime with process-group and file-size-limit support",
    ):
        load_runtime_config(environment)

    assert not state_root.exists()


def test_load_runtime_config_accepts_all_documented_overrides(tmp_path: Path) -> None:
    environment = _base_environment(tmp_path)
    first_root = tmp_path / "inputs-a"
    second_root = tmp_path / "inputs-b"
    first_root.mkdir()
    second_root.mkdir()
    environment.update(
        {
            ALLOWED_ROOTS_ENV: f"{first_root}:{second_root}",
            MAX_CONCURRENT_JOBS_ENV: "1",
            MAX_QUEUE_SIZE_ENV: "7",
            CPU_THREADS_ENV: "3",
            DEFAULT_TIMEOUT_SECONDS_ENV: "90",
            PLAN_TTL_SECONDS_ENV: "30",
            RETENTION_SECONDS_ENV: "120",
            MAX_DIAGNOSTIC_BYTES_ENV: "4096",
            MAX_INPUT_BYTES_ENV: "10000",
            MAX_OUTPUT_BYTES_ENV: "20000",
            MAX_SEQUENCES_ENV: "100",
            MAX_RESIDUES_ENV: "9000",
            MAX_SEQUENCE_LENGTH_ENV: "8000",
            MAX_HEADER_LENGTH_ENV: "512",
            WEIGHT_SOURCE_ENV: "user_provided",
        }
    )

    config = load_runtime_config(environment)

    assert config.allowed_roots == (first_root.resolve(), second_root.resolve())
    assert config.max_queue_size == 7
    assert config.cpu_threads == 3
    assert config.default_timeout_seconds == 90
    assert config.plan_ttl_seconds == 30
    assert config.retention_seconds == 120
    assert config.diagnostic_max_bytes == 4096
    assert config.max_input_bytes == 10_000
    assert config.max_output_bytes == 20_000
    assert config.max_sequences == 100
    assert config.max_residues == 9_000
    assert config.max_sequence_length == 8_000
    assert config.max_header_length == 512
    assert config.weight_source == "user_provided"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (MAX_CONCURRENT_JOBS_ENV, "2"),
        (MAX_QUEUE_SIZE_ENV, "33"),
        (CPU_THREADS_ENV, "0"),
        (CPU_THREADS_ENV, "33"),
        (DEFAULT_TIMEOUT_SECONDS_ENV, "86401"),
        (PLAN_TTL_SECONDS_ENV, " 600"),
        (RETENTION_SECONDS_ENV, "-1"),
        (MAX_DIAGNOSTIC_BYTES_ENV, "65537"),
        (MAX_INPUT_BYTES_ENV, "5000001"),
        (MAX_OUTPUT_BYTES_ENV, "5000001"),
        (MAX_SEQUENCES_ENV, "100001"),
        (MAX_RESIDUES_ENV, "5000001"),
        (MAX_SEQUENCE_LENGTH_ENV, "100001"),
        (MAX_HEADER_LENGTH_ENV, "1025"),
    ],
)
def test_load_runtime_config_rejects_out_of_bounds_values(
    tmp_path: Path, name: str, value: str
) -> None:
    environment = _base_environment(tmp_path)
    environment[name] = value

    with pytest.raises(ValueError, match=name):
        load_runtime_config(environment)


def test_load_runtime_config_requires_an_absolute_existing_checkout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=f"{CHECKOUT_ENV} is required"):
        load_runtime_config({PYTHON_ENV: sys.executable})

    environment = _base_environment(tmp_path)
    environment[CHECKOUT_ENV] = "relative/checkout"
    with pytest.raises(ValueError, match="absolute directory"):
        load_runtime_config(environment)

    environment[CHECKOUT_ENV] = str(tmp_path / "missing")
    with pytest.raises(ValueError, match="existing directory"):
        load_runtime_config(environment)


@pytest.mark.parametrize("name", [PYTHON_ENV, STATE_ROOT_ENV])
@pytest.mark.parametrize("value", [None, ""])
def test_load_runtime_config_requires_explicit_runtime_paths(
    tmp_path: Path,
    name: str,
    value: str | None,
) -> None:
    environment = _base_environment(tmp_path)
    if value is None:
        environment.pop(name)
    else:
        environment[name] = value

    with pytest.raises(ValueError, match=f"{name} is required"):
        load_runtime_config(environment)


def test_load_runtime_config_rejects_unknown_weight_source(tmp_path: Path) -> None:
    environment = _base_environment(tmp_path)
    environment[WEIGHT_SOURCE_ENV] = "automatic_download"

    with pytest.raises(ValueError, match=WEIGHT_SOURCE_ENV):
        load_runtime_config(environment)


def test_python_configuration_requires_an_absolute_executable(tmp_path: Path) -> None:
    environment = _base_environment(tmp_path)
    executable = Path(sys.executable)
    environment[PYTHON_ENV] = executable.name
    environment["PATH"] = str(executable.parent)

    with pytest.raises(ValueError, match="absolute executable path"):
        load_runtime_config(environment)


def test_allowed_roots_reject_missing_duplicate_and_filesystem_root(tmp_path: Path) -> None:
    environment = _base_environment(tmp_path)
    environment[ALLOWED_ROOTS_ENV] = str(tmp_path / "missing")
    with pytest.raises(ValueError, match="existing directory"):
        load_runtime_config(environment)

    input_root = tmp_path / "inputs"
    input_root.mkdir()
    environment[ALLOWED_ROOTS_ENV] = f"{input_root}:{input_root}"
    with pytest.raises(ValueError, match="duplicate roots"):
        load_runtime_config(environment)

    environment[ALLOWED_ROOTS_ENV] = "/"
    with pytest.raises(ValueError, match="filesystem root"):
        load_runtime_config(environment)


def test_state_root_rejects_checkout_and_allowed_root_overlap(tmp_path: Path) -> None:
    checkout = tmp_path / "deepkoala"
    checkout.mkdir()
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    nested_input_root = tmp_path / "state" / "inputs"
    nested_input_root.mkdir(parents=True)

    with pytest.raises(ValidationError, match="outside the DeepKOALA checkout"):
        DeepKoalaRuntimeConfig(
            checkout=checkout,
            python_executable=Path(sys.executable).resolve(),
            state_root=tmp_path,
        )
    with pytest.raises(ValidationError, match="overlap state_root"):
        DeepKoalaRuntimeConfig(
            checkout=checkout,
            python_executable=Path(sys.executable).resolve(),
            state_root=tmp_path / "state",
            allowed_roots=(tmp_path,),
        )
    with pytest.raises(ValidationError, match="overlap state_root"):
        DeepKoalaRuntimeConfig(
            checkout=checkout,
            python_executable=Path(sys.executable).resolve(),
            state_root=tmp_path / "state",
            allowed_roots=(nested_input_root,),
        )


def test_existing_state_root_permissions_are_rejected_without_chmod(tmp_path: Path) -> None:
    state = tmp_path / "shared-state"
    state.mkdir(mode=0o755)
    state.chmod(0o755)

    with pytest.raises(FilesystemSecurityError, match="owner-only"):
        prepare_state_root(state)

    assert state.stat().st_mode & 0o777 == 0o755


def test_runtime_config_is_strict_frozen_and_forbids_extra_fields(tmp_path: Path) -> None:
    config = load_runtime_config(_base_environment(tmp_path))

    with pytest.raises(ValidationError, match="Instance is frozen"):
        config.cpu_threads = 4  # type: ignore[misc]
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DeepKoalaRuntimeConfig.model_validate(
            {**config.model_dump(), "environment": {"SECRET": "value"}}, strict=True
        )


def test_prepare_input_requires_exactly_one_bounded_fasta_source() -> None:
    inline = PrepareDeepKoalaInput(fasta_text=">protein\nMPEPTIDE\n")
    filesystem = PrepareDeepKoalaInput(fasta_path="/allowed/input.faa", device="cpu")

    assert inline.model == "full"
    assert inline.model_date == "latest"
    assert filesystem.device == "cpu"
    with pytest.raises(ValidationError, match="exactly one"):
        PrepareDeepKoalaInput()
    with pytest.raises(ValidationError, match="exactly one"):
        PrepareDeepKoalaInput(fasta_text=">a\nM\n", fasta_path="/allowed/a.faa")
    with pytest.raises(ValidationError, match="absolute"):
        PrepareDeepKoalaInput(fasta_path="relative.faa")
    with pytest.raises(ValidationError):
        PrepareDeepKoalaInput(fasta_text=">a\nM\n", multi=True)  # type: ignore[arg-type]


def test_submit_contract_requires_true_acknowledgement_and_scoped_ids() -> None:
    plan_id = "plan_" + "a" * 32
    digest = "b" * 64

    submitted = SubmitDeepKoalaInput(
        plan_id=plan_id,
        notice_sha256=digest,
        acknowledged=True,
    )

    assert submitted.plan_id == plan_id
    with pytest.raises(ValidationError):
        SubmitDeepKoalaInput(
            plan_id=plan_id,
            notice_sha256=digest,
            acknowledged=False,  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        CancelDeepKoalaJobInput(job_id="job_too-short")


def test_queued_job_can_be_cancelled_without_starting() -> None:
    now = datetime.now(UTC)

    job = JobSummary(
        job_id="job_" + "a" * 32,
        plan_id="plan_" + "b" * 32,
        state=JobState.CANCELLED,
        created_at=now,
        queued_at=now,
        completed_at=now,
    )

    assert job.started_at is None
    assert job.diagnostics_truncated is False


def test_diagnostic_summary_requires_a_terminal_resource_when_truncated() -> None:
    now = datetime.now(UTC)
    job_id = "job_" + "a" * 32
    plan_id = "plan_" + "b" * 32

    retained = JobSummary(
        job_id=job_id,
        plan_id=plan_id,
        state=JobState.FAILED,
        created_at=now,
        queued_at=now,
        started_at=now,
        completed_at=now,
        failure_reason="DeepKOALA failed safely.",
        diagnostic_uri=f"deepkoala-job://jobs/{job_id}/diagnostics",
        diagnostics_truncated=True,
    )

    assert retained.diagnostics_truncated is True
    with pytest.raises(ValidationError, match="require a diagnostic resource"):
        JobSummary(
            job_id=job_id,
            plan_id=plan_id,
            state=JobState.FAILED,
            created_at=now,
            queued_at=now,
            started_at=now,
            completed_at=now,
            failure_reason="DeepKOALA failed safely.",
            diagnostics_truncated=True,
        )
    with pytest.raises(ValidationError, match="only for terminal jobs"):
        JobSummary(
            job_id=job_id,
            plan_id=plan_id,
            state=JobState.RUNNING,
            created_at=now,
            queued_at=now,
            started_at=now,
            diagnostic_uri=f"deepkoala-job://jobs/{job_id}/diagnostics",
        )


def test_tool_envelope_enforces_exact_success_and_error_variants() -> None:
    success = ToolEnvelope[GetDeepKoalaStatusInput](
        ok=True,
        result=ToolPayload(data=GetDeepKoalaStatusInput()),
    )
    failure = ToolEnvelope[GetDeepKoalaStatusInput](
        ok=False,
        error=ErrorDetail(
            code=ErrorCode.CONFIGURATION_INVALID,
            message="The companion is not configured.",
            recoverable=True,
            suggested_action="Configure the required checkout.",
            safe_details=(SafeDetail(name="setting", value="checkout"),),
        ),
    )

    assert success.ok is True
    assert failure.ok is False
    with pytest.raises(ValidationError, match="require only result"):
        ToolEnvelope[GetDeepKoalaStatusInput](ok=True)


def test_status_contract_has_no_path_or_environment_surface(tmp_path: Path) -> None:
    status = CompanionStatus(
        server_version="0.1.0",
        ready=True,
        deepkoala_available=True,
        weights_available=True,
        deepkoala_version="0.1.0",
        weight_source="github_bundled",
        available_model_dates=("202507",),
        supported_models=("full", "frag"),
        supported_devices=("auto", "cpu", "cuda", "mps"),
        defaults=CompanionDefaults(cpu_threads=2),
        limits=CompanionLimits(
            max_input_bytes=5_000_000,
            max_output_bytes=5_000_000,
            max_diagnostic_bytes=65_536,
            max_sequences=100_000,
            max_residues=5_000_000,
            max_sequence_length=100_000,
            max_header_length=1_024,
            max_queue_size=32,
            default_timeout_seconds=3_600,
            plan_ttl_seconds=600,
            retention_seconds=86_400,
        ),
        queue=QueueSnapshot(
            queue_capacity=32,
            running_jobs=0,
            queued_jobs=0,
        ),
        cleanup_pending_jobs=0,
    )

    payload = status.model_dump_json()
    assert str(tmp_path) not in payload
    assert "DEEPKOALA_MCP_" not in payload
    assert status.limits.max_sequences == 100_000
    assert status.limits.max_residues == 5_000_000
    assert status.limits.max_sequence_length == 100_000
    assert status.limits.max_header_length == 1_024
    schema = CompanionStatus.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "path" not in " ".join(schema.get("properties", {})).lower()
    assert JobState.PREPARED.value == "prepared"
    assert datetime.now(UTC).utcoffset() is not None


def test_companion_limits_reject_inconsistent_sequence_bounds() -> None:
    with pytest.raises(ValidationError, match="must not exceed max_residues"):
        CompanionLimits(
            max_input_bytes=5_000_000,
            max_output_bytes=5_000_000,
            max_diagnostic_bytes=65_536,
            max_sequences=100,
            max_residues=100,
            max_sequence_length=101,
            max_header_length=1_024,
            max_queue_size=32,
            default_timeout_seconds=3_600,
            plan_ttl_seconds=600,
            retention_seconds=86_400,
        )


def test_public_input_schema_exposes_hard_bounds_and_forbids_extra_fields() -> None:
    schema = PrepareDeepKoalaInput.model_json_schema()

    assert schema["additionalProperties"] is False
    fasta_text_schema = next(
        item for item in schema["properties"]["fasta_text"]["anyOf"] if item.get("type") == "string"
    )
    assert fasta_text_schema["maxLength"] == 5_000_000
    assert schema["properties"]["batch_size"]["anyOf"][0]["maximum"] == 1_024
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PrepareDeepKoalaInput(fasta_text=">a\nM\n", shell=True)  # type: ignore[call-arg]
