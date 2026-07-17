"""Tests for scoped, bounded, atomic local result persistence."""

import os
import re
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_mcp.domain import ErrorCode, KeggMcpError
from kegg_mcp.services.result_store import (
    DEFAULT_MAX_DATABASE_BYTES,
    DEFAULT_MAX_RESULTS,
    DEFAULT_QUOTA_BYTES,
    DEFAULT_RETENTION_SECONDS,
    DeletedResult,
    ResultArtifactInput,
    ResultArtifactRange,
    ResultMetadataPage,
    ResultStoreError,
    ResultStoreLimits,
    SQLiteResultStore,
)

_NOW = datetime(2026, 7, 14, 3, 0, 0, tzinfo=UTC)
_RESULT_ID_PATTERN = re.compile(r"res_[A-Za-z0-9_-]{32}\Z")


def _artifact(
    section: str,
    content: bytes,
    mime_type: str = "application/json",
) -> ResultArtifactInput:
    return ResultArtifactInput(section=section, mime_type=mime_type, content=content)


def _assert_not_found(error: pytest.ExceptionInfo[KeggMcpError], *secrets: str) -> None:
    assert error.value.detail.code is ErrorCode.RESULT_NOT_FOUND
    assert error.value.detail.recoverable is True
    assert error.value.detail.suggested_action
    assert error.value.detail.safe_details == ()
    serialized = error.value.detail.model_dump_json()
    for secret in secrets:
        if len(secret) >= 8:
            assert secret not in serialized


def test_default_lifecycle_decisions_and_strict_bounded_limit_schema() -> None:
    limits = ResultStoreLimits()

    assert limits.retention_seconds == DEFAULT_RETENTION_SECONDS == 86_400
    assert limits.quota_bytes == DEFAULT_QUOTA_BYTES == 512 * 1024 * 1024
    assert limits.max_results == DEFAULT_MAX_RESULTS == 10_000
    assert limits.max_database_bytes == DEFAULT_MAX_DATABASE_BYTES == 640 * 1024 * 1024

    with pytest.raises(ValidationError):
        ResultStoreLimits.model_validate({"quota_bytes": True}, strict=True)
    with pytest.raises(ValidationError):
        ResultStoreLimits.model_validate({"retention_seconds": 2_592_001}, strict=True)
    with pytest.raises(ValidationError):
        ResultStoreLimits.model_validate(
            {"max_artifact_bytes": 10, "max_result_bytes": 9}, strict=True
        )
    with pytest.raises(ValidationError):
        ResultStoreLimits.model_validate({"unexpected": 1}, strict=True)
    with pytest.raises(ValidationError, match="leave bounded"):
        ResultStoreLimits(quota_bytes=1024 * 1024, max_database_bytes=1024 * 1024)


def test_multiple_artifacts_round_trip_with_complete_immutable_metadata(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "results" / "store.sqlite3")
    artifacts = (
        _artifact("summary.json", b'{"status":"ok"}'),
        _artifact("report.md", b"# Report\n", "text/markdown; charset=utf-8"),
        _artifact("records.csv", b"ko,status\n", "text/csv; charset=utf-8"),
    )

    created = store.create("session-alpha", artifacts, now=_NOW)

    assert _RESULT_ID_PATTERN.fullmatch(created.result_id)
    assert created.created_at == _NOW
    assert created.expires_at == _NOW + timedelta(hours=24)
    assert created.total_bytes == sum(len(artifact.content) for artifact in artifacts)
    assert created.artifact_count == 3
    assert not hasattr(created, "scope_id")
    assert store.get_result("session-alpha", created.result_id, now=_NOW) == created

    page = store.list_artifacts("session-alpha", created.result_id, now=_NOW)

    assert tuple(item.section for item in page.items) == (
        "summary.json",
        "report.md",
        "records.csv",
    )
    assert tuple(item.mime_type for item in page.items) == tuple(
        artifact.mime_type for artifact in artifacts
    )
    assert tuple(item.byte_size for item in page.items) == tuple(
        len(artifact.content) for artifact in artifacts
    )
    with pytest.raises(ValidationError):
        created.total_bytes = 0


