"""Parser-validated user-local SQLite cache for KEGG response payloads."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn, cast

from pydantic import ValidationError

from kegg_mcp._sqlite_security import (
    prepare_private_parent,
    tighten_file_permissions,
    validate_private_directory,
)
from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError, SafeDetail
from kegg_mcp.kegg.contracts import (
    DEFAULT_CACHE_MAX_DATABASE_BYTES,
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_MAX_PAYLOAD_BYTES,
    MAX_HTTP_METADATA_ITEMS,
    HttpMetadata,
    KeggOperation,
    RetrievalEndpointClass,
)

_SCHEMA_VERSION: Final = 3
_MAX_REQUEST_KEY_CHARACTERS: Final = 65_536
_PARSER_VERSION_PATTERN: Final = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_ENDPOINT_FINGERPRINT_PATTERN: Final = re.compile(r"[a-f0-9]{64}\Z")
_HTTP_METADATA_ALLOWLIST: Final = frozenset({"content-type", "date", "etag", "last-modified"})


def _read_only_descriptor_path(descriptor: int) -> Path:
    """Return the native descriptor filesystem path used for race-bound SQLite opens."""
    if sys.platform.startswith("linux"):
        return Path("/proc/self/fd") / str(descriptor)
    if sys.platform == "darwin":
        return Path("/dev/fd") / str(descriptor)
    raise OSError("cache descriptor binding is unavailable")


_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kegg_responses (
    operation TEXT NOT NULL,
    normalized_request_key TEXT NOT NULL,
    endpoint_class TEXT NOT NULL,
    endpoint_fingerprint TEXT NOT NULL,
    response_body BLOB NOT NULL,
    retrieved_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    database_release TEXT,
    http_metadata_json TEXT NOT NULL,
    PRIMARY KEY (
        operation,
        normalized_request_key,
        endpoint_class,
        endpoint_fingerprint
    )
) WITHOUT ROWID
"""

_READ_RESPONSE = """
SELECT
    response_body,
    retrieved_at,
    expires_at,
    parser_version,
    database_release,
    http_metadata_json
FROM kegg_responses
WHERE operation = ?
  AND normalized_request_key = ?
  AND endpoint_class = ?
  AND endpoint_fingerprint = ?
"""

_UPSERT_RESPONSE = """
INSERT INTO kegg_responses (
    operation,
    normalized_request_key,
    endpoint_class,
    endpoint_fingerprint,
    response_body,
    retrieved_at,
    expires_at,
    parser_version,
    database_release,
    http_metadata_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (
    operation,
    normalized_request_key,
    endpoint_class,
    endpoint_fingerprint
) DO UPDATE SET
    response_body = excluded.response_body,
    retrieved_at = excluded.retrieved_at,
    expires_at = excluded.expires_at,
    parser_version = excluded.parser_version,
    database_release = excluded.database_release,
    http_metadata_json = excluded.http_metadata_json
"""

_EXPECTED_CREATE_SCHEMA = _CREATE_SCHEMA.lstrip().replace(
    "CREATE TABLE IF NOT EXISTS",
    "CREATE TABLE",
    1,
)
_EXPECTED_SCHEMA_OBJECTS = (("table", "kegg_responses", "kegg_responses", _EXPECTED_CREATE_SCHEMA),)
_EXPECTED_TABLE_LIST = (("main", "kegg_responses", "table", 10, 1, 0),)
_EXPECTED_TABLE_XINFO = (
    (0, "operation", "TEXT", 1, None, 1, 0),
    (1, "normalized_request_key", "TEXT", 1, None, 2, 0),
    (2, "endpoint_class", "TEXT", 1, None, 3, 0),
    (3, "endpoint_fingerprint", "TEXT", 1, None, 4, 0),
    (4, "response_body", "BLOB", 1, None, 0, 0),
    (5, "retrieved_at", "TEXT", 1, None, 0, 0),
    (6, "expires_at", "TEXT", 1, None, 0, 0),
    (7, "parser_version", "TEXT", 1, None, 0, 0),
    (8, "database_release", "TEXT", 0, None, 0, 0),
    (9, "http_metadata_json", "TEXT", 1, None, 0, 0),
)
_EXPECTED_INDEX_LIST = ((0, "sqlite_autoindex_kegg_responses_1", 1, "pk", 0),)
_EXPECTED_INDEX_XINFO = (
    (0, 0, "operation", 0, "BINARY", 1),
    (1, 1, "normalized_request_key", 0, "BINARY", 1),
    (2, 2, "endpoint_class", 0, "BINARY", 1),
    (3, 3, "endpoint_fingerprint", 0, "BINARY", 1),
    (4, 4, "response_body", 0, "BINARY", 0),
    (5, 5, "retrieved_at", 0, "BINARY", 0),
    (6, 6, "expires_at", 0, "BINARY", 0),
    (7, 7, "parser_version", 0, "BINARY", 0),
    (8, 8, "database_release", 0, "BINARY", 0),
    (9, 9, "http_metadata_json", 0, "BINARY", 0),
)


