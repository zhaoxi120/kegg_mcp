"""MCP discovery, structured output, resource fallback, and stdio tests."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import cast

import pytest
from jsonschema import Draft202012Validator
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from conftest import DETAILED_CSV, ready_probe
from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan
from deepkoala_mcp.server import TOOL_NAMES, create_server


class _Runner:
    def __init__(self, payload: bytes = DETAILED_CSV) -> None:
        self.payload = payload

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        plan.output_path.write_bytes(self.payload)
        return ProcessOutcome(return_code=0)


class _BlockingRunner:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        del plan
        self.started.set()
        await self.release.wait()
        return ProcessOutcome(return_code=0)


def _manager(config: DeepKoalaRuntimeConfig, payload: bytes = DETAILED_CSV) -> DeepKoalaJobManager:
    return DeepKoalaJobManager(config, runner=_Runner(payload), runtime_probe=ready_probe)


def _input(
    config: DeepKoalaRuntimeConfig,
    name: str = "mcp-run",
    *,
    sequence_ids: tuple[str, ...] = ("protein-1",),
) -> dict[str, object]:
    fasta = config.input_roots[0] / f"{name}.faa"
    fasta.write_text(
        "".join(f">{sequence_id}\nMPEPTIDE\n" for sequence_id in sequence_ids),
        encoding="ascii",
    )
    return {
        "fasta_path": str(fasta),
        "output_directory": str(config.output_roots[0] / name),
    }


def _tool(tools: list[types.Tool], name: str) -> types.Tool:
    return next(tool for tool in tools if tool.name == name)


def _validate(tool: types.Tool, result: types.CallToolResult) -> None:
    assert tool.outputSchema is not None
    assert result.structuredContent is not None
    Draft202012Validator(tool.outputSchema).validate(  # pyright: ignore[reportUnknownMemberType]
        result.structuredContent
    )


def _data(result: types.CallToolResult) -> dict[str, object]:
    assert result.structuredContent is not None
    wrapped = cast(dict[str, object], result.structuredContent["result"])
    return cast(dict[str, object], wrapped["data"])


def test_tool_names_are_unique() -> None:
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))


@pytest.mark.asyncio
async def test_cancel_tool_dispatches_to_the_registered_handler(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runner = _BlockingRunner()
    manager = DeepKoalaJobManager(runtime_config, runner=runner, runtime_probe=ready_probe)
    async with create_connected_server_and_client_session(create_server(manager)) as session:
        tools = (await session.list_tools()).tools
        started = await session.call_tool(
            "run_deepkoala_job",
            _input(runtime_config, "cancel-dispatch"),
        )
        job_id = cast(str, cast(dict[str, object], _data(started)["job"])["job_id"])
        await asyncio.wait_for(runner.started.wait(), timeout=2)

        cancelled = await session.call_tool("cancel_deepkoala_job", {"job_id": job_id})

        _validate(_tool(tools, "cancel_deepkoala_job"), cancelled)
        assert cancelled.isError is False
        assert _data(cancelled)["state"] == "cancelled"


@pytest.mark.asyncio
async def test_discovery_declares_five_compact_policy_bounded_tools(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    server = create_server(_manager(runtime_config))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        assert tuple(tool.name for tool in tools) == TOOL_NAMES
        for tool in tools:
            assert tool.inputSchema.get("additionalProperties") is False
            assert tool.outputSchema is not None
            assert tool.annotations is not None
            assert tool.annotations.openWorldHint is False
        run = _tool(tools, "run_deepkoala_job")
        properties = cast(dict[str, object], run.inputSchema["properties"])
        assert {"fasta_path", "output_directory"}.issubset(properties)
        assert run.inputSchema["required"] == ["fasta_path"]
        device = cast(dict[str, object], properties["device"])
        assert device["enum"] == ["cpu", "cuda", "mps"]
        assert device["default"] == "cpu"
        assert "fasta_text" not in properties
        assert "max_bytes" not in properties
        assert "acknowledged" not in properties
        assert run.annotations is not None
        assert run.annotations.idempotentHint is False
        assert (await session.list_resources()).resources == []
        assert len((await session.list_resource_templates()).resourceTemplates) == 2


@pytest.mark.asyncio
async def test_memory_transport_returns_schema_valid_stable_handoff_and_z_timestamps(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    server = create_server(_manager(runtime_config))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        status = await session.call_tool("get_deepkoala_runner_status", {})
        _validate(_tool(tools, "get_deepkoala_runner_status"), status)
        assert status.isError is False
        assert _data(status)["device_policy"] == "cpu"
        assert _data(status)["allowed_devices"] == ["cpu"]
        assert _data(status)["cuda_available"] is False
        assert _data(status)["mps_available"] is False
        assert _data(status)["max_input_bytes"] is None

        arguments = _input(runtime_config)
        invalid = await session.call_tool(
            "run_deepkoala_job",
            {**arguments, "acknowledged": True},
        )
        assert invalid.isError is True
        _validate(_tool(tools, "run_deepkoala_job"), invalid)

        started = await session.call_tool("run_deepkoala_job", arguments)
        assert started.isError is False
        _validate(_tool(tools, "run_deepkoala_job"), started)
        job_id = cast(str, cast(dict[str, object], _data(started)["job"])["job_id"])
        async with asyncio.timeout(5):
            while True:
                polled = await session.call_tool("get_deepkoala_job", {"job_id": job_id})
                _validate(_tool(tools, "get_deepkoala_job"), polled)
                data = _data(polled)
                job = cast(dict[str, object], data["job"])
                if job["state"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
        assert cast(str, job["started_at"]).endswith("Z")
        assert cast(str, job["completed_at"]).endswith("Z")
        handoff = cast(dict[str, object], data["handoff"])
        assert handoff["schema_version"] == "2"
        assert handoff["tool_version"] == "0.5.0"
        assert handoff["output_coverage"] == {
            "input_sequence_count": 1,
            "output_row_count": 1,
            "distinct_output_sequence_count": 1,
            "missing_input_sequence_count": 0,
            "unexpected_output_sequence_count": 0,
        }
        assert Path(cast(str, handoff["annotations_path"])).read_bytes() == DETAILED_CSV
        assert Path(cast(str, handoff["report_path"])).is_file()
        source = cast(dict[str, object], handoff["source"])
        assert source["input_path"] == arguments["fasta_path"]
        assert cast(str, source["annotation_date"]).endswith("Z")

        annotations = await session.read_resource(
            AnyUrl(cast(str, handoff["annotations_resource_uri"]))
        )
        annotation_text = cast(types.TextResourceContents, annotations.contents[0]).text
        assert annotation_text == DETAILED_CSV.decode()
        stable_path = Path(cast(str, handoff["annotations_path"]))
        deleted = await session.call_tool("delete_deepkoala_job", {"job_id": job_id})
        _validate(_tool(tools, "delete_deepkoala_job"), deleted)
        assert stable_path.is_file()
        with pytest.raises(McpError) as missing:
            await session.read_resource(AnyUrl(cast(str, handoff["annotations_resource_uri"])))
        assert missing.value.error.code == -32002


@pytest.mark.asyncio
async def test_large_resource_fallback_is_versioned_bounded_and_reconstructable(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    rows = b"".join(f"protein-{index},K00001,0.95,0.50,*\n".encode() for index in range(4_000))
    payload = b"name,predict_label,probability,threshold,annotate\n" + rows
    server = create_server(_manager(runtime_config, payload))
    async with create_connected_server_and_client_session(server) as session:
        started = await session.call_tool(
            "run_deepkoala_job",
            _input(
                runtime_config,
                "large",
                sequence_ids=tuple(f"protein-{index}" for index in range(4_000)),
            ),
        )
        job_id = cast(str, cast(dict[str, object], _data(started)["job"])["job_id"])
        async with asyncio.timeout(5):
            while True:
                polled = await session.call_tool("get_deepkoala_job", {"job_id": job_id})
                data = _data(polled)
                if cast(dict[str, object], data["job"])["state"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
        handoff = cast(dict[str, object], data["handoff"])
        uri = cast(str, handoff["annotations_resource_uri"])
        resource = await session.read_resource(AnyUrl(uri))
        notice = json.loads(cast(types.TextResourceContents, resource.contents[0]).text)
        assert notice["schema_version"] == "1"
        assert notice["encoding"] == "base64"
        assert notice["page_size"] == 65_536
        chunks: list[bytes] = []
        next_uri = cast(str | None, notice["next_uri"])
        while next_uri is not None:
            page_resource = await session.read_resource(AnyUrl(next_uri))
            page = json.loads(cast(types.TextResourceContents, page_resource.contents[0]).text)
            assert page["schema_version"] == "1"
            assert page["returned_bytes"] <= 65_536
            chunks.append(base64.b64decode(page["content_base64"], validate=True))
            next_uri = cast(str | None, page["next_uri"])
        assert b"".join(chunks) == payload
        with pytest.raises(McpError) as invalid:
            await session.read_resource(AnyUrl(f"deepkoala://jobs/{job_id}/annotations/00/65536"))
        assert invalid.value.error.code == types.INVALID_PARAMS
        with pytest.raises(McpError) as outside_file:
            await session.read_resource(
                AnyUrl(f"deepkoala://jobs/{job_id}/annotations/1073741823/1")
            )
        assert outside_file.value.error.code == -32002
        with pytest.raises(McpError) as oversized_offset:
            await session.read_resource(
                AnyUrl(f"deepkoala://jobs/{job_id}/annotations/1073741824/1")
            )
        assert oversized_offset.value.error.code == types.INVALID_PARAMS
        with pytest.raises(McpError) as missing:
            await session.read_resource(AnyUrl(f"deepkoala://jobs/{'job_' + 'f' * 32}/annotations"))
        assert missing.value.error.code == -32002


@pytest.mark.asyncio
async def test_stdio_process_initializes_without_stdout_noise(
    tmp_path: Path,
    checkout: Path,
) -> None:
    inputs = tmp_path / "stdio-inputs"
    outputs = tmp_path / "stdio-outputs"
    inputs.mkdir()
    outputs.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "DEEPKOALA_MCP_CHECKOUT": str(checkout),
            "DEEPKOALA_MCP_PYTHON": str(Path(sys.executable).resolve()),
            "DEEPKOALA_MCP_STATE_ROOT": str((tmp_path / "stdio-state").resolve()),
            "DEEPKOALA_MCP_INPUT_ROOTS": str(inputs),
            "DEEPKOALA_MCP_OUTPUT_ROOTS": str(outputs),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from deepkoala_mcp.server import main; main()",
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
                    "clientInfo": {"name": "companion-test", "version": "0.1.0"},
                },
            },
        )
        initialized = await _read(process, 1)
        result = cast(dict[str, object], initialized["result"])
        server_info = cast(dict[str, object], result["serverInfo"])
        assert server_info == {"name": "deepkoala-mcp", "version": "0.5.0"}
        await _write(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await _write(process, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = cast(dict[str, object], (await _read(process, 2))["result"])
        tools = cast(list[dict[str, object]], listed["tools"])
        assert tuple(tool["name"] for tool in tools) == TOOL_NAMES
    finally:
        await _stop(process)


async def _write(process: asyncio.subprocess.Process, payload: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
    async with asyncio.timeout(3):
        await process.stdin.drain()


async def _read(process: asyncio.subprocess.Process, request_id: int) -> dict[str, object]:
    assert process.stdout is not None
    async with asyncio.timeout(5):
        line = await process.stdout.readline()
    assert line
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
        async with asyncio.timeout(2):
            await process.wait()
    if process.stdout is not None:
        remaining = await process.stdout.read()
        assert all(json.loads(line) for line in remaining.splitlines())
    if process.stderr is not None:
        stderr = await process.stderr.read()
        assert b"Traceback (most recent call last)" not in stderr
