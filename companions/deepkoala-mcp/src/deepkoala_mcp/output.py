"""Validation and bounded access for DeepKOALA detailed CSV output."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REQUIRED_DETAILED_COLUMNS: Final = (
    "name",
    "predict_label",
    "probability",
    "threshold",
    "annotate",
)
MAX_OUTPUT_BYTES: Final = 5_000_000
MAX_OUTPUT_ROWS: Final = 100_000
MAX_OUTPUT_COLUMNS: Final = 64
MAX_OUTPUT_FIELD_CHARACTERS: Final = 16_384
MAX_RESOURCE_RANGE_BYTES: Final = 1_048_576


class OutputValidationError(Exception):
    """A safe output-validation failure without external diagnostic content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DetailedCsvSummary:
    """Safe immutable facts about one validated detailed CSV artifact."""

    byte_size: int
    row_count: int
    column_count: int
    sha256: str


def validate_detailed_csv(
    path: Path,
    *,
    max_rows: int,
    max_bytes: int = MAX_OUTPUT_BYTES,
) -> DetailedCsvSummary:
    """Validate a private regular UTF-8 detailed CSV without applying a decision policy."""
    if max_rows < 1 or max_rows > MAX_OUTPUT_ROWS:
        raise ValueError("max_rows is outside the supported output bound")
    payload = _read_regular_file(path, max_bytes=max_bytes)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OutputValidationError(
            "OUTPUT_INVALID", "Runner output is not valid UTF-8."
        ) from error

    try:
        rows = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(rows)
    except (csv.Error, StopIteration) as error:
        raise OutputValidationError(
            "OUTPUT_INVALID", "Runner output does not contain a valid CSV header."
        ) from error
    if (
        not header
        or any(not field for field in header)
        or len(header) > MAX_OUTPUT_COLUMNS
        or len(header) != len(set(header))
    ):
        raise OutputValidationError(
            "OUTPUT_INVALID", "Runner output has an invalid or duplicate CSV header."
        )
    missing = tuple(column for column in REQUIRED_DETAILED_COLUMNS if column not in header)
    if missing:
        raise OutputValidationError(
            "OUTPUT_INVALID", "Runner output is not the required DeepKOALA detailed format."
        )
    if any(len(field) > MAX_OUTPUT_FIELD_CHARACTERS for field in header):
        raise OutputValidationError("OUTPUT_LIMIT_EXCEEDED", "Runner output header is oversized.")

    required_indexes = {column: header.index(column) for column in REQUIRED_DETAILED_COLUMNS}
    row_count = 0
    try:
        for row in rows:
            row_count += 1
            if row_count > max_rows:
                raise OutputValidationError(
                    "OUTPUT_LIMIT_EXCEEDED", "Runner output contains too many prediction rows."
                )
            if len(row) != len(header):
                raise OutputValidationError(
                    "OUTPUT_INVALID",
                    "Runner output contains a row with an invalid field count.",
                )
            if len(row) > MAX_OUTPUT_COLUMNS:
                raise OutputValidationError(
                    "OUTPUT_LIMIT_EXCEEDED", "Runner output contains too many columns."
                )
            if any(len(field) > MAX_OUTPUT_FIELD_CHARACTERS for field in row):
                raise OutputValidationError(
                    "OUTPUT_LIMIT_EXCEEDED", "Runner output contains an oversized field."
                )
            if any(
                not row[required_indexes[column]].strip()
                for column in ("name", "predict_label", "probability", "threshold")
            ):
                raise OutputValidationError(
                    "OUTPUT_INVALID", "Runner output contains a missing required value."
                )
    except csv.Error as error:
        raise OutputValidationError(
            "OUTPUT_INVALID", "Runner output contains malformed CSV."
        ) from error
    if row_count < 1:
        raise OutputValidationError("OUTPUT_INVALID", "Runner output contains no prediction rows.")
    return DetailedCsvSummary(
        byte_size=len(payload),
        row_count=row_count,
        column_count=len(header),
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def read_artifact_range(path: Path, *, offset: int, limit: int) -> tuple[bytes, int, int | None]:
    """Read one bounded range from a private regular artifact."""
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit < 1 or limit > MAX_RESOURCE_RANGE_BYTES:
        raise ValueError("limit is outside the resource range bound")
    descriptor, size = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        if offset > size:
            raise ValueError("offset exceeds artifact size")
        os.lseek(descriptor, offset, os.SEEK_SET)
        content = os.read(descriptor, min(limit, size - offset))
        _assert_stable_file(path, descriptor, before)
    finally:
        os.close(descriptor)
    next_offset = offset + len(content) if offset + len(content) < size else None
    return content, size, next_offset


def read_hashed_artifact_range(
    path: Path,
    *,
    offset: int,
    limit: int,
) -> tuple[bytes, int, int | None, str]:
    """Read a range and hash the same stable descriptor before returning it."""
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit < 1 or limit > MAX_RESOURCE_RANGE_BYTES:
        raise ValueError("limit is outside the resource range bound")
    descriptor, size = _open_regular_file(path)
    try:
        if offset > size:
            raise ValueError("offset exceeds artifact size")
        before = os.fstat(descriptor)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65_536):
            digest.update(chunk)
        _assert_stable_file(path, descriptor, before)
        os.lseek(descriptor, offset, os.SEEK_SET)
        content = os.read(descriptor, min(limit, size - offset))
        _assert_stable_file(path, descriptor, before)
    finally:
        os.close(descriptor)
    next_offset = offset + len(content) if offset + len(content) < size else None
    return content, size, next_offset, digest.hexdigest()


