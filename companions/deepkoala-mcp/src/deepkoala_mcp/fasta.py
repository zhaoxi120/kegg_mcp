"""Bounded protein FASTA validation and private staging."""

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
    """The supplied content is not an accepted bounded protein FASTA."""


class FastaLimitError(FastaValidationError):
    """The supplied FASTA exceeds a hard companion limit."""


class InputPathError(ValueError):
    """A caller-supplied path violates the configured input boundary."""


@dataclass(frozen=True, slots=True)
class StagedFasta:
    """Validated FASTA summary and bounded caller-visible origin."""

    summary: FastaSummary
    original_input_path: str | None


def stage_fasta(
    *,
    fasta_text: str | None,
    fasta_path: str | None,
    allowed_roots: tuple[Path, ...],
    job_directory: Path,
) -> StagedFasta:
    """Read one source once and retain a canonical copy plus safe origin metadata."""
    if (fasta_text is None) == (fasta_path is None):
        raise FastaValidationError("exactly one FASTA source is required")
    original_input_path: str | None = None
    if fasta_text is not None:
        try:
            content = fasta_text.encode("ascii")
        except UnicodeEncodeError as error:
            raise FastaValidationError("FASTA must be ASCII") from error
    else:
        assert fasta_path is not None
        content, resolved = _read_allowed_file(Path(fasta_path), allowed_roots)
        original_input_path = str(resolved)
    summary, canonical = validate_fasta_bytes(content)
    _write_private(job_directory / INPUT_FILENAME, canonical)
    return StagedFasta(summary=summary, original_input_path=original_input_path)


def validate_fasta_bytes(content: bytes) -> tuple[FastaSummary, bytes]:
    """Validate bounded protein FASTA bytes and return a canonical ASCII document."""
    if not content:
        raise FastaValidationError("FASTA is empty")
    if len(content) > MAX_FASTA_BYTES:
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
            if len(sequence_ids) > MAX_SEQUENCE_COUNT:
                raise FastaLimitError("FASTA exceeds the sequence-count limit")
            canonical.append(line)
            current_length = 0
            continue
        if not line:
            continue
        if current_length is None:
            raise FastaValidationError("FASTA residues must follow a header")
        if any(character.isspace() or _control(character) for character in line):
            raise FastaValidationError("FASTA residue lines must not contain whitespace or control")
        residues = line.upper()
        if any(character not in _PROTEIN_ALPHABET for character in residues):
            raise FastaValidationError("FASTA contains an invalid protein residue")
        current_length += len(residues)
        total_residues += len(residues)
        if current_length > MAX_SEQUENCE_LENGTH:
            raise FastaLimitError("FASTA sequence exceeds the residue limit")
        if total_residues > MAX_FASTA_BYTES:
            raise FastaLimitError("FASTA exceeds the total-residue limit")
        canonical.append(residues)

    if current_length is None:
        raise FastaValidationError("FASTA contains no records")
    _finish_sequence(current_length, lengths)
    canonical_bytes = ("\n".join(canonical) + "\n").encode("ascii")
    if len(canonical_bytes) > MAX_FASTA_BYTES:
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


def _read_allowed_file(path: Path, allowed_roots: tuple[Path, ...]) -> tuple[bytes, Path]:
    if not path.is_absolute() or ".." in path.parts or not allowed_roots:
        raise InputPathError("input path is not allowed")
    try:
        named = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise InputPathError("input path is unavailable") from error
    if stat.S_ISLNK(named.st_mode) or not stat.S_ISREG(named.st_mode):
        raise InputPathError("input path must be a direct regular file")
    root = next(
        (root for root in allowed_roots if resolved == root or resolved.is_relative_to(root)),
        None,
    )
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
        if before.st_size > MAX_FASTA_BYTES:
            raise FastaLimitError("FASTA exceeds the byte limit")
        content = bytearray()
        while len(content) <= MAX_FASTA_BYTES:
            chunk = os.read(descriptor, min(65_536, MAX_FASTA_BYTES + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        after = os.fstat(descriptor)
        if len(content) > MAX_FASTA_BYTES:
            raise FastaLimitError("FASTA exceeds the byte limit")
        if _file_state(before) != _file_state(after):
            raise InputPathError("input file changed during intake")
        if len(str(resolved)) > 4_096:
            raise InputPathError("resolved input path exceeds the provenance limit")
        return bytes(content), resolved
    finally:
        os.close(descriptor)


def _open_beneath(path: Path, root: Path) -> int:
    """Open a resolved file by walking from its allowed root without symlinks."""
    parts = path.relative_to(root).parts
    if not parts:
        raise OSError("input path names an allowed directory")
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
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
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= os.O_NOFOLLOW
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
