"""Narrow KEGG client ports and one shared reference-loading budget."""

from __future__ import annotations

from typing import Protocol

from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import (
    GetRequest,
    GetResult,
    InfoRequest,
    InfoResult,
    KeggClientConfig,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
)
from kegg_mcp.kegg.contracts import KeggBatchProvenance
from kegg_mcp.services.reference_loading import ReferenceLoadingLimits


class KeggPrimitiveClient(Protocol):
    @property
    def config(self) -> KeggClientConfig: ...

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult: ...

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult: ...


class KeggConnectivityClient(Protocol):
    @property
    def config(self) -> KeggClientConfig: ...

    def info(
        self,
        request: InfoRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> InfoResult: ...


class SharedReferenceBudgetClient:
    """Enforce one aggregate request and response budget across loader types."""

    def __init__(self, client: KeggPrimitiveClient, limits: ReferenceLoadingLimits) -> None:
        self._client = client
        self._limits = limits
        self._request_count = 0
        self._response_bytes = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._client.config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        self._reserve_request()
        result = self._client.get(request, options=options)
        self._record_batches(result.batches)
        return result

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        self._reserve_request()
        result = self._client.link(request, options=options)
        self._record_batches(result.batches)
        return result

    def _reserve_request(self) -> None:
        self._request_count += 1
        if self._request_count > self._limits.max_total_kegg_requests:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The combined reference request budget was exceeded.",
                suggested_action="Request fewer MODULE or pathway references.",
            )

    def _record_batches(self, batches: tuple[KeggBatchProvenance, ...]) -> None:
        self._response_bytes += sum(batch.response_bytes for batch in batches)
        if self._response_bytes > self._limits.max_total_response_bytes:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The combined reference response budget was exceeded.",
                suggested_action="Request fewer or smaller KEGG references.",
            )


__all__ = ["KeggConnectivityClient", "KeggPrimitiveClient", "SharedReferenceBudgetClient"]
