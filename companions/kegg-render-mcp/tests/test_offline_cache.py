"""Renderer offline-cache integration tests with only synthetic KEGG assets."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import kegg_mcp.kegg.client as client_module
import pytest
from jsonschema import Draft202012Validator
from kegg_mcp.kegg import (
    PATHWAY_ASSET_PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    KeggClientLimits,
    PathwayAssetKind,
    PathwayAssetRequest,
    RetrievalEndpointClass,
    endpoint_fingerprint,
)
from kegg_mcp.kegg.cache import SQLiteKeggCache
from kegg_mcp.kegg.contracts import HttpMetadata
from kegg_mcp.kegg.pathway_assets import prepare_pathway_asset
from kegg_mcp.kegg.transport import TransportResponse
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from conftest import synthetic_kgml, synthetic_png
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    ConnectivityStatus,
    ErrorCode,
    RenderFormat,
    RenderMcpError,
)
from kegg_render_mcp.pathway_scene import CorePathwayAssetProvider
from kegg_render_mcp.server import build_runtime, create_server


class ForbiddenTransport:
    """Fail immediately if an offline renderer reaches the transport boundary."""

    def __init__(self) -> None:
        self.request_count = 0

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del url, timeout_seconds, max_response_bytes
        self.request_count += 1
        raise AssertionError("offline renderer must not access the network")


def _offline_config(
    base: RendererRuntimeConfig,
    cache_path: Path,
    *,
    state_name: str,
    licensed_endpoint: str | None = None,
    allow_stale: bool = False,
) -> RendererRuntimeConfig:
    return RendererRuntimeConfig(
        state_root=base.state_root.parent / state_name,
        allowed_roots=base.allowed_roots,
        access_mode="offline_cache",
        licensed_endpoint=licensed_endpoint,
        cache_path=cache_path,
        offline_allow_stale=allow_stale,
        retention_seconds=base.retention_seconds,
        rate_limit_root=base.rate_limit_root,
        limits=base.limits,
    )


def _seed_pathway_assets(
    cache_path: Path,
    *,
    endpoint_class: RetrievalEndpointClass,
    fingerprint: str,
    retrieved_at: datetime,
    expires_at: datetime,
) -> None:
    cache = SQLiteKeggCache(cache_path)
    limits = KeggClientLimits(max_response_bytes=2_000_000)
    assets = (
        (PathwayAssetKind.IMAGE, synthetic_png(), "image/png"),
        (PathwayAssetKind.KGML, synthetic_kgml(), "application/xml"),
    )
    for kind, body, content_type in assets:
        request = PathwayAssetRequest(pathway_id="ko00010", kind=kind)
        prepared = prepare_pathway_asset(request, limits)
        cache.write(
            prepared.operation,
            prepared.normalized_request_key,
            endpoint_class,
            fingerprint,
            body=body,
            retrieved_at=retrieved_at,
            expires_at=expires_at,
            parser_version=PATHWAY_ASSET_PARSER_VERSION,
            database_release=None,
            http_metadata=(HttpMetadata(name="content-type", value=content_type),),
        )


def _forbid_network(monkeypatch: pytest.MonkeyPatch) -> ForbiddenTransport:
    transport = ForbiddenTransport()
    monkeypatch.setattr(client_module, "HttpsTransport", lambda: transport)
    return transport


@pytest.mark.asyncio
async def test_public_offline_cache_renders_a_pathway_without_network(
    tmp_path: Path,
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    cache_path = tmp_path / "public-cache" / "kegg.sqlite3"
    _seed_pathway_assets(
        cache_path,
        endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        fingerprint=PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        retrieved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
    )
    transport = _forbid_network(monkeypatch)
    runtime = build_runtime(
        _offline_config(runtime_config, cache_path, state_name="offline-public-state")
    )
    provider = cast(CorePathwayAssetProvider, runtime.service.provider)

    assert provider.network_enabled is False
    assert await provider.probe() is ConnectivityStatus.OFFLINE_CACHE
    runtime.service.open()
    try:
        result = await runtime.service.render(
            render_input_path=str(render_input_file),
            target_ids=("ko00010",),
            formats=(RenderFormat.SVG,),
            output_directory=None,
        )
        manifest = json.loads(
            runtime.service.store.read(result.render_id, "render_manifest.json").content
        )
    finally:
        runtime.service.close()

    assets = manifest["provenance"]["targets"][0]["assets"]
    assert {item["origin"] for item in assets} == {"cache"}
    assert {item["access_mode"] for item in assets} == {"offline_cache"}
    assert {item["retrieval_endpoint_class"] for item in assets} == {"public_academic"}
    assert {item["is_stale"] for item in assets} == {False}
    assert transport.request_count == 0


@pytest.mark.asyncio
async def test_licensed_offline_namespace_uses_canonical_fingerprint_not_display_label(
    tmp_path: Path,
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    cache_path = tmp_path / "licensed-cache" / "kegg.sqlite3"
    endpoint = "https://licensed.example.test/api"
    _seed_pathway_assets(
        cache_path,
        endpoint_class=RetrievalEndpointClass.LICENSED,
        fingerprint=endpoint_fingerprint(endpoint),
        retrieved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
    )
    transport = _forbid_network(monkeypatch)
    matching = build_runtime(
        _offline_config(
            runtime_config,
            cache_path,
            state_name="offline-licensed-match",
            licensed_endpoint="https://LICENSED.EXAMPLE.TEST/api/",
        )
    )
    provider = cast(CorePathwayAssetProvider, matching.service.provider)

    image = await provider.get_asset("ko00010", "image")
    kgml = await provider.get_asset("ko00010", "kgml")

    assert image.provenance["retrieval_endpoint_class"] == "licensed"
    assert kgml.provenance["access_mode"] == "offline_cache"
    matching.service.open()
    try:
        result = await matching.service.render(
            render_input_path=str(render_input_file),
            target_ids=("ko00010",),
            formats=(RenderFormat.SVG,),
            output_directory=None,
        )
        manifest = matching.service.store.read(result.render_id, "render_manifest.json").content
    finally:
        matching.service.close()
    serialized_manifest = manifest.decode("utf-8")
    for secret in (
        endpoint,
        "https://LICENSED.EXAMPLE.TEST/api/",
        str(cache_path),
        endpoint_fingerprint(endpoint),
        "licensed-renderer-cache",
    ):
        assert secret not in serialized_manifest

    mismatched = build_runtime(
        _offline_config(
            runtime_config,
            cache_path,
            state_name="offline-licensed-mismatch",
            licensed_endpoint="https://other.example.test/api",
        )
    )
    with pytest.raises(RenderMcpError) as missing:
        await mismatched.service.provider.get_asset("ko00010", "image")

    assert missing.value.detail.code is ErrorCode.ASSET_UNAVAILABLE
    assert {item.name: item.value for item in missing.value.detail.safe_details} == {
        "asset_kind": "image",
        "core_error_code": "CACHE_ENTRY_NOT_FOUND",
        "cache_state": "miss",
    }
    assert transport.request_count == 0


@pytest.mark.asyncio
async def test_stale_offline_assets_require_deployment_permission_and_remain_visible(
    tmp_path: Path,
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    cache_path = tmp_path / "stale-cache" / "kegg.sqlite3"
    _seed_pathway_assets(
        cache_path,
        endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        fingerprint=PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        retrieved_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )
    transport = _forbid_network(monkeypatch)
    denied = build_runtime(
        _offline_config(runtime_config, cache_path, state_name="offline-stale-denied")
    )
    with pytest.raises(RenderMcpError) as missing:
        await denied.service.provider.get_asset("ko00010", "image")
    assert {item.name: item.value for item in missing.value.detail.safe_details}[
        "cache_state"
    ] == "stale_disallowed"

    allowed = build_runtime(
        _offline_config(
            runtime_config,
            cache_path,
            state_name="offline-stale-allowed",
            allow_stale=True,
        )
    )
    allowed.service.open()
    try:
        result = await allowed.service.render(
            render_input_path=str(render_input_file),
            target_ids=("ko00010",),
            formats=(RenderFormat.SVG,),
            output_directory=None,
        )
        manifest = json.loads(
            allowed.service.store.read(result.render_id, "render_manifest.json").content
        )
    finally:
        allowed.service.close()

    stale_warning = "One or more KEGG pathway assets were served from stale offline cache entries."
    assert stale_warning in result.warnings
    assert stale_warning in manifest["warnings"]
    assert {item["is_stale"] for item in manifest["provenance"]["targets"][0]["assets"]} == {True}
    assert transport.request_count == 0


@pytest.mark.asyncio
async def test_offline_status_probe_miss_and_module_rendering_are_redacted_and_network_free(
    tmp_path: Path,
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache_path = tmp_path / "missing-cache" / "kegg.sqlite3"
    endpoint = "https://private-cache.example.test/api"
    transport = _forbid_network(monkeypatch)
    runtime = build_runtime(
        _offline_config(
            runtime_config,
            cache_path,
            state_name="offline-missing-state",
            licensed_endpoint=endpoint,
        )
    )

    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        tools = (await session.list_tools()).tools
        status_tool = next(item for item in tools if item.name == "get_renderer_status")
        probe_tool = next(item for item in tools if item.name == "probe_renderer_kegg_connectivity")
        status = await session.call_tool("get_renderer_status", {})
        probe = await session.call_tool("probe_renderer_kegg_connectivity", {})
        missing = await session.call_tool(
            "render_pathway",
            {"render_input_path": str(render_input_file), "target_id": "ko00010"},
        )
        module = await session.call_tool(
            "render_module",
            {"render_input_path": str(render_input_file), "target_id": "M00001"},
        )

    assert status_tool.outputSchema is not None
    assert probe_tool.outputSchema is not None
    Draft202012Validator(status_tool.outputSchema).validate(status.structuredContent)  # pyright: ignore[reportUnknownMemberType]
    Draft202012Validator(probe_tool.outputSchema).validate(probe.structuredContent)  # pyright: ignore[reportUnknownMemberType]
    status_data = status.structuredContent["result"]["data"]  # type: ignore[index]
    assert status_data["ready"] is True
    assert status_data["pathway_access_configured"] is True
    assert status_data["access_mode"] == "offline_cache"
    serialized_status = json.dumps(status.structuredContent, sort_keys=True)
    assert endpoint not in serialized_status
    assert str(cache_path) not in serialized_status
    assert endpoint_fingerprint(endpoint) not in serialized_status
    probe_data = probe.structuredContent["result"]["data"]  # type: ignore[index]
    assert probe_data == {
        "reachable": False,
        "classification": "offline_cache",
        "operation": "info",
        "request_count": 0,
        "message": "Network access is disabled by the renderer offline-cache deployment policy.",
    }
    assert missing.isError is True
    missing_error = cast(dict[str, object], missing.structuredContent["error"])  # type: ignore[index]
    assert missing_error["code"] == "ASSET_UNAVAILABLE"
    assert module.isError is False
    assert transport.request_count == 0
    assert not cache_path.exists()
    assert not cache_path.parent.exists()
    assert isinstance(module.content[0], types.TextContent)
