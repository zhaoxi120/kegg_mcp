"""Bounded protein FASTA intake and private staging."""

from __future__ import annotations

import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from deepkoala_mcp.contracts import (
    MAX_FASTA_BYTES,
    MAX_HEADER_BYTES,
    MAX_SEQUENCE_COUNT,
    MAX_SEQUENCE_LENGTH,
    FastaSummary,
)

INPUT_FILENAME: Final = "input.fasta"
_PROTEIN_ALPHABET: Final = frozenset("ACDEFGHIKLMNPQRSTVWYBXZJUO*")


class FastaValidationError(ValueError):
    """The supplied file is not an accepted protein FASTA."""


class FastaLimitError(FastaValidationError):
    """The supplied FASTA exceeds one deployment bound."""


class InputPathError(ValueError):
    """A caller-supplied path violates the configured input boundary."""


@dataclass(frozen=True, slots=True)
class StagedFasta:
    """A private canonical copy and safe aggregate intake facts."""

    summary: FastaSummary
    input_path: Path


def stage_fasta(
    *,
    fasta_path: str,
    input_roots: tuple[Path, ...],
    job_directory: Path,
    max_bytes: int,
    max_sequences: int,
) -> StagedFasta:
    """Read one allowlisted file once and retain a canonical private copy."""
    content, resolved = _read_allowed_file(
        Path(fasta_path),
        input_roots,
        max_bytes=max_bytes,
    )
    summary, canonical = validate_fasta_bytes(
        content,
        max_bytes=max_bytes,
        max_sequences=max_sequences,
    )
    _write_private(job_directory / INPUT_FILENAME, canonical)
    return StagedFasta(summary=summary, input_path=resolved)


def validate_fasta_bytes(
    content: bytes,
    *,
    max_bytes: int = MAX_FASTA_BYTES,
    max_sequences: int = MAX_SEQUENCE_COUNT,
) -> tuple[FastaSummary, bytes]:
    """Validate bounded protein FASTA bytes and return canonical ASCII bytes."""
    if not 1 <= max_bytes <= MAX_FASTA_BYTES:
        raise ValueError("max_bytes is outside the hard companion limit")
    if not 1 <= max_sequences <= MAX_SEQUENCE_COUNT:
        raise ValueError("max_sequences is outside the hard companion limit")
    if not content:
        raise FastaValidationError("FASTA is empty")
    if len(content) > max_bytes:
        raise FastaLimitError("FASTA exceeds the byte limit")
    try:
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise FastaValidationError("FASTA must be ASCII") from error

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    canonical: list[str] = []
    sequence_ids: set[str] = set()
    lengths: list[int] = []
    current_length: int | None = None
    total_residues = 0

    for line in lines:
        if line.startswith(">"):
            if current_length is not None:
                _finish_sequence(current_length, lengths)
            sequence_id = _validate_header(line[1:])
            if sequence_id in sequence_ids:
                raise FastaValidationError("FASTA sequence identifiers must be unique")
            sequence_ids.add(sequence_id)
            if len(sequence_ids) > max_sequences:
                raise FastaLimitError("FASTA exceeds the sequence-count limit")
            canonical.append(line)
            current_length = 0
            continue
        if not line:
            continue
        if current_length is None:
            raise FastaValidationError("FASTA residues must follow a header")
        if any(character.isspace() or _control(character) for character in line):
            raise FastaValidationError("FASTA residue lines contain whitespace or control text")
        residues = line.upper()
        if any(character not in _PROTEIN_ALPHABET for character in residues):
            raise FastaValidationError("FASTA contains an invalid protein residue")
        current_length += len(residues)
        total_residues += len(residues)
        if current_length > MAX_SEQUENCE_LENGTH:
            raise FastaLimitError("one FASTA sequence exceeds the residue limit")
        if total_residues > max_bytes:
            raise FastaLimitError("FASTA exceeds the total-residue limit")
        canonical.append(residues)

    if current_length is None:
        raise FastaValidationError("FASTA contains no records")
    _finish_sequence(current_length, lengths)
    canonical_bytes = ("\n".join(canonical) + "\n").encode("ascii")
    if len(canonical_bytes) > max_bytes:
        raise FastaLimitError("canonical FASTA exceeds the byte limit")
    return (
        FastaSummary(
            sequence_count=len(lengths),
            total_residues=total_residues,
            max_sequence_length=max(lengths),
            input_bytes=len(canonical_bytes),
        ),
        canonical_bytes,
    )


def _read_allowed_file(
    path: Path,
    allowed_roots: tuple[Path, ...],
    *,
    max_bytes: int,
) -> tuple[bytes, Path]:
    if not path.is_absolute() or ".." in path.parts or not allowed_roots:
        raise InputPathError("input path is not allowed")
    try:
        named = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InputPathError("input path is unavailable") from error
    if resolved != path or stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(named.st_mode):
        raise InputPathError("input path must be a direct regular file without symlinks")
    root = next((root for root in allowed_roots if resolved.is_relative_to(root)), None)
    if root is None:
        raise InputPathError("input path escapes the configured roots")

    try:
        descriptor = _open_beneath(resolved, root)
    except OSError as error:
        raise InputPathError("input path cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if _file_state(named) != _file_state(before) or not stat.S_ISREG(before.st_mode):
            raise InputPathError("input path changed before intake")
        if before.st_size > max_bytes:
            raise FastaLimitError("FASTA exceeds the byte limit")
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) > max_bytes:
            raise FastaLimitError("FASTA exceeds the byte limit")
        if _file_state(before) != _file_state(after):
            raise InputPathError("input file changed during intake")
        if len(str(resolved)) > 4_096:
            raise InputPathError("resolved input path exceeds the provenance limit")
        return bytes(content), resolved
    finally:
        os.close(descriptor)


def _open_beneath(path: Path, root: Path) -> int:
    """Open a resolved file by walking from an allowed root without symlinks."""
    parts = path.relative_to(root).parts
    if not parts:
        raise OSError("input path names an allowed directory")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directories: list[int] = []
    try:
        current = os.open(root, directory_flags)
        directories.append(current)
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            directories.append(current)
        return os.open(parts[-1], file_flags, dir_fd=current)
    finally:
        for descriptor in reversed(directories):
            os.close(descriptor)


def _write_private(path: Path, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)


def _validate_header(header: str) -> str:
    if not header or any(_control(character) for character in header):
        raise FastaValidationError("FASTA header is empty or contains a control character")
    if len(header.encode("ascii")) > MAX_HEADER_BYTES:
        raise FastaLimitError("FASTA header exceeds the byte limit")
    sequence_id = header.split(maxsplit=1)[0]
    if not sequence_id:
        raise FastaValidationError("FASTA header has no sequence identifier")
    return sequence_id


def _finish_sequence(length: int, lengths: list[int]) -> None:
    if length < 1:
        raise FastaValidationError("every FASTA record requires residues")
    lengths.append(length)


def _control(character: str) -> bool:
    return unicodedata.category(character).startswith("C")


def _file_state(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


__all__ = [
    "INPUT_FILENAME",
    "FastaLimitError",
    "FastaValidationError",
    "InputPathError",
    "StagedFasta",
    "stage_fasta",
    "validate_fasta_bytes",
]
