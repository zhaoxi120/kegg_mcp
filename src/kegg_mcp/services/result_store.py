"""Scoped, bounded, user-local storage for immutable analysis artifacts."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, NoReturn, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError, SafeDetail

_SCHEMA_VERSION: Final = 2
_MEBIBYTE: Final = 1024 * 1024
_GIBIBYTE: Final = 1024 * _MEBIBYTE
_SQLITE_MAX_INTEGER: Final = (1 << 63) - 1

DEFAULT_RETENTION_SECONDS: Final = 24 * 60 * 60
DEFAULT_QUOTA_BYTES: Final = 512 * _MEBIBYTE
DEFAULT_MAX_DATABASE_BYTES: Final = 640 * _MEBIBYTE
DEFAULT_MAX_ARTIFACT_BYTES: Final = 128 * _MEBIBYTE
DEFAULT_MAX_RESULT_BYTES: Final = 256 * _MEBIBYTE
DEFAULT_MAX_ARTIFACTS_PER_RESULT: Final = 64
DEFAULT_MAX_RESULTS: Final = 10_000
DEFAULT_PAGE_SIZE: Final = 50
DEFAULT_MAX_PAGE_SIZE: Final = 256
DEFAULT_RANGE_BYTES: Final = 64 * 1024
DEFAULT_MAX_RANGE_BYTES: Final = _MEBIBYTE

HARD_MAX_RETENTION_SECONDS: Final = 30 * 24 * 60 * 60
HARD_MAX_QUOTA_BYTES: Final = 8 * _GIBIBYTE
HARD_MAX_DATABASE_BYTES: Final = 10 * _GIBIBYTE
HARD_MAX_ARTIFACT_BYTES: Final = 256 * _MEBIBYTE
HARD_MAX_RESULT_BYTES: Final = _GIBIBYTE
HARD_MAX_ARTIFACTS_PER_RESULT: Final = 256
HARD_MAX_RESULTS: Final = 1_000_000
HARD_MAX_PAGE_SIZE: Final = 1_000
HARD_MAX_RANGE_BYTES: Final = 8 * _MEBIBYTE

_SCOPE_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SECTION_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_RESULT_ID_PATTERN: Final = re.compile(r"res_[A-Za-z0-9_-]{32}\Z")
_MIME_TYPE_PATTERN: Final = re.compile(
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,126}(?:; charset=utf-8)?\Z"
)
_RESULT_ID_ATTEMPTS: Final = 8

_CREATE_RESULTS = """
CREATE TABLE IF NOT EXISTS stored_results (
    result_id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0),
    artifact_count INTEGER NOT NULL CHECK (artifact_count > 0)
) WITHOUT ROWID
"""

_CREATE_ARTIFACTS = """
CREATE TABLE IF NOT EXISTS result_artifacts (
    result_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    section TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    content BLOB NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    PRIMARY KEY (result_id, section),
    UNIQUE (result_id, position),
    FOREIGN KEY (result_id) REFERENCES stored_results (result_id) ON DELETE CASCADE
) WITHOUT ROWID
"""

_CREATE_EXPIRY_INDEX = """
CREATE INDEX IF NOT EXISTS stored_results_expiry
ON stored_results (expires_at, result_id)
"""

_CREATE_SCOPE_INDEX = """
CREATE INDEX IF NOT EXISTS stored_results_scope_created
ON stored_results (scope_id, created_at DESC, result_id)
"""

_INSERT_RESULT = """
INSERT INTO stored_results (
    result_id,
    scope_id,
    created_at,
    expires_at,
    total_bytes,
    artifact_count
) VALUES (?, ?, ?, ?, ?, ?)
"""

_INSERT_ARTIFACT = """
INSERT INTO result_artifacts (
    result_id,
    position,
    section,
    mime_type,
    content,
    byte_size
) VALUES (?, ?, ?, ?, ?, ?)
"""


class _StoreModel(BaseModel):
    """Strict immutable base for the public result-store contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )


class ResultStoreLimits(_StoreModel):
    """Configurable limits bounded by process-safe hard maxima."""

    retention_seconds: int = Field(
        default=DEFAULT_RETENTION_SECONDS,
        strict=True,
        ge=1,
        le=HARD_MAX_RETENTION_SECONDS,
    )
    quota_bytes: int = Field(
        default=DEFAULT_QUOTA_BYTES,
        strict=True,
        ge=1,
        le=HARD_MAX_QUOTA_BYTES,
    )
    max_database_bytes: int = Field(
        default=DEFAULT_MAX_DATABASE_BYTES,
        strict=True,
        ge=_MEBIBYTE,
        le=HARD_MAX_DATABASE_BYTES,
    )
    max_artifact_bytes: int = Field(
        default=DEFAULT_MAX_ARTIFACT_BYTES,
        strict=True,
        ge=1,
        le=HARD_MAX_ARTIFACT_BYTES,
    )
    max_result_bytes: int = Field(
        default=DEFAULT_MAX_RESULT_BYTES,
        strict=True,
        ge=1,
        le=HARD_MAX_RESULT_BYTES,
    )
    max_artifacts_per_result: int = Field(
        default=DEFAULT_MAX_ARTIFACTS_PER_RESULT,
        strict=True,
        ge=1,
        le=HARD_MAX_ARTIFACTS_PER_RESULT,
    )
    max_results: int = Field(
        default=DEFAULT_MAX_RESULTS,
        strict=True,
        ge=1,
        le=HARD_MAX_RESULTS,
    )
    default_page_size: int = Field(
        default=DEFAULT_PAGE_SIZE,
        strict=True,
        ge=1,
        le=HARD_MAX_PAGE_SIZE,
    )
    max_page_size: int = Field(
        default=DEFAULT_MAX_PAGE_SIZE,
        strict=True,
        ge=1,
        le=HARD_MAX_PAGE_SIZE,
    )
    default_range_bytes: int = Field(
        default=DEFAULT_RANGE_BYTES,
        strict=True,
        ge=1,
        le=HARD_MAX_RANGE_BYTES,
    )
    max_range_bytes: int = Field(
        default=DEFAULT_MAX_RANGE_BYTES,
        strict=True,
        ge=1,
        le=HARD_MAX_RANGE_BYTES,
    )

    @model_validator(mode="after")
    def validate_limit_relationships(self) -> Self:
        """Keep configured defaults and per-item limits internally consistent."""
        if self.max_artifact_bytes > self.max_result_bytes:
            raise ValueError("max_artifact_bytes must not exceed max_result_bytes")
        metadata_reserve = max(
            _MEBIBYTE,
            self.max_results * (4_096 + self.max_artifacts_per_result * 128),
        )
        if self.quota_bytes + metadata_reserve > self.max_database_bytes:
            raise ValueError(
                "quota_bytes must leave bounded metadata space below max_database_bytes"
            )
        if self.default_page_size > self.max_page_size:
            raise ValueError("default_page_size must not exceed max_page_size")
        if self.default_range_bytes > self.max_range_bytes:
            raise ValueError("default_range_bytes must not exceed max_range_bytes")
        return self


