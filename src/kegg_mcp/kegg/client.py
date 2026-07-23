"""Typed KEGG service client with local caching, retries, and provenance."""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from datetime import UTC, datetime

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.cache import CacheReadState, SQLiteKeggCache
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
    KeggEntryRef,
    KeggFlatFileDocument,
    KeggFlatFileEntry,
    KeggGetDatabase,
    KeggGetDocument,
    KeggInfoDocument,
    KeggPairDocument,
    KeggPairRow,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    OfflineCacheAccess,
    PublicAcademicAccess,
    RetrievalEndpointClass,
    endpoint_fingerprint,
)
from kegg_mcp.kegg.executor import ExecutedBatch, KeggRequestExecutor, RateLimiter
from kegg_mcp.kegg.operations import (
    PreparedRequest,
    prepare_conv,
    prepare_get,
    prepare_info,
    prepare_link,
)
from kegg_mcp.kegg.pathway_asset_client import PathwayAssetClient
from kegg_mcp.kegg.pathway_assets import (
    PathwayAssetRequest,
    PathwayAssetResult,
    prepare_pathway_asset,
)
from kegg_mcp.kegg.rate_limit import DeploymentRateLimiter
from kegg_mcp.kegg.transport import HttpsTransport, Transport


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
        if (
            isinstance(config.access, OfflineCacheAccess)
            and cache is not None
            and not cache.read_only
        ):
            raise ValueError("offline cache access requires a read-only cache adapter")
        self._config = config
        self._cache = cache or SQLiteKeggCache(
            config.cache.path,
            max_entries=config.cache.max_entries,
            max_payload_bytes=config.cache.max_payload_bytes,
            max_database_bytes=config.cache.max_database_bytes,
            read_only=isinstance(config.access, OfflineCacheAccess),
        )
        access = config.access
        if isinstance(access, PublicAcademicAccess):
            self._endpoint = access.endpoint
            self._retrieval_endpoint_class = RetrievalEndpointClass.PUBLIC_ACADEMIC
            self._endpoint_label = PUBLIC_KEGG_ENDPOINT_LABEL
            self._endpoint_fingerprint = endpoint_fingerprint(self._endpoint)
        elif isinstance(access, OfflineCacheAccess):
            self._endpoint = access.endpoint
            self._retrieval_endpoint_class = access.retrieval_endpoint_class
            self._endpoint_label = access.endpoint_label
            self._endpoint_fingerprint = access.endpoint_fingerprint
        else:
            self._endpoint = access.endpoint
            self._retrieval_endpoint_class = RetrievalEndpointClass.LICENSED
            self._endpoint_label = access.endpoint_label
            self._endpoint_fingerprint = endpoint_fingerprint(self._endpoint)
        mandatory_rate_limiter = DeploymentRateLimiter(
            self._endpoint_fingerprint,
            config.limits.requests_per_second,
            state_root=config.rate_limit.state_root,
        )
        self._executor = KeggRequestExecutor(
            config,
            endpoint=self._endpoint,
            retrieval_endpoint_class=self._retrieval_endpoint_class,
            endpoint_label=self._endpoint_label,
            endpoint_fingerprint=self._endpoint_fingerprint,
            transport=transport or HttpsTransport(),
            cache=self._cache,
            mandatory_rate_limiter=mandatory_rate_limiter,
            additional_rate_limiter=rate_limiter,
            clock=clock,
            sleeper=sleeper,
            random_uniform=random_uniform,
        )
        self._pathway_assets = PathwayAssetClient(self._executor)

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
        executed = self._executor.execute(
            prepared,
            self._effective_options(options),
            info_request=request,
        )
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
        effective_options = self._effective_options(options)
        prepared_batches, executed_batches = self._execute_get_batches(request, effective_options)
        documents: list[KeggGetDocument] = []
        returned_keys: set[tuple[KeggGetDatabase, str]] = set()

        for prepared, executed in zip(prepared_batches, executed_batches, strict=True):
            document = executed.document
            expected_by_identifier = {
                entry.identifier: entry for entry in prepared.requested_entries
            }
            if isinstance(document, KeggFlatFileDocument):
                returned_by_identifier: dict[str, KeggFlatFileEntry] = {}
                for entry in document.entries:
                    if (
                        entry.identifier not in expected_by_identifier
                        or entry.identifier in returned_by_identifier
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
                    returned_by_identifier[entry.identifier] = entry
                    requested_entry = expected_by_identifier[entry.identifier]
                    returned_keys.add((requested_entry.database, requested_entry.identifier))
                ordered_entries = tuple(
                    returned_by_identifier[entry.identifier]
                    for entry in prepared.requested_entries
                    if entry.identifier in returned_by_identifier
                )
                documents.append(document.model_copy(update={"entries": ordered_entries}))
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

    def get_pathway_asset(
        self,
        request: PathwayAssetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> PathwayAssetResult:
        """Retrieve one validated PNG or preflight-checked KGML pathway asset."""
        prepared = prepare_pathway_asset(request, self._config.limits)
        return self._pathway_assets.execute(
            request,
            prepared,
            self._effective_options(options),
        )

    def _execute_get_batches(
        self,
        request: GetRequest,
        options: KeggRequestOptions,
    ) -> tuple[tuple[PreparedRequest, ...], tuple[ExecutedBatch, ...]]:
        if options.refresh or len(request.entries) < 2:
            return self._execute_get_entries(request.entries, options)

        cache_hits = self._read_entry_cache_hits(request, options)
        prepared_batches: list[PreparedRequest] = []
        executed_batches: list[ExecutedBatch] = []
        missing_entries: list[KeggEntryRef] = []

        def flush_missing() -> None:
            if not missing_entries:
                return
            prepared, executed = self._execute_get_entries(tuple(missing_entries), options)
            prepared_batches.extend(prepared)
            executed_batches.extend(executed)
            missing_entries.clear()

        for entry, cache_hit in zip(request.entries, cache_hits, strict=True):
            if cache_hit is None:
                missing_entries.append(entry)
                continue
            flush_missing()
            prepared, executed = cache_hit
            prepared_batches.append(prepared)
            executed_batches.append(executed)
        flush_missing()
        return tuple(prepared_batches), tuple(executed_batches)

    def _execute_get_entries(
        self,
        entries: tuple[KeggEntryRef, ...],
        options: KeggRequestOptions,
    ) -> tuple[tuple[PreparedRequest, ...], tuple[ExecutedBatch, ...]]:
        prepared_batches = prepare_get(GetRequest(entries=entries), self._config.limits)
        executed_batches = tuple(
            self._executor.execute(prepared, options) for prepared in prepared_batches
        )
        for prepared, executed in zip(prepared_batches, executed_batches, strict=True):
            self._store_entry_cache_rows(prepared, executed)
        return prepared_batches, executed_batches

    def _read_entry_cache_hits(
        self,
        request: GetRequest,
        options: KeggRequestOptions,
    ) -> tuple[tuple[PreparedRequest, ExecutedBatch] | None, ...]:
        prepared_entries = tuple(
            prepare_get(GetRequest(entries=(entry,)), self._config.limits)[0]
            for entry in request.entries
        )
        now = self._executor.read_clock()
        cache_hits: list[tuple[PreparedRequest, ExecutedBatch] | None] = []
        for prepared in prepared_entries:
            lookup = self._cache.read(
                prepared.operation,
                prepared.normalized_request_key,
                self._retrieval_endpoint_class,
                self._endpoint_fingerprint,
                now=now,
                expected_parser_version=PARSER_VERSION,
            )
            if lookup.state is CacheReadState.FRESH and lookup.response is not None:
                cache_hits.append(
                    (
                        prepared,
                        self._executor.from_cache(
                            prepared,
                            lookup.response,
                            CacheLookupState.FRESH_HIT,
                            now,
                            info_request=None,
                            is_stale=False,
                        ),
                    )
                )
            elif (
                lookup.state is CacheReadState.STALE
                and options.allow_stale
                and lookup.response is not None
            ):
                cache_hits.append(
                    (
                        prepared,
                        self._executor.from_cache(
                            prepared,
                            lookup.response,
                            CacheLookupState.STALE_HIT,
                            now,
                            info_request=None,
                            is_stale=True,
                        ),
                    )
                )
            else:
                cache_hits.append(None)
        return tuple(cache_hits)

    def _store_entry_cache_rows(
        self,
        prepared: PreparedRequest,
        executed: ExecutedBatch,
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
                self._endpoint_fingerprint,
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
            prepare_link(
                request,
                self._config.limits,
                url_prefix_bytes=len(self._endpoint.encode("ascii")),
            ),
            self._effective_options(options),
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
        rows, provenance = self._execute_pair_batches(prepared, self._effective_options(options))
        return ConvResult(request=request, rows=rows, batches=provenance)

    def _effective_options(self, options: KeggRequestOptions | None) -> KeggRequestOptions:
        effective = options or KeggRequestOptions()
        if isinstance(self._config.access, OfflineCacheAccess):
            return effective.model_copy(update={"refresh": False, "cache_only": True})
        return effective

    def _execute_pair_batches(
        self,
        prepared_batches: tuple[PreparedRequest, ...],
        options: KeggRequestOptions,
    ) -> tuple[tuple[KeggPairRow, ...], tuple[KeggBatchProvenance, ...]]:
        rows: list[KeggPairRow] = []
        provenance: list[KeggBatchProvenance] = []
        for batch_index, prepared in enumerate(prepared_batches):
            executed = self._executor.execute(prepared, options)
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
