"""Tests for the integrity-checked user-local KEGG response cache."""

import hashlib
import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import kegg_mcp.kegg.cache as cache_module
from kegg_mcp.domain import ErrorCode, KeggMcpError
from kegg_mcp.kegg.cache import (
    CachedResponse,
    CacheLookup,
    CacheReadState,
    SQLiteKeggCache,
)
from kegg_mcp.kegg.contracts import (
    MAX_HTTP_METADATA_ITEMS,
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    HttpMetadata,
    KeggOperation,
    RetrievalEndpointClass,
)

_RETRIEVED_AT = datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC)
_EXPIRES_AT = _RETRIEVED_AT + timedelta(days=7)
_REQUEST_KEY = "/get/K00001"
_PARSER_VERSION = "1"


def _write_response(
    cache: SQLiteKeggCache,
    *,
    operation: KeggOperation = KeggOperation.GET,
    request_key: str = _REQUEST_KEY,
    endpoint_class: RetrievalEndpointClass = RetrievalEndpointClass.PUBLIC_ACADEMIC,
    endpoint_fingerprint: str = PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    body: bytes = b"ENTRY       K00001\n///\n",
) -> CachedResponse:
    return cache.write(
        operation,
        request_key,
        endpoint_class,
        endpoint_fingerprint,
        body=body,
        retrieved_at=_RETRIEVED_AT,
        expires_at=_EXPIRES_AT,
        parser_version=_PARSER_VERSION,
        database_release="Release 115.0+/07-14, Jul 26",
        http_metadata=(
            HttpMetadata(name="content-type", value="text/plain; charset=utf-8"),
            HttpMetadata(name="etag", value='"stable"'),
        ),
    )


def _read_response(
    cache: SQLiteKeggCache,
    *,
    operation: KeggOperation = KeggOperation.GET,
    request_key: str = _REQUEST_KEY,
    endpoint_class: RetrievalEndpointClass = RetrievalEndpointClass.PUBLIC_ACADEMIC,
    endpoint_fingerprint: str = PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    now: datetime = _RETRIEVED_AT,
    parser_version: str = _PARSER_VERSION,
) -> CacheLookup:
    return cache.read(
        operation,
        request_key,
        endpoint_class,
        endpoint_fingerprint,
        now=now,
        expected_parser_version=parser_version,
    )


def _assert_cache_failed(error: pytest.ExceptionInfo[KeggMcpError], path: Path) -> None:
    assert error.value.detail.code is ErrorCode.CACHE_FAILED
    assert error.value.detail.recoverable is True
    assert error.value.detail.suggested_action
    assert str(path) not in error.value.detail.model_dump_json()


def test_round_trip_returns_fresh_response_and_complete_metadata(tmp_path: Path) -> None:
    cache_path = tmp_path / "cache" / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)
    written = _write_response(cache)

    lookup = _read_response(cache, now=_RETRIEVED_AT + timedelta(hours=1))

    assert lookup.state is CacheReadState.FRESH
    assert lookup.response is not None
    assert lookup.response == written
    assert lookup.response.retrieved_at == _RETRIEVED_AT
    assert lookup.response.expires_at == _EXPIRES_AT
    assert lookup.response.parser_version == _PARSER_VERSION
    assert lookup.response.database_release == "Release 115.0+/07-14, Jul 26"
    assert tuple(item.name for item in lookup.response.http_metadata) == (
        "content-type",
        "etag",
    )


def test_expired_response_is_returned_as_stale_without_policy_decision(tmp_path: Path) -> None:
    cache = SQLiteKeggCache(tmp_path / "kegg.sqlite3")
    _write_response(cache)

    at_expiry = _read_response(cache, now=_EXPIRES_AT)
    after_expiry = _read_response(cache, now=_EXPIRES_AT + timedelta(seconds=1))

    assert at_expiry.state is CacheReadState.STALE
    assert at_expiry.response is not None
    assert after_expiry.state is CacheReadState.STALE
    assert after_expiry.response is not None


