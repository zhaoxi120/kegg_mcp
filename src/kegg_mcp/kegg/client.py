"""Typed KEGG service client with local caching, retries, and provenance."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeAlias

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, SafeDetail, fail
from kegg_mcp.kegg.cache import CachedResponse, CacheReadState, SQLiteKeggCache
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    CacheLookupState,
    ConvRequest,
    ConvResult,
    GetRequest,
    GetResult,
    InfoRequest,
    InfoResult,
    KeggBatchProvenance,
    KeggBriteHtextDocument,
    KeggClientConfig,
    KeggFlatFileDocument,
    KeggGetDatabase,
    KeggGetDocument,
    KeggInfoDocument,
    KeggPairDocument,
    KeggPairRow,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    PublicAcademicAccess,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.operations import (
    PreparedRequest,
    ResponseParser,
    pair_target_matches,
    prepare_conv,
    prepare_get,
    prepare_info,
    prepare_link,
)
from kegg_mcp.kegg.parsers import (
    parse_brite_htext_response,
    parse_flat_file_response,
    parse_info_response,
    parse_pair_response,
)
from kegg_mcp.kegg.rate_limit import ProcessWideRateLimiter
from kegg_mcp.kegg.transport import HttpsTransport, Transport, TransportError, TransportResponse

_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 500, 502, 503, 504})

_ParsedDocument: TypeAlias = (
    KeggInfoDocument | KeggFlatFileDocument | KeggBriteHtextDocument | KeggPairDocument
)


class RateLimiter(Protocol):
    """Small injectable rate-limiter surface used by the service client."""

    def acquire(self) -> None:
        """Wait until one request attempt is permitted."""
        ...


@dataclass(frozen=True, slots=True)
class _ExecutedBatch:
    document: _ParsedDocument
    provenance: KeggBatchProvenance
    body: bytes


class KeggClient:
    """Execute the Milestone 2 KEGG operations through typed service methods."""

    def __init__(
        self,
        config: KeggClientConfig,
        *,
        transport: Transport | None = None,
        cache: SQLiteKeggCache | None = None,
        rate_limiter: RateLimiter | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleeper: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._config = config
        self._cache = cache or SQLiteKeggCache(config.cache.path)
        self._clock = clock
        self._sleeper = sleeper
        self._random_uniform = random_uniform

        access = config.access
        if isinstance(access, PublicAcademicAccess):
            self._endpoint = access.endpoint
            self._retrieval_endpoint_class = RetrievalEndpointClass.PUBLIC_ACADEMIC
            self._endpoint_label = PUBLIC_KEGG_ENDPOINT_LABEL
        else:
            self._endpoint = access.endpoint
            self._retrieval_endpoint_class = RetrievalEndpointClass.LICENSED
            self._endpoint_label = access.endpoint_label
        self._transport = transport or HttpsTransport()
        self._mandatory_rate_limiter = ProcessWideRateLimiter(
            self._endpoint_label,
            config.limits.requests_per_second,
        )
        self._additional_rate_limiter = rate_limiter

    @property
    def config(self) -> KeggClientConfig:
        """Return the immutable serializable client configuration."""
        return self._config

    def info(
        self,
        request: InfoRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> InfoResult:
        """Retrieve one allowlisted KEGG INFO document."""
        prepared = prepare_info(request, self._config.limits)[0]
        executed = self._execute(prepared, options or KeggRequestOptions(), info_request=request)
        if not isinstance(executed.document, KeggInfoDocument):
            raise AssertionError("INFO preparation selected an incompatible parser")
        return InfoResult(request=request, document=executed.document, batch=executed.provenance)

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        """Retrieve selected textual entries and report partial 200 responses explicitly."""
        effective_options = options or KeggRequestOptions()
        entry_cache_hits = self._read_complete_entry_cache(request, effective_options)
        if entry_cache_hits is None:
            prepared_batches = prepare_get(request, self._config.limits)
            executed_batches = tuple(
                self._execute(prepared, effective_options) for prepared in prepared_batches
            )
            for prepared, executed in zip(prepared_batches, executed_batches, strict=True):
                self._store_entry_cache_rows(prepared, executed)
        else:
            prepared_batches, executed_batches = entry_cache_hits
        documents: list[KeggGetDocument] = []
        returned_keys: set[tuple[KeggGetDatabase, str]] = set()

        for prepared, executed in zip(prepared_batches, executed_batches, strict=True):
            document = executed.document
            expected_by_identifier = {
                entry.identifier: entry for entry in prepared.requested_entries
            }
            if isinstance(document, KeggFlatFileDocument):
                returned_identifiers: set[str] = set()
                for entry in document.entries:
                    if (
                        entry.identifier not in expected_by_identifier
                        or entry.identifier in returned_identifiers
                    ):
                        fail(
                            ErrorCode.KEGG_PARSE_FAILED,
                            "The KEGG GET response did not match the bounded request.",
                            suggested_action=(
                                "Refresh the response or verify the configured endpoint "
                                "compatibility."
                            ),
                            safe_details=(
                                SafeDetail(name="reason", value="unexpected_or_duplicate_entry"),
                            ),
                        )
                    returned_identifiers.add(entry.identifier)
                    requested_entry = expected_by_identifier[entry.identifier]
                    returned_keys.add((requested_entry.database, requested_entry.identifier))
                documents.append(document)
            elif isinstance(document, KeggBriteHtextDocument):
                requested_entry = prepared.requested_entries[0]
                if document.identifier != requested_entry.identifier:
                    raise AssertionError("BRITE parser returned a different requested identifier")
                if document.lines:
                    returned_keys.add((requested_entry.database, requested_entry.identifier))
                documents.append(document)
            else:
                raise AssertionError("GET preparation selected an incompatible parser")

        missing_entries = tuple(
            entry
            for entry in request.entries
            if (entry.database, entry.identifier) not in returned_keys
        )
        return GetResult(
            request=request,
            documents=tuple(documents),
            missing_entries=missing_entries,
            batches=tuple(batch.provenance for batch in executed_batches),
        )

    def _read_complete_entry_cache(
        self,
        request: GetRequest,
        options: KeggRequestOptions,
    ) -> tuple[tuple[PreparedRequest, ...], tuple[_ExecutedBatch, ...]] | None:
        if options.refresh or len(request.entries) < 2:
            return None
        prepared_entries = tuple(
            prepare_get(GetRequest(entries=(entry,)), self._config.limits)[0]
            for entry in request.entries
        )
        now = self._read_clock()
        executed: list[_ExecutedBatch] = []
        for prepared in prepared_entries:
            lookup = self._cache.read(
                prepared.operation,
                prepared.normalized_request_key,
                self._retrieval_endpoint_class,
                self._endpoint_label,
                now=now,
                expected_parser_version=PARSER_VERSION,
            )
            if lookup.state is CacheReadState.FRESH and lookup.response is not None:
                executed.append(
                    self._from_cache(
                        prepared,
                        lookup.response,
                        CacheLookupState.FRESH_HIT,
                        now,
                        info_request=None,
                        is_stale=False,
                    )
                )
            elif (
                lookup.state is CacheReadState.STALE
                and options.allow_stale
                and lookup.response is not None
            ):
                executed.append(
                    self._from_cache(
                        prepared,
                        lookup.response,
                        CacheLookupState.STALE_HIT,
                        now,
                        info_request=None,
                        is_stale=True,
                    )
                )
            else:
                return None
        return prepared_entries, tuple(executed)

    def _store_entry_cache_rows(
        self,
        prepared: PreparedRequest,
        executed: _ExecutedBatch,
    ) -> None:
        document = executed.document
        if not isinstance(document, KeggFlatFileDocument) or len(prepared.requested_entries) < 2:
            return
        chunks = tuple(
            chunk.lstrip(b"\r\n") + b"///\n"
            for chunk in executed.body.split(b"///")
            if chunk.strip()
        )
        if len(chunks) != len(document.entries):
            return
        requested_by_identifier = {entry.identifier: entry for entry in prepared.requested_entries}
        provenance = executed.provenance
        for parsed_entry, body in zip(document.entries, chunks, strict=True):
            requested_entry = requested_by_identifier.get(parsed_entry.identifier)
            if requested_entry is None:
                continue
            single = prepare_get(GetRequest(entries=(requested_entry,)), self._config.limits)[0]
            self._cache.write(
                single.operation,
                single.normalized_request_key,
                self._retrieval_endpoint_class,
                self._endpoint_label,
                body=body,
                retrieved_at=provenance.retrieved_at,
                expires_at=provenance.expires_at,
                parser_version=provenance.parser_version,
                database_release=provenance.database_release,
                http_metadata=provenance.http_metadata,
            )

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        """Retrieve one approved selected-entry KEGG relationship."""
        rows, provenance = self._execute_pair_batches(
            prepare_link(request, self._config.limits),
            options or KeggRequestOptions(),
        )
        return LinkResult(request=request, rows=rows, batches=provenance)

    def conv(
        self,
        request: ConvRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> ConvResult:
        """Convert one bounded selected set of approved gene identifiers."""
        prepared = prepare_conv(request, self._config.limits)
        rows, provenance = self._execute_pair_batches(prepared, options or KeggRequestOptions())
        return ConvResult(request=request, rows=rows, batches=provenance)

    def _execute_pair_batches(
        self,
        prepared_batches: tuple[PreparedRequest, ...],
        options: KeggRequestOptions,
    ) -> tuple[tuple[KeggPairRow, ...], tuple[KeggBatchProvenance, ...]]:
        rows: list[KeggPairRow] = []
        provenance: list[KeggBatchProvenance] = []
        for batch_index, prepared in enumerate(prepared_batches):
            executed = self._execute(prepared, options)
            if not isinstance(executed.document, KeggPairDocument):
                raise AssertionError("relationship preparation selected an incompatible parser")
            rows.extend(
                KeggPairRow(
                    batch_index=batch_index,
                    line_number=row.line_number,
                    source_id=row.source_id,
                    target_id=row.target_id,
                )
                for row in executed.document.rows
            )
            provenance.append(executed.provenance)
        return tuple(rows), tuple(provenance)

    def _execute(
        self,
        prepared: PreparedRequest,
        options: KeggRequestOptions,
        *,
        info_request: InfoRequest | None = None,
    ) -> _ExecutedBatch:
        now = self._read_clock()
        if options.refresh:
            cache_state = CacheLookupState.REFRESH_BYPASS
        else:
            lookup = self._cache.read(
                prepared.operation,
                prepared.normalized_request_key,
                self._retrieval_endpoint_class,
                self._endpoint_label,
                now=now,
                expected_parser_version=PARSER_VERSION,
            )
            if lookup.state is CacheReadState.FRESH:
                if lookup.response is None:
                    raise AssertionError("a fresh cache hit omitted its response")
                return self._from_cache(
                    prepared,
                    lookup.response,
                    CacheLookupState.FRESH_HIT,
                    now,
                    info_request=info_request,
                    is_stale=False,
                )
            if lookup.state is CacheReadState.STALE and options.allow_stale:
                if lookup.response is None:
                    raise AssertionError("a stale cache hit omitted its response")
                return self._from_cache(
                    prepared,
                    lookup.response,
                    CacheLookupState.STALE_HIT,
                    now,
                    info_request=info_request,
                    is_stale=True,
                )
            cache_state = (
                CacheLookupState.STALE_DISALLOWED
                if lookup.state is CacheReadState.STALE
                else CacheLookupState.MISS
            )

        if options.cache_only:
            fail(
                ErrorCode.CACHE_ENTRY_NOT_FOUND,
                "The requested KEGG response is unavailable in the selected cache namespace.",
                suggested_action="Fetch the entry through an ordinary network-enabled request.",
                safe_details=(SafeDetail(name="cache_state", value=cache_state.value),),
            )

        response, attempt_count = self._request_with_retries(prepared)
        retrieved_at = self._read_clock()
        self._validate_response_body(prepared, response.body, origin=ResponseOrigin.NETWORK)
        document = self._parse_response(prepared, response.body, info_request=info_request)
        database_release = document.release if isinstance(document, KeggInfoDocument) else None
        expires_at = retrieved_at + timedelta(seconds=self._config.cache.ttl_seconds)
        cached = self._cache.write(
            prepared.operation,
            prepared.normalized_request_key,
            self._retrieval_endpoint_class,
            self._endpoint_label,
            body=response.body,
            retrieved_at=retrieved_at,
            expires_at=expires_at,
            parser_version=PARSER_VERSION,
            database_release=database_release,
            http_metadata=response.http_metadata,
        )
        provenance = self._provenance(
            prepared,
            cached,
            origin=ResponseOrigin.NETWORK,
            cache_state=cache_state,
            served_at=retrieved_at,
            attempt_count=attempt_count,
            is_stale=False,
        )
        return _ExecutedBatch(document=document, provenance=provenance, body=cached.body)

    def _from_cache(
        self,
        prepared: PreparedRequest,
        cached: CachedResponse,
        cache_state: CacheLookupState,
        served_at: datetime,
        *,
        info_request: InfoRequest | None,
        is_stale: bool,
    ) -> _ExecutedBatch:
        self._validate_response_body(prepared, cached.body, origin=ResponseOrigin.CACHE)
        try:
            document = self._parse_response(prepared, cached.body, info_request=info_request)
        except KeggMcpError:
            fail(
                ErrorCode.CACHE_FAILED,
                "A cached KEGG response failed parser validation.",
                suggested_action="Refresh or remove the affected local cache entry.",
                safe_details=(
                    SafeDetail(name="operation", value=prepared.operation.value),
                    SafeDetail(name="stage", value="cached_parse"),
                ),
            )
        return _ExecutedBatch(
            document=document,
            provenance=self._provenance(
                prepared,
                cached,
                origin=ResponseOrigin.CACHE,
                cache_state=cache_state,
                served_at=served_at,
                attempt_count=0,
                is_stale=is_stale,
            ),
            body=cached.body,
        )

    def _parse_response(
        self,
        prepared: PreparedRequest,
        body: bytes,
        *,
        info_request: InfoRequest | None,
    ) -> _ParsedDocument:
        if prepared.parser is ResponseParser.INFO:
            if info_request is None:
                raise AssertionError("INFO parsing requires its typed request")
            return parse_info_response(body, info_request.database)
        if prepared.parser is ResponseParser.FLAT_FILE:
            document = parse_flat_file_response(body)
            expected_identifiers = {entry.identifier for entry in prepared.requested_entries}
            returned_identifiers = [entry.identifier for entry in document.entries]
            if len(returned_identifiers) != len(set(returned_identifiers)) or any(
                identifier not in expected_identifiers for identifier in returned_identifiers
            ):
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "The KEGG GET response did not match the bounded request.",
                    suggested_action=(
                        "Refresh the response or verify the configured endpoint compatibility."
                    ),
                    safe_details=(
                        SafeDetail(name="reason", value="unexpected_or_duplicate_entry"),
                    ),
                )
            return document
        if prepared.parser is ResponseParser.BRITE_HTEXT:
            return parse_brite_htext_response(body, prepared.requested_entries[0].identifier)
        if prepared.parser is ResponseParser.PAIR_TABLE:
            document = parse_pair_response(body)
            target_database = prepared.pair_target_database
            if target_database is None:
                raise AssertionError("pair-table parsing requires a target contract")
            if any(
                row.source_id not in prepared.expected_pair_source_ids
                or not pair_target_matches(target_database, row.target_id)
                for row in document.rows
            ):
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "The KEGG mapping response did not match the bounded request.",
                    suggested_action=(
                        "Refresh the response or verify the configured endpoint compatibility."
                    ),
                    safe_details=(
                        SafeDetail(name="reason", value="unexpected_mapping_identifier"),
                    ),
                )
            return document
        raise AssertionError("unsupported prepared response parser")

    def _request_with_retries(self, prepared: PreparedRequest) -> tuple[TransportResponse, int]:
        url = f"{self._endpoint}{prepared.path}"
        if len(url.encode("ascii")) > self._config.limits.max_url_bytes:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The complete KEGG request exceeds the configured URL-size limit.",
                suggested_action="Use fewer identifiers or configure a smaller relation batch.",
                safe_details=(SafeDetail(name="operation", value=prepared.operation.value),),
            )

        maximum_attempts = self._config.retry.max_retries + 1
        for attempt in range(1, maximum_attempts + 1):
            self._mandatory_rate_limiter.acquire()
            if self._additional_rate_limiter is not None:
                self._additional_rate_limiter.acquire()
            response: TransportResponse | None = None
            terminal_transport_failure = False
            terminal_transport_kind = None
            try:
                response = self._transport.request(
                    url,
                    timeout_seconds=self._config.limits.timeout_seconds,
                    max_response_bytes=self._config.limits.max_response_bytes,
                )
            except TransportError as error:
                if error.transient and attempt < maximum_attempts:
                    self._wait_before_retry(attempt)
                    continue
                terminal_transport_failure = True
                terminal_transport_kind = error.kind
            if terminal_transport_failure:
                fail(
                    ErrorCode.KEGG_REQUEST_FAILED,
                    "The KEGG request failed before a valid response was received.",
                    suggested_action=(
                        "Retry later or verify the configured endpoint and network availability."
                    ),
                    safe_details=(
                        SafeDetail(name="operation", value=prepared.operation.value),
                        SafeDetail(name="attempt_count", value=str(attempt)),
                        SafeDetail(
                            name="transport_kind",
                            value=(
                                "unknown"
                                if terminal_transport_kind is None
                                else terminal_transport_kind.value
                            ),
                        ),
                    ),
                )
            if response is None:
                raise AssertionError("transport attempt produced no response or failure")

            if response.status_code == 200:
                return response, attempt
            if response.status_code == 404:
                fail(
                    ErrorCode.KEGG_ENTRY_NOT_FOUND,
                    "KEGG returned no entry for the bounded request.",
                    suggested_action="Verify the KEGG identifier and selected database.",
                    safe_details=(SafeDetail(name="operation", value=prepared.operation.value),),
                )
            if response.status_code == 429:
                if attempt < maximum_attempts:
                    self._wait_before_retry(attempt)
                    continue
                fail(
                    ErrorCode.KEGG_RATE_LIMITED,
                    "The configured KEGG endpoint continued to rate-limit the request.",
                    suggested_action="Retry later or lower the configured request rate.",
                    safe_details=(SafeDetail(name="attempt_count", value=str(attempt)),),
                )
            if response.status_code in _TRANSIENT_HTTP_STATUSES and attempt < maximum_attempts:
                self._wait_before_retry(attempt)
                continue
            fail(
                ErrorCode.KEGG_REQUEST_FAILED,
                "The configured KEGG endpoint returned an unsuccessful response.",
                suggested_action="Verify the request configuration or retry later.",
                safe_details=(
                    SafeDetail(name="operation", value=prepared.operation.value),
                    SafeDetail(name="status_code", value=str(response.status_code)),
                    SafeDetail(name="attempt_count", value=str(attempt)),
                ),
            )
        raise AssertionError("bounded retry loop terminated unexpectedly")

    def _validate_response_body(
        self,
        prepared: PreparedRequest,
        body: object,
        *,
        origin: ResponseOrigin,
    ) -> None:
        if isinstance(body, bytes) and len(body) <= self._config.limits.max_response_bytes:
            return
        if origin is ResponseOrigin.CACHE:
            fail(
                ErrorCode.CACHE_FAILED,
                "A cached KEGG response exceeds the active safety contract.",
                suggested_action="Refresh or replace the affected local cache entry.",
                safe_details=(
                    SafeDetail(name="operation", value=prepared.operation.value),
                    SafeDetail(name="stage", value="response_size_check"),
                ),
            )
        fail(
            ErrorCode.KEGG_REQUEST_FAILED,
            "The KEGG transport returned a response outside the active size contract.",
            suggested_action="Lower the response scope or verify the configured transport.",
            safe_details=(SafeDetail(name="operation", value=prepared.operation.value),),
        )

    def _wait_before_retry(self, failed_attempt: int) -> None:
        policy = self._config.retry
        exponential = policy.initial_backoff_seconds * (2 ** (failed_attempt - 1))
        base_delay = min(exponential, policy.max_backoff_seconds)
        jitter = self._random_uniform(0.0, policy.jitter_seconds)
        if not 0.0 <= jitter <= policy.jitter_seconds:
            raise RuntimeError("random jitter provider returned a value outside its bounds")
        self._sleeper(base_delay + jitter)

    def _provenance(
        self,
        prepared: PreparedRequest,
        cached: CachedResponse,
        *,
        origin: ResponseOrigin,
        cache_state: CacheLookupState,
        served_at: datetime,
        attempt_count: int,
        is_stale: bool,
    ) -> KeggBatchProvenance:
        return KeggBatchProvenance(
            operation=prepared.operation,
            request_key=prepared.normalized_request_key,
            access_mode=self._config.access.mode,
            retrieval_endpoint_class=self._retrieval_endpoint_class,
            endpoint_label=self._endpoint_label,
            origin=origin,
            cache_lookup_state=cache_state,
            retrieved_at=cached.retrieved_at,
            served_at=served_at,
            expires_at=cached.expires_at,
            response_bytes=len(cached.body),
            parser_name=prepared.parser.value,
            parser_version=cached.parser_version,
            database_release=cached.database_release,
            http_metadata=cached.http_metadata,
            attempt_count=attempt_count,
            is_stale=is_stale,
        )

    def _read_clock(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise RuntimeError("KEGG client clock must return timezone-aware datetimes")
        return value.astimezone(UTC)