class CacheReadState(StrEnum):
    """Freshness state returned by one cache lookup."""

    MISS = "miss"
    FRESH = "fresh"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """One validated successful response retrieved from the local cache."""

    body: bytes
    retrieved_at: datetime
    expires_at: datetime
    parser_version: str
    database_release: str | None
    http_metadata: tuple[HttpMetadata, ...]


@dataclass(frozen=True, slots=True)
class CacheLookup:
    """Cache lookup outcome; misses never carry a response payload."""

    state: CacheReadState
    response: CachedResponse | None

    def __post_init__(self) -> None:
        if (self.state is CacheReadState.MISS) != (self.response is None):
            raise ValueError("cache misses alone must omit a response")


@dataclass(frozen=True, slots=True)
class CacheStatus:
    """Redacted bounded cache capacity metadata."""

    entry_count: int
    expired_entry_count: int
    payload_bytes: int
    database_bytes: int
    max_entries: int
    max_payload_bytes: int
    max_database_bytes: int


@dataclass(frozen=True, slots=True)
class CacheCleanupSummary:
    """Redacted outcome of one explicit expired-row cleanup."""

    expired_entries: int
    expired_payload_bytes: int
    remaining_entries: int
    remaining_payload_bytes: int
    database_bytes: int


class _CacheIntegrityError(Exception):
    """Internal marker for malformed or incompatible cache content."""


class _CacheCapacityError(Exception):
    """Internal marker for a configured cache capacity refusal."""

    def __init__(self, stage: str) -> None:
        super().__init__("cache capacity exceeded")
        self.stage = stage


