"""Configuration, public-schema, and FASTA boundary tests."""

from __future__ import annotations

import os
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from deepkoala_mcp import fasta as fasta_module
from deepkoala_mcp.config import (
    ALLOWED_ROOTS_ENV,
    CHECKOUT_ENV,
    CPU_THREADS_ENV,
    MAX_QUEUE_SIZE_ENV,
    PYTHON_ENV,
    STATE_ROOT_ENV,
    DeepKoalaRuntimeConfig,
    load_runtime_config,
)
from deepkoala_mcp.contracts import (
    MAX_FASTA_BYTES,
    ImportHandoff,
    PrepareDeepKoalaInput,
    SourceProvenance,
    SubmitDeepKoalaInput,
)
from deepkoala_mcp.fasta import (
    FastaLimitError,
    FastaValidationError,
    InputPathError,
    stage_fasta,
    validate_fasta_bytes,
)


def test_load_runtime_config_uses_only_small_cpu_defaults(
    tmp_path: Path,
    checkout: Path,
) -> None:
    state = tmp_path / "state"
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = load_runtime_config(
        {
            CHECKOUT_ENV: str(checkout),
            PYTHON_ENV: str(Path(sys.executable).resolve()),
            STATE_ROOT_ENV: str(state),
            ALLOWED_ROOTS_ENV: str(allowed),
        }
    )

    assert config.cpu_threads == 2
    assert config.max_queue_size == 4
    assert config.allowed_roots == (allowed.resolve(),)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (CPU_THREADS_ENV, "0"),
        (CPU_THREADS_ENV, "5"),
        (MAX_QUEUE_SIZE_ENV, "9"),
        (MAX_QUEUE_SIZE_ENV, "1.5"),
    ],
)
def test_load_runtime_config_rejects_unbounded_integers(
    tmp_path: Path,
    checkout: Path,
    name: str,
    value: str,
) -> None:
    environment = {
        CHECKOUT_ENV: str(checkout),
        PYTHON_ENV: str(Path(sys.executable).resolve()),
        STATE_ROOT_ENV: str(tmp_path / "state"),
        name: value,
    }
    with pytest.raises(ValueError, match=name):
        load_runtime_config(environment)


def test_load_runtime_config_requires_explicit_paths(checkout: Path) -> None:
    with pytest.raises(ValueError, match=STATE_ROOT_ENV):
        load_runtime_config(
            {
                CHECKOUT_ENV: str(checkout),
                PYTHON_ENV: str(Path(sys.executable).resolve()),
            }
        )


def test_runtime_config_rejects_overlapping_state_and_input_roots(
    tmp_path: Path,
    checkout: Path,
) -> None:
    root = tmp_path / "private"
    root.mkdir()
    with pytest.raises(ValidationError, match="overlap"):
        DeepKoalaRuntimeConfig(
            checkout=checkout,
            python_executable=Path(sys.executable).resolve(),
            state_root=root / "state",
            allowed_roots=(root,),
        )


