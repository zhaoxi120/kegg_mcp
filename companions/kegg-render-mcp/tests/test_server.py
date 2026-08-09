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
from kegg_mcp.domain.errors import (
    ErrorCode as CoreErrorCode,
)
from kegg_mcp.domain.errors import (
    ErrorDetail as CoreErrorDetail,
)
from kegg_mcp.domain.errors import (
    KeggMcpError as CoreKeggMcpError,
)
from kegg_mcp.domain.errors import (
    SafeDetail as CoreSafeDetail,
)
from kegg_mcp.kegg import (
    KeggClient,
    KeggClientConfig,
    KeggRequestOptions,
    PublicAcademicAccess,
)
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from conftest import SyntheticProvider
from kegg_render_mcp._platform import (
    UNSUPPORTED_PLATFORM_DIAGNOSTIC,
    UnsupportedRendererPlatformError,
)
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    ConnectivityStatus,
    ErrorCode,
    ErrorDetail,
    RendererStatus,
    RenderMcpError,
)
from kegg_render_mcp.pathway_scene import CorePathwayAssetProvider
from kegg_render_mcp.render_service import RendererService
from kegg_render_mcp.server import (
    TOOL_NAMES,
    RendererRuntime,
    build_runtime,
    create_server,
)


def test_startup_reports_static_unsupported_platform_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kegg_render_mcp import server as server_module

    def reject_startup(function: object) -> None:
        del function
        raise UnsupportedRendererPlatformError("private synthetic platform detail")

    monkeypatch.setattr(server_module.anyio, "run", reject_startup)

    with pytest.raises(SystemExit) as raised:
        server_module.main()

    assert raised.value.code == 2
    diagnostic = capsys.readouterr().err
    assert UNSUPPORTED_PLATFORM_DIAGNOSTIC in diagnostic
    assert "private synthetic platform detail" not in diagnostic


class _ProbeClient:
    def __init__(self) -> None:
        self.config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))
        self.options: list[KeggRequestOptions | None] = []

    def info(self, request: object, *, options: KeggRequestOptions | None = None) -> object:
        del request
        self.options.append(options)
        return object()


class _FailingProbeClient:
    config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))

    def info(self, request: object, *, options: KeggRequestOptions | None = None) -> object:
        del request, options
        raise CoreKeggMcpError(
            CoreErrorDetail(
                code=CoreErrorCode.KEGG_REQUEST_FAILED,
                message="The KEGG request failed before a valid response was received.",
                recoverable=True,
                suggested_action="Verify network availability.",
                safe_details=(CoreSafeDetail(name="transport_kind", value="dns"),),
            )
        )


class _FailingAssetClient:
    config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))

    def __init__(self, detail: CoreErrorDetail) -> None:
        self.detail = detail

    def get_pathway_asset(
        self,
        request: object,
        *,
        options: KeggRequestOptions | None = None,
    ) -> object:
        del request, options
        raise CoreKeggMcpError(self.detail)


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
    client = runtime.service.provider._client  # pyright: ignore[reportPrivateUsage]
    assert client.config.retry.max_retries == 0
    assert client.config.limits.max_response_bytes == configured.limits.max_asset_bytes


@pytest.mark.asyncio
async def test_explicit_probe_bypasses_cache_for_one_wire_attempt() -> None:
    client = _ProbeClient()
    provider = CorePathwayAssetProvider(cast(KeggClient, client))

    assert await provider.probe() is ConnectivityStatus.REACHABLE
    assert client.options == [KeggRequestOptions(refresh=True)]


def test_tool_names_are_unique() -> None:
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))


@pytest.mark.asyncio
async def test_probe_classifies_redacted_transport_failures() -> None:
    provider = CorePathwayAssetProvider(cast(KeggClient, _FailingProbeClient()))

    assert await provider.probe() is ConnectivityStatus.DNS_FAILURE