class SQLiteKeggCache:
    """Store KEGG responses in endpoint-scoped local SQLite namespaces."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        max_payload_bytes: int = DEFAULT_CACHE_MAX_PAYLOAD_BYTES,
        max_database_bytes: int = DEFAULT_CACHE_MAX_DATABASE_BYTES,
        read_only: bool = False,
    ) -> None:
        self._configured_path = os.fspath(path)
        raw_max_entries = cast(object, max_entries)
        raw_max_payload_bytes = cast(object, max_payload_bytes)
        raw_max_database_bytes = cast(object, max_database_bytes)
        raw_read_only = cast(object, read_only)
        if (
            isinstance(raw_max_entries, bool)
            or not isinstance(raw_max_entries, int)
            or raw_max_entries <= 0
        ):
            raise ValueError("max_entries must be a positive integer")
        if (
            isinstance(raw_max_payload_bytes, bool)
            or not isinstance(raw_max_payload_bytes, int)
            or raw_max_payload_bytes <= 0
        ):
            raise ValueError("max_payload_bytes must be a positive integer")
        if (
            isinstance(raw_max_database_bytes, bool)
            or not isinstance(raw_max_database_bytes, int)
            or raw_max_database_bytes < 1024 * 1024
            or raw_max_payload_bytes >= raw_max_database_bytes
        ):
            raise ValueError("max_database_bytes must leave bounded metadata capacity")
        if not isinstance(raw_read_only, bool):
            raise TypeError("read_only must be a boolean")
        self._max_entries = raw_max_entries
        self._max_payload_bytes = raw_max_payload_bytes
        self._max_database_bytes = raw_max_database_bytes
        self._read_only = raw_read_only

    @property
    def read_only(self) -> bool:
        """Report whether this cache is restricted to existing read-only storage."""
        return self._read_only

    def read(
        self,
        operation: KeggOperation,
        normalized_request_key: str,
        retrieval_endpoint_class: RetrievalEndpointClass,
        endpoint_fingerprint: str,
        *,
        now: datetime,
        expected_parser_version: str,
    ) -> CacheLookup:
        """Read and validate one cache row without deciding stale-use policy."""
        try:
            namespace = self._validate_namespace(
                operation,
                normalized_request_key,
                retrieval_endpoint_class,
                endpoint_fingerprint,
            )
            checked_now = _normalize_datetime(now)
            _validate_parser_version(expected_parser_version)
        except (TypeError, ValueError):
            _raise_cache_failed(operation, "request_validation")

        try:
            connection = self._connect_existing() if self._read_only else self._connect()
            if connection is None:
                return CacheLookup(state=CacheReadState.MISS, response=None)
            with closing(connection):
                raw_row = connection.execute(_READ_RESPONSE, namespace).fetchone()
        except (OSError, RuntimeError, sqlite3.Error, _CacheIntegrityError):
            _raise_cache_failed(operation, "read")

        if raw_row is None:
            return CacheLookup(state=CacheReadState.MISS, response=None)

        try:
            response = _decode_row(raw_row, expected_parser_version)
        except (TypeError, ValueError, ValidationError, _CacheIntegrityError):
            _raise_cache_failed(operation, "integrity_check")

        state = CacheReadState.STALE if checked_now >= response.expires_at else CacheReadState.FRESH
        return CacheLookup(state=state, response=response)

    def write(
        self,
        operation: KeggOperation,
        normalized_request_key: str,
        retrieval_endpoint_class: RetrievalEndpointClass,
        endpoint_fingerprint: str,
        *,
        body: bytes,
        retrieved_at: datetime,
        expires_at: datetime,
        parser_version: str,
        database_release: str | None,
        http_metadata: tuple[HttpMetadata, ...] = (),
    ) -> CachedResponse:
        """Atomically insert or replace one already validated successful response."""
        if self._read_only:
            _raise_cache_failed(operation, "read_only")
        try:
            namespace = self._validate_namespace(
                operation,
                normalized_request_key,
                retrieval_endpoint_class,
                endpoint_fingerprint,
            )
            response = _validate_response_for_write(
                body=body,
                retrieved_at=retrieved_at,
                expires_at=expires_at,
                parser_version=parser_version,
                database_release=database_release,
                http_metadata=http_metadata,
            )
            metadata_json = _encode_http_metadata(response.http_metadata)
        except (TypeError, ValueError, ValidationError):
            _raise_cache_failed(operation, "response_validation")

        values = (
            *namespace,
            response.body,
            _encode_datetime(response.retrieved_at),
            _encode_datetime(response.expires_at),
            response.parser_version,
            response.database_release,
            metadata_json,
        )
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "DELETE FROM kegg_responses WHERE expires_at <= ?",
                        (_encode_datetime(response.retrieved_at),),
                    )
                    existing = connection.execute(
                        """
                        SELECT length(response_body)
                        FROM kegg_responses
                        WHERE operation = ?
                          AND normalized_request_key = ?
                          AND endpoint_class = ?
                          AND endpoint_fingerprint = ?
                        """,
                        namespace,
                    ).fetchone()
                    aggregate = connection.execute(
                        "SELECT COUNT(*), COALESCE(SUM(length(response_body)), 0) "
                        "FROM kegg_responses"
                    ).fetchone()
                    entry_count, payload_bytes = _decode_integer_row(aggregate, length=2)
                    replaced_bytes = 0
                    if existing is not None:
                        if len(existing) != 1 or not isinstance(existing[0], int):
                            raise _CacheIntegrityError("invalid existing cache payload size")
                        replaced_bytes = existing[0]
                    proposed_entries = entry_count + (0 if existing is not None else 1)
                    proposed_payload = payload_bytes - replaced_bytes + len(response.body)
                    if proposed_entries > self._max_entries:
                        raise _CacheCapacityError("entry_limit")
                    if proposed_payload > self._max_payload_bytes:
                        raise _CacheCapacityError("payload_limit")
                    connection.execute(_UPSERT_RESPONSE, values)
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
        except _CacheCapacityError as error:
            _raise_cache_failed(operation, error.stage)
        except (OSError, RuntimeError, sqlite3.Error, _CacheIntegrityError):
            _raise_cache_failed(operation, "write")
        return response

    def status(self, *, now: datetime) -> CacheStatus:
        """Return cache counts and configured limits without paths or endpoint identities."""
        try:
            checked_now = _normalize_datetime(now)
            connection = self._connect_existing()
            if connection is None:
                return CacheStatus(
                    entry_count=0,
                    expired_entry_count=0,
                    payload_bytes=0,
                    database_bytes=0,
                    max_entries=self._max_entries,
                    max_payload_bytes=self._max_payload_bytes,
                    max_database_bytes=self._max_database_bytes,
                )
            with closing(connection):
                aggregate = connection.execute(
                    "SELECT COUNT(*), COALESCE(SUM(length(response_body)), 0) FROM kegg_responses"
                ).fetchone()
                expired = connection.execute(
                    "SELECT COUNT(*) FROM kegg_responses WHERE expires_at <= ?",
                    (_encode_datetime(checked_now),),
                ).fetchone()
                entry_count, payload_bytes = _decode_integer_row(aggregate, length=2)
                (expired_entry_count,) = _decode_integer_row(expired, length=1)
                database_bytes = _database_bytes(connection)
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError, _CacheIntegrityError):
            _raise_cache_failed("status", "status")
        return CacheStatus(
            entry_count=entry_count,
            expired_entry_count=expired_entry_count,
            payload_bytes=payload_bytes,
            database_bytes=database_bytes,
            max_entries=self._max_entries,
            max_payload_bytes=self._max_payload_bytes,
            max_database_bytes=self._max_database_bytes,
        )

    def cleanup_expired(self, *, now: datetime) -> CacheCleanupSummary:
        """Delete only TTL-expired rows and compact the private cache database."""
        if self._read_only:
            _raise_cache_failed("cleanup", "read_only")
        try:
            checked_now = _normalize_datetime(now)
            connection = self._connect_existing()
            if connection is None:
                return CacheCleanupSummary(
                    expired_entries=0,
                    expired_payload_bytes=0,
                    remaining_entries=0,
                    remaining_payload_bytes=0,
                    database_bytes=0,
                )
            with closing(connection):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    expired = connection.execute(
                        """
                        SELECT COUNT(*), COALESCE(SUM(length(response_body)), 0)
                        FROM kegg_responses
                        WHERE expires_at <= ?
                        """,
                        (_encode_datetime(checked_now),),
                    ).fetchone()
                    expired_entries, expired_payload_bytes = _decode_integer_row(
                        expired,
                        length=2,
                    )
                    connection.execute(
                        "DELETE FROM kegg_responses WHERE expires_at <= ?",
                        (_encode_datetime(checked_now),),
                    )
                    remaining = connection.execute(
                        "SELECT COUNT(*), COALESCE(SUM(length(response_body)), 0) "
                        "FROM kegg_responses"
                    ).fetchone()
                    remaining_entries, remaining_payload_bytes = _decode_integer_row(
                        remaining,
                        length=2,
                    )
                    connection.commit()
                except BaseException:
                    if connection.in_transaction:
                        connection.rollback()
                    raise
                database_bytes = _database_bytes(connection)
        except (OSError, RuntimeError, sqlite3.Error, TypeError, ValueError, _CacheIntegrityError):
            _raise_cache_failed("cleanup", "cleanup")
        return CacheCleanupSummary(
            expired_entries=expired_entries,
            expired_payload_bytes=expired_payload_bytes,
            remaining_entries=remaining_entries,
            remaining_payload_bytes=remaining_payload_bytes,
            database_bytes=database_bytes,
        )

    def _connect(self) -> sqlite3.Connection:
        return self._connect_path(self._prepare_location(), create=True)

    def _connect_existing(self) -> sqlite3.Connection | None:
        path = self._existing_location()
        if path is None:
            return None
        try:
            if self._read_only:
                return self._connect_read_only(path)
            return self._connect_path(path, create=False)
        except FileNotFoundError:
            return None

    def _connect_read_only(self, path: Path) -> sqlite3.Connection:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags)
        connection: sqlite3.Connection | None = None
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise OSError("cache is not a regular file")
            if hasattr(os, "geteuid") and descriptor_stat.st_uid != os.geteuid():
                raise OSError("cache must be owned by the current user")
            if stat.S_IMODE(descriptor_stat.st_mode) != 0o600:
                raise OSError("read-only cache mode must be 0600")
            if descriptor_stat.st_size > self._max_database_bytes:
                raise OSError("cache database exceeds the configured physical-size bound")
            _validate_existing_cache_parent(path.parent)
            path_stat = _validate_named_cache_file(path)
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise OSError("cache changed while it was opened")
            descriptor_path = _read_only_descriptor_path(descriptor)
            descriptor_path_stat = descriptor_path.stat()
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                descriptor_path_stat.st_dev,
                descriptor_path_stat.st_ino,
            ):
                raise OSError("cache descriptor binding is unavailable")
            connection = sqlite3.connect(
                f"{descriptor_path.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            (query_only,) = _decode_integer_row(
                connection.execute("PRAGMA query_only").fetchone(),
                length=1,
            )
            if query_only != 1:
                raise _CacheIntegrityError("SQLite query-only mode was not applied")
            (trusted_schema,) = _decode_integer_row(
                connection.execute("PRAGMA trusted_schema").fetchone(),
                length=1,
            )
            if trusted_schema != 0:
                raise _CacheIntegrityError("SQLite trusted-schema mode was not disabled")
            if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                raise _CacheIntegrityError("read-only cache requires delete journal mode")
            page_size_row = connection.execute("PRAGMA page_size").fetchone()
            (page_size,) = _decode_integer_row(page_size_row, length=1, positive=True)
            (page_count,) = _decode_integer_row(
                connection.execute("PRAGMA page_count").fetchone(),
                length=1,
                positive=True,
            )
            if page_count * page_size > self._max_database_bytes:
                raise _CacheIntegrityError("cache database exceeds the configured logical bound")
            schema_version_row = connection.execute("PRAGMA user_version").fetchone()
            if schema_version_row is None or len(schema_version_row) != 1:
                raise _CacheIntegrityError("missing SQLite schema version")
            if schema_version_row[0] != _SCHEMA_VERSION:
                raise _CacheIntegrityError("unsupported SQLite schema version")
            (auto_vacuum,) = _decode_integer_row(
                connection.execute("PRAGMA auto_vacuum").fetchone(),
                length=1,
            )
            if auto_vacuum != 1:
                raise _CacheIntegrityError("cache database must use full auto-vacuum")
            _validate_cache_schema(connection)
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        finally:
            os.close(descriptor)
        return connection

    def _connect_path(self, path: Path, *, create: bool) -> sqlite3.Connection:
        flags = os.O_RDWR | (os.O_CREAT if create else 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(path, flags, 0o600)
        try:
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                raise OSError("cache is not a regular file")
            if hasattr(os, "geteuid") and descriptor_stat.st_uid != os.geteuid():
                raise OSError("cache must be owned by the current user")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            connection = sqlite3.connect(path, timeout=5.0)
            try:
                path_stat = path.lstat()
                if stat.S_ISLNK(path_stat.st_mode) or (
                    descriptor_stat.st_dev,
                    descriptor_stat.st_ino,
                ) != (path_stat.st_dev, path_stat.st_ino):
                    raise OSError("cache changed while it was opened")
            except BaseException:
                connection.close()
                raise
        finally:
            os.close(descriptor)
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA journal_mode = DELETE")
            page_size_row = connection.execute("PRAGMA page_size").fetchone()
            (page_size,) = _decode_integer_row(page_size_row, length=1, positive=True)
            max_page_count = max(1, self._max_database_bytes // page_size)
            (configured_page_count,) = _decode_integer_row(
                connection.execute(f"PRAGMA max_page_count = {max_page_count}").fetchone(),
                length=1,
                positive=True,
            )
            if configured_page_count > max_page_count:
                raise _CacheIntegrityError("SQLite database limit was not applied")
            schema_version_row = connection.execute("PRAGMA user_version").fetchone()
            if schema_version_row is None or len(schema_version_row) != 1:
                raise _CacheIntegrityError("missing SQLite schema version")
            schema_version = schema_version_row[0]
            if not isinstance(schema_version, int):
                raise _CacheIntegrityError("invalid SQLite schema version")
            if schema_version not in {0, _SCHEMA_VERSION}:
                raise _CacheIntegrityError("unsupported SQLite schema version")
            with connection:
                if schema_version == 0:
                    connection.execute("PRAGMA auto_vacuum = FULL")
                connection.execute(_CREATE_SCHEMA)
                if schema_version == 0:
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            (auto_vacuum,) = _decode_integer_row(
                connection.execute("PRAGMA auto_vacuum").fetchone(),
                length=1,
            )
            if auto_vacuum != 1:
                raise _CacheIntegrityError("cache database must use full auto-vacuum")
            _validate_cache_schema(connection)
        except BaseException:
            connection.close()
            raise
        tighten_file_permissions(path)
        return connection

    def _prepare_location(self) -> Path:
        configured = Path(self._configured_path).expanduser()
        if ".." in configured.parts:
            raise OSError("cache path must not contain traversal components")
        if not configured.is_absolute():
            raise OSError("cache path must be absolute")
        path = configured
        parent = path.parent
        prepare_private_parent(parent)
        try:
            path_stat = path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(path_stat.st_mode):
                raise OSError("cache path must not be a symlink")
            if not stat.S_ISREG(path_stat.st_mode):
                raise OSError("cache must be a regular file")
            if hasattr(os, "geteuid") and path_stat.st_uid != os.geteuid():
                raise OSError("cache must be owned by the current user")
        return path

    def _existing_location(self) -> Path | None:
        path = Path(self._configured_path).expanduser()
        if ".." in path.parts:
            raise OSError("cache path must not contain traversal components")
        if not path.is_absolute():
            raise OSError("cache path must be absolute")
        parent = path.parent
        try:
            _validate_existing_cache_parent(parent)
        except FileNotFoundError:
            return None
        try:
            _validate_named_cache_file(path)
        except FileNotFoundError:
            return None
        return path

    @staticmethod
    def _validate_namespace(
        operation: object,
        normalized_request_key: object,
        retrieval_endpoint_class: object,
        endpoint_fingerprint: object,
    ) -> tuple[str, str, str, str]:
        if not isinstance(operation, KeggOperation):
            raise TypeError("operation must be a KeggOperation")
        if not isinstance(retrieval_endpoint_class, RetrievalEndpointClass):
            raise TypeError("endpoint class must be a RetrievalEndpointClass")
        if not isinstance(normalized_request_key, str):
            raise TypeError("normalized request key must be text")
        if (
            not normalized_request_key
            or len(normalized_request_key) > _MAX_REQUEST_KEY_CHARACTERS
            or "\x00" in normalized_request_key
        ):
            raise ValueError("invalid normalized request key")
        normalized_request_key.encode("utf-8", errors="strict")
        if not isinstance(endpoint_fingerprint, str) or not _ENDPOINT_FINGERPRINT_PATTERN.fullmatch(
            endpoint_fingerprint
        ):
            raise ValueError("invalid endpoint fingerprint")
        return (
            operation.value,
            normalized_request_key,
            retrieval_endpoint_class.value,
            endpoint_fingerprint,
        )


def _validate_response_for_write(
    *,
    body: object,
    retrieved_at: object,
    expires_at: object,
    parser_version: object,
    database_release: object,
    http_metadata: object,
) -> CachedResponse:
    if not isinstance(body, bytes):
        raise TypeError("cache response body must be bytes")
    checked_retrieved_at = _normalize_datetime(retrieved_at)
    checked_expires_at = _normalize_datetime(expires_at)
    if checked_expires_at <= checked_retrieved_at:
        raise ValueError("cache expiry must follow retrieval")
    checked_parser_version = _validate_parser_version(parser_version)
    checked_release = _validate_database_release(database_release)
    checked_metadata = _validate_http_metadata(http_metadata)
    return CachedResponse(
        body=body,
        retrieved_at=checked_retrieved_at,
        expires_at=checked_expires_at,
        parser_version=checked_parser_version,
        database_release=checked_release,
        http_metadata=checked_metadata,
    )


def _decode_row(raw_row: tuple[object, ...], expected_parser_version: str) -> CachedResponse:
    if len(raw_row) != 6:
        raise _CacheIntegrityError("unexpected cache row shape")
    raw_body, raw_retrieved, raw_expires, raw_parser, raw_release, raw_metadata = raw_row
    if not isinstance(raw_body, bytes):
        raise _CacheIntegrityError("cache body is not a BLOB")
    if not isinstance(raw_retrieved, str) or not isinstance(raw_expires, str):
        raise _CacheIntegrityError("invalid cache timestamp metadata")
    retrieved_at = _decode_datetime(raw_retrieved)
    expires_at = _decode_datetime(raw_expires)
    if expires_at <= retrieved_at:
        raise _CacheIntegrityError("invalid cache timestamp order")
    if not isinstance(raw_parser, str):
        raise _CacheIntegrityError("invalid parser metadata")
    _validate_parser_version(raw_parser)
    if raw_parser != expected_parser_version:
        raise _CacheIntegrityError("cache parser version is incompatible")
    database_release = _validate_database_release(raw_release)
    if not isinstance(raw_metadata, str):
        raise _CacheIntegrityError("invalid HTTP metadata")
    http_metadata = _decode_http_metadata(raw_metadata)
    return CachedResponse(
        body=raw_body,
        retrieved_at=retrieved_at,
        expires_at=expires_at,
        parser_version=raw_parser,
        database_release=database_release,
        http_metadata=http_metadata,
    )


def _normalize_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("cache timestamps must be timezone-aware datetimes")
    return value.astimezone(UTC)


def _encode_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decode_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _CacheIntegrityError("invalid cache timestamp") from error
    try:
        return _normalize_datetime(parsed)
    except ValueError as error:
        raise _CacheIntegrityError("invalid cache timestamp") from error


def _validate_parser_version(value: object) -> str:
    if not isinstance(value, str) or not _PARSER_VERSION_PATTERN.fullmatch(value):
        raise ValueError("invalid parser version")
    return value


def _validate_database_release(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid database release")
    if not value or len(value) > 256 or any(ord(character) < 32 for character in value):
        raise ValueError("invalid database release")
    value.encode("utf-8", errors="strict")
    return value


def _validate_http_metadata(
    metadata: object,
) -> tuple[HttpMetadata, ...]:
    if not isinstance(metadata, tuple):
        raise TypeError("HTTP metadata must be a tuple")
    metadata_items = cast(tuple[object, ...], metadata)
    if len(metadata_items) > MAX_HTTP_METADATA_ITEMS:
        raise ValueError("HTTP metadata exceeds the bounded item count")
    checked_items: list[HttpMetadata] = []
    for item in metadata_items:
        if not isinstance(item, HttpMetadata) or item.name not in _HTTP_METADATA_ALLOWLIST:
            raise ValueError("HTTP metadata is not allowlisted")
        checked_items.append(item)
    return tuple(checked_items)


def _encode_http_metadata(metadata: tuple[HttpMetadata, ...]) -> str:
    values = [{"name": item.name, "value": item.value} for item in metadata]
    return json.dumps(values, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _decode_http_metadata(value: str) -> tuple[HttpMetadata, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise _CacheIntegrityError("invalid HTTP metadata JSON") from error
    if not isinstance(decoded, list):
        raise _CacheIntegrityError("invalid HTTP metadata shape")
    decoded_items = cast(list[object], decoded)
    if len(decoded_items) > MAX_HTTP_METADATA_ITEMS:
        raise _CacheIntegrityError("HTTP metadata exceeds the bounded item count")
    items: list[HttpMetadata] = []
    for raw_item in decoded_items:
        if not isinstance(raw_item, dict):
            raise _CacheIntegrityError("invalid HTTP metadata item")
        item = HttpMetadata.model_validate(raw_item, strict=True)
        if item.name not in _HTTP_METADATA_ALLOWLIST:
            raise _CacheIntegrityError("HTTP metadata is not allowlisted")
        items.append(item)
    return tuple(items)


def _decode_integer_row(
    row: object,
    *,
    length: int,
    positive: bool = False,
) -> tuple[int, ...]:
    if not isinstance(row, tuple):
        raise _CacheIntegrityError("invalid SQLite integer row")
    values = cast(tuple[object, ...], row)
    minimum = 1 if positive else 0
    if len(values) != length or any(
        isinstance(value, bool) or not isinstance(value, int) or value < minimum for value in values
    ):
        raise _CacheIntegrityError("invalid SQLite integer row")
    return cast(tuple[int, ...], values)


def _database_bytes(connection: sqlite3.Connection) -> int:
    (page_count,) = _decode_integer_row(
        connection.execute("PRAGMA page_count").fetchone(),
        length=1,
        positive=True,
    )
    (page_size,) = _decode_integer_row(
        connection.execute("PRAGMA page_size").fetchone(),
        length=1,
        positive=True,
    )
    return page_count * page_size


def _validate_cache_schema(connection: sqlite3.Connection) -> None:
    schema_objects = _fetch_bounded_rows(
        connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name LIMIT 2"
        ),
        maximum=1,
    )
    if schema_objects != _EXPECTED_SCHEMA_OBJECTS:
        raise _CacheIntegrityError("cache schema objects do not match the supported version")
    table_list = tuple(
        row
        for row in _fetch_bounded_rows(connection.execute("PRAGMA table_list"), maximum=16)
        if row[0] == "main" and not str(row[1]).startswith("sqlite_")
    )
    if table_list != _EXPECTED_TABLE_LIST:
        raise _CacheIntegrityError("cache table flags do not match the supported version")
    table_xinfo = _fetch_bounded_rows(
        connection.execute("PRAGMA table_xinfo(kegg_responses)"),
        maximum=len(_EXPECTED_TABLE_XINFO),
    )
    if table_xinfo != _EXPECTED_TABLE_XINFO:
        raise _CacheIntegrityError("cache columns do not match the supported version")
    index_list = _fetch_bounded_rows(
        connection.execute("PRAGMA index_list(kegg_responses)"),
        maximum=len(_EXPECTED_INDEX_LIST),
    )
    if index_list != _EXPECTED_INDEX_LIST:
        raise _CacheIntegrityError("cache primary key does not match the supported version")
    index_xinfo = _fetch_bounded_rows(
        connection.execute("PRAGMA index_xinfo(sqlite_autoindex_kegg_responses_1)"),
        maximum=len(_EXPECTED_INDEX_XINFO),
    )
    if index_xinfo != _EXPECTED_INDEX_XINFO:
        raise _CacheIntegrityError("cache index columns do not match the supported version")


def _fetch_bounded_rows(cursor: sqlite3.Cursor, *, maximum: int) -> tuple[tuple[object, ...], ...]:
    rows = tuple(cursor.fetchmany(maximum + 1))
    if len(rows) > maximum:
        raise _CacheIntegrityError("cache schema metadata exceeds the supported bound")
    return rows


def _validate_existing_cache_parent(path: Path) -> os.stat_result:
    return validate_private_directory(path)


def _validate_named_cache_file(path: Path) -> os.stat_result:
    path_stat = path.lstat()
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISREG(path_stat.st_mode):
        raise OSError("cache must be a regular file")
    if hasattr(os, "geteuid") and path_stat.st_uid != os.geteuid():
        raise OSError("cache must be owned by the current user")
    return path_stat


def _raise_cache_failed(operation: object, stage: str) -> NoReturn:
    operation_name = operation.value if isinstance(operation, KeggOperation) else "unknown"
    raise KeggMcpError(
        ErrorDetail(
            code=ErrorCode.CACHE_FAILED,
            message="The local KEGG cache could not be used safely.",
            recoverable=True,
            suggested_action=(
                "Inspect or replace the configured local KEGG cache before retrying."
            ),
            safe_details=(
                SafeDetail(name="operation", value=operation_name),
                SafeDetail(name="stage", value=stage),
            ),
        )
    ) from None
