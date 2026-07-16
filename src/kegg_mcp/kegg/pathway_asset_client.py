"""Cache-aware execution for typed KEGG pathway asset requests."""

from __future__ import annotations

from datetime import datetime, timedelta

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.cache import CachedResponse, CacheReadState, SQLiteKeggCache
from kegg_mcp.kegg.contracts import (
    CacheLookupState,
    HttpMetadata,
    KeggClientConfig,
    KeggRequestOptions,
    ResponseOrigin,
)
from kegg_mcp.kegg.executor import KeggRequestExecutor
from kegg_mcp.kegg.operations import PreparedRequest
from kegg_mcp.kegg.pathway_assets import (
    PATHWAY_ASSET_PARSER_VERSION,
    PathwayAssetRequest,
    PathwayAssetResult,
    validate_pathway_asset_content,
)


class PathwayAssetClient:
    """Retrieve one validated pathway asset through the shared executor policy."""

    def __init__(
        self,
        config: KeggClientConfig,
        cache: SQLiteKeggCache,
        executor: KeggRequestExecutor,
    ) -> None:
        self._config = config
        self._cache = cache
        self._executor = executor

    def execute(
        self,
        request: PathwayAssetRequest,
        prepared: PreparedRequest,
        options: KeggRequestOptions,
    ) -> PathwayAssetResult:
        now = self._executor.read_clock()
        if options.refresh:
            cache_state = CacheLookupState.REFRESH_BYPASS
        else:
            lookup = self._cache.read(
                prepared.operation,
                prepared.normalized_request_key,
                self._executor.retrieval_endpoint_class,
                self._executor.endpoint_label,
                now=now,
                expected_parser_version=PATHWAY_ASSET_PARSER_VERSION,
            )
            if lookup.state is CacheReadState.FRESH:
                if lookup.response is None:
                    raise AssertionError("a fresh pathway-asset cache hit omitted its response")
                return self._from_cache(
                    request,
                    prepared,
                    lookup.response,
                    CacheLookupState.FRESH_HIT,
                    now,
                    is_stale=False,
                )
            if lookup.state is CacheReadState.STALE and options.allow_stale:
                if lookup.response is None:
                    raise AssertionError("a stale pathway-asset cache hit omitted its response")
                return self._from_cache(
                    request,
                    prepared,
                    lookup.response,
                    CacheLookupState.STALE_HIT,
                    now,
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
                "The requested pathway asset is unavailable in the selected cache namespace.",
                suggested_action="Fetch the asset through an ordinary network-enabled request.",
                safe_details=(SafeDetail(name="cache_state", value=cache_state.value),),
            )

        response, attempt_count = self._executor.request_with_retries(prepared)
        retrieved_at = self._executor.read_clock()
        self._executor.validate_response_body(
            prepared,
            response.body,
            origin=ResponseOrigin.NETWORK,
        )
        mime_type, width, height = self._validate_payload(
            request,
            response.body,
            response.http_metadata,
            origin=ResponseOrigin.NETWORK,
        )
        expires_at = retrieved_at + timedelta(seconds=self._config.cache.ttl_seconds)
        cached = self._cache.write(
            prepared.operation,
            prepared.normalized_request_key,
            self._executor.retrieval_endpoint_class,
            self._executor.endpoint_label,
            body=response.body,
            retrieved_at=retrieved_at,
            expires_at=expires_at,
            parser_version=PATHWAY_ASSET_PARSER_VERSION,
            database_release=None,
            http_metadata=response.http_metadata,
        )
        return PathwayAssetResult(
            request=request,
            content=cached.body,
            mime_type=mime_type,
            width=width,
            height=height,
            provenance=self._executor.provenance(
                prepared,
                cached,
                origin=ResponseOrigin.NETWORK,
                cache_state=cache_state,
                served_at=retrieved_at,
                attempt_count=attempt_count,
                is_stale=False,
            ),
        )

    def _from_cache(
        self,
        request: PathwayAssetRequest,
        prepared: PreparedRequest,
        cached: CachedResponse,
        cache_state: CacheLookupState,
        served_at: datetime,
        *,
        is_stale: bool,
    ) -> PathwayAssetResult:
        self._executor.validate_response_body(
            prepared,
            cached.body,
            origin=ResponseOrigin.CACHE,
        )
        mime_type, width, height = self._validate_payload(
            request,
            cached.body,
            cached.http_metadata,
            origin=ResponseOrigin.CACHE,
        )
        return PathwayAssetResult(
            request=request,
            content=cached.body,
            mime_type=mime_type,
            width=width,
            height=height,
            provenance=self._executor.provenance(
                prepared,
                cached,
                origin=ResponseOrigin.CACHE,
                cache_state=cache_state,
                served_at=served_at,
                attempt_count=0,
                is_stale=is_stale,
            ),
        )

    @staticmethod
    def _validate_payload(
        request: PathwayAssetRequest,
        body: bytes,
        http_metadata: tuple[HttpMetadata, ...],
        *,
        origin: ResponseOrigin,
    ) -> tuple[str, int | None, int | None]:
        content_types = tuple(item.value for item in http_metadata if item.name == "content-type")
        try:
            if len(content_types) > 1:
                raise ValueError("multiple content types are not supported")
            return validate_pathway_asset_content(
                request,
                body,
                content_type=content_types[0] if content_types else None,
            )
        except ValueError:
            if origin is ResponseOrigin.CACHE:
                fail(
                    ErrorCode.CACHE_FAILED,
                    "A cached pathway asset failed content validation.",
                    suggested_action="Refresh or remove the affected local cache entry.",
                    safe_details=(SafeDetail(name="stage", value="pathway_asset_validation"),),
                )
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The pathway asset response failed bounded content validation.",
                suggested_action="Refresh the asset or verify endpoint compatibility.",
                safe_details=(SafeDetail(name="asset_kind", value=request.kind.value),),
            )


__all__ = ["PathwayAssetClient"]