def test_each_created_result_uses_a_distinct_unpredictable_opaque_id(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "store.sqlite3")

    first = store.create("scope", (_artifact("one.json", b"1"),), now=_NOW)
    second = store.create("scope", (_artifact("two.json", b"2"),), now=_NOW)

    assert first.result_id != second.result_id
    assert _RESULT_ID_PATTERN.fullmatch(first.result_id)
    assert _RESULT_ID_PATTERN.fullmatch(second.result_id)
    assert "scope" not in first.result_id


def test_every_result_operation_is_isolated_to_the_originating_scope(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "store.sqlite3")
    created = store.create("origin-scope", (_artifact("summary.json", b"secret"),), now=_NOW)

    operations = (
        lambda: store.get_result("other-scope", created.result_id, now=_NOW),
        lambda: store.list_artifacts("other-scope", created.result_id, now=_NOW),
        lambda: store.read_artifact("other-scope", created.result_id, "summary.json", now=_NOW),
        lambda: store.delete("other-scope", created.result_id, now=_NOW),
    )
    for operation in operations:
        with pytest.raises(KeggMcpError) as error:
            operation()
        _assert_not_found(error, created.result_id, "origin-scope", "other-scope", "secret")

    assert store.list_results("other-scope", now=_NOW).total_items == 0
    assert store.get_result("origin-scope", created.result_id, now=_NOW) == created


@pytest.mark.parametrize(
    "invalid_result_id",
    (
        "../result",
        "..%2Fresult",
        "res_%2e%2e%2fsecret",
        "res_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/",
        "res_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\\",
        "res_short",
        "' OR 1=1 --",
    ),
)
def test_invalid_or_encoded_result_identifiers_fail_as_not_found(
    tmp_path: Path,
    invalid_result_id: str,
) -> None:
    store = SQLiteResultStore(tmp_path / "sensitive-directory" / "store.sqlite3")

    with pytest.raises(KeggMcpError) as error:
        store.get_result("scope", invalid_result_id, now=_NOW)

    _assert_not_found(error, invalid_result_id, str(tmp_path))


@pytest.mark.parametrize(
    "invalid_section",
    ("../summary.json", "..%2Fsummary.json", "%2e%2e", "a/b", "a\\b", ".", ".."),
)
def test_traversal_and_encoded_separators_are_rejected_for_sections(
    tmp_path: Path,
    invalid_section: str,
) -> None:
    store = SQLiteResultStore(tmp_path / "store.sqlite3")
    created = store.create("scope", (_artifact("summary.json", b"safe"),), now=_NOW)

    with pytest.raises(KeggMcpError) as error:
        store.read_artifact("scope", created.result_id, invalid_section, now=_NOW)

    _assert_not_found(error, invalid_section, created.result_id)


@pytest.mark.parametrize("invalid_scope", ("../scope", "scope%2Fother", "a/b", ".", ".."))
def test_invalid_creation_scopes_are_rejected_before_storage(
    tmp_path: Path,
    invalid_scope: str,
) -> None:
    store = SQLiteResultStore(tmp_path / "store.sqlite3")

    with pytest.raises(ValueError, match="invalid result scope identifier"):
        store.create(invalid_scope, (_artifact("summary.json", b"safe"),), now=_NOW)

    assert not (tmp_path / "store.sqlite3").exists()


