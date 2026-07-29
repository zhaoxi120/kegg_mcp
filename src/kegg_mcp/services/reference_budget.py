"""Narrow KEGG client ports and one shared reference-loading budget."""

from __future__ import annotations

from typing import Protocol

from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import (
    ConvRequest,
    ConvResult,
    FindRequest,
    FindResult,
    GetRequest,
    GetResult,
    InfoRequest,
    InfoResult,
    KeggClientConfig,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    OrganismPathwayListRequest,
    OrganismPathwayListResult,
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


class KeggRelationClient(Protocol):
    """Narrow selected-entry LINK port shared by query and audit services."""

    @property
    def config(self) -> KeggClientConfig: ...

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult: ...


class KeggQueryClient(KeggRelationClient, Protocol):
    """Narrow FIND/GET/CONV/LINK port for bounded query services."""

    def find(
        self,
        request: FindRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> FindResult: ...

    def conv(
        self,
        request: ConvRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> ConvResult: ...

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult: ...

    def list_organism_pathways(
        self,
        request: OrganismPathwayListRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> OrganismPathwayListResult: ...


class KeggConnectivityClient(Protocol):
    @property
    def config(self) -> KeggClientConfig: ...

    def info(
        self,
        request: InfoRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> InfoResult: ...


class KeggMcpClient(KeggQueryClient, KeggConnectivityClient, Protocol):
    """Complete client contract required by the unconditionally registered MCP tools."""


_DEFAULT_SERVICE_QUERY_OPTIONS = KeggRequestOptions(refresh=False)


def effective_query_options(
    options: KeggRequestOptions | None,
) -> KeggRequestOptions:
    """Prefer fresh local cache for service calls unless a caller explicitly requests refresh."""
    return _DEFAULT_SERVICE_QUERY_OPTIONS if options is None else options


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
        self._require_request_capacity()
        result = self._client.get(request, options=options)
        self._record_batches(result.batches)
        return result

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        self._require_request_capacity()
        result = self._client.link(request, options=options)
        self._record_batches(result.batches)
        return result

    def _require_request_capacity(self) -> None:
        if self._request_count + 1 > self._limits.max_total_kegg_requests:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The combined reference request budget was exceeded.",
                suggested_action="Request fewer MODULE or pathway references.",
            )

    def _record_batches(self, batches: tuple[KeggBatchProvenance, ...]) -> None:
        next_request_count = self._request_count + len(batches)
        next_response_bytes = self._response_bytes + sum(batch.response_bytes for batch in batches)
        if next_request_count > self._limits.max_total_kegg_requests:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The combined reference request budget was exceeded.",
                suggested_action="Request fewer MODULE or pathway references.",
            )
        if next_response_bytes > self._limits.max_total_response_bytes:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The combined reference response budget was exceeded.",
                suggested_action="Request fewer or smaller KEGG references.",
            )
        self._request_count = next_request_count
        self._response_bytes = next_response_bytes


__all__ = [
    "KeggConnectivityClient",
    "KeggMcpClient",
    "KeggPrimitiveClient",
    "KeggQueryClient",
    "KeggRelationClient",
    "SharedReferenceBudgetClient",
    "effective_query_options",
]