@pytest.mark.parametrize(
    ("core_code", "core_details", "renderer_code", "action_fragment"),
    [
        (
            CoreErrorCode.CACHE_ENTRY_NOT_FOUND,
            (CoreSafeDetail(name="cache_state", value="miss"),),
            ErrorCode.ASSET_UNAVAILABLE,
            "Populate",
        ),
        (
            CoreErrorCode.CACHE_FAILED,
            (CoreSafeDetail(name="stage", value="read"),),
            ErrorCode.ASSET_UNAVAILABLE,
            "Inspect",
        ),
        (
            CoreErrorCode.CACHE_FAILED,
            (CoreSafeDetail(name="stage", value="pathway_asset_validation"),),
            ErrorCode.ASSET_INVALID,
            "Refresh",
        ),
        (
            CoreErrorCode.KEGG_PARSE_FAILED,
            (CoreSafeDetail(name="operation", value="get"),),
            ErrorCode.ASSET_INVALID,
            "Refresh",
        ),
        (
            CoreErrorCode.INPUT_LIMIT_EXCEEDED,
            (CoreSafeDetail(name="operation", value="get"),),
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "bounds",
        ),
        (
            CoreErrorCode.KEGG_RATE_LIMITED,
            (CoreSafeDetail(name="status_code", value="429"),),
            ErrorCode.ASSET_UNAVAILABLE,
            "Retry later",
        ),
        (
            CoreErrorCode.KEGG_REQUEST_FAILED,
            (CoreSafeDetail(name="transport_kind", value="dns"),),
            ErrorCode.ASSET_UNAVAILABLE,
            "connectivity probe",
        ),
    ],
)
@pytest.mark.asyncio
async def test_core_asset_errors_keep_stable_actionable_renderer_structure(
    core_code: CoreErrorCode,
    core_details: tuple[CoreSafeDetail, ...],
    renderer_code: ErrorCode,
    action_fragment: str,
) -> None:
    private_details = (
        CoreSafeDetail(name="endpoint_label", value="private-endpoint-label"),
        CoreSafeDetail(name="cache_path", value="/private/cache/kegg.sqlite3"),
        CoreSafeDetail(name="endpoint_fingerprint", value="f" * 64),
        CoreSafeDetail(name="request_key", value="/get/private/request"),
        CoreSafeDetail(name="payload", value="private-payload"),
    )
    provider = CorePathwayAssetProvider(
        cast(
            KeggClient,
            _FailingAssetClient(
                CoreErrorDetail(
                    code=core_code,
                    message="Synthetic core failure.",
                    recoverable=True,
                    suggested_action="Synthetic core action.",
                    safe_details=(*core_details, *private_details),
                )
            ),
        )
    )

    with pytest.raises(RenderMcpError) as raised:
        await provider.get_asset("ko00010", "image")

    detail = raised.value.detail
    serialized = detail.model_dump(mode="json")
    Draft202012Validator(ErrorDetail.model_json_schema(mode="serialization")).validate(  # pyright: ignore[reportUnknownMemberType]
        serialized
    )
    assert ErrorDetail.model_validate_json(detail.model_dump_json(), strict=True) == detail
    assert detail.code is renderer_code
    assert action_fragment in detail.suggested_action
    safe_details = {item.name: item.value for item in detail.safe_details}
    assert safe_details["asset_kind"] == "image"
    assert safe_details["core_error_code"] == core_code.value
    assert not {
        "endpoint_label",
        "cache_path",
        "endpoint_fingerprint",
        "request_key",
        "payload",
    }.intersection(safe_details)


