"""Integrity-checked user-local SQLite cache for KEGG response payloads."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn, cast

from pydantic import ValidationError

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError, SafeDetail
from kegg_mcp.kegg.contracts import (
    MAX_HTTP_METADATA_ITEMS,
    HttpMetadata,
    KeggOperation,
    RetrievalEndpointClass,
)

_SCHEMA_VERSION: Final = 1
_MAX_REQUEST_KEY_CHARACTERS: Final = 65_536
_PARSER_VERSION_PATTERN: Final = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_SHA256_PATTERN: Final = re.compile(r"[a-f0-9]{64}\Z")
_HTTP_METADATA_ALLOWLIST: Final = frozenset({"content-type", "date", "etag", "last-modified"})

_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS kegg_responses (
    operation TEXT NOT NULL,
    normalized_request_key TEXT NOT NULL,
    endpoint_class TEXT NOT NULL,
    endpoint_fingerprint TEXT NOT NULL,
    response_body BLOB NOT NULL,
    response_sha256 TEXT NOT NULL,
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
    response_sha256,
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
    response_sha256,
    retrieved_at,
    expires_at,
    parser_version,
    database_release,
    http_metadata_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (
    operation,
    normalized_request_key,
    endpoint_class,
    endpoint_fingerprint
) DO UPDATE SET
    response_body = excluded.response_body,
    response_sha256 = excluded.response_sha256,
    retrieved_at = excluded.retrieved_at,
    expires_at = excluded.expires_at,
    parser_version = excluded.parser_version,
    database_release = excluded.database_release,
    http_metadata_json = excluded.http_metadata_json
"""


class CacheReadState(StrEnum):
    """Freshness state returned by one cache lookup."""

    MISS = "miss"
    FRESH = "fresh"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """One validated successful response retrieved from the local cache."""

    body: bytes
    response_sha256: str
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


class _CacheIntegrityError(Exception):
    """Internal marker for malformed or incompatible cache content."""


class SQLiteKeggCache:
    """Store KEGG responses in endpoint-scoped local SQLite namespaces."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._configured_path = os.fspath(path)

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
            with closing(self._connect()) as connection:
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
            response.response_sha256,
            _encode_datetime(response.retrieved_at),
            _encode_datetime(response.expires_at),
            response.parser_version,
            response.database_release,
            metadata_json,
        )
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute(_UPSERT_RESPONSE, values)
        except (OSError, RuntimeError, sqlite3.Error, _CacheIntegrityError):
            _raise_cache_failed(operation, "write")
        return response

    def _connect(self) -> sqlite3.Connection:
        path = self._prepare_location()
        flags = os.O_RDWR | os.O_CREAT
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
            schema_version_row = connection.execute("PRAGMA user_version").fetchone()
            if schema_version_row is None or len(schema_version_row) != 1:
                raise _CacheIntegrityError("missing SQLite schema version")
            schema_version = schema_version_row[0]
            if not isinstance(schema_version, int):
                raise _CacheIntegrityError("invalid SQLite schema version")
            if schema_version not in {0, _SCHEMA_VERSION}:
                raise _CacheIntegrityError("unsupported SQLite schema version")
            with connection:
                connection.execute(_CREATE_SCHEMA)
                if schema_version == 0:
                    connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        except BaseException:
            connection.close()
            raise
        _tighten_file_permissions(path)
        return connection

    def _prepare_location(self) -> Path:
        configured = Path(self._configured_path).expanduser()
        if ".." in configured.parts:
            raise OSError("cache path must not contain traversal components")
        if not configured.is_absolute():
            raise OSError("cache path must be absolute")
        path = configured
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
            raise OSError("cache parent must be a real directory")
        if hasattr(os, "geteuid") and parent_stat.st_uid != os.geteuid():
            raise OSError("cache parent must be owned by the current user")
        if stat.S_IMODE(parent_stat.st_mode) & 0o022:
            raise OSError("cache parent must not be group- or world-writable")
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
        if not isinstance(endpoint_fingerprint, str) or not _SHA256_PATTERN.fullmatch(
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
        response_sha256=hashlib.sha256(body).hexdigest(),
        retrieved_at=checked_retrieved_at,
        expires_at=checked_expires_at,
        parser_version=checked_parser_version,
        database_release=checked_release,
        http_metadata=checked_metadata,
    )


def _decode_row(raw_row: tuple[object, ...], expected_parser_version: str) -> CachedResponse:
    if len(raw_row) != 7:
        raise _CacheIntegrityError("unexpected cache row shape")
    raw_body, raw_sha, raw_retrieved, raw_expires, raw_parser, raw_release, raw_metadata = raw_row
    if not isinstance(raw_body, bytes):
        raise _CacheIntegrityError("cache body is not a BLOB")
    if not isinstance(raw_sha, str) or not _SHA256_PATTERN.fullmatch(raw_sha):
        raise _CacheIntegrityError("invalid response checksum metadata")
    if hashlib.sha256(raw_body).hexdigest() != raw_sha:
        raise _CacheIntegrityError("cache response checksum mismatch")
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
        response_sha256=raw_sha,
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


def _tighten_directory_permissions(path: Path) -> None:
    try:
        path_stat = path.lstat()
        if stat.S_ISDIR(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
            path.chmod(0o700)
    except OSError:
        pass


def _reject_symlink_components(path: Path) -> None:
    """Reject every existing symlink or non-directory parent component."""
    if not path.is_absolute():
        raise OSError("cache parent must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(component_stat.st_mode):
            raise OSError("cache parent must not contain symlinks")
        if not stat.S_ISDIR(component_stat.st_mode):
            raise OSError("cache parent components must be directories")


def _tighten_file_permissions(path: Path) -> None:
    try:
        path_stat = path.lstat()
        if stat.S_ISREG(path_stat.st_mode) and not stat.S_ISLNK(path_stat.st_mode):
            path.chmod(0o600)
    except OSError:
        pass


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