def test_result_and_artifact_listing_are_paginated_with_explicit_totals(
    tmp_path: Path,
) -> None:
    limits = ResultStoreLimits(default_page_size=2, max_page_size=2)
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)
    first = store.create(
        "scope",
        (
            _artifact("a.json", b"a"),
            _artifact("b.json", b"b"),
            _artifact("c.json", b"c"),
        ),
        now=_NOW,
    )
    store.create("scope", (_artifact("d.json", b"d"),), now=_NOW + timedelta(seconds=1))
    store.create("scope", (_artifact("e.json", b"e"),), now=_NOW + timedelta(seconds=2))

    result_page_one = store.list_results("scope", now=_NOW + timedelta(seconds=3))
    result_page_two = store.list_results("scope", offset=2, now=_NOW + timedelta(seconds=3))
    artifact_page_one = store.list_artifacts("scope", first.result_id, now=_NOW)
    artifact_page_two = store.list_artifacts("scope", first.result_id, offset=2, now=_NOW)

    assert result_page_one.total_items == 3
    assert len(result_page_one.items) == 2
    assert result_page_one.next_offset == 2
    assert len(result_page_two.items) == 1
    assert result_page_two.next_offset is None
    assert artifact_page_one.total_items == 3
    assert tuple(item.section for item in artifact_page_one.items) == ("a.json", "b.json")
    assert artifact_page_one.next_offset == 2
    assert tuple(item.section for item in artifact_page_two.items) == ("c.json",)
    assert artifact_page_two.next_offset is None


def test_result_listing_does_not_create_a_missing_store(tmp_path: Path) -> None:
    parent = tmp_path / "missing"
    store = SQLiteResultStore(parent / "store.sqlite3")

    page = store.list_results("scope", offset=7, limit=3, now=_NOW)

    assert page.items == ()
    assert page.total_items == 0
    assert page.offset == 7
    assert page.limit == 3
    assert page.next_offset is None
    assert not parent.exists()


def test_result_reads_do_not_create_a_missing_store(tmp_path: Path) -> None:
    parent = tmp_path / "missing-reads"
    store = SQLiteResultStore(parent / "store.sqlite3")
    result_id = "res_" + "a" * 32
    operations = (
        lambda: store.get_result("scope", result_id, now=_NOW),
        lambda: store.list_artifacts("scope", result_id, now=_NOW),
        lambda: store.read_artifact("scope", result_id, "summary.json", now=_NOW),
    )

    for operation in operations:
        with pytest.raises(KeggMcpError) as error:
            operation()
        _assert_not_found(error, result_id)
    assert not parent.exists()


class _ReadOnlyAccessStore(SQLiteResultStore):
    def _connect(self) -> sqlite3.Connection:
        raise AssertionError("result read requested a writable connection")


def test_result_reads_use_read_only_connections_for_an_existing_store(tmp_path: Path) -> None:
    path = tmp_path / "store.sqlite3"
    created = SQLiteResultStore(path).create(
        "scope",
        (_artifact("summary.json", b"safe"),),
        now=_NOW,
    )
    before = path.stat()
    store = _ReadOnlyAccessStore(path)

    result = store.get_result("scope", created.result_id, now=_NOW)
    page = store.list_results("scope", now=_NOW)
    artifacts = store.list_artifacts("scope", created.result_id, now=_NOW)
    content = store.read_artifact("scope", created.result_id, "summary.json", now=_NOW)

    after = path.stat()
    assert result == created
    assert len(page.items) == 1
    assert tuple(item.section for item in artifacts.items) == ("summary.json",)
    assert content.content == b"safe"
    assert after.st_mtime_ns == before.st_mtime_ns
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)