def _read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    descriptor, size = _open_regular_file(path)
    try:
        before = os.fstat(descriptor)
        if size < 1:
            raise OutputValidationError("OUTPUT_INVALID", "Runner output is empty.")
        if size > max_bytes:
            raise OutputValidationError(
                "OUTPUT_LIMIT_EXCEEDED", "Runner output exceeds the import handoff byte limit."
            )
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(65_536, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        if len(payload) != size or len(payload) > max_bytes:
            raise OutputValidationError(
                "OUTPUT_LIMIT_EXCEEDED", "Runner output changed or exceeded its byte limit."
            )
        _assert_stable_file(path, descriptor, before)
        return bytes(payload)
    finally:
        os.close(descriptor)


def _open_regular_file(path: Path) -> tuple[int, int]:
    try:
        before = path.lstat()
    except OSError as error:
        raise OutputValidationError("OUTPUT_INVALID", "Runner output is unavailable.") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise OutputValidationError("OUTPUT_INVALID", "Runner output is not a regular file.")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise OutputValidationError(
            "OUTPUT_INVALID", "Runner output could not be opened safely."
        ) from error
    try:
        opened = os.fstat(descriptor)
        after = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or _file_state(before) != _file_state(opened)
            or _file_state(opened) != _file_state(after)
        ):
            raise OutputValidationError(
                "OUTPUT_INVALID", "Runner output changed during validation."
            )
        return descriptor, opened.st_size
    except Exception:
        os.close(descriptor)
        raise


def _assert_stable_file(path: Path, descriptor: int, before: os.stat_result) -> None:
    after = os.fstat(descriptor)
    try:
        named = path.lstat()
    except OSError as error:
        raise OutputValidationError(
            "OUTPUT_INVALID", "Retained artifact changed during read."
        ) from error
    if (
        not stat.S_ISREG(after.st_mode)
        or stat.S_ISLNK(named.st_mode)
        or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        or (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino)
        or _file_state(before) != _file_state(after)
        or _file_state(after) != _file_state(named)
    ):
        raise OutputValidationError("OUTPUT_INVALID", "Retained artifact changed during read.")


def _file_state(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


__all__ = [
    "MAX_OUTPUT_BYTES",
    "MAX_OUTPUT_COLUMNS",
    "MAX_OUTPUT_FIELD_CHARACTERS",
    "MAX_OUTPUT_ROWS",
    "MAX_RESOURCE_RANGE_BYTES",
    "DetailedCsvSummary",
    "OutputValidationError",
    "read_artifact_range",
    "read_hashed_artifact_range",
    "validate_detailed_csv",
]