@pytest.mark.asyncio
async def test_discovery_declares_six_strict_tools_and_two_resources(
    runtime_config: RendererRuntimeConfig,
) -> None:
    runtime = RendererRuntime(runtime_config, RendererService(runtime_config, SyntheticProvider()))
    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        tools = (await session.list_tools()).tools
        assert tuple(item.name for item in tools) == TOOL_NAMES
        for tool in tools:
            assert tool.inputSchema.get("additionalProperties") is False
            Draft202012Validator.check_schema(tool.inputSchema)
            serialized_schema = json.dumps(tool.inputSchema, separators=(",", ":"))
            assert "$defs" not in tool.inputSchema
            assert '"$ref"' not in serialized_schema
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
        render_schema = _tool(tools, "render_analysis_bundle").inputSchema
        # Keep the complete schema below Codex's 5,000-byte compact-tool threshold reviewed on
        # 2026-07-22; crossing it can prune the explicit alternatives this contract requires.
        assert len(json.dumps(render_schema, separators=(",", ":")).encode("utf-8")) < 5_000
        properties = cast(dict[str, dict[str, object]], render_schema["properties"])
        assert set(properties) == {
            "render_input_path",
            "render_input_json",
            "output_directory",
            "formats",
            "target_ids",
        }
        assert all(properties[name].get("description") for name in properties)
        formats = properties["formats"]
        assert formats["minItems"] == 1
        assert formats["maxItems"] == 2
        assert formats["uniqueItems"] is True
        assert cast(dict[str, object], formats["items"])["enum"] == ["svg", "png"]
        target_ids = properties["target_ids"]
        target_arrays: list[dict[str, object]] = []
        for alternative in cast(list[object], target_ids["anyOf"]):
            if isinstance(alternative, dict):
                alternative_mapping = cast(dict[str, object], alternative)
                if alternative_mapping.get("type") == "array":
                    target_arrays.append(alternative_mapping)
        assert len(target_arrays) == 1
        target_array = target_arrays[0]
        assert target_array["minItems"] == 1
        assert target_array["maxItems"] == 32
        assert target_ids["uniqueItems"] is True
        assert cast(dict[str, object], target_array["items"])["pattern"] == (
            r"^(?:ko[0-9]{5}|M[0-9]{5})$"
        )

        alternatives = cast(list[dict[str, object]], render_schema["oneOf"])
        assert [item["required"] for item in alternatives] == [
            ["render_input_path"],
            ["render_input_json"],
        ]
        for alternative in alternatives:
            assert alternative["type"] == "object"
            assert alternative["additionalProperties"] is False
            assert set(cast(dict[str, object], alternative["properties"])) == set(properties)

        validator = Draft202012Validator(render_schema)
        validator.validate({"render_input_path": "/allowed/render_input.json"})  # pyright: ignore[reportUnknownMemberType]
        validator.validate({"render_input_json": "{}"})  # pyright: ignore[reportUnknownMemberType]
        validator.validate(  # pyright: ignore[reportUnknownMemberType]
            {
                "render_input_path": "/allowed/render_input.json",
                "render_input_json": None,
            }
        )
        assert list(validator.iter_errors({}))  # pyright: ignore[reportUnknownMemberType]
        both_sources = {
            "render_input_path": "/allowed/render_input.json",
            "render_input_json": "{}",
        }
        both_source_errors = list(
            validator.iter_errors(both_sources)  # pyright: ignore[reportUnknownMemberType]
        )
        assert both_source_errors
        empty_target_errors = list(
            validator.iter_errors(  # pyright: ignore[reportUnknownMemberType]
                {"render_input_path": "/allowed/render_input.json", "target_ids": []}
            )
        )
        assert empty_target_errors

        for name in ("render_pathway", "render_module"):
            one_schema = _tool(tools, name).inputSchema
            assert set(cast(dict[str, object], one_schema["properties"])) == {
                "render_input_path",
                "render_input_json",
                "target_id",
                "output_directory",
                "formats",
            }
            assert all(
                set(cast(dict[str, object], branch["properties"]))
                == set(cast(dict[str, object], one_schema["properties"]))
                for branch in cast(list[dict[str, object]], one_schema["oneOf"])
            )


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
        status_data = cast(
            dict[str, object],
            cast(dict[str, object], status.structuredContent["result"])["data"],  # type: ignore[index]
        )
        bounds = cast(dict[str, object], status_data["bounds"])
        assert status_data["render_input_schema_version"] == "6"
        assert "render_input_schema_version" in RendererStatus.model_json_schema()["required"]
        assert bounds["max_results"] == runtime_config.limits.max_results
        assert bounds["max_xml_depth"] == runtime_config.limits.max_xml_depth
        assert bounds["max_total_polyline_points"] == (
            runtime_config.limits.max_total_polyline_points
        )
        assert bounds["max_total_polyline_length"] == (
            runtime_config.limits.max_total_polyline_length
        )
        assert bounds["max_graphic_ko_associations"] == (
            runtime_config.limits.max_graphic_ko_associations
        )
        assert bounds["max_svg_nodes"] == runtime_config.limits.max_svg_nodes
        assert status_data["retained_storage_bytes"] == 0
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
        output_directory = Path(cast(str, data["output_directory"]))
        assert output_directory.parent == runtime_config.allowed_roots[-1]
        assert output_directory.name.startswith("kegg-render-")
        artifact_metadata = cast(list[dict[str, object]], data["artifacts"])
        assert all(Path(cast(str, item["output_path"])).is_file() for item in artifact_metadata)

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

        inline = await session.call_tool(
            "render_module",
            {
                "render_input_json": render_input_file.read_text(encoding="utf-8"),
                "target_id": "M00001",
            },
        )
        _validate(_tool(tools, "render_module"), inline)
        assert inline.isError is False

        pathway = await session.call_tool(
            "render_pathway",
            {
                "render_input_path": str(render_input_file),
                "target_id": "ko00010",
                "formats": ["svg"],
            },
        )
        _validate(_tool(tools, "render_pathway"), pathway)
        assert pathway.isError is False

        deleted = await session.call_tool("delete_render_result", {"render_id": render_id})
        _validate(_tool(tools, "delete_render_result"), deleted)
        assert deleted.isError is False
        with pytest.raises(McpError):
            await session.read_resource(AnyUrl(f"kegg-render://results/{render_id}"))