def test_artifact_range_reads_reconstruct_content_and_bound_each_chunk(tmp_path: Path) -> None:
    limits = ResultStoreLimits(default_range_bytes=3, max_range_bytes=3)
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)
    created = store.create("scope", (_artifact("payload.json", b"abcdefgh"),), now=_NOW)

    first = store.read_artifact("scope", created.result_id, "payload.json", now=_NOW)
    second = store.read_artifact(
        "scope", created.result_id, "payload.json", offset=first.next_offset or 0, now=_NOW
    )
    third = store.read_artifact(
        "scope", created.result_id, "payload.json", offset=second.next_offset or 0, now=_NOW
    )
    beyond = store.read_artifact("scope", created.result_id, "payload.json", offset=99, now=_NOW)

    assert first.content + second.content + third.content == b"abcdefgh"
    assert (first.total_bytes, first.returned_bytes, first.next_offset) == (8, 3, 3)
    assert (second.offset, second.returned_bytes, second.next_offset) == (3, 3, 6)
    assert (third.offset, third.returned_bytes, third.next_offset) == (6, 2, None)
    assert (beyond.total_bytes, beyond.content, beyond.next_offset) == (8, b"", None)
    with pytest.raises(KeggMcpError) as error:
        store.read_artifact("scope", created.result_id, "payload.json", limit=4, now=_NOW)
    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_extreme_offsets_fail_with_a_bounded_safe_domain_error(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "store.sqlite3")
    created = store.create("scope", (_artifact("payload.json", b"safe"),), now=_NOW)
    extreme_offset = 10**100
    operations = (
        lambda: store.list_results("scope", offset=extreme_offset, now=_NOW),
        lambda: store.list_artifacts(
            "scope",
            created.result_id,
            offset=extreme_offset,
            now=_NOW,
        ),
        lambda: store.read_artifact(
            "scope",
            created.result_id,
            "payload.json",
            offset=extreme_offset,
            now=_NOW,
        ),
    )

    for operation in operations:
        with pytest.raises(KeggMcpError) as error:
            operation()
        assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
        assert error.value.detail.recoverable is True
        assert tuple(detail.name for detail in error.value.detail.safe_details) == (
            "limit",
            "actual",
            "maximum",
        )
        assert len(error.value.detail.safe_details[1].value) < 32


def test_expired_results_are_hidden_identically_and_cleanup_reclaims_them(tmp_path: Path) -> None:
    limits = ResultStoreLimits(retention_seconds=10)
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)
    created = store.create("scope", (_artifact("summary.json", b"expired"),), now=_NOW)
    at_expiry = _NOW + timedelta(seconds=10)

    operations = (
        lambda: store.get_result("scope", created.result_id, now=at_expiry),
        lambda: store.list_artifacts("scope", created.result_id, now=at_expiry),
        lambda: store.read_artifact("scope", created.result_id, "summary.json", now=at_expiry),
        lambda: store.delete("scope", created.result_id, now=at_expiry),
    )
    for operation in operations:
        with pytest.raises(KeggMcpError) as error:
            operation()
        _assert_not_found(error, created.result_id, "scope", "expired")

    assert store.list_results("scope", now=at_expiry).total_items == 0
    summary = store.cleanup(now=at_expiry)
    assert summary.expired_results == 1
    assert summary.expired_bytes == len(b"expired")
    assert summary.evicted_results == 0
    assert summary.remaining_results == 0
    assert summary.remaining_bytes == 0


