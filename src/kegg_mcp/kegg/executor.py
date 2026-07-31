"""Shared KEGG request execution, caching, parsing, retry, and provenance."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Generic, NoReturn, Protocol, TypeAlias, TypeVar

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, SafeDetail, fail
from kegg_mcp.kegg.cache import CachedResponse, CacheReadState, SQLiteKeggCache
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    AccessMode,
    CacheLookupState,
    HttpMetadata,
    InfoRequest,
    KeggBatchProvenance,
    KeggBriteHtextDocument,
    KeggClientConfig,
    KeggFindDocument,
    KeggFlatFileDocument,
    KeggInfoDocument,
    KeggOperation,
    KeggOrganismPathwayDocument,
    KeggPairDocument,
    KeggRequestOptions,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.operations import (
    PreparedRequest,
    ResponseParser,
    get_entry_matches,
    pair_target_matches,
)
from kegg_mcp.kegg.parsers import (
    parse_brite_htext_response,
    parse_find_response,
    parse_flat_file_response,
    parse_info_response,
    parse_organism_pathway_list_response,
    parse_pair_response,
)
from kegg_mcp.kegg.transport import Transport, TransportError, TransportResponse

_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 500, 502, 503, 504})

ParsedDocument: TypeAlias = (
    KeggInfoDocument
    | KeggOrganismPathwayDocument
    | KeggFindDocument
    | KeggFlatFileDocument
    | KeggBriteHtextDocument
    | KeggPairDocument
)
PayloadT = TypeVar("PayloadT")


class RateLimiter(Protocol):
    """Small injectable rate-limiter surface used by the service client."""

    def acquire(self) -> None:
        """Wait until one request attempt is permitted."""
        ...


@dataclass(frozen=True, slots=True)
class ExecutedBatch:
    document: ParsedDocument
    provenance: KeggBatchProvenance
    body: bytes


@dataclass(frozen=True, slots=True)
class _ExecutedPayload(Generic[PayloadT]):
    value: PayloadT
    provenance: KeggBatchProvenance
    body: bytes


class KeggRequestExecutor:
    """Execute prepared requests through one bounded cache and transport policy."""

    def __init__(
        self,
        config: KeggClientConfig,
        *,
        endpoint: str,
        retrieval_endpoint_class: RetrievalEndpointClass,
        endpoint_label: str,
        endpoint_fingerprint: str,
        transport: Transport,
        cache: SQLiteKeggCache,
        mandatory_rate_limiter: RateLimiter,
        additional_rate_limiter: RateLimiter | None,
        clock: Callable[[], datetime],
        sleeper: Callable[[float], None],
        random_uniform: Callable[[float, float], float],
    ) -> None:
        self._config = config
        self._endpoint = endpoint
        self._retrieval_endpoint_class = retrieval_endpoint_class
        self._endpoint_label = endpoint_label
        self._endpoint_fingerprint = endpoint_fingerprint
        self._transport = transport
        self._cache = cache
        self._mandatory_rate_limiter = mandatory_rate_limiter
        self._additional_rate_limiter = additional_rate_limiter
        self._clock = clock
        self._sleeper = sleeper
        self._random_uniform = random_uniform
        self._network_enabled = config.access.mode is not AccessMode.OFFLINE_CACHE

    @property
    def retrieval_endpoint_class(self) -> RetrievalEndpointClass:
        return self._retrieval_endpoint_class

    @property
    def endpoint_label(self) -> str:
        return self._endpoint_label

    @property
    def endpoint_fingerprint(self) -> str:
        """Return the opaque cache and rate-limit endpoint identity."""
        return self._endpoint_fingerprint

    def execute(
        self,
        prepared: PreparedRequest,
        options: KeggRequestOptions,
        *,
        info_request: InfoRequest | None = None,
    ) -> ExecutedBatch:
        executed = self._execute_payload(
            prepared,
            options,
            parser_version=PARSER_VERSION,
            decode=lambda body, _metadata: self._parse_response(
                prepared,
                body,
                info_request=info_request,
            ),
            database_release=lambda document: (
                document.release if isinstance(document, KeggInfoDocument) else None
            ),
            cache_only_failure=self._raise_document_cache_miss,
            cached_validation_failure=lambda: self._raise_cached_document_failure(prepared),
        )
        return ExecutedBatch(
            document=executed.value,
            provenance=executed.provenance,
            body=executed.body,
        )

    def _execute_payload(
        self,
        prepared: PreparedRequest,
        options: KeggRequestOptions,
        *,
        parser_version: str,
        decode: Callable[[bytes, tuple[HttpMetadata, ...]], PayloadT],
        database_release: Callable[[PayloadT], str | None],
        cache_only_failure: Callable[[CacheLookupState], NoReturn],
        cached_validation_failure: Callable[[], NoReturn],
    ) -> _ExecutedPayload[PayloadT]:
        """Run one parser-specific payload through the shared cache/transport state machine."""
        if not self._network_enabled:
            options = options.model_copy(update={"refresh": False, "cache_only": True})
        now = self.read_clock()
        if options.refresh:
            cache_state = CacheLookupState.REFRESH_BYPASS
        else:
            lookup = self._cache.read(
                prepared.operation,
                prepared.normalized_request_key,
                self._retrieval_endpoint_class,
                self._endpoint_fingerprint,
                now=now,
                expected_parser_version=parser_version,
            )
            if lookup.state is CacheReadState.FRESH:
                if lookup.response is None:
                    raise AssertionError("a fresh cache hit omitted its response")
                return self._from_cached_payload(
                    prepared,
                    lookup.response,
                    CacheLookupState.FRESH_HIT,
                    now,
                    decode=decode,
                    cached_validation_failure=cached_validation_failure,
                    is_stale=False,
                )
            if lookup.state is CacheReadState.STALE and options.allow_stale:
                if lookup.response is None:
                    raise AssertionError("a stale cache hit omitted its response")
                return self._from_cached_payload(
                    prepared,
                    lookup.response,
                    CacheLookupState.STALE_HIT,
                    now,
                    decode=decode,
                    cached_validation_failure=cached_validation_failure,
                    is_stale=True,
                )
            cache_state = (
                CacheLookupState.STALE_DISALLOWED
                if lookup.state is CacheReadState.STALE
                else CacheLookupState.MISS
            )

        if options.cache_only:
            cache_only_failure(cache_state)

        response, attempt_count = self.request_with_retries(prepared)
        retrieved_at = self.read_clock()
        self.validate_response_body(prepared, response.body, origin=ResponseOrigin.NETWORK)
        value = decode(response.body, response.http_metadata)
        expires_at = retrieved_at + timedelta(seconds=self._config.cache.ttl_seconds)
        cached = self._cache.write(
            prepared.operation,
            prepared.normalized_request_key,
            self._retrieval_endpoint_class,
            self._endpoint_fingerprint,
            body=response.body,
            retrieved_at=retrieved_at,
            expires_at=expires_at,
            parser_version=parser_version,
            database_release=database_release(value),
            http_metadata=response.http_metadata,
        )
        provenance = self.provenance(
            prepared,
            cached,
            origin=ResponseOrigin.NETWORK,
            cache_state=cache_state,
            served_at=retrieved_at,
            attempt_count=attempt_count,
            is_stale=False,
        )
        return _ExecutedPayload(value=value, provenance=provenance, body=cached.body)

    def from_cache(
        self,
        prepared: PreparedRequest,
        cached: CachedResponse,
        cache_state: CacheLookupState,
        served_at: datetime,
        *,
        info_request: InfoRequest | None,
        is_stale: bool,
    ) -> ExecutedBatch:
        executed = self._from_cached_payload(
            prepared,
            cached,
            cache_state,
            served_at,
            decode=lambda body, _metadata: self._parse_response(
                prepared,
                body,
                info_request=info_request,
            ),
            cached_validation_failure=lambda: self._raise_cached_document_failure(prepared),
            is_stale=is_stale,
        )
        return ExecutedBatch(
            document=executed.value,
            provenance=executed.provenance,
            body=executed.body,
        )

    def _from_cached_payload(
        self,
        prepared: PreparedRequest,
        cached: CachedResponse,
        cache_state: CacheLookupState,
        served_at: datetime,
        *,
        decode: Callable[[bytes, tuple[HttpMetadata, ...]], PayloadT],
        cached_validation_failure: Callable[[], NoReturn],
        is_stale: bool,
    ) -> _ExecutedPayload[PayloadT]:
        self.validate_response_body(prepared, cached.body, origin=ResponseOrigin.CACHE)
        try:
            value = decode(cached.body, cached.http_metadata)
        except KeggMcpError:
            cached_validation_failure()
        return _ExecutedPayload(
            value=value,
            provenance=self.provenance(
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

    @staticmethod
    def _raise_document_cache_miss(cache_state: CacheLookupState) -> NoReturn:
        fail(
            ErrorCode.CACHE_ENTRY_NOT_FOUND,
            "The requested KEGG response is unavailable in the selected cache namespace.",
            suggested_action="Fetch the entry through an ordinary network-enabled request.",
            safe_details=(SafeDetail(name="cache_state", value=cache_state.value),),
        )

    @staticmethod
    def _raise_cached_document_failure(prepared: PreparedRequest) -> NoReturn:
        fail(
            ErrorCode.CACHE_FAILED,
            "A cached KEGG response failed parser validation.",
            suggested_action="Refresh or remove the affected local cache entry.",
            safe_details=(
                SafeDetail(name="operation", value=prepared.operation.value),
                SafeDetail(name="stage", value="cached_parse"),
            ),
        )

    def request_with_retries(self, prepared: PreparedRequest) -> tuple[TransportResponse, int]:
        if not self._network_enabled:
            fail(
                ErrorCode.CACHE_ENTRY_NOT_FOUND,
                "Network access is disabled by the offline cache profile.",
                suggested_action=(
                    "Populate the selected cache namespace through authorized live access or "
                    "switch to a live access profile."
                ),
                safe_details=(SafeDetail(name="operation", value=prepared.operation.value),),
            )
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
                if prepared.operation is KeggOperation.GET:
                    return (
                        TransportResponse(
                            status_code=response.status_code,
                            body=b"",
                            http_metadata=response.http_metadata,
                        ),
                        attempt,
                    )
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

    def validate_response_body(
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

    def provenance(
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

    def read_clock(self) -> datetime:
        value = self._clock()
        if value.utcoffset() is None:
            raise RuntimeError("KEGG client clock must return timezone-aware datetimes")
        return value.astimezone(UTC)

    def _parse_response(
        self,
        prepared: PreparedRequest,
        body: bytes,
        *,
        info_request: InfoRequest | None,
    ) -> ParsedDocument:
        if prepared.parser is ResponseParser.INFO:
            if info_request is None:
                raise AssertionError("INFO parsing requires its typed request")
            return parse_info_response(body, info_request.database)
        if prepared.parser is ResponseParser.ORGANISM_PATHWAY_LIST:
            if prepared.list_organism is None:
                raise AssertionError(
                    "organism pathway list parsing requires its canonical organism"
                )
            return parse_organism_pathway_list_response(body, prepared.list_organism)
        if prepared.parser is ResponseParser.FIND_TABLE:
            if prepared.find_database is None:
                raise AssertionError("FIND parsing requires its expected database")
            return parse_find_response(
                body,
                prepared.find_database,
                organism=prepared.find_organism,
            )
        if prepared.parser is ResponseParser.FLAT_FILE:
            document = parse_flat_file_response(body)
            matched_entries: set[tuple[object, str]] = set()
            response_matches_request = True
            for returned_entry in document.entries:
                matches = tuple(
                    requested_entry
                    for requested_entry in prepared.requested_entries
                    if get_entry_matches(requested_entry, returned_entry)
                )
                if len(matches) != 1:
                    response_matches_request = False
                    break
                matched_key = (matches[0].database, matches[0].identifier)
                if matched_key in matched_entries:
                    response_matches_request = False
                    break
                matched_entries.add(matched_key)
            if not response_matches_request:
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
                or (
                    prepared.expected_pair_target_prefix is not None
                    and not row.target_id.startswith(prepared.expected_pair_target_prefix)
                )
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

    def _wait_before_retry(self, failed_attempt: int) -> None:
        policy = self._config.retry
        exponential = policy.initial_backoff_seconds * (2 ** (failed_attempt - 1))
        base_delay = min(exponential, policy.max_backoff_seconds)
        jitter = self._random_uniform(0.0, policy.jitter_seconds)
        if not 0.0 <= jitter <= policy.jitter_seconds:
            raise RuntimeError("random jitter provider returned a value outside its bounds")
        self._sleeper(base_delay + jitter)


__all__ = ["ExecutedBatch", "KeggRequestExecutor", "RateLimiter"]