@pytest.mark.parametrize(
    ("operation", "request_key", "endpoint_class", "endpoint_fingerprint"),
    [
        (
            KeggOperation.INFO,
            _REQUEST_KEY,
            RetrievalEndpointClass.PUBLIC_ACADEMIC,
            PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        ),
        (
            KeggOperation.GET,
            "/get/K00002",
            RetrievalEndpointClass.PUBLIC_ACADEMIC,
            PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        ),
        (
            KeggOperation.GET,
            _REQUEST_KEY,
            RetrievalEndpointClass.LICENSED,
            PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        ),
        (
            KeggOperation.GET,
            _REQUEST_KEY,
            RetrievalEndpointClass.PUBLIC_ACADEMIC,
            "f" * 64,
        ),
    ],
)
def test_cache_namespace_includes_every_retrieval_dimension(
    tmp_path: Path,
    operation: KeggOperation,
    request_key: str,
    endpoint_class: RetrievalEndpointClass,
    endpoint_fingerprint: str,
) -> None:
    cache = SQLiteKeggCache(tmp_path / "kegg.sqlite3")
    _write_response(cache)

    lookup = _read_response(
        cache,
        operation=operation,
        request_key=request_key,
        endpoint_class=endpoint_class,
        endpoint_fingerprint=endpoint_fingerprint,
    )

    assert lookup == CacheLookup(state=CacheReadState.MISS, response=None)


def test_upsert_replaces_one_namespace_atomically(tmp_path: Path) -> None:
    cache = SQLiteKeggCache(tmp_path / "kegg.sqlite3")
    _write_response(cache, body=b"first")

    replacement = _write_response(cache, body=b"second")
    lookup = _read_response(cache)

    assert lookup.response == replacement
    assert lookup.response is not None
    assert lookup.response.body == b"second"