def test_expired_only_cleanup_preserves_active_results_and_never_quota_evicts(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(
        tmp_path / "store.sqlite3",
        limits=ResultStoreLimits(retention_seconds=10),
    )
    expired = store.create("scope-a", (_artifact("old.json", b"old"),), now=_NOW)
    active = store.create(
        "scope-b",
        (_artifact("active.json", b"active"),),
        now=_NOW + timedelta(seconds=5),
    )

    summary = store.cleanup_expired(now=_NOW + timedelta(seconds=10))

    assert summary.expired_results == 1
    assert summary.expired_bytes == 3
    assert summary.evicted_results == 0
    assert summary.evicted_bytes == 0
    assert summary.remaining_results == 1
    assert store.get_result("scope-b", active.result_id, now=_NOW + timedelta(seconds=10)) == active
    with pytest.raises(KeggMcpError) as error:
        store.get_result("scope-a", expired.result_id, now=_NOW + timedelta(seconds=10))
    _assert_not_found(error, expired.result_id)


def test_scope_deletion_removes_only_one_session_and_is_noop_without_database(
    tmp_path: Path,
) -> None:
    missing_store = SQLiteResultStore(tmp_path / "missing" / "store.sqlite3")
    assert missing_store.delete_scope("scope-a").deleted_results == 0
    assert not (tmp_path / "missing").exists()

    store = SQLiteResultStore(tmp_path / "store.sqlite3")
    first = store.create(
        "scope-a",
        (_artifact("one.json", b"one"), _artifact("two.json", b"two")),
        now=_NOW,
    )
    retained = store.create("scope-b", (_artifact("three.json", b"three"),), now=_NOW)

    summary = store.delete_scope("scope-a")

    assert summary.deleted_results == 1
    assert summary.deleted_artifacts == 2
    assert summary.deleted_bytes == 6
    assert store.get_result("scope-b", retained.result_id, now=_NOW) == retained
    with pytest.raises(KeggMcpError) as error:
        store.get_result("scope-a", first.result_id, now=_NOW)
    _assert_not_found(error, first.result_id)


def test_create_rejects_quota_overflow_without_evicting_active_results(tmp_path: Path) -> None:
    limits = ResultStoreLimits(
        quota_bytes=6,
        max_artifact_bytes=6,
        max_result_bytes=6,
    )
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)
    oldest = store.create("scope", (_artifact("old.json", b"old"),), now=_NOW)
    middle = store.create(
        "scope", (_artifact("middle.json", b"mid"),), now=_NOW + timedelta(seconds=1)
    )

    with pytest.raises(KeggMcpError) as error:
        store.create(
            "scope",
            (_artifact("new.json", b"new"),),
            now=_NOW + timedelta(seconds=2),
        )

    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert store.get_result("scope", oldest.result_id, now=_NOW + timedelta(seconds=3)) == oldest
    assert store.get_result("scope", middle.result_id, now=_NOW + timedelta(seconds=3)) == middle
    page = store.list_results("scope", now=_NOW + timedelta(seconds=3))
    assert page.total_items == 2
    assert sum(item.total_bytes for item in page.items) == 6


def test_capacity_failure_in_one_scope_never_evicts_another_scope(tmp_path: Path) -> None:
    limits = ResultStoreLimits(
        quota_bytes=3,
        max_artifact_bytes=3,
        max_result_bytes=3,
    )
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)
    retained = store.create("scope-a", (_artifact("a.json", b"aaa"),), now=_NOW)

    with pytest.raises(KeggMcpError) as error:
        store.create(
            "scope-b",
            (_artifact("b.json", b"b"),),
            now=_NOW + timedelta(seconds=1),
        )

    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert (
        store.get_result("scope-a", retained.result_id, now=_NOW + timedelta(seconds=2)) == retained
    )
    assert store.list_results("scope-b", now=_NOW + timedelta(seconds=2)).total_items == 0


def test_max_result_count_bounds_zero_byte_metadata_growth(tmp_path: Path) -> None:
    limits = ResultStoreLimits(max_results=2)
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)
    first = store.create("scope", (_artifact("first.json", b""),), now=_NOW)
    second = store.create(
        "scope",
        (_artifact("second.json", b""),),
        now=_NOW + timedelta(seconds=1),
    )

    with pytest.raises(KeggMcpError) as error:
        store.create(
            "scope",
            (_artifact("third.json", b""),),
            now=_NOW + timedelta(seconds=2),
        )

    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert store.get_result("scope", first.result_id, now=_NOW + timedelta(seconds=3)) == first
    assert store.get_result("scope", second.result_id, now=_NOW + timedelta(seconds=3)) == second


