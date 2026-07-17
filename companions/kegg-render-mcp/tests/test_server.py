"""MCP discovery, schema, resources, deletion, and clean stdio tests."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from kegg_mcp.kegg import KeggClient, KeggRequestOptions
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from conftest import SyntheticProvider
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.pathway_scene import CorePathwayAssetProvider
from kegg_render_mcp.render_service import RendererService
from kegg_render_mcp.server import TOOL_NAMES, RendererRuntime, build_runtime, create_server


class _ProbeClient:
    def __init__(self) -> None:
        self.options: list[KeggRequestOptions | None] = []

    def info(self, request: object, *, options: KeggRequestOptions | None = None) -> object:
        del request
        self.options.append(options)
        return object()


def _tool(tools: list[types.Tool], name: str) -> types.Tool:
    return next(item for item in tools if item.name == name)


def _validate(tool: types.Tool, result: types.CallToolResult) -> None:
    assert tool.outputSchema is not None
    assert result.structuredContent is not None
    Draft202012Validator(tool.outputSchema).validate(result.structuredContent)  # pyright: ignore[reportUnknownMemberType]


def test_default_network_runtime_uses_zero_wire_retries(
    runtime_config: RendererRuntimeConfig,
) -> None:
    configured = runtime_config.model_copy(update={"access_mode": "public_academic"})
    runtime = build_runtime(configured)
    assert isinstance(runtime.service.provider, CorePathwayAssetProvider)
    assert runtime.service.provider.maximum_retries == 0
    assert runtime.service.provider.maximum_response_bytes == configured.limits.max_asset_bytes


@pytest.mark.asyncio
async def test_explicit_probe_bypasses_cache_for_one_wire_attempt() -> None:
    client = _ProbeClient()
    provider = CorePathwayAssetProvider(cast(KeggClient, client))

    assert await provider.probe() is True
    assert client.options == [KeggRequestOptions(refresh=True)]


@pytest.mark.asyncio
async def test_discovery_declares_six_strict_tools_and_two_resources(
    runtime_config: RendererRuntimeConfig,
) -> None:
    runtime = RendererRuntime(runtime_config, RendererService(runtime_config, SyntheticProvider()))
    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        tools = (await session.list_tools()).tools
        assert tuple(item.name for item in tools) == TOOL_NAMES
        assert all(item.inputSchema.get("additionalProperties") is False for item in tools)
        assert _tool(tools, "get_renderer_status").annotations.openWorldHint is False  # type: ignore[union-attr]
        probe_annotations = _tool(tools, "probe_renderer_kegg_connectivity").annotations
        assert probe_annotations is not None
        assert probe_annotations.readOnlyHint is False
        assert probe_annotations.destructiveHint is False
        assert probe_annotations.idempotentHint is False
        assert probe_annotations.openWorldHint is True
        assert _tool(tools, "render_pathway").annotations.openWorldHint is True  # type: ignore[union-attr]
        assert _tool(tools, "render_module").annotations.openWorldHint is False  # type: ignore[union-attr]
        delete_annotations = _tool(tools, "delete_render_result").annotations
        assert delete_annotations is not None
        assert delete_annotations.destructiveHint is True
        assert delete_annotations.idempotentHint is True
        assert len((await session.list_resources()).resources) == 1
        assert len((await session.list_resource_templates()).resourceTemplates) == 2


@pytest.mark.asyncio
async def test_memory_transport_renders_reads_binary_and_deletes(
    runtime_config: RendererRuntimeConfig, render_input_file: Path
) -> None:
    runtime = RendererRuntime(runtime_config, RendererService(runtime_config, SyntheticProvider()))
    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        tools = (await session.list_tools()).tools
        status = await session.call_tool("get_renderer_status", {})
        _validate(_tool(tools, "get_renderer_status"), status)
        assert status.isError is False
        probe = await session.call_tool("probe_renderer_kegg_connectivity", {})
        _validate(_tool(tools, "probe_renderer_kegg_connectivity"), probe)
        assert probe.isError is False

        rendered = await session.call_tool(
            "render_analysis_bundle",
            {
                "render_input_path": str(render_input_file),
                "formats": ["svg", "png"],
                "target_ids": ["ko00010", "M00001"],
            },
        )
        _validate(_tool(tools, "render_analysis_bundle"), rendered)
        assert rendered.isError is False
        structured = cast(dict[str, object], rendered.structuredContent)
        data = cast(dict[str, object], cast(dict[str, object], structured["result"])["data"])
        render_id = cast(str, data["render_id"])

        index = await session.read_resource(AnyUrl(f"kegg-render://results/{render_id}"))
        assert index.contents[0].mimeType == "application/json"
        png = await session.read_resource(AnyUrl(f"kegg-render://results/{render_id}/ko00010.png"))
        png_content = png.contents[0]
        assert isinstance(png_content, types.BlobResourceContents)
        assert png_content.mimeType == "image/png"
        assert isinstance(png_content.blob, str)
        svg = await session.read_resource(AnyUrl(f"kegg-render://results/{render_id}/M00001.svg"))
        svg_content = svg.contents[0]
        assert isinstance(svg_content, types.TextResourceContents)
        assert svg_content.mimeType == "image/svg+xml"
        assert "<svg" in svg_content.text

        deleted = await session.call_tool("delete_render_result", {"render_id": render_id})
        _validate(_tool(tools, "delete_render_result"), deleted)
        assert deleted.isError is False
        with pytest.raises(McpError):
            await session.read_resource(AnyUrl(f"kegg-render://results/{render_id}"))


@pytest.mark.asyncio
async def test_invalid_request_and_unconfigured_probe_return_schema_errors(
    runtime_config: RendererRuntimeConfig, render_input_file: Path
) -> None:
    from kegg_render_mcp.pathway_scene import UnconfiguredAssetProvider

    runtime = RendererRuntime(
        runtime_config,
        RendererService(runtime_config, UnconfiguredAssetProvider()),
    )
    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        tools = (await session.list_tools()).tools
        invalid = await session.call_tool(
            "render_module",
            {"render_input_path": str(render_input_file), "target_id": "ko00010"},
        )
        assert invalid.isError is True
        _validate(_tool(tools, "render_module"), invalid)
        probe = await session.call_tool("probe_renderer_kegg_connectivity", {})
        assert probe.isError is True
        _validate(_tool(tools, "probe_renderer_kegg_connectivity"), probe)
        assert probe.structuredContent["error"]["code"] == "ASSET_UNAVAILABLE"  # type: ignore[index]


@pytest.mark.asyncio
async def test_internal_error_returns_correlation_id_without_exception_details(
    runtime_config: RendererRuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = RendererRuntime(runtime_config, RendererService(runtime_config, SyntheticProvider()))

    def fail_delete(render_id: str) -> object:
        del render_id
        raise RuntimeError("private-renderer-exception-detail")

    monkeypatch.setattr(runtime.service.store, "delete", fail_delete)
    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        result = await session.call_tool(
            "delete_render_result",
            {"render_id": "render_" + "a" * 32},
        )

    assert result.isError is True
    error = cast(dict[str, object], result.structuredContent["error"])  # type: ignore[index]
    assert error["code"] == "INTERNAL_ERROR"
    details = cast(list[dict[str, str]], error["safe_details"])
    detail_map = {item["name"]: item["value"] for item in details}
    assert detail_map["correlation_id"].startswith("err_")
    assert detail_map["stage"] == "tool:delete_render_result"
    diagnostic = capsys.readouterr().err
    assert detail_map["correlation_id"] in diagnostic
    assert "type=RuntimeError" in diagnostic
    assert "private-renderer-exception-detail" not in diagnostic


@pytest.mark.asyncio
async def test_stdio_initializes_without_stdout_noise(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir(mode=0o700)
    companion = Path(__file__).resolve().parents[1]
    repository = companion.parents[1]
    environment = dict(os.environ)
    environment.update(
        {
            "KEGG_RENDER_MCP_STATE_ROOT": str(tmp_path / "state"),
            "KEGG_RENDER_MCP_ALLOWED_ROOTS": str(allowed),
            "KEGG_RENDER_MCP_ACCESS_MODE": "unconfigured",
            "PYTHONPATH": os.pathsep.join((str(companion / "src"), str(repository / "src"))),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from kegg_render_mcp.server import main; main()",
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        await _write(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "renderer-test", "version": "0.1.0"},
                },
            },
        )
        initialized = await _read(process, 1)
        result = cast(dict[str, object], initialized["result"])
        assert cast(dict[str, object], result["serverInfo"])["name"] == "kegg-render-mcp"
        await _write(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await _write(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = cast(dict[str, object], (await _read(process, 2))["result"])
        assert (
            tuple(item["name"] for item in cast(list[dict[str, object]], listed["tools"]))
            == TOOL_NAMES
        )
    finally:
        await _stop(process)


async def _write(process: asyncio.subprocess.Process, payload: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
    await process.stdin.drain()


async def _read(process: asyncio.subprocess.Process, request_id: int) -> dict[str, object]:
    assert process.stdout is not None
    async with asyncio.timeout(5):
        line = await process.stdout.readline()
    payload = cast(dict[str, object], json.loads(line))
    assert payload.get("id") == request_id
    assert "error" not in payload
    return payload


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()
    try:
        async with asyncio.timeout(2):
            await process.wait()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
    if process.stderr is not None:
        assert b"Traceback (most recent call last)" not in await process.stderr.read()
