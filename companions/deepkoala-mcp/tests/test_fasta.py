"""Security and boundary tests for controlled protein FASTA intake."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

import deepkoala_mcp.filesystem as filesystem
from deepkoala_mcp.fasta import (
    DEFAULT_MAX_HEADER_BYTES,
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_MAX_RESIDUES_PER_SEQUENCE,
    DEFAULT_MAX_SEQUENCES,
    DEFAULT_MAX_TOTAL_RESIDUES,
    FastaLimits,
    FastaValidationError,
    ingest_inline_fasta,
    ingest_path_fasta,
    validate_fasta_bytes,
    validate_stored_fasta,
)
from deepkoala_mcp.filesystem import (
    FilesystemSecurityError,
    cleanup_job_directory,
    cleanup_session_directory,
    create_job_directory,
    create_session_directory,
    prepare_state_root,
    secure_write_bytes,
)


@pytest.fixture
def controlled_job(tmp_path: Path) -> Path:
    session = create_session_directory(tmp_path, "session_test")
    return create_job_directory(session, "job_test")


def test_prepare_state_root_creates_private_parents_and_final_directory(tmp_path: Path) -> None:
    parent = tmp_path / "new-parent"
    state_root = parent / "state"

    assert prepare_state_root(state_root) == state_root
    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o700

    state_root.chmod(0o755)
    with pytest.raises(FilesystemSecurityError, match="owner-only"):
        prepare_state_root(state_root)
    assert stat.S_IMODE(state_root.stat().st_mode) == 0o755


def test_prepare_state_root_rejects_relative_root_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(FilesystemSecurityError, match="must be absolute"):
        prepare_state_root(Path("relative-state"))
    with pytest.raises(FilesystemSecurityError, match="filesystem root"):
        prepare_state_root(Path("/"))

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(FilesystemSecurityError, match="symlink"):
        prepare_state_root(linked / "state")


def test_default_limits_match_the_hard_intake_contract() -> None:
    assert FastaLimits() == FastaLimits(
        max_input_bytes=5_000_000,
        max_sequences=100_000,
        max_total_residues=5_000_000,
        max_residues_per_sequence=100_000,
        max_header_bytes=1_024,
    )
    assert DEFAULT_MAX_INPUT_BYTES == 5_000_000
    assert DEFAULT_MAX_SEQUENCES == 100_000
    assert DEFAULT_MAX_TOTAL_RESIDUES == 5_000_000
    assert DEFAULT_MAX_RESIDUES_PER_SEQUENCE == 100_000
    assert DEFAULT_MAX_HEADER_BYTES == 1_024


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_input_bytes", 5_000_001),
        ("max_sequences", 100_001),
        ("max_total_residues", 5_000_001),
        ("max_residues_per_sequence", 100_001),
        ("max_header_bytes", 1_025),
    ],
)
def test_limits_cannot_exceed_hard_maxima(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        FastaLimits(**{field: value})  # type: ignore[arg-type]


def test_private_session_job_and_inline_file_permissions(controlled_job: Path) -> None:
    summary = ingest_inline_fasta(
        ">protein-1 description\nMPEPTIDE\n", job_directory=controlled_job
    )

    assert stat.S_IMODE(controlled_job.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(controlled_job.stat().st_mode) == 0o700
    assert stat.S_IMODE((controlled_job / "input.fasta").stat().st_mode) == 0o600
    assert summary.sequence_count == 1
    assert summary.total_residues == 8
    assert summary.min_length == 8
    assert summary.max_length == 8
    assert summary.input_bytes == len(b">protein-1 description\nMPEPTIDE\n")
    assert summary.input_sha256 == hashlib.sha256(b">protein-1 description\nMPEPTIDE\n").hexdigest()
    assert not hasattr(summary, "path")
    assert not hasattr(summary, "headers")
    assert not hasattr(summary, "sequences")


def test_private_directories_and_files_are_exclusive(controlled_job: Path) -> None:
    with pytest.raises(FilesystemSecurityError, match="already exists"):
        create_job_directory(controlled_job.parent, controlled_job.name)
    ingest_inline_fasta(">p1\nM\n", job_directory=controlled_job)
    with pytest.raises(FilesystemSecurityError, match="already exists"):
        ingest_inline_fasta(">p2\nM\n", job_directory=controlled_job)


def test_valid_multiple_records_and_crlf_are_normalized_for_summary() -> None:
    content = b">p1 description\r\nMPEPTIDE\r\n>p2\r\nBXZJUO*\r\n"
    canonical = b">p1 description\nMPEPTIDE\n>p2\nBXZJUO*\n"
    summary = validate_fasta_bytes(content)

    assert summary.sequence_count == 2
    assert summary.total_residues == 15
    assert summary.min_length == 7
    assert summary.max_length == 8
    assert summary.input_bytes == len(canonical)
    assert summary.input_sha256 == hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize(
    "content",
    [
        b">p1\nM PEPTIDE\n",
        b">p1\nM\tPEPTIDE\n",
        b">p1\nM\vPEPTIDE\n",
        b">p1\nM\fPEPTIDE\n",
        b">p1\n   \nMPEPTIDE\n",
    ],
)
def test_residue_line_whitespace_is_rejected(content: bytes) -> None:
    with pytest.raises(FastaValidationError, match="residue lines must not contain whitespace"):
        validate_fasta_bytes(content)


@pytest.mark.parametrize(
    "content",
    [
        b"> seq\nM\n",
        b">   seq\nM\n",
        b">   \nM\n",
        b">\tseq\nM\n",
        ">s\N{LATIN SMALL LETTER E WITH ACUTE}q\nM\n".encode(),
    ],
)
def test_headers_incompatible_with_official_identifier_parsing_are_rejected(
    content: bytes,
) -> None:
    with pytest.raises(FastaValidationError):
        validate_fasta_bytes(content)


def test_inline_intake_writes_only_canonical_ascii_fasta(controlled_job: Path) -> None:
    canonical = b">p1 description\nMPEP\nTIDE\n"

    summary = ingest_inline_fasta(
        ">p1 description\r\nMPEP\r\n\r\nTIDE",
        job_directory=controlled_job,
    )

    assert (controlled_job / "input.fasta").read_bytes() == canonical
    assert summary.input_bytes == len(canonical)
    assert summary.input_sha256 == hashlib.sha256(canonical).hexdigest()


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"   \n",
        b"MPEPTIDE\n",
        b">\nM\n",
        b">only-header\n",
        b">p1\nM\n>p2\n",
        b">p1\nM\x00P\n",
        b">p1\x00\nMP\n",
        b">p1\nMp\n",
        b">p1\nM-P\n",
        b"\xff\xfe",
    ],
)
def test_malformed_or_non_protein_fasta_is_rejected(content: bytes) -> None:
    with pytest.raises(FastaValidationError):
        validate_fasta_bytes(content)


def test_duplicate_first_token_identifier_is_rejected() -> None:
    with pytest.raises(FastaValidationError, match="identifiers must be unique"):
        validate_fasta_bytes(b">same first\nMP\n>same second\nPE\n")


def test_nucleotide_only_heuristic_starts_at_one_hundred_residues() -> None:
    assert validate_fasta_bytes(b">short\n" + b"A" * 99 + b"\n").total_residues == 99
    with pytest.raises(FastaValidationError, match="appears to contain nucleotide"):
        validate_fasta_bytes(b">long\n" + b"ACGTUN" * 16 + b"ACGT" + b"\n")


@pytest.mark.parametrize(
    ("content", "limits", "message"),
    [
        (b">p1\nMP\n", FastaLimits(max_input_bytes=6), "byte limit"),
        (b">p1\nM\n>p2\nP\n", FastaLimits(max_sequences=1), "sequence-count"),
        (b">p1\nMPE\n", FastaLimits(max_total_residues=2), "total-residue"),
        (
            b">p1\nMPE\n",
            FastaLimits(max_residues_per_sequence=2),
            "per-sequence",
        ),
        (b">long-header\nMP\n", FastaLimits(max_header_bytes=4), "header byte"),
    ],
)
def test_each_configurable_boundary_is_enforced(
    content: bytes, limits: FastaLimits, message: str
) -> None:
    with pytest.raises(FastaValidationError, match=message):
        validate_fasta_bytes(content, limits)


def test_path_intake_copies_from_allowed_regular_file(tmp_path: Path, controlled_job: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "proteins.faa"
    source.write_bytes(b">p1\r\nMPEPTIDE\r\n")

    summary = ingest_path_fasta(
        source,
        allowed_roots=[allowed],
        job_directory=controlled_job,
    )

    source.write_bytes(b">changed\nAAAAAAAAA\n")
    assert (controlled_job / "input.fasta").read_bytes() == b">p1\nMPEPTIDE\n"
    assert summary == validate_stored_fasta(controlled_job)


def test_stored_fasta_revalidation_rejects_noncanonical_content(
    controlled_job: Path,
) -> None:
    secure_write_bytes(controlled_job, "input.fasta", b">p1\r\nMPEPTIDE\r\n")

    with pytest.raises(FastaValidationError, match="canonical ASCII"):
        validate_stored_fasta(controlled_job)


def test_path_intake_removes_internal_copy_when_validation_fails(
    tmp_path: Path, controlled_job: Path
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "invalid.faa"
    source.write_bytes(b">p1\nlowercase\n")

    with pytest.raises(FastaValidationError):
        ingest_path_fasta(source, allowed_roots=[allowed], job_directory=controlled_job)

    assert not (controlled_job / "input.fasta").exists()


def test_failed_duplicate_path_intake_does_not_delete_existing_input(
    tmp_path: Path, controlled_job: Path
) -> None:
    existing = b">existing\nMPEPTIDE\n"
    ingest_inline_fasta(existing.decode("ascii"), job_directory=controlled_job)
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "other.faa"
    source.write_bytes(b">other\nMPEPTIDE\n")

    with pytest.raises(FilesystemSecurityError, match="already exists"):
        ingest_path_fasta(source, allowed_roots=[allowed], job_directory=controlled_job)

    assert (controlled_job / "input.fasta").read_bytes() == existing


@pytest.mark.parametrize("source", [Path("relative.faa"), Path("../escape.faa")])
def test_path_intake_rejects_non_absolute_paths(source: Path, controlled_job: Path) -> None:
    with pytest.raises(FilesystemSecurityError, match="must be absolute"):
        ingest_path_fasta(
            source,
            allowed_roots=[controlled_job.parent],
            job_directory=controlled_job,
        )


def test_path_intake_rejects_lexical_parent_traversal(tmp_path: Path, controlled_job: Path) -> None:
    source = Path(f"{tmp_path}/allowed/../outside.faa")
    with pytest.raises(FilesystemSecurityError, match="parent traversal"):
        ingest_path_fasta(source, allowed_roots=[tmp_path], job_directory=controlled_job)


def test_path_intake_rejects_nul_character(tmp_path: Path, controlled_job: Path) -> None:
    with pytest.raises(FilesystemSecurityError, match="invalid character"):
        ingest_path_fasta(
            f"{tmp_path}/allowed/invalid\x00.faa",
            allowed_roots=[tmp_path],
            job_directory=controlled_job,
        )


def test_path_intake_requires_an_explicit_matching_root(
    tmp_path: Path, controlled_job: Path
) -> None:
    source = tmp_path / "protein.faa"
    source.write_bytes(b">p1\nMPEPTIDE\n")
    with pytest.raises(FilesystemSecurityError, match="requires at least one"):
        ingest_path_fasta(source, allowed_roots=[], job_directory=controlled_job)
    with pytest.raises(FilesystemSecurityError, match="outside"):
        ingest_path_fasta(
            source,
            allowed_roots=[controlled_job],
            job_directory=controlled_job,
        )


def test_path_intake_rejects_allowed_root_symlink(tmp_path: Path, controlled_job: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "protein.faa").write_bytes(b">p1\nMPEPTIDE\n")
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(FilesystemSecurityError, match="symlink"):
        ingest_path_fasta(
            linked / "protein.faa",
            allowed_roots=[linked],
            job_directory=controlled_job,
        )


def test_path_intake_rejects_parent_symlink(tmp_path: Path, controlled_job: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    real = allowed / "real"
    real.mkdir()
    (real / "protein.faa").write_bytes(b">p1\nMPEPTIDE\n")
    (allowed / "linked").symlink_to(real, target_is_directory=True)

    with pytest.raises(FilesystemSecurityError, match="symlink"):
        ingest_path_fasta(
            allowed / "linked" / "protein.faa",
            allowed_roots=[allowed],
            job_directory=controlled_job,
        )


def test_path_intake_rejects_final_symlink(tmp_path: Path, controlled_job: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    real = allowed / "real.faa"
    real.write_bytes(b">p1\nMPEPTIDE\n")
    linked = allowed / "linked.faa"
    linked.symlink_to(real)

    with pytest.raises(FilesystemSecurityError, match="regular file"):
        ingest_path_fasta(linked, allowed_roots=[allowed], job_directory=controlled_job)


def test_path_intake_rejects_directory_fifo_and_device(
    tmp_path: Path, controlled_job: Path
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    directory = allowed / "directory"
    directory.mkdir()
    fifo = allowed / "pipe"
    os.mkfifo(fifo)

    for source in (directory, fifo):
        with pytest.raises(FilesystemSecurityError, match="regular file"):
            ingest_path_fasta(source, allowed_roots=[allowed], job_directory=controlled_job)

    if Path("/dev/null").exists():
        with pytest.raises(FilesystemSecurityError, match="regular file"):
            ingest_path_fasta(
                Path("/dev/null"),
                allowed_roots=[Path("/dev")],
                job_directory=controlled_job,
            )


def test_path_byte_overflow_leaves_no_partial_internal_file(
    tmp_path: Path, controlled_job: Path
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "large.faa"
    source.write_bytes(b">p1\nMPEPTIDE\n")

    with pytest.raises(FastaValidationError, match="byte limit"):
        ingest_path_fasta(
            source,
            allowed_roots=[allowed],
            job_directory=controlled_job,
            limits=FastaLimits(max_input_bytes=8),
        )

    assert not (controlled_job / "input.fasta").exists()


def test_path_intake_detects_ctime_change_when_size_and_mtime_are_restored(
    tmp_path: Path,
    controlled_job: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "protein.faa"
    source.write_bytes(b">p1\nMPEPTIDE\n")
    original = source.stat()
    original_read = filesystem.os.read
    changed = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, count)
        opened = os.fstat(descriptor)
        if content and opened.st_ino == original.st_ino and not changed:
            changed = True
            source.write_bytes(b">p1\nREPLACED\n")
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns))
        return content

    monkeypatch.setattr(filesystem.os, "read", racing_read)

    with pytest.raises(FilesystemSecurityError, match="changed during intake"):
        ingest_path_fasta(
            source,
            allowed_roots=[allowed],
            job_directory=controlled_job,
        )
    assert not (controlled_job / "input.fasta").exists()


def test_controlled_read_detects_ctime_change_when_size_and_mtime_are_restored(
    controlled_job: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ingest_inline_fasta(">p1\nMPEPTIDE\n", job_directory=controlled_job)
    stored = controlled_job / "input.fasta"
    original = stored.stat()
    original_read = filesystem.os.read
    changed = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, count)
        opened = os.fstat(descriptor)
        if content and opened.st_ino == original.st_ino and not changed:
            changed = True
            stored.write_bytes(b">p1\nREPLACED\n")
            os.utime(stored, ns=(original.st_atime_ns, original.st_mtime_ns))
        return content

    monkeypatch.setattr(filesystem.os, "read", racing_read)

    with pytest.raises(FilesystemSecurityError, match="changed while it was read"):
        validate_stored_fasta(controlled_job)


def test_cleanup_removes_only_known_entries_without_following_symlinks(
    tmp_path: Path, controlled_job: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    (controlled_job / "input.fasta").symlink_to(outside)
    secure_write_bytes(controlled_job, "diagnostics.txt", b"bounded diagnostics")

    cleanup_job_directory(controlled_job)

    assert not controlled_job.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_cleanup_refuses_unknown_entries_without_partial_deletion(controlled_job: Path) -> None:
    secure_write_bytes(controlled_job, "input.fasta", b">p1\nM\n")
    unknown = controlled_job / "unexpected.txt"
    unknown.write_text("do not delete", encoding="utf-8")

    with pytest.raises(FilesystemSecurityError, match="unknown entry"):
        cleanup_job_directory(controlled_job)

    assert (controlled_job / "input.fasta").exists()
    assert unknown.exists()


def test_cleanup_rejects_non_job_name(tmp_path: Path) -> None:
    directory = tmp_path / "not-a-job"
    directory.mkdir()
    with pytest.raises(FilesystemSecurityError, match="invalid controlled job"):
        cleanup_job_directory(directory)


def test_cleanup_session_removes_only_an_empty_controlled_directory(tmp_path: Path) -> None:
    state_root = prepare_state_root(tmp_path / "state")
    session = create_session_directory(state_root, "session_cleanup")

    cleanup_session_directory(session)

    assert not session.exists()
    assert state_root.exists()


def test_cleanup_session_refuses_nonempty_directory_without_deleting_it(tmp_path: Path) -> None:
    state_root = prepare_state_root(tmp_path / "state")
    session = create_session_directory(state_root, "session_nonempty")
    create_job_directory(session, "job_retained")

    with pytest.raises(FilesystemSecurityError, match="not empty"):
        cleanup_session_directory(session)

    assert session.exists()
    assert (session / "job_retained").exists()


def test_cleanup_session_does_not_follow_a_symlink(tmp_path: Path) -> None:
    state_root = prepare_state_root(tmp_path / "state")
    target = tmp_path / "target"
    target.mkdir()
    linked = state_root / "session_linked"
    linked.symlink_to(target, target_is_directory=True)

    with pytest.raises(FilesystemSecurityError, match="controlled directory"):
        cleanup_session_directory(linked)

    assert linked.is_symlink()
    assert target.exists()