def test_cache_refuses_fresh_entry_eviction_at_entry_capacity(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = SQLiteKeggCache(
        cache_path,
        max_entries=1,
        max_payload_bytes=1_000,
        max_database_bytes=1024 * 1024,
    )
    _write_response(cache, request_key="/get/K00001", body=b"first")

    with pytest.raises(KeggMcpError) as error:
        _write_response(cache, request_key="/get/K00002", body=b"second")

    _assert_cache_failed(error, cache_path)
    assert any(detail.value == "entry_limit" for detail in error.value.detail.safe_details)
    assert _read_response(cache, request_key="/get/K00001").response is not None
    assert _read_response(cache, request_key="/get/K00002").state is CacheReadState.MISS


def test_cache_refuses_payload_growth_without_deleting_fresh_rows(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = SQLiteKeggCache(
        cache_path,
        max_entries=10,
        max_payload_bytes=8,
        max_database_bytes=1024 * 1024,
    )
    _write_response(cache, body=b"12345678")

    with pytest.raises(KeggMcpError) as error:
        _write_response(cache, request_key="/get/K00002", body=b"x")

    _assert_cache_failed(error, cache_path)
    assert any(detail.value == "payload_limit" for detail in error.value.detail.safe_details)


def test_write_removes_expired_rows_before_applying_capacity(tmp_path: Path) -> None:
    cache = SQLiteKeggCache(
        tmp_path / "kegg.sqlite3",
        max_entries=1,
        max_payload_bytes=100,
        max_database_bytes=1024 * 1024,
    )
    _write_response(cache, request_key="/get/K00001", body=b"expired")
    retrieved_at = _EXPIRES_AT + timedelta(seconds=1)

    cache.write(
        KeggOperation.GET,
        "/get/K00002",
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        body=b"fresh",
        retrieved_at=retrieved_at,
        expires_at=retrieved_at + timedelta(days=1),
        parser_version=_PARSER_VERSION,
        database_release=None,
    )

    assert _read_response(cache, request_key="/get/K00001").state is CacheReadState.MISS
    assert (
        _read_response(cache, request_key="/get/K00002", now=retrieved_at).state
        is CacheReadState.FRESH
    )


def test_status_and_expired_cleanup_are_redacted_and_bounded(tmp_path: Path) -> None:
    cache_path = tmp_path / "private-cache-name.sqlite3"
    cache = SQLiteKeggCache(
        cache_path,
        max_entries=10,
        max_payload_bytes=1_000,
        max_database_bytes=1024 * 1024,
    )
    _write_response(cache, body=b"expired-payload")

    status = cache.status(now=_EXPIRES_AT)

    assert status.entry_count == 1
    assert status.expired_entry_count == 1
    assert status.payload_bytes == len(b"expired-payload")
    assert status.database_bytes <= status.max_database_bytes
    assert str(cache_path) not in repr(status)
    assert PUBLIC_KEGG_ENDPOINT_FINGERPRINT not in repr(status)

    summary = cache.cleanup_expired(now=_EXPIRES_AT)

    assert summary.expired_entries == 1
    assert summary.expired_payload_bytes == len(b"expired-payload")
    assert summary.remaining_entries == 0
    assert summary.remaining_payload_bytes == 0
    assert summary.database_bytes <= status.max_database_bytes


def test_status_and_cleanup_do_not_create_a_missing_cache_or_parent(tmp_path: Path) -> None:
    cache_path = tmp_path / "missing-parent" / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)

    status = cache.status(now=_RETRIEVED_AT)
    cleanup = cache.cleanup_expired(now=_RETRIEVED_AT)

    assert status.entry_count == 0
    assert status.expired_entry_count == 0
    assert status.payload_bytes == 0
    assert status.database_bytes == 0
    assert cleanup.expired_entries == 0
    assert cleanup.expired_payload_bytes == 0
    assert cleanup.remaining_entries == 0
    assert cleanup.remaining_payload_bytes == 0
    assert cleanup.database_bytes == 0
    assert not cache_path.parent.exists()


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        ("retrieved_at", "not-a-timestamp"),
        ("expires_at", "2020-01-01T00:00:00+00:00"),
        ("parser_version", "invalid-version"),
        ("http_metadata_json", "not-json"),
    ],
)
def test_tampered_row_is_an_explicit_cache_failure(
    tmp_path: Path, column: str, replacement: bytes | str
) -> None:
    cache_path = tmp_path / "private" / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)
    _write_response(cache)
    with sqlite3.connect(cache_path) as connection:
        connection.execute(f"UPDATE kegg_responses SET {column} = ?", (replacement,))

    with pytest.raises(KeggMcpError) as error:
        _read_response(cache)

    _assert_cache_failed(error, cache_path)
    assert any(detail.value == "integrity_check" for detail in error.value.detail.safe_details)


def test_parser_version_mismatch_is_cache_failure_not_miss(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)
    _write_response(cache)

    with pytest.raises(KeggMcpError) as error:
        _read_response(cache, parser_version="2")

    _assert_cache_failed(error, cache_path)


def test_non_allowlisted_http_metadata_is_rejected_before_storage(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)

    with pytest.raises(KeggMcpError) as error:
        cache.write(
            KeggOperation.GET,
            _REQUEST_KEY,
            RetrievalEndpointClass.PUBLIC_ACADEMIC,
            PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
            body=b"valid parsed response",
            retrieved_at=_RETRIEVED_AT,
            expires_at=_EXPIRES_AT,
            parser_version=_PARSER_VERSION,
            database_release=None,
            http_metadata=(HttpMetadata(name="authorization", value="secret"),),
        )

    _assert_cache_failed(error, cache_path)
    assert not cache_path.exists()


def test_excess_http_metadata_is_rejected_before_storage(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)
    metadata = tuple(
        HttpMetadata(name="etag", value=f'"value-{index}"')
        for index in range(MAX_HTTP_METADATA_ITEMS + 1)
    )

    with pytest.raises(KeggMcpError) as error:
        cache.write(
            KeggOperation.GET,
            _REQUEST_KEY,
            RetrievalEndpointClass.PUBLIC_ACADEMIC,
            PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
            body=b"valid parsed response",
            retrieved_at=_RETRIEVED_AT,
            expires_at=_EXPIRES_AT,
            parser_version=_PARSER_VERSION,
            database_release=None,
            http_metadata=metadata,
        )

    _assert_cache_failed(error, cache_path)
    assert not cache_path.exists()