@pytest.mark.asyncio
async def test_failed_default_render_does_not_leave_an_allocated_directory(
    runtime_config: RendererRuntimeConfig,
    render_input_file: Path,
) -> None:
    runtime = RendererRuntime(runtime_config, RendererService(runtime_config, SyntheticProvider()))
    before = tuple(runtime_config.allowed_roots[-1].glob("kegg-render-*"))
    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        failed = await session.call_tool(
            "render_analysis_bundle",
            {
                "render_input_path": str(render_input_file),
                "target_ids": ["ko99999"],
            },
        )

    assert failed.isError is True
    assert tuple(runtime_config.allowed_roots[-1].glob("kegg-render-*")) == before


@pytest.mark.asyncio
async def test_invalid_request_is_actionable_and_unconfigured_probe_is_classified(
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
        invalid_error = cast(dict[str, object], invalid.structuredContent["error"])  # type: ignore[index]
        invalid_details = cast(list[dict[str, str]], invalid_error["safe_details"])
        assert {item["name"]: item["value"] for item in invalid_details} == {
            "field_path": "target_id",
            "validation_issue_count": "1",
            "stage": "tool_input",
        }
        ambiguous = await session.call_tool(
            "render_analysis_bundle",
            {
                "render_input_path": str(render_input_file),
                "render_input_json": render_input_file.read_text(encoding="utf-8"),
            },
        )
        assert ambiguous.isError is True
        _validate(_tool(tools, "render_analysis_bundle"), ambiguous)
        ambiguous_error = cast(dict[str, object], ambiguous.structuredContent["error"])  # type: ignore[index]
        ambiguous_details = cast(list[dict[str, str]], ambiguous_error["safe_details"])
        assert {item["name"]: item["value"] for item in ambiguous_details} == {
            "field_path": "root",
            "validation_issue_count": "1",
            "stage": "tool_input",
        }
        probe = await session.call_tool("probe_renderer_kegg_connectivity", {})
        assert probe.isError is False
        _validate(_tool(tools, "probe_renderer_kegg_connectivity"), probe)
        probe_data = probe.structuredContent["result"]["data"]  # type: ignore[index]
        assert probe_data["reachable"] is False
        assert probe_data["classification"] == "not_configured"
        assert probe_data["request_count"] == 0


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
