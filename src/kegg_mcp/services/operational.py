"""Operational status, connectivity, and retained-result management."""

from __future__ import annotations

from datetime import UTC, datetime

from kegg_mcp.domain.errors import KeggMcpError
from kegg_mcp.kegg import (
    AccessMode,
    InfoRequest,
    KeggInfoDatabase,
    KeggRequestOptions,
    OfflineCacheAccess,
    PublicAcademicAccess,
    RetrievalEndpointClass,
)
from kegg_mcp.services.models import ConnectivityProbeResult, ConnectivityState, ServerStatusResult
from kegg_mcp.services.reference_budget import KeggConnectivityClient, KeggPrimitiveClient
from kegg_mcp.services.result_store import DeletedResult, ResultMetadataPage, SQLiteResultStore


def get_server_status_service(
    *,
    server_version: str,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    supported_tools: tuple[str, ...],
    allowed_root_count: int,
) -> ServerStatusResult:
    """Return redacted configuration facts without probing or revealing paths."""
    access = client.config.access
    cache_endpoint_class = (
        access.retrieval_endpoint_class
        if isinstance(access, OfflineCacheAccess)
        else (
            RetrievalEndpointClass.PUBLIC_ACADEMIC
            if isinstance(access, PublicAcademicAccess)
            else RetrievalEndpointClass.LICENSED
        )
    )
    return ServerStatusResult(
        server_version=server_version,
        access_mode=access.mode,
        cache_endpoint_class=cache_endpoint_class,
        network_enabled=access.mode is not AccessMode.OFFLINE_CACHE,
        academic_use_confirmed=access.mode is AccessMode.PUBLIC_ACADEMIC,
        licensed_use_confirmed=cache_endpoint_class is RetrievalEndpointClass.LICENSED,
        file_handoff_enabled=allowed_root_count > 0,
        allowed_root_count=allowed_root_count,
        supported_tools=supported_tools,
        result_active_ttl_seconds=result_store.limits.retention_seconds,
        orphan_cleanup_after_seconds=result_store.limits.retention_seconds,
        result_quota_bytes=result_store.limits.quota_bytes,
    )


def delete_analysis_result(
    result_id: str,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> DeletedResult:
    """Delete one current-session retained result without exposing other scopes."""
    return result_store.delete(scope_id, result_id)


def list_analysis_results(
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
    offset: int,
    limit: int,
) -> ResultMetadataPage:
    """List one bounded page from only the current stdio result scope."""
    return result_store.list_results(scope_id, offset=offset, limit=limit)


def probe_kegg_connectivity_service(
    client: KeggConnectivityClient,
    *,
    now: datetime | None = None,
) -> ConnectivityProbeResult:
    """Probe a typed INFO endpoint once and classify failures before biological analysis."""
    checked_now = (now or datetime.now(UTC)).astimezone(UTC)
    access = client.config.access
    endpoint_class = (
        access.retrieval_endpoint_class
        if isinstance(access, OfflineCacheAccess)
        else (
            RetrievalEndpointClass.PUBLIC_ACADEMIC
            if isinstance(access, PublicAcademicAccess)
            else RetrievalEndpointClass.LICENSED
        )
    )
    if isinstance(access, OfflineCacheAccess):
        return ConnectivityProbeResult(
            state=ConnectivityState.NETWORK_DISABLED,
            access_mode=access.mode,
            endpoint_class=endpoint_class,
            probed_at=checked_now,
            suggested_action=(
                "Switch to public_academic or licensed access before requesting a live probe."
            ),
        )
    try:
        result = client.info(
            InfoRequest(database=KeggInfoDatabase.KEGG),
            options=KeggRequestOptions(refresh=True),
        )
    except KeggMcpError as error:
        details = {item.name: item.value for item in error.detail.safe_details}
        transport_kind = details.get("transport_kind")
        if transport_kind == "dns":
            state = ConnectivityState.DNS_FAILURE
        elif transport_kind == "connection":
            state = ConnectivityState.CONNECTION_FAILURE
        else:
            state = ConnectivityState.AUTHORIZATION_CONFIGURATION_FAILURE
        return ConnectivityProbeResult(
            state=state,
            access_mode=access.mode,
            endpoint_class=endpoint_class,
            probed_at=checked_now,
            error_code=error.detail.code,
            suggested_action=error.detail.suggested_action,
        )
    return ConnectivityProbeResult(
        state=ConnectivityState.REACHABLE,
        access_mode=access.mode,
        endpoint_class=endpoint_class,
        probed_at=result.batch.served_at,
    )


__all__ = [
    "delete_analysis_result",
    "get_server_status_service",
    "list_analysis_results",
    "probe_kegg_connectivity_service",
]
