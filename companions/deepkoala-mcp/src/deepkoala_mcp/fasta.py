"""Structurally bounded protein FASTA intake and private staging."""

from __future__ import annotations

import asyncio
import os
import stat
import unicodedata
from dataclasses import dataclass
from functools import partial
from io import BytesIO, TextIOWrapper
from pathlib import Path
from typing import BinaryIO, Final

from anyio.to_thread import run_sync as run_sync_in_worker_thread

from deepkoala_mcp.contracts import (
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
    """The supplied FASTA exceeds one structural bound."""


class InputPathError(ValueError):
    """A caller-supplied path violates the configured input boundary."""


@dataclass(frozen=True, slots=True)
class StagedFasta:
    """A private canonical copy and safe aggregate intake facts."""

    summary: FastaSummary
    input_path: Path


@dataclass(frozen=True, slots=True)
class _PinnedFasta:
    descriptor: int
    resolved: Path
    root: Path
    file_state: tuple[int, int, int, int, int]
    ancestry: tuple[tuple[int, int], ...]


def stage_fasta(
    *,
    fasta_path: str,
    input_roots: tuple[Path, ...],
    job_directory: Path,
    max_sequences: int,
) -> StagedFasta:
    """Stream one allowlisted file into a validated canonical private copy."""
    pinned = _open_allowed_file(Path(fasta_path), input_roots)
    staged_path = job_directory / INPUT_FILENAME
    output_descriptor: int | None = None
    try:
        output_descriptor = _create_private(staged_path)
        with (
            os.fdopen(os.dup(pinned.descriptor), "rb") as source,
            os.fdopen(output_descriptor, "wb", closefd=False) as destination,
        ):
            summary = _validate_fasta_stream(
                source,
                destination,
                max_sequences=max_sequences,
            )
            destination.flush()
            os.fsync(destination.fileno())
        if pinned.file_state != _file_state(os.fstat(pinned.descriptor)):
            raise InputPathError("input file changed during intake")
        _revalidate_pinned_path(pinned)
        return StagedFasta(summary=summary, input_path=pinned.resolved)
    except Exception:
        if output_descriptor is not None:
            _remove_owned_staging_file(staged_path, output_descriptor)
        raise
    finally:
        if output_descriptor is not None:
            os.close(output_descriptor)
        os.close(pinned.descriptor)


async def stage_fasta_in_worker(
    *,
    fasta_path: str,
    input_roots: tuple[Path, ...],
    job_directory: Path,
    max_sequences: int,
) -> StagedFasta:
    """Run intake off the event loop and join it before propagating cancellation."""
    worker = asyncio.create_task(
        run_sync_in_worker_thread(
            partial(
                stage_fasta,
                fasta_path=fasta_path,
                input_roots=input_roots,
                job_directory=job_directory,
                max_sequences=max_sequences,
            )
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError as cancellation:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise cancellation


def validate_fasta_bytes(
    content: bytes,
    *,
    max_sequences: int = MAX_SEQUENCE_COUNT,
) -> tuple[FastaSummary, bytes]:
    """Validate protein FASTA bytes and return canonical ASCII bytes."""
    source = BytesIO(content)
    canonical = BytesIO()
    summary = _validate_fasta_stream(source, canonical, max_sequences=max_sequences)
    return summary, canonical.getvalue()


def _validate_fasta_stream(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    max_sequences: int,
) -> FastaSummary:
    if not 1 <= max_sequences <= MAX_SEQUENCE_COUNT:
        raise ValueError("max_sequences is outside the hard companion limit")
    sequence_ids: set[str] = set()
    lengths: list[int] = []
    current_length: int | None = None
    total_residues = 0
    input_bytes = 0

    try:
        with TextIOWrapper(source, encoding="ascii", newline=None) as text:
            while True:
                raw_line = text.readline(MAX_SEQUENCE_LENGTH + 2)
                if not raw_line:
                    break
                if not raw_line.endswith("\n") and len(raw_line) > MAX_SEQUENCE_LENGTH:
                    raise FastaLimitError("one FASTA line exceeds the structural limit")
                line = raw_line.removesuffix("\n")
                if line.startswith(">"):
                    if current_length is not None:
                        _finish_sequence(current_length, lengths)
                    sequence_id = _validate_header(line[1:])
                    if sequence_id in sequence_ids:
                        raise FastaValidationError("FASTA sequence identifiers must be unique")
                    sequence_ids.add(sequence_id)
                    if len(sequence_ids) > max_sequences:
                        raise FastaLimitError("FASTA exceeds the sequence-count limit")
                    encoded = f"{line}\n".encode("ascii")
                    destination.write(encoded)
                    input_bytes += len(encoded)
                    current_length = 0
                    continue
                if not line:
                    continue
                if current_length is None:
                    raise FastaValidationError("FASTA residues must follow a header")
                if any(character.isspace() or _control(character) for character in line):
                    raise FastaValidationError(
                        "FASTA residue lines contain whitespace or control text"
                    )
                residues = line.upper()
                if any(character not in _PROTEIN_ALPHABET for character in residues):
                    raise FastaValidationError("FASTA contains an invalid protein residue")
                current_length += len(residues)
                total_residues += len(residues)
                if current_length > MAX_SEQUENCE_LENGTH:
                    raise FastaLimitError("one FASTA sequence exceeds the residue limit")
                encoded = f"{residues}\n".encode("ascii")
                destination.write(encoded)
                input_bytes += len(encoded)
    except UnicodeDecodeError as error:
        raise FastaValidationError("FASTA must be ASCII") from error

    if current_length is None:
        raise FastaValidationError("FASTA contains no records")
    _finish_sequence(current_length, lengths)
    return FastaSummary(
        sequence_count=len(lengths),
        total_residues=total_residues,
        max_sequence_length=max(lengths),
        input_bytes=input_bytes,
    )


def _open_allowed_file(
    path: Path,
    allowed_roots: tuple[Path, ...],
) -> _PinnedFasta:
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
        descriptor, ancestry = _open_beneath(resolved, root)
    except OSError as error:
        raise InputPathError("input path cannot be opened safely") from error
    try:
        before = os.fstat(descriptor)
        if _file_state(named) != _file_state(before) or not stat.S_ISREG(before.st_mode):
            raise InputPathError("input path changed before intake")
        if len(str(resolved)) > 4_096:
            raise InputPathError("resolved input path exceeds the provenance limit")
        return _PinnedFasta(
            descriptor=descriptor,
            resolved=resolved,
            root=root,
            file_state=_file_state(before),
            ancestry=ancestry,
        )
    except Exception:
        os.close(descriptor)
        raise


def _revalidate_pinned_path(pinned: _PinnedFasta) -> None:
    descriptor: int | None = None
    try:
        descriptor, ancestry = _open_beneath(pinned.resolved, pinned.root)
        current = os.fstat(descriptor)
    except OSError as error:
        raise InputPathError("input path changed during intake") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if (
        ancestry != pinned.ancestry
        or _file_state(current) != pinned.file_state
        or not stat.S_ISREG(current.st_mode)
    ):
        raise InputPathError("input path changed during intake")


def _open_beneath(path: Path, root: Path) -> tuple[int, tuple[tuple[int, int], ...]]:
    """Open a resolved file by walking from an allowed root without symlinks."""
    parts = path.relative_to(root).parts
    if not parts:
        raise OSError("input path names an allowed directory")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    directories: list[int] = []
    ancestry: list[tuple[int, int]] = []
    try:
        current = os.open(root, directory_flags)
        directories.append(current)
        ancestry.append(_path_identity(os.fstat(current)))
        for component in parts[:-1]:
            current = os.open(component, directory_flags, dir_fd=current)
            directories.append(current)
            ancestry.append(_path_identity(os.fstat(current)))
        return os.open(parts[-1], file_flags, dir_fd=current), tuple(ancestry)
    finally:
        for descriptor in reversed(directories):
            os.close(descriptor)


def _create_private(path: Path) -> int:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, 0o600)


def _remove_owned_staging_file(path: Path, descriptor: int) -> None:
    """Remove only the still-linked staging inode created by this intake."""
    try:
        current = path.lstat()
        created = os.fstat(descriptor)
    except OSError:
        return
    if _path_identity(current) != _path_identity(created):
        return
    try:
        path.unlink()
    except OSError:
        return


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


def _path_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


__all__ = [
    "INPUT_FILENAME",
    "FastaLimitError",
    "FastaValidationError",
    "InputPathError",
    "StagedFasta",
    "stage_fasta",
    "validate_fasta_bytes",
]
