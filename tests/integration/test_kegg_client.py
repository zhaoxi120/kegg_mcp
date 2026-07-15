"""Offline integration tests for the typed KEGG client and local cache."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, NoReturn

import pytest

import kegg_mcp.kegg.client as client_module
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, fail
from kegg_mcp.kegg.cache import CacheLookup, CacheReadState, SQLiteKeggCache
from kegg_mcp.kegg.client import KeggClient
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    CachePolicy,
    ConvRequest,
    GetRequest,
    HttpMetadata,
    InfoRequest,
    KeggBriteEntryKind,
    KeggClientConfig,
    KeggClientLimits,
    KeggConvDatabase,
    KeggEntryRef,
    KeggFlatFileDocument,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    KeggOperation,
    KeggRequestOptions,
    LinkRequest,
    PublicAcademicAccess,
    ResponseOrigin,
    RetrievalEndpointClass,
    RetryPolicy,
)
from kegg_mcp.kegg.operations import (
    PreparedRequest,
    prepare_conv,
    prepare_get,
    prepare_info,
    prepare_link,
)
from kegg_mcp.kegg.transport import (
    TransportError,
    TransportErrorKind,
    TransportResponse,
)

_NOW = datetime(2026, 7, 14, 3, 0, tzinfo=UTC)
_INFO_BODY = (
    b"ko              KEGG Orthology database\n"
    b"ko              Release 116.0+/07-13, Jul 26\n"
    b"10 entries\n"
)


def _flat_entry(identifier: str) -> bytes:
    return (
        f"ENTRY       {identifier}                      KO\n"
        f"NAME        Synthetic {identifier}\n"
        "///\n"
    ).encode()


class QueueTransport:
    def __init__(self, responses: list[TransportResponse | TransportError]) -> None:
        self._responses = responses.copy()
        self.urls: list[str] = []

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del timeout_seconds, max_response_bytes
        self.urls.append(url)
        if not self._responses:
            raise AssertionError("unexpected transport request")
        response = self._responses.pop(0)
        if isinstance(response, TransportError):
            raise response
        return response


class BombTransport:
    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del url, timeout_seconds, max_response_bytes
        raise AssertionError("cache-only request attempted network access")


class RecordingLimiter:
    def __init__(self) -> None:
        self.acquire_count = 0

    def acquire(self) -> None:
        self.acquire_count += 1


class WriteFailingCache(SQLiteKeggCache):
    def write(
        self,
        operation: KeggOperation,
        normalized_request_key: str,
        retrieval_endpoint_class: RetrievalEndpointClass,
        endpoint_label: str,
        *,
        body: bytes,
        retrieved_at: datetime,
        expires_at: datetime,
        parser_version: str,
        database_release: str | None,
        http_metadata: tuple[HttpMetadata, ...] = (),
    ) -> NoReturn:
        del (
            normalized_request_key,
            retrieval_endpoint_class,
            endpoint_label,
            body,
            retrieved_at,
            expires_at,
            parser_version,
            database_release,
            http_metadata,
        )
        fail(
            ErrorCode.CACHE_FAILED,
            "The local KEGG cache could not be used safely.",
            suggested_action="Replace the configured local cache before retrying.",
            safe_details=(),
        )


class _NoWaitMandatoryLimiter:
    instances: ClassVar[list[_NoWaitMandatoryLimiter]] = []

    def __init__(self, scope: str, requests_per_second: float) -> None:
        del scope, requests_per_second
        self.acquire_count = 0
        self.instances.append(self)

    def acquire(self) -> None:
        self.acquire_count += 1


@pytest.fixture(autouse=True)
def replace_mandatory_limiter_for_network_free_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _NoWaitMandatoryLimiter.instances.clear()
    monkeypatch.setattr(client_module, "ProcessWideRateLimiter", _NoWaitMandatoryLimiter)


def _public_config(
    cache_path: Path,
    *,
    ttl_seconds: int = 60,
    retry: RetryPolicy | None = None,
    limits: KeggClientLimits | None = None,
) -> KeggClientConfig:
    return KeggClientConfig(
        access=PublicAcademicAccess(academic_use_confirmed=True),
        cache=CachePolicy(path=str(cache_path), ttl_seconds=ttl_seconds),
        retry=retry or RetryPolicy(),
        limits=limits or KeggClientLimits(),
    )


def _clock(value: datetime) -> Callable[[], datetime]:
    return lambda: value


def _read_public_cache(
    cache_path: Path, prepared: PreparedRequest, *, now: datetime
) -> CacheLookup:
    return SQLiteKeggCache(cache_path).read(
        prepared.operation,
        prepared.normalized_request_key,
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_LABEL,
        now=now,
        expected_parser_version=PARSER_VERSION,
    )


def test_network_result_is_cached_and_reused_without_network(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    transport = QueueTransport(
        [
            TransportResponse(
                status_code=200,
                body=_INFO_BODY,
                http_metadata=(HttpMetadata(name="etag", value='"info-v1"'),),
            )
        ]
    )
    limiter = RecordingLimiter()
    request = InfoRequest(database=KeggInfoDatabase.KO)
    live = KeggClient(
        _public_config(cache_path),
        transport=transport,
        rate_limiter=limiter,
        clock=_clock(_NOW),
    )

    network_result = live.info(request)
    cached_result = KeggClient(
        _public_config(cache_path),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=1)),
    ).info(
        request,
        options=KeggRequestOptions(refresh=False, cache_only=True),
    )

    assert network_result.batch.origin is ResponseOrigin.NETWORK
    assert network_result.batch.access_mode is AccessMode.PUBLIC_ACADEMIC
    assert network_result.batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert network_result.batch.retrieval_endpoint_class is RetrievalEndpointClass.PUBLIC_ACADEMIC
    assert network_result.batch.endpoint_label == PUBLIC_KEGG_ENDPOINT_LABEL
    assert network_result.batch.response_bytes == len(_INFO_BODY)
    assert network_result.batch.parser_version == PARSER_VERSION
    assert network_result.batch.http_metadata == (HttpMetadata(name="etag", value='"info-v1"'),)
    assert network_result.batch.retrieved_at == _NOW
    assert network_result.batch.served_at == _NOW
    assert network_result.batch.expires_at == _NOW + timedelta(seconds=60)
    assert network_result.batch.attempt_count == 1
    assert network_result.document.release == "116.0+/07-13, Jul 26"
    assert cached_result.batch.origin is ResponseOrigin.CACHE
    assert cached_result.batch.access_mode is AccessMode.PUBLIC_ACADEMIC
    assert cached_result.batch.cache_lookup_state is CacheLookupState.FRESH_HIT
    assert cached_result.batch.http_metadata == network_result.batch.http_metadata
    assert cached_result.batch.attempt_count == 0
    assert cached_result.document == network_result.document
    assert limiter.acquire_count == 1
    assert sum(instance.acquire_count for instance in _NoWaitMandatoryLimiter.instances) == 1
    assert len(transport.urls) == 1


def test_multi_entry_get_populates_single_entry_and_arbitrary_subset_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "entry-level.sqlite3"
    entries = (
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00002"),
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00003"),
    )
    transport = QueueTransport(
        [
            TransportResponse(
                status_code=200,
                body=b"".join(_flat_entry(entry.identifier) for entry in entries),
            )
        ]
    )
    live = KeggClient(
        _public_config(cache_path),
        transport=transport,
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    live.get(GetRequest(entries=entries))
    cached = KeggClient(
        _public_config(cache_path),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=1)),
    )
    cache_only = KeggRequestOptions(refresh=False, cache_only=True)
    single = cached.get(GetRequest(entries=(entries[1],)), options=cache_only)
    subset = cached.get(GetRequest(entries=(entries[2], entries[0])), options=cache_only)

    assert single.batches[0].origin is ResponseOrigin.CACHE
    assert all(isinstance(document, KeggFlatFileDocument) for document in subset.documents)
    flat_documents = tuple(
        document for document in subset.documents if isinstance(document, KeggFlatFileDocument)
    )
    assert [document.entries[0].identifier for document in flat_documents] == [
        "K00003",
        "K00001",
    ]
    assert all(batch.origin is ResponseOrigin.CACHE for batch in subset.batches)
    assert len(transport.urls) == 1


def test_relationship_cache_key_is_independent_of_identifier_order(tmp_path: Path) -> None:
    cache_path = tmp_path / "canonical-link.sqlite3"
    first = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_MODULE,
        source_identifiers=("K00002", "K00001"),
    )
    reversed_request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_MODULE,
        source_identifiers=("K00001", "K00002"),
    )
    first_prepared = prepare_link(first, KeggClientLimits())[0]
    reversed_prepared = prepare_link(reversed_request, KeggClientLimits())[0]
    assert first_prepared.normalized_request_key == reversed_prepared.normalized_request_key

    KeggClient(
        _public_config(cache_path),
        transport=QueueTransport(
            [
                TransportResponse(
                    status_code=200,
                    body=b"ko:K00001\tmd:M00001\nko:K00002\tmd:M00002\n",
                )
            ]
        ),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    ).link(first)
    reused = KeggClient(
        _public_config(cache_path),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=1)),
    ).link(
        reversed_request,
        options=KeggRequestOptions(refresh=False, cache_only=True),
    )

    assert reused.batches[0].origin is ResponseOrigin.CACHE
    assert [(row.source_id, row.target_id) for row in reused.rows] == [
        ("ko:K00001", "md:M00001"),
        ("ko:K00002", "md:M00002"),
    ]


def test_cache_only_miss_never_calls_injected_transport(tmp_path: Path) -> None:
    client = KeggClient(
        _public_config(tmp_path / "missing.sqlite3"),
        transport=BombTransport(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.info(
            InfoRequest(database=KeggInfoDatabase.KO),
            options=KeggRequestOptions(refresh=False, cache_only=True),
        )

    assert caught.value.detail.code is ErrorCode.CACHE_ENTRY_NOT_FOUND


def test_stale_cache_requires_explicit_permission_for_cache_only_reads(tmp_path: Path) -> None:
    cache_path = tmp_path / "stale.sqlite3"
    request = InfoRequest(database=KeggInfoDatabase.KO)
    live = KeggClient(
        _public_config(cache_path, ttl_seconds=10),
        transport=QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )
    live.info(request)
    stale_client = KeggClient(
        _public_config(cache_path, ttl_seconds=10),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=10)),
    )

    with pytest.raises(KeggMcpError) as caught:
        stale_client.info(
            request,
            options=KeggRequestOptions(refresh=False, cache_only=True),
        )
    stale_result = stale_client.info(
        request,
        options=KeggRequestOptions(refresh=False, allow_stale=True, cache_only=True),
    )

    assert caught.value.detail.code is ErrorCode.CACHE_ENTRY_NOT_FOUND
    assert stale_result.batch.is_stale is True
    assert stale_result.batch.origin is ResponseOrigin.CACHE


def test_live_refresh_bypasses_and_replaces_a_fresh_cache_entry(tmp_path: Path) -> None:
    cache_path = tmp_path / "refresh.sqlite3"
    request = InfoRequest(database=KeggInfoDatabase.KO)
    first_body = _INFO_BODY
    refreshed_body = _INFO_BODY.replace(b"116.0+", b"117.0+")
    KeggClient(
        _public_config(cache_path),
        transport=QueueTransport([TransportResponse(status_code=200, body=first_body)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    ).info(request)
    refresh_transport = QueueTransport([TransportResponse(status_code=200, body=refreshed_body)])
    refreshed = KeggClient(
        _public_config(cache_path),
        transport=refresh_transport,
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW + timedelta(seconds=1)),
    ).info(request, options=KeggRequestOptions(refresh=True))
    cached = KeggClient(
        _public_config(cache_path),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=2)),
    ).info(
        request,
        options=KeggRequestOptions(refresh=False, cache_only=True),
    )

    assert refreshed.batch.origin is ResponseOrigin.NETWORK
    assert refreshed.batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert refreshed.document.release == "117.0+/07-13, Jul 26"
    assert cached.document == refreshed.document
    assert len(refresh_transport.urls) == 1


def test_live_stale_policy_can_refetch_or_explicitly_serve_without_network(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "live-stale.sqlite3"
    request = InfoRequest(database=KeggInfoDatabase.KO)
    initial = KeggClient(
        _public_config(cache_path, ttl_seconds=10),
        transport=QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )
    initial.info(request)

    stale = KeggClient(
        _public_config(cache_path, ttl_seconds=10),
        transport=BombTransport(),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW + timedelta(seconds=10)),
    ).info(request, options=KeggRequestOptions(refresh=False, allow_stale=True))

    assert stale.batch.origin is ResponseOrigin.CACHE
    assert stale.batch.cache_lookup_state is CacheLookupState.STALE_HIT
    assert stale.batch.is_stale is True

    refreshed_body = _INFO_BODY.replace(b"116.0+", b"118.0+")
    transport = QueueTransport([TransportResponse(status_code=200, body=refreshed_body)])
    refetched = KeggClient(
        _public_config(cache_path, ttl_seconds=10),
        transport=transport,
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW + timedelta(seconds=10)),
    ).info(request)

    assert refetched.batch.origin is ResponseOrigin.NETWORK
    assert refetched.batch.cache_lookup_state is CacheLookupState.REFRESH_BYPASS
    assert len(transport.urls) == 1


def test_transient_responses_retry_with_backoff_and_rate_limit_each_attempt(
    tmp_path: Path,
) -> None:
    retry = RetryPolicy(
        max_retries=2,
        initial_backoff_seconds=0.5,
        max_backoff_seconds=2.0,
        jitter_seconds=0.0,
    )
    transport = QueueTransport(
        [
            TransportResponse(status_code=503, body=b"unavailable"),
            TransportResponse(status_code=200, body=_INFO_BODY),
        ]
    )
    limiter = RecordingLimiter()
    sleeps: list[float] = []
    client = KeggClient(
        _public_config(tmp_path / "retry.sqlite3", retry=retry),
        transport=transport,
        rate_limiter=limiter,
        sleeper=sleeps.append,
        random_uniform=lambda lower, upper: lower + upper,
        clock=_clock(_NOW),
    )

    result = client.info(InfoRequest(database=KeggInfoDatabase.KO))

    assert result.batch.attempt_count == 2
    assert limiter.acquire_count == 2
    assert sleeps == [0.5]


def test_transient_transport_error_is_retried(tmp_path: Path) -> None:
    transport = QueueTransport(
        [
            TransportError(TransportErrorKind.TIMEOUT, transient=True),
            TransportResponse(status_code=200, body=_INFO_BODY),
        ]
    )
    limiter = RecordingLimiter()
    client = KeggClient(
        _public_config(
            tmp_path / "transport-retry.sqlite3",
            retry=RetryPolicy(
                max_retries=1,
                initial_backoff_seconds=0.0,
                max_backoff_seconds=0.0,
                jitter_seconds=0.0,
            ),
        ),
        transport=transport,
        rate_limiter=limiter,
        sleeper=lambda delay: None,
        clock=_clock(_NOW),
    )

    result = client.info(InfoRequest(database=KeggInfoDatabase.KO))

    assert result.batch.attempt_count == 2
    assert limiter.acquire_count == 2


def test_terminal_transport_error_is_wrapped_without_exception_context(tmp_path: Path) -> None:
    client = KeggClient(
        _public_config(tmp_path / "terminal-transport.sqlite3"),
        transport=QueueTransport([TransportError(TransportErrorKind.TLS, transient=False)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.info(InfoRequest(database=KeggInfoDatabase.KO))

    assert caught.value.detail.code is ErrorCode.KEGG_REQUEST_FAILED
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    [
        (400, ErrorCode.KEGG_REQUEST_FAILED),
        (404, ErrorCode.KEGG_ENTRY_NOT_FOUND),
    ],
)
def test_deterministic_client_errors_are_not_retried(
    tmp_path: Path, status_code: int, expected_code: ErrorCode
) -> None:
    transport = QueueTransport([TransportResponse(status_code=status_code, body=b"")])
    limiter = RecordingLimiter()
    client = KeggClient(
        _public_config(tmp_path / f"status-{status_code}.sqlite3"),
        transport=transport,
        rate_limiter=limiter,
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.info(InfoRequest(database=KeggInfoDatabase.KO))

    assert caught.value.detail.code is expected_code
    assert limiter.acquire_count == 1
    assert len(transport.urls) == 1


def test_exhausted_429_responses_return_rate_limited_error(tmp_path: Path) -> None:
    transport = QueueTransport(
        [
            TransportResponse(status_code=429, body=b""),
            TransportResponse(status_code=429, body=b""),
        ]
    )
    limiter = RecordingLimiter()
    client = KeggClient(
        _public_config(
            tmp_path / "rate.sqlite3",
            retry=RetryPolicy(
                max_retries=1,
                initial_backoff_seconds=0.0,
                max_backoff_seconds=0.0,
                jitter_seconds=0.0,
            ),
        ),
        transport=transport,
        rate_limiter=limiter,
        sleeper=lambda delay: None,
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.info(InfoRequest(database=KeggInfoDatabase.KO))

    assert caught.value.detail.code is ErrorCode.KEGG_RATE_LIMITED
    assert limiter.acquire_count == 2


def test_get_reports_partial_200_entries_without_calling_them_absent_evidence(
    tmp_path: Path,
) -> None:
    request = GetRequest(
        entries=(
            KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),
            KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00002"),
        )
    )
    client = KeggClient(
        _public_config(tmp_path / "partial.sqlite3"),
        transport=QueueTransport([TransportResponse(status_code=200, body=_flat_entry("K00001"))]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    result = client.get(request)

    assert [entry.identifier for entry in result.missing_entries] == ["K00002"]
    assert len(result.documents) == 1


def test_get_reconciles_the_enzyme_entry_ec_marker(tmp_path: Path) -> None:
    request = GetRequest(
        entries=(KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier="1.1.1.1"),)
    )
    client = KeggClient(
        _public_config(tmp_path / "enzyme.sqlite3"),
        transport=QueueTransport(
            [
                TransportResponse(
                    status_code=200,
                    body=b"ENTRY       EC 1.1.1.1\nNAME        alcohol dehydrogenase\n///\n",
                )
            ]
        ),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    result = client.get(request)

    assert result.missing_entries == ()


def test_unrequested_get_entry_fails_before_the_response_is_cached(tmp_path: Path) -> None:
    cache_path = tmp_path / "unexpected.sqlite3"
    request = GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),))
    config = _public_config(cache_path)
    client = KeggClient(
        config,
        transport=QueueTransport([TransportResponse(status_code=200, body=_flat_entry("K99999"))]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.get(request)

    prepared = prepare_get(request, config.limits)[0]
    lookup = SQLiteKeggCache(cache_path).read(
        KeggOperation.GET,
        prepared.normalized_request_key,
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_LABEL,
        now=_NOW,
        expected_parser_version=PARSER_VERSION,
    )

    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert lookup.state is CacheReadState.MISS


@pytest.mark.parametrize(
    "body",
    [
        b"ko:K99999\tmd:M00001\n",
        b"ko:K00001\tpath:map00010\n",
    ],
)
def test_unexpected_link_mapping_fails_before_cache_write(tmp_path: Path, body: bytes) -> None:
    cache_path = tmp_path / "link.sqlite3"
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_MODULE,
        source_identifiers=("K00001",),
    )
    config = _public_config(cache_path)
    client = KeggClient(
        config,
        transport=QueueTransport([TransportResponse(status_code=200, body=body)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.link(request)

    prepared = prepare_link(request, config.limits)[0]
    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert _read_public_cache(cache_path, prepared, now=_NOW).state is CacheReadState.MISS


@pytest.mark.parametrize(
    "body",
    [
        b"ko:K00001\tec:1.--.1.1\n",
        b"ko:K00001\tec:1.1-2.3.4\n",
        b"ko:K00001\tec:1.1.-.1\n",
    ],
)
def test_malformed_enzyme_target_fails_before_cache_write(tmp_path: Path, body: bytes) -> None:
    cache_path = tmp_path / "enzyme.sqlite3"
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_ENZYME,
        source_identifiers=("K00001",),
    )
    config = _public_config(cache_path)
    client = KeggClient(
        config,
        transport=QueueTransport([TransportResponse(status_code=200, body=body)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.link(request)

    prepared = prepare_link(request, config.limits)[0]
    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert _read_public_cache(cache_path, prepared, now=_NOW).state is CacheReadState.MISS


def test_non_htext_brite_response_fails_before_cache_write(tmp_path: Path) -> None:
    cache_path = tmp_path / "invalid-brite.sqlite3"
    request = GetRequest(
        entries=(
            KeggEntryRef(
                database=KeggGetDatabase.BRITE,
                identifier="br08901",
                brite_kind=KeggBriteEntryKind.HIERARCHY,
            ),
        )
    )
    config = _public_config(cache_path)
    client = KeggClient(
        config,
        transport=QueueTransport([TransportResponse(status_code=200, body=b"Not found\n")]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.get(request)

    prepared = prepare_get(request, config.limits)[0]
    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert _read_public_cache(cache_path, prepared, now=_NOW).state is CacheReadState.MISS


def test_client_rechecks_fake_transport_response_size_before_parsing(tmp_path: Path) -> None:
    cache_path = tmp_path / "oversized-network.sqlite3"
    limits = KeggClientLimits(max_response_bytes=10)
    request = InfoRequest(database=KeggInfoDatabase.KO)
    config = _public_config(cache_path, limits=limits)
    client = KeggClient(
        config,
        transport=QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.info(request)

    prepared = prepare_info(request, config.limits)[0]
    assert caught.value.detail.code is ErrorCode.KEGG_REQUEST_FAILED
    assert _read_public_cache(cache_path, prepared, now=_NOW).state is CacheReadState.MISS


def test_cache_only_read_rechecks_cached_response_size_under_current_limit(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "oversized-cache.sqlite3"
    limits = KeggClientLimits(max_response_bytes=10)
    config = _public_config(cache_path, limits=limits)
    request = InfoRequest(database=KeggInfoDatabase.KO)
    prepared = prepare_info(request, config.limits)[0]
    cache = SQLiteKeggCache(cache_path)
    cache.write(
        prepared.operation,
        prepared.normalized_request_key,
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_LABEL,
        body=_INFO_BODY,
        retrieved_at=_NOW,
        expires_at=_NOW + timedelta(seconds=60),
        parser_version=PARSER_VERSION,
        database_release=None,
    )

    with pytest.raises(KeggMcpError) as caught:
        KeggClient(
            config,
            cache=cache,
            transport=BombTransport(),
            clock=_clock(_NOW),
        ).info(
            request,
            options=KeggRequestOptions(refresh=False, cache_only=True),
        )

    assert caught.value.detail.code is ErrorCode.CACHE_FAILED


def test_link_rows_record_their_batch_index(tmp_path: Path) -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_MODULE,
        source_identifiers=("K00001", "K00002"),
    )
    limits = KeggClientLimits(relation_batch_size=1)
    client = KeggClient(
        _public_config(tmp_path / "link.sqlite3", limits=limits),
        transport=QueueTransport(
            [
                TransportResponse(status_code=200, body=b"ko:K00001\tmd:M00001\n"),
                TransportResponse(status_code=200, body=b"ko:K00002\tmd:M00002\n"),
            ]
        ),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    result = client.link(request)

    assert [row.batch_index for row in result.rows] == [0, 1]
    assert len(result.batches) == 2


def test_selected_conversion_uses_the_typed_client_surface(tmp_path: Path) -> None:
    request = ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.UNIPROT,
        source_identifiers=("uniprot:P12345",),
    )
    client = KeggClient(
        _public_config(tmp_path / "conv.sqlite3"),
        transport=QueueTransport(
            [TransportResponse(status_code=200, body=b"up:P12345\tddi:DDB_G0291764\n")]
        ),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    result = client.conv(request)

    assert [(row.source_id, row.target_id) for row in result.rows] == [
        ("up:P12345", "ddi:DDB_G0291764")
    ]


@pytest.mark.parametrize(
    ("conv_request", "body", "cache_name"),
    [
        (
            ConvRequest(
                target_database=KeggConvDatabase.GENES,
                source_database=KeggConvDatabase.UNIPROT,
                source_identifiers=("uniprot:P12345",),
            ),
            b"up:P12345\tuniprot:P12345\n",
            "wrong-gene-target.sqlite3",
        ),
        (
            ConvRequest(
                target_database=KeggConvDatabase.GENES,
                source_database=KeggConvDatabase.UNIPROT,
                source_identifiers=("uniprot:P12345",),
            ),
            b"up:P12345\tvtax:1234\n",
            "wrong-vtax-target.sqlite3",
        ),
        (
            ConvRequest(
                target_database=KeggConvDatabase.NCBI_GENEID,
                source_database=KeggConvDatabase.GENES,
                source_identifiers=("hsa:1",),
            ),
            b"hsa:1\tncbi-geneid:not-a-number\n",
            "wrong-ncbi-gene-target.sqlite3",
        ),
    ],
)
def test_unexpected_conversion_target_fails_before_cache_write(
    tmp_path: Path,
    conv_request: ConvRequest,
    body: bytes,
    cache_name: str,
) -> None:
    cache_path = tmp_path / cache_name
    config = _public_config(cache_path)
    client = KeggClient(
        config,
        transport=QueueTransport([TransportResponse(status_code=200, body=body)]),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.conv(conv_request)

    prepared = prepare_conv(conv_request, config.limits)[0]
    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert _read_public_cache(cache_path, prepared, now=_NOW).state is CacheReadState.MISS


def test_cached_parser_failure_is_reported_as_cache_failure(tmp_path: Path) -> None:
    cache_path = tmp_path / "bad-parser.sqlite3"
    request = InfoRequest(database=KeggInfoDatabase.KO)
    config = _public_config(cache_path)
    prepared = prepare_info(request, config.limits)[0]
    cache = SQLiteKeggCache(cache_path)
    cache.write(
        prepared.operation,
        prepared.normalized_request_key,
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_LABEL,
        body=b"not an INFO response\n",
        retrieved_at=_NOW,
        expires_at=_NOW + timedelta(seconds=60),
        parser_version=PARSER_VERSION,
        database_release=None,
    )
    client = KeggClient(config, cache=cache, transport=BombTransport(), clock=_clock(_NOW))

    with pytest.raises(KeggMcpError) as caught:
        client.info(
            request,
            options=KeggRequestOptions(refresh=False, cache_only=True),
        )

    assert caught.value.detail.code is ErrorCode.CACHE_FAILED


def test_live_cache_read_failure_does_not_fall_back_to_network(tmp_path: Path) -> None:
    transport = QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)])
    client = KeggClient(
        _public_config(tmp_path),
        transport=transport,
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.info(
            InfoRequest(database=KeggInfoDatabase.KO),
            options=KeggRequestOptions(refresh=False),
        )

    assert caught.value.detail.code is ErrorCode.CACHE_FAILED
    assert transport.urls == []


def test_live_cache_write_failure_does_not_return_an_unpersisted_result(tmp_path: Path) -> None:
    cache_path = tmp_path / "write-failure.sqlite3"
    transport = QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)])
    client = KeggClient(
        _public_config(cache_path),
        transport=transport,
        cache=WriteFailingCache(cache_path),
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )

    with pytest.raises(KeggMcpError) as caught:
        client.info(InfoRequest(database=KeggInfoDatabase.KO))

    assert caught.value.detail.code is ErrorCode.CACHE_FAILED
    assert len(transport.urls) == 1