class ResultArtifactInput(_StoreModel):
    """One immutable artifact supplied when a result is created."""

    section: str = Field(min_length=1, max_length=128)
    mime_type: str = Field(min_length=3, max_length=255)
    content: bytes = Field(max_length=HARD_MAX_ARTIFACT_BYTES, repr=False)

    @field_validator("section")
    @classmethod
    def validate_section(cls, value: str) -> str:
        return _validate_section_name(value)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _validate_mime_type(value)


class ResultArtifactMetadata(_StoreModel):
    """Safe metadata for one stored artifact without its content."""

    section: str = Field(min_length=1, max_length=128)
    mime_type: str = Field(min_length=3, max_length=255)
    byte_size: int = Field(strict=True, ge=0, le=HARD_MAX_ARTIFACT_BYTES)

    @field_validator("section")
    @classmethod
    def validate_section(cls, value: str) -> str:
        return _validate_section_name(value)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _validate_mime_type(value)


class ResultMetadata(_StoreModel):
    """Metadata for a stored result; the originating scope is intentionally omitted."""

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")
    created_at: datetime
    expires_at: datetime
    total_bytes: int = Field(strict=True, ge=0, le=HARD_MAX_RESULT_BYTES)
    artifact_count: int = Field(
        strict=True,
        ge=1,
        le=HARD_MAX_ARTIFACTS_PER_RESULT,
    )

    @model_validator(mode="after")
    def validate_timestamp_order(self) -> Self:
        if self.created_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("result timestamps must be timezone-aware")
        if self.expires_at <= self.created_at:
            raise ValueError("result expiry must follow creation")
        return self


class ResultMetadataPage(_StoreModel):
    """One bounded page of active results for a single validated scope."""

    items: tuple[ResultMetadata, ...]
    total_items: int = Field(strict=True, ge=0)
    offset: int = Field(strict=True, ge=0)
    limit: int = Field(strict=True, ge=1, le=HARD_MAX_PAGE_SIZE)
    next_offset: int | None = Field(strict=True, ge=0)


class ResultArtifactPage(_StoreModel):
    """One bounded metadata page for sections under an authorized result."""

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")
    items: tuple[ResultArtifactMetadata, ...]
    total_items: int = Field(strict=True, ge=1, le=HARD_MAX_ARTIFACTS_PER_RESULT)
    offset: int = Field(strict=True, ge=0)
    limit: int = Field(strict=True, ge=1, le=HARD_MAX_PAGE_SIZE)
    next_offset: int | None = Field(strict=True, ge=0)


class ResultArtifactRange(_StoreModel):
    """One bounded byte range from an authorized immutable artifact."""

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")
    section: str = Field(min_length=1, max_length=128)
    mime_type: str = Field(min_length=3, max_length=255)
    total_bytes: int = Field(strict=True, ge=0, le=HARD_MAX_ARTIFACT_BYTES)
    offset: int = Field(strict=True, ge=0)
    limit: int = Field(strict=True, ge=1, le=HARD_MAX_RANGE_BYTES)
    returned_bytes: int = Field(strict=True, ge=0, le=HARD_MAX_RANGE_BYTES)
    next_offset: int | None = Field(strict=True, ge=0)
    content: bytes = Field(max_length=HARD_MAX_RANGE_BYTES, repr=False)

    @field_validator("section")
    @classmethod
    def validate_section(cls, value: str) -> str:
        return _validate_section_name(value)

    @field_validator("mime_type")
    @classmethod
    def validate_mime_type(cls, value: str) -> str:
        return _validate_mime_type(value)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if self.returned_bytes != len(self.content):
            raise ValueError("returned_bytes must equal content length")
        expected_next = self.offset + self.returned_bytes
        if self.next_offset is None:
            if expected_next < self.total_bytes:
                raise ValueError("a non-final artifact range requires next_offset")
        elif self.next_offset != expected_next or self.next_offset >= self.total_bytes:
            raise ValueError("next_offset must identify the next unread byte")
        return self