def test_create_removes_expired_results_before_evicting_active_results(tmp_path: Path) -> None:
    limits = ResultStoreLimits(
        retention_seconds=10,
        quota_bytes=6,
        max_artifact_bytes=6,
        max_result_bytes=6,
    )
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)
    expired = store.create("scope", (_artifact("old.json", b"old"),), now=_NOW)
    active = store.create(
        "scope", (_artifact("active.json", b"act"),), now=_NOW + timedelta(seconds=5)
    )
    newest = store.create(
        "scope", (_artifact("new.json", b"new"),), now=_NOW + timedelta(seconds=11)
    )

    with pytest.raises(KeggMcpError):
        store.get_result("scope", expired.result_id, now=_NOW + timedelta(seconds=11))
    assert store.get_result("scope", active.result_id, now=_NOW + timedelta(seconds=11)) == active
    assert store.get_result("scope", newest.result_id, now=_NOW + timedelta(seconds=11)) == newest


def test_oversized_or_duplicate_artifact_groups_never_partially_store(tmp_path: Path) -> None:
    limits = ResultStoreLimits(
        quota_bytes=4,
        max_artifact_bytes=3,
        max_result_bytes=4,
    )
    store = SQLiteResultStore(tmp_path / "store.sqlite3", limits=limits)

    with pytest.raises(KeggMcpError) as error:
        store.create("scope", (_artifact("large.json", b"1234"),), now=_NOW)
    assert error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    with pytest.raises(ValueError, match="section names must be unique"):
        store.create(
            "scope",
            (_artifact("same.json", b"1"), _artifact("same.json", b"2")),
            now=_NOW,
        )

    assert store.list_results("scope", now=_NOW).total_items == 0


