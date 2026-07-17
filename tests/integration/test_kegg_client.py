"""Offline integration tests for the typed KEGG client and local cache."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, NoReturn

import pytest

import kegg_mcp.kegg.client as client_module
import kegg_mcp.services.kegg_mapping as kegg_mapping_module
from kegg_mcp.analysis import PathwaySelection, PathwaySelectionMode
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, fail
from kegg_mcp.kegg.cache import CacheLookup, CacheReadState, SQLiteKeggCache
from kegg_mcp.kegg.client import KeggClient
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
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
    LicensedAccess,
    LinkRequest,
    OfflineCacheAccess,
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
from kegg_mcp.services.annotation_analysis import analyze_annotation_targets
from kegg_mcp.services.kegg_mapping import retrieve_kegg_entries
from kegg_mcp.services.models import MAX_GET_PROVENANCE_BATCHES, NormalizeAnnotationsRequest
from kegg_mcp.services.result_store import SQLiteResultStore

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
        endpoint_fingerprint: str,
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
            endpoint_fingerprint,
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

    def __init__(
        self,
        scope: str,
        requests_per_second: float,
        *,
        state_root: str,
    ) -> None:
        del requests_per_second, state_root
        self.scope = scope
        self.acquire_count = 0
        self.instances.append(self)

    def acquire(self) -> None:
        self.acquire_count += 1


@pytest.fixture(autouse=True)
def replace_mandatory_limiter_for_network_free_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _NoWaitMandatoryLimiter.instances.clear()
    monkeypatch.setattr(client_module, "DeploymentRateLimiter", _NoWaitMandatoryLimiter)


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


def _licensed_config(
    cache_path: Path,
    *,
    endpoint: str,
    label: str,
) -> KeggClientConfig:
    return KeggClientConfig(
        access=LicensedAccess(
            authorized_use_confirmed=True,
            endpoint=endpoint,
            endpoint_label=label,
        ),
        cache=CachePolicy(path=str(cache_path), ttl_seconds=60),
    )


def _offline_config(cache_path: Path, *, ttl_seconds: int = 60) -> KeggClientConfig:
    return KeggClientConfig(
        access=OfflineCacheAccess(),
        cache=CachePolicy(path=str(cache_path), ttl_seconds=ttl_seconds),
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
        PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
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


def test_offline_profile_reuses_public_cache_and_never_calls_injected_transport(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "offline.sqlite3"
    request = InfoRequest(database=KeggInfoDatabase.KO)
    KeggClient(
        _public_config(cache_path),
        transport=QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)]),
        clock=_clock(_NOW),
    ).info(request)

    offline = KeggClient(
        _offline_config(cache_path),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=1)),
    ).info(request)

    assert offline.batch.origin is ResponseOrigin.CACHE
    assert offline.batch.access_mode is AccessMode.OFFLINE_CACHE
    assert offline.batch.retrieval_endpoint_class is RetrievalEndpointClass.PUBLIC_ACADEMIC
    assert _NoWaitMandatoryLimiter.instances[-1].acquire_count == 0


def test_offline_profile_cache_miss_never_calls_injected_transport(tmp_path: Path) -> None:
    cache_path = tmp_path / "missing.sqlite3"
    with pytest.raises(KeggMcpError) as caught:
        KeggClient(
            _offline_config(cache_path),
            transport=BombTransport(),
            clock=_clock(_NOW),
        ).info(InfoRequest(database=KeggInfoDatabase.KO))

    assert caught.value.detail.code is ErrorCode.CACHE_ENTRY_NOT_FOUND
    assert _NoWaitMandatoryLimiter.instances[-1].acquire_count == 0
    assert not cache_path.exists()


def test_offline_profile_rejects_an_injected_writable_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / "injected.sqlite3"

    with pytest.raises(ValueError, match="read-only cache adapter"):
        KeggClient(
            _offline_config(cache_path),
            cache=SQLiteKeggCache(cache_path),
            transport=BombTransport(),
            clock=_clock(_NOW),
        )

    assert not cache_path.exists()


def test_same_licensed_endpoint_uses_one_cache_and_rate_scope_across_labels(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "licensed.sqlite3"
    endpoint = "https://licensed.example.test/api"
    request = InfoRequest(database=KeggInfoDatabase.KO)
    first = KeggClient(
        _licensed_config(cache_path, endpoint=endpoint, label="operator-a"),
        transport=QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)]),
        clock=_clock(_NOW),
    ).info(request)
    second = KeggClient(
        _licensed_config(cache_path, endpoint=endpoint, label="operator-b"),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=1)),
    ).info(request, options=KeggRequestOptions(refresh=False, cache_only=True))

    assert first.batch.endpoint_label == "operator-a"
    assert second.batch.endpoint_label == "operator-b"
    assert second.batch.origin is ResponseOrigin.CACHE
    assert (
        _NoWaitMandatoryLimiter.instances[-2].scope == _NoWaitMandatoryLimiter.instances[-1].scope
    )


def test_distinct_licensed_endpoints_do_not_share_cache_with_the_same_label(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "licensed.sqlite3"
    request = InfoRequest(database=KeggInfoDatabase.KO)
    KeggClient(
        _licensed_config(
            cache_path,
            endpoint="https://licensed-a.example.test/api",
            label="institutional",
        ),
        transport=QueueTransport([TransportResponse(status_code=200, body=_INFO_BODY)]),
        clock=_clock(_NOW),
    ).info(request)

    with pytest.raises(KeggMcpError) as caught:
        KeggClient(
            _licensed_config(
                cache_path,
                endpoint="https://licensed-b.example.test/api",
                label="institutional",
            ),
            transport=BombTransport(),
            clock=_clock(_NOW + timedelta(seconds=1)),
        ).info(request, options=KeggRequestOptions(refresh=False, cache_only=True))

    assert caught.value.detail.code is ErrorCode.CACHE_ENTRY_NOT_FOUND
    assert (
        _NoWaitMandatoryLimiter.instances[-2].scope != _NoWaitMandatoryLimiter.instances[-1].scope
    )


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


def test_offline_multi_entry_service_bounds_direct_provenance_and_retains_every_batch(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "offline-entry-level.sqlite3"
    entries = tuple(
        KeggEntryRef(database=KeggGetDatabase.KO, identifier=f"K{index:05d}")
        for index in range(1, MAX_GET_PROVENANCE_BATCHES + 2)
    )
    request = GetRequest(entries=entries)
    KeggClient(
        _public_config(cache_path),
        transport=QueueTransport(
            [
                TransportResponse(
                    status_code=200,
                    body=b"".join(_flat_entry(entry.identifier) for entry in entries),
                )
            ]
        ),
        clock=_clock(_NOW),
    ).get(request)
    store = SQLiteResultStore(tmp_path / "offline-entry-results.sqlite3")

    result = retrieve_kegg_entries(
        request,
        client=KeggClient(
            _offline_config(cache_path),
            transport=BombTransport(),
            clock=_clock(_NOW + timedelta(seconds=1)),
        ),
        result_store=store,
        scope_id="offline-entry-service",
    )

    assert result.provenance_batch_count == len(entries)
    assert len(result.provenance) == MAX_GET_PROVENANCE_BATCHES
    assert result.provenance_truncated is True
    assert all(batch.origin is ResponseOrigin.CACHE for batch in result.provenance)
    retained = json.loads(
        store.read_artifact(
            "offline-entry-service",
            result.result.result_id,
            "detail",
            limit=1_000_000,
        ).content
    )
    assert len(retained["batches"]) == len(entries)
    assert all(batch["origin"] == "cache" for batch in retained["batches"])


def test_partial_entry_cache_service_bounds_preview_without_losing_mixed_provenance(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "partial-entry-service.sqlite3"
    entries = tuple(
        KeggEntryRef(database=KeggGetDatabase.KO, identifier=f"K{index:05d}")
        for index in range(1, 8)
    )
    cached_entries = entries[::2]
    KeggClient(
        _public_config(cache_path),
        transport=QueueTransport(
            [
                TransportResponse(
                    status_code=200,
                    body=b"".join(_flat_entry(entry.identifier) for entry in cached_entries),
                )
            ]
        ),
        clock=_clock(_NOW),
    ).get(GetRequest(entries=cached_entries))
    transport = QueueTransport(
        [
            TransportResponse(status_code=200, body=_flat_entry(entry.identifier))
            for entry in entries[1::2]
        ]
    )
    store = SQLiteResultStore(tmp_path / "partial-entry-service-results.sqlite3")

    result = retrieve_kegg_entries(
        GetRequest(entries=entries),
        client=KeggClient(
            _public_config(cache_path),
            transport=transport,
            clock=_clock(_NOW + timedelta(seconds=1)),
        ),
        result_store=store,
        scope_id="partial-entry-service",
        options=KeggRequestOptions(refresh=False),
    )

    assert result.provenance_batch_count == len(entries)
    assert len(result.provenance) == MAX_GET_PROVENANCE_BATCHES
    assert result.provenance_truncated is True
    retained = json.loads(
        store.read_artifact(
            "partial-entry-service",
            result.result.result_id,
            "detail",
            limit=1_000_000,
        ).content
    )
    assert [batch["origin"] for batch in retained["batches"]] == [
        "cache",
        "network",
        "cache",
        "network",
        "cache",
        "network",
        "cache",
    ]
    assert transport.urls == [
        "https://rest.kegg.jp/get/K00002",
        "https://rest.kegg.jp/get/K00004",
        "https://rest.kegg.jp/get/K00006",
    ]


def test_entry_result_model_failure_compensates_the_created_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001")
    store = SQLiteResultStore(tmp_path / "compensated-entry-results.sqlite3")

    def reject_result_model(**values: object) -> NoReturn:
        del values
        raise ValueError("synthetic result-model failure")

    monkeypatch.setattr(kegg_mapping_module, "KeggEntriesServiceResult", reject_result_model)

    with pytest.raises(ValueError, match="synthetic result-model failure"):
        retrieve_kegg_entries(
            GetRequest(entries=(entry,)),
            client=KeggClient(
                _public_config(tmp_path / "compensated-entry-cache.sqlite3"),
                transport=QueueTransport(
                    [TransportResponse(status_code=200, body=_flat_entry(entry.identifier))]
                ),
                clock=_clock(_NOW),
            ),
            result_store=store,
            scope_id="compensated-entry-service",
        )

    assert store.list_results("compensated-entry-service").total_items == 0


def test_partial_entry_cache_requests_only_contiguous_misses_and_preserves_order(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "partial-entry-cache.sqlite3"
    first = KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001")
    second = KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00002")
    third = KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00003")
    fourth = KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00004")
    KeggClient(
        _public_config(cache_path),
        transport=QueueTransport(
            [
                TransportResponse(
                    status_code=200,
                    body=_flat_entry(first.identifier) + _flat_entry(third.identifier),
                )
            ]
        ),
        clock=_clock(_NOW),
    ).get(GetRequest(entries=(first, third)))
    transport = QueueTransport(
        [
            TransportResponse(
                status_code=200,
                body=_flat_entry(fourth.identifier) + _flat_entry(second.identifier),
            )
        ]
    )

    result = KeggClient(
        _public_config(cache_path),
        transport=transport,
        clock=_clock(_NOW + timedelta(seconds=1)),
    ).get(
        GetRequest(entries=(first, second, fourth, third)),
        options=KeggRequestOptions(refresh=False),
    )

    ordered_identifiers = [
        entry.identifier
        for document in result.documents
        if isinstance(document, KeggFlatFileDocument)
        for entry in document.entries
    ]
    assert ordered_identifiers == ["K00001", "K00002", "K00004", "K00003"]
    assert [batch.origin for batch in result.batches] == [
        ResponseOrigin.CACHE,
        ResponseOrigin.NETWORK,
        ResponseOrigin.CACHE,
    ]
    assert transport.urls == ["https://rest.kegg.jp/get/K00002+K00004"]


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


def test_seventy_three_ko_link_request_uses_one_batch_and_reuses_its_cache(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "adaptive-link.sqlite3"
    ko_ids = tuple(f"K{index:05d}" for index in range(1, 74))
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=ko_ids,
    )
    body = b"".join(
        f"ko:{ko_id}\tpath:ko{index:05d}\n".encode("ascii")
        for index, ko_id in enumerate(ko_ids, start=1)
    )
    transport = QueueTransport([TransportResponse(status_code=200, body=body)])
    first = KeggClient(
        _public_config(cache_path),
        transport=transport,
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    ).link(request)

    reused = KeggClient(
        _public_config(cache_path),
        transport=BombTransport(),
        clock=_clock(_NOW + timedelta(seconds=1)),
    ).link(
        LinkRequest(
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            source_identifiers=tuple(reversed(ko_ids)),
        ),
        options=KeggRequestOptions(refresh=False, cache_only=True),
    )

    assert len(transport.urls) == 1
    assert len(first.batches) == 1
    assert len(first.rows) == 73
    assert len(reused.batches) == 1
    assert reused.batches[0].origin is ResponseOrigin.CACHE
    assert len(reused.rows) == 73


def test_equivalent_top_pathway_analysis_reports_cache_hits_without_network_repeats(
    tmp_path: Path,
) -> None:
    ko_ids = tuple(f"K{index:05d}" for index in range(1, 74))
    mapping_body = b"".join(f"ko:{ko_id}\tpath:ko00001\n".encode("ascii") for ko_id in ko_ids)
    denominator_body = b"".join(
        f"path:ko00001\tko:{ko_id}\n".encode("ascii") for ko_id in ko_ids[:3]
    )
    metadata_body = (
        b"ENTRY       ko00001                    Pathway\n"
        b"NAME        Synthetic cached pathway\n"
        b"CLASS       Metabolism; Synthetic class\n"
        b"///\n"
    )
    transport = QueueTransport(
        [
            TransportResponse(status_code=200, body=mapping_body),
            TransportResponse(status_code=200, body=denominator_body),
            TransportResponse(status_code=200, body=metadata_body),
        ]
    )
    client = KeggClient(
        _public_config(tmp_path / "analysis-cache.sqlite3"),
        transport=transport,
        rate_limiter=RecordingLimiter(),
        clock=_clock(_NOW),
    )
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    request = NormalizeAnnotationsRequest(text="\n".join(ko_ids))
    selection = PathwaySelection(mode=PathwaySelectionMode.TOP_DETECTED, top_n=1)

    first = analyze_annotation_targets(
        request,
        module_ids=(),
        pathways=(),
        pathway_selection=selection,
        client=client,
        result_store=store,
        scope_id="cache-summary",
    )
    second = analyze_annotation_targets(
        request,
        module_ids=(),
        pathways=(),
        pathway_selection=selection,
        client=client,
        result_store=store,
        scope_id="cache-summary",
    )

    assert len(transport.urls) == 3
    assert first.summary.kegg_request_count == 3
    assert first.summary.network_request_count == 3
    assert first.summary.cache_hit_count == 0
    assert second.summary.kegg_request_count == 3
    assert second.summary.network_request_count == 0
    assert second.summary.cache_hit_count == 3
    retained = json.loads(
        store.read_artifact(
            "cache-summary",
            second.result.result_id,
            "structured",
            limit=1_000_000,
        ).content
    )["report"]
    assert retained["execution_metrics"][1]["cache_hit_count"] == 1
    assert retained["execution_metrics"][3]["cache_hit_count"] == 2


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
        sleeper=lambda _delay: None,
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
        sleeper=lambda _delay: None,
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
        PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
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
        PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
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
    limits = KeggClientLimits(link_batch_size=1)
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
        PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
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