def test_prepare_contract_keeps_device_service_owned_and_memory_bounded() -> None:
    request = PrepareDeepKoalaInput(fasta_text=">p\nMPEPTIDE\n")
    assert request.batch_size == 1
    with pytest.raises(ValidationError):
        PrepareDeepKoalaInput(fasta_text=">p\nM\n", batch_size=65)
    with pytest.raises(ValidationError):
        PrepareDeepKoalaInput.model_validate(
            {"fasta_text": ">p\nM\n", "device": "cuda"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        PrepareDeepKoalaInput.model_validate(
            {"fasta_text": ">p\nM\n", "num_workers": 1},
            strict=True,
        )


def test_submit_accepts_only_the_opaque_job_identifier() -> None:
    job_id = "job_" + "a" * 32
    assert SubmitDeepKoalaInput(job_id=job_id).job_id == job_id
    with pytest.raises(ValidationError):
        SubmitDeepKoalaInput.model_validate(
            {"job_id": job_id, "acknowledged": True},
            strict=True,
        )


def test_handoff_validates_annotation_output_and_original_input_independently() -> None:
    job_id = "job_" + "a" * 32
    source = SourceProvenance(
        source_version="0.1-test",
        model_name="full",
        model_version="202502",
        annotation_date=datetime(2026, 7, 16, tzinfo=UTC),
        input_uri=f"mcp://deepkoala-mcp/jobs/{job_id}/output",
        input_path="/allowed/original.faa",
    )
    handoff = ImportHandoff(output_path="/private/annotations.csv", source=source)
    assert handoff.output_path != handoff.source.input_path

    with pytest.raises(ValidationError, match="output_path"):
        ImportHandoff(output_path="annotations.csv", source=source)
    with pytest.raises(ValidationError, match="input_path"):
        SourceProvenance(
            source_version="0.1-test",
            model_name="full",
            model_version="202502",
            annotation_date=datetime(2026, 7, 16, tzinfo=UTC),
            input_uri=f"mcp://deepkoala-mcp/jobs/{job_id}/output",
            input_path="original.faa",
        )


def test_validate_fasta_normalizes_newlines_case_and_reports_only_aggregates() -> None:
    summary, canonical = validate_fasta_bytes(b">p1 note\r\nmpep\r\n>p2\rW\r")
    assert canonical == b">p1 note\nMPEP\n>p2\nW\n"
    assert summary.sequence_count == 2
    assert summary.total_residues == 5
    assert summary.max_sequence_length == 4
    assert "p1" not in summary.model_dump_json()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"MPEPTIDE\n",
        b">p\n",
        b">p\nM  P\n",
        b">p\nM?P\n",
        b">p\nM\n>p\nW\n",
        b">\nM\n",
        ">p\nMé\n".encode(),
    ],
)
def test_validate_fasta_rejects_invalid_documents(content: bytes) -> None:
    with pytest.raises(FastaValidationError):
        validate_fasta_bytes(content)


def test_validate_fasta_enforces_byte_limit() -> None:
    with pytest.raises(FastaLimitError):
        validate_fasta_bytes(b">p\n" + b"M" * MAX_FASTA_BYTES)


def test_stage_path_requires_allowlist_and_writes_owner_only_copy(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    job = tmp_path / "job"
    allowed.mkdir()
    job.mkdir(mode=0o700)
    source = allowed / "proteins.faa"
    source.write_text(">p\nMPEPTIDE\n", encoding="ascii")

    staged_result = stage_fasta(
        fasta_text=None,
        fasta_path=str(source),
        allowed_roots=(allowed.resolve(),),
        job_directory=job,
    )

    staged = job / "input.fasta"
    assert staged_result.summary.sequence_count == 1
    assert staged_result.original_input_path == str(source.resolve())
    assert staged.read_text(encoding="ascii") == ">p\nMPEPTIDE\n"
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600


def test_stage_path_rejects_escape_and_final_symlink(tmp_path: Path) -> None:
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

    for path in (source, link, allowed / ".." / "outside" / "private.faa"):
        with pytest.raises(InputPathError):
            stage_fasta(
                fasta_text=None,
                fasta_path=str(path),
                allowed_roots=(allowed.resolve(),),
                job_directory=job,
            )
    assert not (job / "input.fasta").exists()


def test_stage_path_rejects_intermediate_symlink_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    inside = allowed / "inside"
    outside = tmp_path / "outside"
    job = tmp_path / "job"
    inside.mkdir(parents=True)
    outside.mkdir()
    job.mkdir()
    source = inside / "proteins.faa"
    source.write_text(">inside\nM\n", encoding="ascii")
    (outside / source.name).write_text(">outside\nW\n", encoding="ascii")
    original = fasta_module._open_beneath  # pyright: ignore[reportPrivateUsage]

    def swap_then_open(path: Path, root: Path) -> int:
        saved = allowed / "saved"
        inside.rename(saved)
        inside.symlink_to(outside, target_is_directory=True)
        return original(path, root)

    monkeypatch.setattr(fasta_module, "_open_beneath", swap_then_open)
    with pytest.raises(InputPathError):
        stage_fasta(
            fasta_text=None,
            fasta_path=str(source),
            allowed_roots=(allowed.resolve(),),
            job_directory=job,
        )
    assert not (job / "input.fasta").exists()


def test_stage_inline_never_requires_an_allowed_root(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir(mode=0o700)
    staged = stage_fasta(
        fasta_text=">inline\nM\n",
        fasta_path=None,
        allowed_roots=(),
        job_directory=job,
    )
    assert staged.summary.sequence_count == 1
    assert staged.original_input_path is None
    assert os.access(job / "input.fasta", os.R_OK)
