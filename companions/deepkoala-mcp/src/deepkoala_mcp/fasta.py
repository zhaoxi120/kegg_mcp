"""Bounded protein FASTA validation and controlled intake."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Final

from deepkoala_mcp.filesystem import (
    FileSizeLimitError,
    copy_allowed_regular_file,
    read_controlled_file,
    remove_controlled_file,
    secure_write_bytes,
)

DEFAULT_MAX_INPUT_BYTES: Final = 5_000_000
DEFAULT_MAX_SEQUENCES: Final = 100_000
DEFAULT_MAX_TOTAL_RESIDUES: Final = 5_000_000
DEFAULT_MAX_RESIDUES_PER_SEQUENCE: Final = 100_000
DEFAULT_MAX_HEADER_BYTES: Final = 1_024

HARD_MAX_INPUT_BYTES: Final = 5_000_000
HARD_MAX_SEQUENCES: Final = 100_000
HARD_MAX_TOTAL_RESIDUES: Final = 5_000_000
HARD_MAX_RESIDUES_PER_SEQUENCE: Final = 100_000
HARD_MAX_HEADER_BYTES: Final = 1_024

_PROTEIN_ALPHABET: Final = frozenset("ACDEFGHIKLMNPQRSTVWYBXZJUO*")
_NUCLEOTIDE_ALPHABET: Final = frozenset("ACGTUN")
_INLINE_ASCII_WHITESPACE: Final = frozenset(" \t\v\f")
_INPUT_FILENAME: Final = "input.fasta"


class FastaValidationError(ValueError):
    """Raised when input is not a bounded, unambiguous protein FASTA document."""


@dataclass(frozen=True, slots=True)
class FastaLimits:
    """Configurable intake limits capped by non-overridable hard maxima."""

    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES
    max_sequences: int = DEFAULT_MAX_SEQUENCES
    max_total_residues: int = DEFAULT_MAX_TOTAL_RESIDUES
    max_residues_per_sequence: int = DEFAULT_MAX_RESIDUES_PER_SEQUENCE
    max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES

    def __post_init__(self) -> None:
        hard_maxima = {
            "max_input_bytes": HARD_MAX_INPUT_BYTES,
            "max_sequences": HARD_MAX_SEQUENCES,
            "max_total_residues": HARD_MAX_TOTAL_RESIDUES,
            "max_residues_per_sequence": HARD_MAX_RESIDUES_PER_SEQUENCE,
            "max_header_bytes": HARD_MAX_HEADER_BYTES,
        }
        for field in fields(self):
            value = getattr(self, field.name)
            hard_maximum = hard_maxima[field.name]
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{field.name} must be an integer")
            if value < 1 or value > hard_maximum:
                raise ValueError(f"{field.name} must be between 1 and {hard_maximum}")


@dataclass(frozen=True, slots=True)
class FastaSummary:
    """Non-sensitive summary of one validated FASTA input."""

    sequence_count: int
    total_residues: int
    min_length: int
    max_length: int
    input_bytes: int
    input_sha256: str


def validate_fasta_bytes(
    content: bytes,
    limits: FastaLimits | None = None,
) -> FastaSummary:
    """Validate protein FASTA bytes and return only bounded aggregate metadata."""
    effective_limits = limits or FastaLimits()
    summary, _ = _validate_and_normalize_fasta(content, effective_limits)
    return summary


def _validate_and_normalize_fasta(
    content: bytes,
    limits: FastaLimits,
) -> tuple[FastaSummary, bytes]:
    """Validate FASTA and return the exact canonical bytes safe for DeepKOALA."""
    if not content:
        raise FastaValidationError("FASTA input must not be empty")
    if len(content) > limits.max_input_bytes:
        raise FastaValidationError("FASTA input exceeds the byte limit")
    try:
        text = content.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise FastaValidationError("FASTA input must contain ASCII text only") from error
    if not text:
        raise FastaValidationError("FASTA input must not be empty")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    canonical_lines: list[str] = []
    sequence_ids: set[str] = set()
    sequence_lengths: list[int] = []
    current_length: int | None = None
    total_residues = 0
    nucleotide_only = True

    for line in normalized.split("\n"):
        if line.startswith(">"):
            if current_length is not None:
                _finish_sequence(current_length, sequence_lengths)
            header = line[1:]
            sequence_id = _validate_header(header, limits)
            if sequence_id in sequence_ids:
                raise FastaValidationError("FASTA sequence identifiers must be unique")
            sequence_ids.add(sequence_id)
            if len(sequence_ids) > limits.max_sequences:
                raise FastaValidationError("FASTA input exceeds the sequence-count limit")
            canonical_lines.append(line)
            current_length = 0
            continue

        if not line:
            continue
        if current_length is None:
            raise FastaValidationError("FASTA residues must follow a header line")

        residues: list[str] = []
        for character in line:
            if character in _INLINE_ASCII_WHITESPACE:
                raise FastaValidationError("FASTA residue lines must not contain whitespace")
            if _is_control(character):
                raise FastaValidationError("FASTA input contains a control character")
            if character not in _PROTEIN_ALPHABET:
                raise FastaValidationError("FASTA input contains an invalid protein residue")
            residues.append(character)
        canonical_lines.append(line)
        residue_count = len(residues)
        current_length += residue_count
        total_residues += residue_count
        if current_length > limits.max_residues_per_sequence:
            raise FastaValidationError("FASTA sequence exceeds the per-sequence residue limit")
        if total_residues > limits.max_total_residues:
            raise FastaValidationError("FASTA input exceeds the total-residue limit")
        if nucleotide_only and any(residue not in _NUCLEOTIDE_ALPHABET for residue in residues):
            nucleotide_only = False

    if current_length is None:
        raise FastaValidationError("FASTA input must contain at least one header")
    _finish_sequence(current_length, sequence_lengths)
    if nucleotide_only and total_residues >= 100:
        raise FastaValidationError("FASTA input appears to contain nucleotide sequences")

    normalized_bytes = ("\n".join(canonical_lines) + "\n").encode("ascii")
    if len(normalized_bytes) > limits.max_input_bytes:
        raise FastaValidationError("normalized FASTA input exceeds the byte limit")

    return FastaSummary(
        sequence_count=len(sequence_lengths),
        total_residues=total_residues,
        min_length=min(sequence_lengths),
        max_length=max(sequence_lengths),
        input_bytes=len(normalized_bytes),
        input_sha256=hashlib.sha256(normalized_bytes).hexdigest(),
    ), normalized_bytes


def ingest_inline_fasta(
    content: str,
    *,
    job_directory: Path | str,
    limits: FastaLimits | None = None,
) -> FastaSummary:
    """Validate inline FASTA and store an exclusive private canonical ASCII copy."""
    effective_limits = limits or FastaLimits()
    try:
        encoded = content.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise FastaValidationError("FASTA input must be valid UTF-8") from error
    summary, normalized = _validate_and_normalize_fasta(encoded, effective_limits)
    secure_write_bytes(job_directory, _INPUT_FILENAME, normalized)
    return summary


def ingest_path_fasta(
    source_path: Path | str,
    *,
    allowed_roots: Sequence[Path | str],
    job_directory: Path | str,
    limits: FastaLimits | None = None,
) -> FastaSummary:
    """Copy an allowlisted FASTA from its descriptor, validate it, and retain the copy."""
    effective_limits = limits or FastaLimits()
    copied_to_job = False
    try:
        copy_allowed_regular_file(
            source_path,
            allowed_roots=allowed_roots,
            job_directory=job_directory,
            filename=_INPUT_FILENAME,
            max_bytes=effective_limits.max_input_bytes,
        )
        copied_to_job = True
        copied = read_controlled_file(
            job_directory,
            _INPUT_FILENAME,
            max_bytes=effective_limits.max_input_bytes,
        )
        summary, normalized = _validate_and_normalize_fasta(copied, effective_limits)
        if copied != normalized:
            remove_controlled_file(job_directory, _INPUT_FILENAME)
            copied_to_job = False
            secure_write_bytes(job_directory, _INPUT_FILENAME, normalized)
            copied_to_job = True
        return summary
    except FileSizeLimitError as error:
        if copied_to_job:
            remove_controlled_file(job_directory, _INPUT_FILENAME)
        raise FastaValidationError("FASTA input exceeds the byte limit") from error
    except Exception:
        if copied_to_job:
            remove_controlled_file(job_directory, _INPUT_FILENAME)
        raise


def validate_stored_fasta(
    job_directory: Path | str,
    limits: FastaLimits | None = None,
) -> FastaSummary:
    """Revalidate the fixed internal FASTA copy without disclosing its contents."""
    effective_limits = limits or FastaLimits()
    try:
        copied = read_controlled_file(
            job_directory,
            _INPUT_FILENAME,
            max_bytes=effective_limits.max_input_bytes,
        )
    except FileSizeLimitError as error:
        raise FastaValidationError("FASTA input exceeds the byte limit") from error
    summary, normalized = _validate_and_normalize_fasta(copied, effective_limits)
    if copied != normalized:
        raise FastaValidationError("stored FASTA input is not in canonical ASCII form")
    return summary


def _finish_sequence(length: int, sequence_lengths: list[int]) -> None:
    if length < 1:
        raise FastaValidationError("every FASTA record must contain at least one residue")
    sequence_lengths.append(length)


def _validate_header(header: str, limits: FastaLimits) -> str:
    sequence_id = header.split(" ", maxsplit=1)[0]
    if not sequence_id:
        raise FastaValidationError("FASTA headers must contain a sequence identifier")
    if len(header.encode("utf-8")) > limits.max_header_bytes:
        raise FastaValidationError("FASTA header exceeds the header byte limit")
    if any(_is_control(character) for character in header):
        raise FastaValidationError("FASTA header contains a control character")
    return sequence_id


def _is_control(character: str) -> bool:
    return unicodedata.category(character).startswith("C")