class DeletedResult(_StoreModel):
    """Metadata returned after one authorized explicit deletion."""

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")
    deleted_at: datetime
    deleted_artifacts: int = Field(strict=True, ge=1, le=HARD_MAX_ARTIFACTS_PER_RESULT)
    deleted_bytes: int = Field(strict=True, ge=0, le=HARD_MAX_RESULT_BYTES)


class CleanupSummary(_StoreModel):
    """Counts from deterministic expiry cleanup and quota eviction."""

    expired_results: int = Field(strict=True, ge=0)
    expired_bytes: int = Field(strict=True, ge=0)
    evicted_results: int = Field(strict=True, ge=0)
    evicted_bytes: int = Field(strict=True, ge=0)
    remaining_results: int = Field(strict=True, ge=0)
    remaining_bytes: int = Field(strict=True, ge=0)


class ScopeDeletionSummary(_StoreModel):
    """Counts removed when one process-local result scope is discarded."""

    deleted_results: int = Field(strict=True, ge=0)
    deleted_artifacts: int = Field(strict=True, ge=0)
    deleted_bytes: int = Field(strict=True, ge=0)


class ResultStoreError(RuntimeError):
    """Sanitized internal persistence failure without paths or payload data."""

    def __init__(self, stage: str) -> None:
        super().__init__("The local result store could not be used safely.")
        self.stage = stage


class _ResultStoreIntegrityError(Exception):
    """Internal marker for malformed or incompatible persisted state."""