def test_excess_http_metadata_in_cache_row_is_an_integrity_failure(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)
    _write_response(cache)
    metadata_json = json.dumps(
        [
            {"name": "etag", "value": f'"value-{index}"'}
            for index in range(MAX_HTTP_METADATA_ITEMS + 1)
        ]
    )
    with sqlite3.connect(cache_path) as connection:
        connection.execute(
            "UPDATE kegg_responses SET http_metadata_json = ?",
            (metadata_json,),
        )

    with pytest.raises(KeggMcpError) as error:
        _read_response(cache)

    _assert_cache_failed(error, cache_path)
    assert any(detail.value == "integrity_check" for detail in error.value.detail.safe_details)


@pytest.mark.parametrize(
    ("retrieved_at", "expires_at"),
    [
        (
            datetime(2026, 7, 14, 1, 2, 3),
            datetime(2026, 7, 15, 1, 2, 3, tzinfo=UTC),
        ),
        (_RETRIEVED_AT, _RETRIEVED_AT),
        (_EXPIRES_AT, _RETRIEVED_AT),
    ],
)
def test_invalid_cache_timestamps_fail_before_storage(
    tmp_path: Path, retrieved_at: datetime, expires_at: datetime
) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)

    with pytest.raises(KeggMcpError) as error:
        cache.write(
            KeggOperation.GET,
            _REQUEST_KEY,
            RetrievalEndpointClass.PUBLIC_ACADEMIC,
            PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
            body=b"valid parsed response",
            retrieved_at=retrieved_at,
            expires_at=expires_at,
            parser_version=_PARSER_VERSION,
            database_release=None,
        )

    _assert_cache_failed(error, cache_path)


class _FailingCache(SQLiteKeggCache):
    def _connect(self) -> sqlite3.Connection:
        raise sqlite3.OperationalError("/sensitive/local/cache/path could not be opened")