def test_sqlite_failure_after_first_artifact_rolls_back_the_entire_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "store.sqlite3"
    store = SQLiteResultStore(database)
    seed = store.create("scope", (_artifact("seed.json", b"seed"),), now=_NOW)
    store.delete("scope", seed.result_id, now=_NOW)
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_second_artifact
            BEFORE INSERT ON result_artifacts
            WHEN NEW.section = 'reject.json'
            BEGIN
                SELECT RAISE(ABORT, 'sensitive payload path must never escape');
            END
            """
        )

    with pytest.raises(ResultStoreError) as error:
        store.create(
            "scope",
            (_artifact("first.json", b"first"), _artifact("reject.json", b"second")),
            now=_NOW,
        )

    assert error.value.stage == "create"
    assert "sensitive" not in str(error.value)
    assert str(database) not in str(error.value)
    assert store.list_results("scope", now=_NOW).total_items == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM stored_results").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM result_artifacts").fetchone() == (0,)


def test_explicit_delete_returns_counts_and_then_hides_the_identifier(tmp_path: Path) -> None:
    store = SQLiteResultStore(tmp_path / "store.sqlite3")
    created = store.create(
        "scope",
        (_artifact("one.json", b"one"), _artifact("two.json", b"two")),
        now=_NOW,
    )

    deleted = store.delete("scope", created.result_id, now=_NOW + timedelta(seconds=1))

    assert deleted == DeletedResult(
        result_id=created.result_id,
        deleted_at=_NOW + timedelta(seconds=1),
        deleted_artifacts=2,
        deleted_bytes=6,
    )
    with pytest.raises(KeggMcpError) as error:
        store.get_result("scope", created.result_id, now=_NOW + timedelta(seconds=1))
    _assert_not_found(error, created.result_id)


class _FailingResultStore(SQLiteResultStore):
    def _connect(self) -> sqlite3.Connection:
        raise sqlite3.OperationalError(
            "/private/user/result-store.sqlite3 includes API_SECRET=do-not-leak"
        )


def test_storage_failures_never_leak_paths_secrets_or_payloads(tmp_path: Path) -> None:
    store = _FailingResultStore(tmp_path / "private" / "store.sqlite3")

    with pytest.raises(ResultStoreError) as error:
        store.create("scope", (_artifact("summary.json", b"private-payload"),), now=_NOW)

    serialized = str(error.value)
    assert error.value.stage == "create"
    assert str(tmp_path) not in serialized
    assert "API_SECRET" not in serialized
    assert "private-payload" not in serialized


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are unavailable")
def test_new_store_directory_and_database_have_restrictive_permissions(tmp_path: Path) -> None:
    store_directory = tmp_path / "private-results"
    database = store_directory / "store.sqlite3"
    store = SQLiteResultStore(database)

    store.create("scope", (_artifact("summary.json", b"safe"),), now=_NOW)

    assert stat.S_IMODE(store_directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership checks are unavailable")
def test_store_rejects_final_database_not_owned_by_current_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "store.sqlite3"
    database.touch(mode=0o600)
    actual_uid = database.stat().st_uid
    observed_uids = iter((actual_uid, actual_uid + 1))
    monkeypatch.setattr(os, "geteuid", lambda: next(observed_uids))

    with pytest.raises(ResultStoreError):
        SQLiteResultStore(database).create(
            "scope",
            (_artifact("summary.json", b"safe"),),
            now=_NOW,
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink checks are unavailable")
def test_store_rejects_parent_and_final_symlink_escapes(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir(mode=0o700)
    outside.mkdir(mode=0o700)
    (allowed / "jump").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ResultStoreError):
        SQLiteResultStore(allowed / "jump" / "escaped.sqlite3").create(
            "scope",
            (_artifact("summary.json", b"safe"),),
            now=_NOW,
        )
    assert not (outside / "escaped.sqlite3").exists()

    target = outside / "target.sqlite3"
    target.write_bytes(b"do-not-touch")
    (allowed / "store.sqlite3").symlink_to(target)
    with pytest.raises(ResultStoreError):
        SQLiteResultStore(allowed / "store.sqlite3").create(
            "scope",
            (_artifact("summary.json", b"safe"),),
            now=_NOW,
        )
    assert target.read_bytes() == b"do-not-touch"


def test_store_rejects_configured_path_traversal(tmp_path: Path) -> None:
    configured = tmp_path / "allowed" / ".." / "escaped.sqlite3"

    with pytest.raises(ResultStoreError):
        SQLiteResultStore(configured).create(
            "scope",
            (_artifact("summary.json", b"safe"),),
            now=_NOW,
        )

    assert not (tmp_path / "escaped.sqlite3").exists()


def test_cleanup_releases_free_pages_with_full_auto_vacuum(tmp_path: Path) -> None:
    database = tmp_path / "store.sqlite3"
    limits = ResultStoreLimits(
        retention_seconds=1,
        quota_bytes=2 * 1024 * 1024,
        max_artifact_bytes=2 * 1024 * 1024,
        max_result_bytes=2 * 1024 * 1024,
    )
    store = SQLiteResultStore(database, limits=limits)
    store.create(
        "scope",
        (_artifact("large.json", b"x" * (1024 * 1024)),),
        now=_NOW,
    )
    size_before = database.stat().st_size

    summary = store.cleanup(now=_NOW + timedelta(seconds=1))
    size_after = database.stat().st_size

    assert summary.expired_results == 1
    assert summary.remaining_results == 0
    assert size_after < size_before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA auto_vacuum").fetchone() == (1,)


def test_public_contracts_reject_extra_fields_and_encode_bytes_safely() -> None:
    with pytest.raises(ValidationError):
        ResultArtifactInput.model_validate(
            {
                "section": "summary.json",
                "mime_type": "application/json",
                "content": b"{}",
                "local_path": "/secret/result.json",
            },
            strict=True,
        )
    with pytest.raises(ValidationError):
        ResultMetadataPage.model_validate(
            {
                "items": (),
                "total_items": 0,
                "offset": 0,
                "limit": 1,
                "next_offset": None,
                "scope_id": "secret-scope",
            },
            strict=True,
        )

    artifact_range = ResultArtifactRange(
        result_id="res_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        section="binary.dat",
        mime_type="application/octet-stream",
        total_bytes=2,
        offset=0,
        limit=2,
        returned_bytes=2,
        next_offset=None,
        content=b"\xff\x00",
    )
    assert '"content":"_wA="' in artifact_range.model_dump_json()
    assert ResultArtifactInput.model_json_schema()["additionalProperties"] is False