class SQLiteResultStore:
    """Store immutable artifact groups in a scope-isolated local SQLite database."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        limits: ResultStoreLimits | None = None,
    ) -> None:
        self._configured_path = os.fspath(path)
        self._limits = limits if limits is not None else ResultStoreLimits()

    @property
    def limits(self) -> ResultStoreLimits:
        """Return immutable configured limits for service compatibility checks."""
        return self._limits

    def create(
        self,
        scope_id: str,
        artifacts: tuple[ResultArtifactInput, ...],
        *,
        now: datetime | None = None,
    ) -> ResultMetadata:
        """Atomically store a non-empty ordered artifact group under one opaque ID."""
        checked_scope = _validate_scope_id(scope_id)
        checked_now = _normalize_now(now)
        checked_artifacts, total_bytes = self._validate_artifacts(artifacts)
        expires_at = checked_now + timedelta(seconds=self._limits.retention_seconds)
        created_text = _encode_datetime(checked_now)
        expires_text = _encode_datetime(expires_at)

        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._delete_expired(connection, checked_now)
                    self._require_capacity(
                        connection,
                        incoming_bytes=total_bytes,
                        incoming_results=1,
                    )
                    result_id = self._allocate_result_id(connection)
                    connection.execute(
                        _INSERT_RESULT,
                        (
                            result_id,
                            checked_scope,
                            created_text,
                            expires_text,
                            total_bytes,
                            len(checked_artifacts),
                        ),
                    )
                    for position, artifact in enumerate(checked_artifacts):
                        connection.execute(
                            _INSERT_ARTIFACT,
                            (
                                result_id,
                                position,
                                artifact.section,
                                artifact.mime_type,
                                sqlite3.Binary(artifact.content),
                                len(artifact.content),
                            ),
                        )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("create") from None

        return ResultMetadata(
            result_id=result_id,
            created_at=checked_now,
            expires_at=expires_at,
            total_bytes=total_bytes,
            artifact_count=len(checked_artifacts),
        )

    def get_result(
        self,
        scope_id: str,
        result_id: str,
        *,
        now: datetime | None = None,
    ) -> ResultMetadata:
        """Return active result metadata only for the originating local scope."""
        checked_scope, checked_result = _validate_lookup(scope_id, result_id)
        checked_now = _normalize_now(now)
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT
                        r.result_id,
                        r.created_at,
                        r.expires_at,
                        r.total_bytes,
                        r.artifact_count,
                        COUNT(a.section),
                        COALESCE(SUM(a.byte_size), 0)
                    FROM stored_results AS r
                    LEFT JOIN result_artifacts AS a ON a.result_id = r.result_id
                    WHERE r.scope_id = ?
                      AND r.result_id = ?
                      AND r.expires_at > ?
                    GROUP BY r.result_id
                    """,
                    (checked_scope, checked_result, _encode_datetime(checked_now)),
                ).fetchone()
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("read_metadata") from None
        if row is None:
            _raise_result_not_found()
        try:
            metadata = _decode_result_row(row[:5])
            actual_count = _decode_nonnegative_integer(row[5])
            actual_bytes = _decode_nonnegative_integer(row[6])
            if actual_count != metadata.artifact_count or actual_bytes != metadata.total_bytes:
                raise _ResultStoreIntegrityError("result aggregate metadata mismatch")
            return metadata
        except (TypeError, ValueError, ValidationError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None

    def list_results(
        self,
        scope_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> ResultMetadataPage:
        """List a bounded page of active results for exactly one local scope."""
        try:
            checked_scope = _validate_scope_id(scope_id)
        except (TypeError, ValueError):
            _raise_result_not_found()
        checked_offset, checked_limit = self._validate_page(offset, limit)
        checked_now = _normalize_now(now)
        now_text = _encode_datetime(checked_now)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                total_row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM stored_results
                    WHERE scope_id = ? AND expires_at > ?
                    """,
                    (checked_scope, now_text),
                ).fetchone()
                rows = connection.execute(
                    """
                    SELECT result_id, created_at, expires_at, total_bytes, artifact_count
                    FROM stored_results
                    WHERE scope_id = ? AND expires_at > ?
                    ORDER BY created_at DESC, result_id
                    LIMIT ? OFFSET ?
                    """,
                    (checked_scope, now_text, checked_limit, checked_offset),
                ).fetchall()
                connection.commit()
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("list_results") from None
        try:
            total_items = _decode_count_row(total_row)
            items = tuple(_decode_result_row(row) for row in rows)
        except (TypeError, ValueError, ValidationError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None
        return ResultMetadataPage(
            items=items,
            total_items=total_items,
            offset=checked_offset,
            limit=checked_limit,
            next_offset=_next_offset(checked_offset, len(items), total_items),
        )

    def list_artifacts(
        self,
        scope_id: str,
        result_id: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> ResultArtifactPage:
        """List a bounded page of artifact metadata for an authorized active result."""
        checked_scope, checked_result = _validate_lookup(scope_id, result_id)
        checked_offset, checked_limit = self._validate_page(offset, limit)
        checked_now = _normalize_now(now)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN")
                result_row = connection.execute(
                    """
                    SELECT artifact_count
                    FROM stored_results
                    WHERE scope_id = ? AND result_id = ? AND expires_at > ?
                    """,
                    (checked_scope, checked_result, _encode_datetime(checked_now)),
                ).fetchone()
                if result_row is None:
                    connection.rollback()
                    _raise_result_not_found()
                rows = connection.execute(
                    """
                    SELECT section, mime_type, byte_size
                    FROM result_artifacts
                    WHERE result_id = ?
                    ORDER BY position
                    LIMIT ? OFFSET ?
                    """,
                    (checked_result, checked_limit, checked_offset),
                ).fetchall()
                actual_count_row = connection.execute(
                    "SELECT COUNT(*) FROM result_artifacts WHERE result_id = ?",
                    (checked_result,),
                ).fetchone()
                connection.commit()
        except KeggMcpError:
            raise
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("list_artifacts") from None
        try:
            total_items = _decode_single_positive_integer(result_row)
            actual_count = _decode_count_row(actual_count_row)
            if total_items != actual_count:
                raise _ResultStoreIntegrityError("artifact count mismatch")
            items = tuple(_decode_artifact_metadata(row) for row in rows)
        except (TypeError, ValueError, ValidationError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None
        return ResultArtifactPage(
            result_id=checked_result,
            items=items,
            total_items=total_items,
            offset=checked_offset,
            limit=checked_limit,
            next_offset=_next_offset(checked_offset, len(items), total_items),
        )

    def read_artifact(
        self,
        scope_id: str,
        result_id: str,
        section: str,
        *,
        offset: int = 0,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> ResultArtifactRange:
        """Read one bounded byte range without exposing another scope's artifact."""
        checked_scope, checked_result = _validate_lookup(scope_id, result_id)
        try:
            checked_section = _validate_section_name(section)
        except (TypeError, ValueError):
            _raise_result_not_found()
        checked_offset, checked_limit = self._validate_range(offset, limit)
        checked_now = _normalize_now(now)
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT
                        a.mime_type,
                        a.byte_size,
                        length(a.content),
                        substr(a.content, ? + 1, ?)
                    FROM result_artifacts AS a
                    JOIN stored_results AS r ON r.result_id = a.result_id
                    WHERE r.scope_id = ?
                      AND r.result_id = ?
                      AND r.expires_at > ?
                      AND a.section = ?
                    """,
                    (
                        checked_offset,
                        checked_limit,
                        checked_scope,
                        checked_result,
                        _encode_datetime(checked_now),
                        checked_section,
                    ),
                ).fetchone()
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("read_artifact") from None
        if row is None:
            _raise_result_not_found()
        try:
            if len(row) != 4:
                raise _ResultStoreIntegrityError("unexpected artifact row shape")
            mime_type = _validate_mime_type(row[0])
            total_bytes = _decode_nonnegative_integer(row[1])
            persisted_length = _decode_nonnegative_integer(row[2])
            content = row[3]
            if (
                total_bytes > HARD_MAX_ARTIFACT_BYTES
                or persisted_length != total_bytes
                or not isinstance(content, bytes)
            ):
                raise _ResultStoreIntegrityError("invalid artifact metadata")
        except (TypeError, ValueError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None
        returned_bytes = len(content)
        next_offset = _next_offset(checked_offset, returned_bytes, total_bytes)
        return ResultArtifactRange(
            result_id=checked_result,
            section=checked_section,
            mime_type=mime_type,
            total_bytes=total_bytes,
            offset=checked_offset,
            limit=checked_limit,
            returned_bytes=returned_bytes,
            next_offset=next_offset,
            content=content,
        )

    def delete(
        self,
        scope_id: str,
        result_id: str,
        *,
        now: datetime | None = None,
    ) -> DeletedResult:
        """Delete one active result only when its originating scope is supplied."""
        checked_scope, checked_result = _validate_lookup(scope_id, result_id)
        checked_now = _normalize_now(now)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        """
                        SELECT total_bytes, artifact_count
                        FROM stored_results
                        WHERE scope_id = ? AND result_id = ? AND expires_at > ?
                        """,
                        (checked_scope, checked_result, _encode_datetime(checked_now)),
                    ).fetchone()
                    if row is None:
                        connection.rollback()
                        _raise_result_not_found()
                    connection.execute(
                        "DELETE FROM stored_results WHERE scope_id = ? AND result_id = ?",
                        (checked_scope, checked_result),
                    )
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
        except KeggMcpError:
            raise
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("delete") from None
        try:
            deleted_bytes = _decode_nonnegative_integer(row[0])
            deleted_artifacts = _decode_positive_integer(row[1])
        except (TypeError, ValueError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None
        return DeletedResult(
            result_id=checked_result,
            deleted_at=checked_now,
            deleted_artifacts=deleted_artifacts,
            deleted_bytes=deleted_bytes,
        )

    def delete_scope(self, scope_id: str) -> ScopeDeletionSummary:
        """Delete every retained result owned by one validated process-local scope."""
        checked_scope = _validate_scope_id(scope_id)
        try:
            if not self._configured_store_exists():
                return ScopeDeletionSummary(
                    deleted_results=0,
                    deleted_artifacts=0,
                    deleted_bytes=0,
                )
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    row = connection.execute(
                        """
                        SELECT
                            COUNT(*),
                            COALESCE(SUM(artifact_count), 0),
                            COALESCE(SUM(total_bytes), 0)
                        FROM stored_results
                        WHERE scope_id = ?
                        """,
                        (checked_scope,),
                    ).fetchone()
                    connection.execute(
                        "DELETE FROM stored_results WHERE scope_id = ?",
                        (checked_scope,),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("delete_scope") from None
        try:
            if row is None or len(row) != 3:
                raise _ResultStoreIntegrityError("unexpected scope summary row shape")
            deleted_results = _decode_nonnegative_integer(row[0])
            deleted_artifacts = _decode_nonnegative_integer(row[1])
            deleted_bytes = _decode_nonnegative_integer(row[2])
        except (TypeError, ValueError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None
        return ScopeDeletionSummary(
            deleted_results=deleted_results,
            deleted_artifacts=deleted_artifacts,
            deleted_bytes=deleted_bytes,
        )

    def cleanup_expired(self, *, now: datetime | None = None) -> CleanupSummary:
        """Delete only TTL-expired results without evicting active results for quota."""
        checked_now = _normalize_now(now)
        try:
            if not self._configured_store_exists():
                return CleanupSummary(
                    expired_results=0,
                    expired_bytes=0,
                    evicted_results=0,
                    evicted_bytes=0,
                    remaining_results=0,
                    remaining_bytes=0,
                )
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    expired_results, expired_bytes = self._delete_expired(
                        connection,
                        checked_now,
                    )
                    remaining_row = connection.execute(
                        "SELECT COUNT(*), COALESCE(SUM(total_bytes), 0) FROM stored_results"
                    ).fetchone()
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("cleanup_expired") from None
        try:
            remaining_results, remaining_bytes = _decode_count_and_bytes(remaining_row)
        except (TypeError, ValueError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None
        return CleanupSummary(
            expired_results=expired_results,
            expired_bytes=expired_bytes,
            evicted_results=0,
            evicted_bytes=0,
            remaining_results=remaining_results,
            remaining_bytes=remaining_bytes,
        )

    def cleanup(self, *, now: datetime | None = None) -> CleanupSummary:
        """Delete expired results, then evict oldest active results over quota."""
        checked_now = _normalize_now(now)
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    expired_results, expired_bytes = self._delete_expired(
                        connection,
                        checked_now,
                    )
                    evicted_results, evicted_bytes = self._evict_to_fit(
                        connection,
                        incoming_bytes=0,
                        incoming_results=0,
                    )
                    remaining_row = connection.execute(
                        "SELECT COUNT(*), COALESCE(SUM(total_bytes), 0) FROM stored_results"
                    ).fetchone()
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except (OSError, sqlite3.Error, _ResultStoreIntegrityError):
            raise ResultStoreError("cleanup") from None
        try:
            remaining_results, remaining_bytes = _decode_count_and_bytes(remaining_row)
        except (TypeError, ValueError, _ResultStoreIntegrityError):
            raise ResultStoreError("integrity_check") from None
        return CleanupSummary(
            expired_results=expired_results,
            expired_bytes=expired_bytes,
            evicted_results=evicted_results,
            evicted_bytes=evicted_bytes,
            remaining_results=remaining_results,
            remaining_bytes=remaining_bytes,
        )

    def _validate_artifacts(
        self,
        artifacts: object,
    ) -> tuple[tuple[ResultArtifactInput, ...], int]:
        if not isinstance(artifacts, tuple):
            raise TypeError("artifacts must be a tuple")
        typed_artifacts = cast(tuple[object, ...], artifacts)
        if not typed_artifacts:
            raise ValueError("at least one result artifact is required")
        if len(typed_artifacts) > self._limits.max_artifacts_per_result:
            _raise_input_limit(
                "artifact_count",
                len(typed_artifacts),
                self._limits.max_artifacts_per_result,
            )
        checked: list[ResultArtifactInput] = []
        sections: set[str] = set()
        total_bytes = 0
        for artifact in typed_artifacts:
            if not isinstance(artifact, ResultArtifactInput):
                raise TypeError("every artifact must be a ResultArtifactInput")
            if artifact.section in sections:
                raise ValueError("artifact section names must be unique within a result")
            sections.add(artifact.section)
            artifact_bytes = len(artifact.content)
            if artifact_bytes > self._limits.max_artifact_bytes:
                _raise_input_limit(
                    "artifact_bytes",
                    artifact_bytes,
                    self._limits.max_artifact_bytes,
                )
            total_bytes += artifact_bytes
            checked.append(artifact)
        effective_result_limit = min(
            self._limits.max_result_bytes,
            self._limits.quota_bytes,
        )
        if total_bytes > effective_result_limit:
            _raise_input_limit("result_bytes", total_bytes, effective_result_limit)
        return tuple(checked), total_bytes

    def _validate_page(self, offset: object, limit: object) -> tuple[int, int]:
        checked_limit = self._limits.default_page_size if limit is None else limit
        validated_limit = _validate_positive_limit(
            checked_limit,
            maximum=self._limits.max_page_size,
            limit_name="page_size",
        )
        checked_offset = _validate_offset(
            offset,
            maximum=_SQLITE_MAX_INTEGER - validated_limit,
        )
        return checked_offset, validated_limit

    def _validate_range(self, offset: object, limit: object) -> tuple[int, int]:
        checked_limit = self._limits.default_range_bytes if limit is None else limit
        validated_limit = _validate_positive_limit(
            checked_limit,
            maximum=self._limits.max_range_bytes,
            limit_name="range_bytes",
        )
        checked_offset = _validate_offset(
            offset,
            maximum=_SQLITE_MAX_INTEGER - validated_limit,
        )
        return checked_offset, validated_limit

    def _allocate_result_id(self, connection: sqlite3.Connection) -> str:
        for _ in range(_RESULT_ID_ATTEMPTS):
            candidate = f"res_{secrets.token_urlsafe(24)}"
            if not _RESULT_ID_PATTERN.fullmatch(candidate):
                raise _ResultStoreIntegrityError("invalid generated result identifier")
            row = connection.execute(
                "SELECT 1 FROM stored_results WHERE result_id = ?",
                (candidate,),
            ).fetchone()
            if row is None:
                return candidate
        raise _ResultStoreIntegrityError("could not allocate a unique result identifier")

    def _delete_expired(
        self,
        connection: sqlite3.Connection,
        now: datetime,
    ) -> tuple[int, int]:
        rows = connection.execute(
            """
            SELECT result_id, total_bytes
            FROM stored_results
            WHERE expires_at <= ?
            ORDER BY expires_at, result_id
            """,
            (_encode_datetime(now),),
        ).fetchall()
        decoded = _decode_result_id_and_bytes_rows(rows)
        for result_id, _ in decoded:
            connection.execute(
                "DELETE FROM stored_results WHERE result_id = ?",
                (result_id,),
            )
        return len(decoded), sum(byte_size for _, byte_size in decoded)

    def _evict_to_fit(
        self,
        connection: sqlite3.Connection,
        *,
        incoming_bytes: int,
        incoming_results: int,
    ) -> tuple[int, int]:
        total_row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_bytes), 0) FROM stored_results"
        ).fetchone()
        current_results, current_bytes = _decode_count_and_bytes(total_row)
        if (
            current_bytes + incoming_bytes <= self._limits.quota_bytes
            and current_results + incoming_results <= self._limits.max_results
        ):
            return 0, 0
        rows = connection.execute(
            """
            SELECT result_id, total_bytes
            FROM stored_results
            ORDER BY created_at, result_id
            """
        ).fetchall()
        decoded = _decode_result_id_and_bytes_rows(rows)
        evicted_results = 0
        evicted_bytes = 0
        for result_id, byte_size in decoded:
            if (
                current_bytes + incoming_bytes <= self._limits.quota_bytes
                and current_results + incoming_results <= self._limits.max_results
            ):
                break
            connection.execute(
                "DELETE FROM stored_results WHERE result_id = ?",
                (result_id,),
            )
            current_bytes -= byte_size
            current_results -= 1
            evicted_results += 1
            evicted_bytes += byte_size
        if (
            current_bytes + incoming_bytes > self._limits.quota_bytes
            or current_results + incoming_results > self._limits.max_results
        ):
            raise _ResultStoreIntegrityError("quota could not be satisfied atomically")
        return evicted_results, evicted_bytes

    def _require_capacity(
        self,
        connection: sqlite3.Connection,
        *,
        incoming_bytes: int,
        incoming_results: int,
    ) -> None:
        total_row = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(total_bytes), 0) FROM stored_results"
        ).fetchone()
        current_results, current_bytes = _decode_count_and_bytes(total_row)
        if current_bytes + incoming_bytes > self._limits.quota_bytes:
            _raise_store_capacity(
                "quota_bytes",
                current_bytes + incoming_bytes,
                self._limits.quota_bytes,
            )
        if current_results + incoming_results > self._limits.max_results:
            _raise_store_capacity(
                "max_results",
                current_results + incoming_results,
                self._limits.max_results,
            )

    def _connect(self) -> sqlite3.Connection:
        path = self._prepare_location()
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise OSError("result store is not a regular file")
            if hasattr(os, "geteuid") and descriptor_stat.st_uid != os.geteuid():
                raise OSError("result store must be owned by the current user")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            connection = sqlite3.connect(path, timeout=5.0)
            path_stat = path.stat(follow_symlinks=False)
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                connection.close()
                raise OSError("result store changed while it was opened")
        finally:
            os.close(descriptor)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("PRAGMA journal_mode = DELETE")
            page_size = _decode_single_positive_integer(
                connection.execute("PRAGMA page_size").fetchone()
            )
            maximum_pages = self._limits.max_database_bytes // page_size
            configured_maximum_pages = _decode_single_positive_integer(
                connection.execute(f"PRAGMA max_page_count = {maximum_pages}").fetchone()
            )
            if configured_maximum_pages > maximum_pages:
                raise _ResultStoreIntegrityError("database already exceeds its physical limit")
            version_row = connection.execute("PRAGMA user_version").fetchone()
            if version_row is None or len(version_row) != 1:
                raise _ResultStoreIntegrityError("missing SQLite schema version")
            schema_version = version_row[0]
            if not isinstance(schema_version, int) or schema_version not in {
                0,
                _SCHEMA_VERSION,
            }:
                raise _ResultStoreIntegrityError("unsupported SQLite schema version")
            auto_vacuum_row = connection.execute("PRAGMA auto_vacuum").fetchone()
            auto_vacuum_mode = _decode_single_nonnegative_integer(auto_vacuum_row)
            if auto_vacuum_mode != 1:
                connection.execute("PRAGMA auto_vacuum = FULL")
                if schema_version != 0:
                    connection.execute("VACUUM")
                auto_vacuum_row = connection.execute("PRAGMA auto_vacuum").fetchone()
                if _decode_single_nonnegative_integer(auto_vacuum_row) != 1:
                    raise _ResultStoreIntegrityError("full auto-vacuum could not be enabled")
            with connection:
                connection.execute(_CREATE_RESULTS)
                connection.execute(_CREATE_ARTIFACTS)
                connection.execute(_CREATE_EXPIRY_INDEX)
                connection.execute(_CREATE_SCOPE_INDEX)
                if schema_version == 0:
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except BaseException:
            connection.close()
            raise
        _tighten_file_permissions(path)
        return connection

    def _configured_store_exists(self) -> bool:
        configured = Path(self._configured_path).expanduser()
        if ".." in configured.parts:
            raise OSError("result store path must not contain traversal components")
        path = configured if configured.is_absolute() else Path.cwd() / configured
        return os.path.lexists(path)

    def _prepare_location(self) -> Path:
        configured = Path(self._configured_path).expanduser()
        if ".." in configured.parts:
            raise OSError("result store path must not contain traversal components")
        path = configured if configured.is_absolute() else Path.cwd() / configured
        parent = path.parent
        missing_directories: list[Path] = []
        candidate = parent
        while not candidate.exists() and candidate != candidate.parent:
            missing_directories.append(candidate)
            candidate = candidate.parent
        _reject_symlink_components(candidate)
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in missing_directories:
            _tighten_directory_permissions(directory)
        _reject_symlink_components(parent)
        parent_stat = parent.lstat()
        if not stat.S_ISDIR(parent_stat.st_mode) or stat.S_ISLNK(parent_stat.st_mode):
            raise OSError("result store parent must be a real directory")
        if hasattr(os, "geteuid") and parent_stat.st_uid != os.geteuid():
            raise OSError("result store parent must be owned by the current user")
        if stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise OSError("result store parent must not be group- or world-writable")
        return path


def compensate_created_result(
    result_store: SQLiteResultStore,
    scope_id: str,
    result_id: str,
    created_at: datetime,
) -> None:
    """Compensate a just-created result when a later durable write fails."""
    try:
        result_store.delete(scope_id, result_id, now=created_at)
    except Exception as error:
        raise RuntimeError("retained-result compensation failed") from error


def _validate_scope_id(value: object) -> str:
    if not isinstance(value, str) or not _SCOPE_PATTERN.fullmatch(value):
        raise ValueError("invalid result scope identifier")
    if value in {".", ".."}:
        raise ValueError("invalid result scope identifier")
    return value


def _validate_section_name(value: object) -> str:
    if not isinstance(value, str) or not _SECTION_PATTERN.fullmatch(value):
        raise ValueError("invalid result section name")
    if value in {".", ".."}:
        raise ValueError("invalid result section name")
    return value


def _validate_mime_type(value: object) -> str:
    if not isinstance(value, str) or not _MIME_TYPE_PATTERN.fullmatch(value):
        raise ValueError("invalid artifact MIME type")
    return value


def _validate_lookup(scope_id: object, result_id: object) -> tuple[str, str]:
    try:
        checked_scope = _validate_scope_id(scope_id)
        if not isinstance(result_id, str) or not _RESULT_ID_PATTERN.fullmatch(result_id):
            raise ValueError("invalid result identifier")
        return checked_scope, result_id
    except (TypeError, ValueError):
        _raise_result_not_found()


def _normalize_now(value: object) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("result store timestamps must be timezone-aware datetimes")
    return value.astimezone(UTC)


def _encode_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decode_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise _ResultStoreIntegrityError("invalid persisted timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _ResultStoreIntegrityError("invalid persisted timestamp") from error
    if parsed.utcoffset() is None:
        raise _ResultStoreIntegrityError("persisted timestamp is not timezone-aware")
    return parsed.astimezone(UTC)


def _validate_offset(value: object, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("offset must be a non-negative integer")
    if value > maximum:
        _raise_input_limit("offset", maximum + 1, maximum)
    return value


def _validate_positive_limit(value: object, *, maximum: int, limit_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{limit_name} must be a positive integer")
    if value > maximum:
        _raise_input_limit(limit_name, value, maximum)
    return value


def _next_offset(offset: int, returned_items: int, total_items: int) -> int | None:
    next_offset = offset + returned_items
    return next_offset if returned_items > 0 and next_offset < total_items else None


def _decode_result_row(row: tuple[object, ...]) -> ResultMetadata:
    if len(row) != 5:
        raise _ResultStoreIntegrityError("unexpected result row shape")
    result_id, created_raw, expires_raw, total_raw, count_raw = row
    if not isinstance(result_id, str) or not _RESULT_ID_PATTERN.fullmatch(result_id):
        raise _ResultStoreIntegrityError("invalid persisted result identifier")
    return ResultMetadata(
        result_id=result_id,
        created_at=_decode_datetime(created_raw),
        expires_at=_decode_datetime(expires_raw),
        total_bytes=_decode_nonnegative_integer(total_raw),
        artifact_count=_decode_positive_integer(count_raw),
    )


def _decode_artifact_metadata(row: tuple[object, ...]) -> ResultArtifactMetadata:
    if len(row) != 3:
        raise _ResultStoreIntegrityError("unexpected artifact metadata row shape")
    section, mime_type, byte_size = row
    return ResultArtifactMetadata(
        section=_validate_section_name(section),
        mime_type=_validate_mime_type(mime_type),
        byte_size=_decode_nonnegative_integer(byte_size),
    )


def _decode_nonnegative_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _ResultStoreIntegrityError("invalid non-negative integer")
    return value


def _decode_positive_integer(value: object) -> int:
    decoded = _decode_nonnegative_integer(value)
    if decoded == 0:
        raise _ResultStoreIntegrityError("invalid positive integer")
    return decoded


def _decode_count_row(row: tuple[object, ...] | None) -> int:
    if row is None or len(row) != 1:
        raise _ResultStoreIntegrityError("invalid count result")
    return _decode_nonnegative_integer(row[0])


def _decode_single_nonnegative_integer(row: tuple[object, ...] | None) -> int:
    return _decode_count_row(row)


def _decode_single_positive_integer(row: tuple[object, ...] | None) -> int:
    if row is None or len(row) != 1:
        raise _ResultStoreIntegrityError("invalid count result")
    return _decode_positive_integer(row[0])


def _decode_count_and_bytes(row: tuple[object, ...] | None) -> tuple[int, int]:
    if row is None or len(row) != 2:
        raise _ResultStoreIntegrityError("invalid aggregate result")
    return _decode_nonnegative_integer(row[0]), _decode_nonnegative_integer(row[1])


def _decode_result_id_and_bytes_rows(
    rows: list[tuple[object, ...]],
) -> tuple[tuple[str, int], ...]:
    decoded: list[tuple[str, int]] = []
    for row in rows:
        if len(row) != 2:
            raise _ResultStoreIntegrityError("invalid cleanup row")
        result_id, byte_size = row
        if not isinstance(result_id, str) or not _RESULT_ID_PATTERN.fullmatch(result_id):
            raise _ResultStoreIntegrityError("invalid persisted result identifier")
        decoded.append((result_id, _decode_nonnegative_integer(byte_size)))
    return tuple(decoded)


def _reject_symlink_components(path: Path) -> None:
    """Reject every existing symlink or non-directory parent component."""
    if not path.is_absolute():
        raise OSError("result store parent must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise OSError("result store parent must not contain symlinks")
        if not stat.S_ISDIR(component_stat.st_mode):
            raise OSError("result store parent components must be directories")


def _tighten_directory_permissions(path: Path) -> None:
    try:
        path_stat = path.lstat()
        if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
            path.chmod(0o700)
    except OSError:
        pass


def _tighten_file_permissions(path: Path) -> None:
    try:
        path_stat = path.lstat()
        if stat.S_ISREG(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
            path.chmod(0o600)
    except OSError:
        pass


def _raise_input_limit(limit_name: str, actual: int, maximum: int) -> NoReturn:
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.INPUT_LIMIT_EXCEEDED,
            message="The requested result-store operation exceeds a configured limit.",
            recoverable=True,
            suggested_action="Reduce the result size or request a smaller page before retrying.",
            safe_details=(
                SafeDetail(name="limit", value=limit_name),
                SafeDetail(name="actual", value=str(actual)),
                SafeDetail(name="maximum", value=str(maximum)),
            ),
        )
    )


def _raise_store_capacity(limit_name: str, projected: int, maximum: int) -> NoReturn:
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.INPUT_LIMIT_EXCEEDED,
            message="The local result store has insufficient retained-result capacity.",
            recoverable=True,
            suggested_action=(
                "Delete retained results in the authorized scope, run operator cleanup, or use "
                "a smaller bounded result."
            ),
            safe_details=(
                SafeDetail(name="limit", value=limit_name),
                SafeDetail(name="projected", value=str(projected)),
                SafeDetail(name="maximum", value=str(maximum)),
            ),
        )
    )


def _raise_result_not_found() -> NoReturn:
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.RESULT_NOT_FOUND,
            message="The requested active result is unavailable in this local scope.",
            recoverable=True,
            suggested_action="Use an active result identifier created in the current local scope.",
            safe_details=(),
        )
    )


__all__ = [
    "DEFAULT_MAX_ARTIFACTS_PER_RESULT",
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "DEFAULT_MAX_DATABASE_BYTES",
    "DEFAULT_MAX_PAGE_SIZE",
    "DEFAULT_MAX_RANGE_BYTES",
    "DEFAULT_MAX_RESULTS",
    "DEFAULT_MAX_RESULT_BYTES",
    "DEFAULT_PAGE_SIZE",
    "DEFAULT_QUOTA_BYTES",
    "DEFAULT_RANGE_BYTES",
    "DEFAULT_RETENTION_SECONDS",
    "CleanupSummary",
    "DeletedResult",
    "ResultArtifactInput",
    "ResultArtifactMetadata",
    "ResultArtifactPage",
    "ResultArtifactRange",
    "ResultMetadata",
    "ResultMetadataPage",
    "ResultStoreError",
    "ResultStoreLimits",
    "SQLiteResultStore",
    "ScopeDeletionSummary",
    "compensate_created_result",
]