def test_read_storage_failure_is_safe_and_not_a_cache_miss(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = _FailingCache(cache_path)

    with pytest.raises(KeggMcpError) as error:
        _read_response(cache)

    _assert_cache_failed(error, cache_path)
    serialized = error.value.detail.model_dump_json()
    assert "/sensitive/local/cache/path" not in serialized
    assert any(detail.value == "read" for detail in error.value.detail.safe_details)


def test_write_storage_failure_is_safe_and_not_silently_ignored(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    cache = _FailingCache(cache_path)

    with pytest.raises(KeggMcpError) as error:
        _write_response(cache)

    _assert_cache_failed(error, cache_path)
    assert any(detail.value == "write" for detail in error.value.detail.safe_details)


def test_unsupported_schema_version_is_an_explicit_cache_failure(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    with sqlite3.connect(cache_path) as connection:
        connection.execute("PRAGMA user_version = 999")
    cache = SQLiteKeggCache(cache_path)

    with pytest.raises(KeggMcpError) as error:
        _read_response(cache)

    _assert_cache_failed(error, cache_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are unavailable")
def test_new_cache_directory_and_database_use_restrictive_permissions(tmp_path: Path) -> None:
    cache_directory = tmp_path / "new-private-cache"
    cache_path = cache_directory / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path)

    _write_response(cache)

    assert stat.S_IMODE(cache_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache_path.stat().st_mode) == 0o600


def test_cache_rejects_relative_path_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    configured = Path("relative.sqlite3")

    with pytest.raises(KeggMcpError) as error:
        _write_response(SQLiteKeggCache(configured))

    _assert_cache_failed(error, configured)
    assert not (tmp_path / configured).exists()


def test_cache_rejects_configured_path_traversal(tmp_path: Path) -> None:
    configured = tmp_path / "allowed" / ".." / "escaped.sqlite3"

    with pytest.raises(KeggMcpError) as error:
        _write_response(SQLiteKeggCache(configured))

    _assert_cache_failed(error, configured)
    assert not (tmp_path / "escaped.sqlite3").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink checks are unavailable")
def test_cache_rejects_parent_and_final_symlink_escapes(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (allowed / "jump").symlink_to(outside, target_is_directory=True)
    parent_escape = allowed / "jump" / "escaped.sqlite3"

    with pytest.raises(KeggMcpError) as parent_error:
        _write_response(SQLiteKeggCache(parent_escape))

    _assert_cache_failed(parent_error, parent_escape)
    assert not (outside / "escaped.sqlite3").exists()

    target = outside / "target.sqlite3"
    target.write_bytes(b"do-not-touch")
    final_escape = allowed / "cache.sqlite3"
    final_escape.symlink_to(target)

    with pytest.raises(KeggMcpError) as final_error:
        _write_response(SQLiteKeggCache(final_escape))

    _assert_cache_failed(final_error, final_escape)
    assert target.read_bytes() == b"do-not-touch"


def test_cache_rejects_existing_non_regular_file(tmp_path: Path) -> None:
    configured = tmp_path / "cache.sqlite3"
    configured.mkdir()

    with pytest.raises(KeggMcpError) as error:
        _write_response(SQLiteKeggCache(configured))

    _assert_cache_failed(error, configured)
    assert configured.is_dir()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are unavailable")
def test_cache_rejects_unsafe_parent_permissions(tmp_path: Path) -> None:
    parent = tmp_path / "shared-cache"
    parent.mkdir(mode=0o700)
    parent.chmod(0o770)
    configured = parent / "cache.sqlite3"

    with pytest.raises(KeggMcpError) as error:
        _write_response(SQLiteKeggCache(configured))

    _assert_cache_failed(error, configured)
    assert not configured.exists()


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership checks are unavailable")
def test_cache_rejects_parent_not_owned_by_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "private-cache"
    parent.mkdir(mode=0o700)
    configured = parent / "cache.sqlite3"
    actual_uid = parent.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: actual_uid + 1)

    with pytest.raises(KeggMcpError) as error:
        _write_response(SQLiteKeggCache(configured))

    _assert_cache_failed(error, configured)
    assert not configured.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are unavailable")
def test_existing_cache_file_permissions_are_tightened(tmp_path: Path) -> None:
    configured = tmp_path / "cache.sqlite3"
    configured.touch(mode=0o666)
    configured.chmod(0o666)

    _write_response(SQLiteKeggCache(configured))

    assert stat.S_IMODE(configured.stat().st_mode) == 0o600


def test_read_only_cache_miss_does_not_create_a_database_or_parent(tmp_path: Path) -> None:
    cache_path = tmp_path / "missing-private" / "kegg.sqlite3"
    cache = SQLiteKeggCache(cache_path, read_only=True)

    lookup = _read_response(cache)
    status = cache.status(now=_RETRIEVED_AT)

    assert cache.read_only is True
    assert lookup == CacheLookup(state=CacheReadState.MISS, response=None)
    assert status.entry_count == 0
    assert not cache_path.parent.exists()


def test_read_only_cache_reuses_an_existing_database_and_rejects_mutation(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    _write_response(SQLiteKeggCache(cache_path))
    cache = SQLiteKeggCache(cache_path, read_only=True)
    digest_before = hashlib.sha256(cache_path.read_bytes()).hexdigest()
    modified_before = cache_path.stat().st_mtime_ns
    sidecars_before = tuple(sorted(item.name for item in tmp_path.glob("kegg.sqlite3-*")))

    lookup = _read_response(cache)
    status = cache.status(now=_RETRIEVED_AT)

    assert lookup.state is CacheReadState.FRESH
    assert lookup.response is not None
    assert status.entry_count == 1
    assert hashlib.sha256(cache_path.read_bytes()).hexdigest() == digest_before
    assert cache_path.stat().st_mtime_ns == modified_before
    assert tuple(sorted(item.name for item in tmp_path.glob("kegg.sqlite3-*"))) == sidecars_before
    with pytest.raises(KeggMcpError) as write_error:
        _write_response(cache)
    with pytest.raises(KeggMcpError) as cleanup_error:
        cache.cleanup_expired(now=_EXPIRES_AT)
    _assert_cache_failed(write_error, cache_path)
    _assert_cache_failed(cleanup_error, cache_path)
    assert {item.value for item in write_error.value.detail.safe_details} >= {"read_only"}
    assert {item.value for item in cleanup_error.value.detail.safe_details} >= {"read_only"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are unavailable")
def test_read_only_cache_requires_exact_owner_only_file_and_safe_parent_modes(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "private" / "kegg.sqlite3"
    cache_path.parent.mkdir(mode=0o700)
    _write_response(SQLiteKeggCache(cache_path))

    cache_path.chmod(0o640)
    with pytest.raises(KeggMcpError) as file_mode_error:
        _read_response(SQLiteKeggCache(cache_path, read_only=True))
    _assert_cache_failed(file_mode_error, cache_path)

    cache_path.chmod(0o600)
    cache_path.parent.chmod(0o770)
    with pytest.raises(KeggMcpError) as parent_mode_error:
        _read_response(SQLiteKeggCache(cache_path, read_only=True))
    _assert_cache_failed(parent_mode_error, cache_path)


def test_read_only_cache_rejects_symlink_schema_and_physical_size_failures(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "private" / "kegg.sqlite3"
    cache_path.parent.mkdir(mode=0o700)
    _write_response(SQLiteKeggCache(cache_path))
    symlink_path = tmp_path / "cache-link.sqlite3"
    symlink_path.symlink_to(cache_path)

    with pytest.raises(KeggMcpError) as symlink_error:
        _read_response(SQLiteKeggCache(symlink_path, read_only=True))
    _assert_cache_failed(symlink_error, symlink_path)

    with sqlite3.connect(cache_path) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(KeggMcpError) as schema_error:
        _read_response(SQLiteKeggCache(cache_path, read_only=True))
    _assert_cache_failed(schema_error, cache_path)

    cache_path.unlink()
    _write_response(SQLiteKeggCache(cache_path))
    with cache_path.open("r+b") as cache_file:
        cache_file.truncate(1024 * 1024 + 1)
    bounded = SQLiteKeggCache(
        cache_path,
        max_payload_bytes=512 * 1024,
        max_database_bytes=1024 * 1024,
        read_only=True,
    )
    with pytest.raises(KeggMcpError) as size_error:
        _read_response(bounded)
    _assert_cache_failed(size_error, cache_path)


@pytest.mark.parametrize("swap_kind", ["file", "ancestor"])
def test_read_only_connection_remains_bound_to_the_validated_descriptor_during_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_kind: str,
) -> None:
    original_directory = tmp_path / "original"
    replacement_directory = tmp_path / "replacement"
    original_directory.mkdir(mode=0o700)
    replacement_directory.mkdir(mode=0o700)
    original_path = original_directory / "kegg.sqlite3"
    replacement_path = replacement_directory / "kegg.sqlite3"
    _write_response(SQLiteKeggCache(original_path), body=b"validated-original")
    _write_response(SQLiteKeggCache(replacement_path), body=b"unvalidated-replacement")
    real_connect = cache_module.sqlite3.connect
    swapped = False

    def swapping_connect(
        database: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        nonlocal swapped
        if not swapped and kwargs.get("uri") is True:
            assert "/proc/self/fd/" in os.fsdecode(database)
            if swap_kind == "file":
                moved_path = original_directory / "validated.sqlite3"
                original_path.rename(moved_path)
                replacement_path.rename(original_path)
            else:
                moved_directory = tmp_path / "validated-original-directory"
                original_directory.rename(moved_directory)
                replacement_directory.rename(original_directory)
            swapped = True
        return real_connect(database, *args, **kwargs)  # type: ignore[call-overload]

    monkeypatch.setattr(cache_module.sqlite3, "connect", swapping_connect)

    lookup = _read_response(SQLiteKeggCache(original_path, read_only=True))

    assert swapped is True
    assert lookup.response is not None
    assert lookup.response.body == b"validated-original"


def test_read_only_connection_verifies_trusted_schema_was_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    _write_response(SQLiteKeggCache(cache_path))
    real_connect = cache_module.sqlite3.connect

    class TrustedSchemaProxy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def execute(
            self,
            statement: str,
            parameters: tuple[object, ...] = (),
        ) -> sqlite3.Cursor:
            if statement == "PRAGMA trusted_schema = OFF":
                statement = "PRAGMA trusted_schema = ON"
            return self.connection.execute(statement, parameters)

        def close(self) -> None:
            self.connection.close()

    def connect_with_ignored_pragma(
        database: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        connection = cast(
            sqlite3.Connection,
            real_connect(database, *args, **kwargs),  # type: ignore[call-overload]
        )
        return cast(sqlite3.Connection, TrustedSchemaProxy(connection))

    monkeypatch.setattr(cache_module.sqlite3, "connect", connect_with_ignored_pragma)

    with pytest.raises(KeggMcpError) as error:
        _read_response(SQLiteKeggCache(cache_path, read_only=True))

    _assert_cache_failed(error, cache_path)


@pytest.mark.parametrize("invalid_schema", ["view", "columns", "primary_key", "trigger"])
def test_read_only_cache_rejects_same_version_with_incompatible_schema_structure(
    tmp_path: Path,
    invalid_schema: str,
) -> None:
    cache_path = tmp_path / f"invalid-{invalid_schema}.sqlite3"
    _write_response(SQLiteKeggCache(cache_path))
    with sqlite3.connect(cache_path) as connection:
        if invalid_schema == "view":
            connection.execute("DROP TABLE kegg_responses")
            connection.execute(
                "CREATE VIEW kegg_responses AS SELECT 'get' AS operation, X'00' AS response_body"
            )
        elif invalid_schema == "columns":
            connection.execute("DROP TABLE kegg_responses")
            connection.execute(
                "CREATE TABLE kegg_responses (operation TEXT PRIMARY KEY) WITHOUT ROWID"
            )
        else:
            if invalid_schema == "primary_key":
                schema_row = connection.execute(
                    "SELECT sql FROM sqlite_schema WHERE type = 'table' AND name = 'kegg_responses'"
                ).fetchone()
                assert schema_row is not None
                original_schema = str(schema_row[0])
                connection.execute("DROP TABLE kegg_responses")
                reversed_primary_key = original_schema.replace(
                    "operation,\n        normalized_request_key,",
                    "normalized_request_key,\n        operation,",
                )
                connection.execute(reversed_primary_key)
            else:
                connection.execute(
                    "CREATE TRIGGER reject_cache_update BEFORE UPDATE ON kegg_responses "
                    "BEGIN SELECT RAISE(ABORT, 'blocked'); END"
                )
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (1,)
    cache_path.chmod(0o600)

    with pytest.raises(KeggMcpError) as error:
        _read_response(SQLiteKeggCache(cache_path, read_only=True))

    _assert_cache_failed(error, cache_path)


def test_read_only_cache_rejects_incompatible_journal_mode(tmp_path: Path) -> None:
    cache_path = tmp_path / "wal-cache.sqlite3"
    _write_response(SQLiteKeggCache(cache_path))
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
    cache_path.chmod(0o600)

    with pytest.raises(KeggMcpError) as error:
        _read_response(SQLiteKeggCache(cache_path, read_only=True))

    _assert_cache_failed(error, cache_path)


def test_read_only_cache_rejects_incompatible_auto_vacuum_mode(tmp_path: Path) -> None:
    cache_path = tmp_path / "no-auto-vacuum.sqlite3"
    _write_response(SQLiteKeggCache(cache_path))
    with sqlite3.connect(cache_path) as connection:
        connection.execute("PRAGMA auto_vacuum = NONE")
        connection.execute("VACUUM")
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (0,)
    cache_path.chmod(0o600)

    with pytest.raises(KeggMcpError) as error:
        _read_response(SQLiteKeggCache(cache_path, read_only=True))

    _assert_cache_failed(error, cache_path)
