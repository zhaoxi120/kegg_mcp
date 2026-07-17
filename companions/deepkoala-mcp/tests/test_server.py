"""MCP discovery, execution, schema, and raw stdio tests."""

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
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan
from deepkoala_mcp.server import TOOL_NAMES, create_server


class _Runner:
    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        plan.output_path.write_text("opaque handoff\n", encoding="utf-8")
        return ProcessOutcome(return_code=0)


def _tool(tools: list[types.Tool], name: str) -> types.Tool:
    return next(tool for tool in tools if tool.name == name)


def _validate(tool: types.Tool, result: types.CallToolResult) -> None:
    assert tool.outputSchema is not None
    assert result.structuredContent is not None
    Draft202012Validator(tool.outputSchema).validate(  # pyright: ignore[reportUnknownMemberType]
        result.structuredContent
    )


@pytest.mark.asyncio
async def test_discovery_declares_six_bounded_auto_device_tools(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    server = create_server(DeepKoalaJobManager(runtime_config, runner=_Runner()))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        assert tuple(tool.name for tool in tools) == TOOL_NAMES
        for tool in tools:
            assert tool.inputSchema.get("additionalProperties") is False
            assert tool.outputSchema is not None
            assert tool.annotations is not None
            assert tool.annotations.openWorldHint is False
        prepare = _tool(tools, "prepare_deepkoala_job")
        properties = cast(dict[str, object], prepare.inputSchema["properties"])
        assert "device" not in properties
        assert "num_workers" not in properties
        batch = cast(dict[str, object], properties["batch_size"])
        assert batch["maximum"] == 64
        cancel = _tool(tools, "cancel_deepkoala_job")
        assert cancel.annotations is not None
        assert cancel.annotations.destructiveHint is True
        assert cancel.annotations.idempotentHint is True
        submit = _tool(tools, "submit_deepkoala_job")
        assert submit.annotations is not None
        assert submit.annotations.idempotentHint is True
        assert set(cast(dict[str, object], submit.inputSchema["properties"])) == {"job_id"}
        delete = _tool(tools, "delete_deepkoala_job")
        assert delete.annotations is not None
        assert delete.annotations.idempotentHint is True


@pytest.mark.asyncio
async def test_memory_transport_workflow_returns_schema_valid_file_handoff(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    server = create_server(DeepKoalaJobManager(runtime_config, runner=_Runner()))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        status = await session.call_tool("get_deepkoala_runner_status", {})
        _validate(_tool(tools, "get_deepkoala_runner_status"), status)
        assert status.isError is False

        prepared = await session.call_tool(
            "prepare_deepkoala_job",
            {"fasta_text": ">p\nMPEPTIDE\n"},
        )
        _validate(_tool(tools, "prepare_deepkoala_job"), prepared)
        assert prepared.structuredContent is not None
        prepared_data = cast(
            dict[str, object],
            cast(dict[str, object], prepared.structuredContent["result"])["data"],
        )
        job_id = cast(str, prepared_data["job_id"])

        invalid = await session.call_tool(
            "submit_deepkoala_job", {"job_id": job_id, "acknowledged": True}
        )
        assert invalid.isError is True
        _validate(_tool(tools, "submit_deepkoala_job"), invalid)

        submitted = await session.call_tool(
            "submit_deepkoala_job",
            {"job_id": job_id},
        )
        assert submitted.isError is False
        async with asyncio.timeout(5):
            while True:
                polled = await session.call_tool("get_deepkoala_job", {"job_id": job_id})
                _validate(_tool(tools, "get_deepkoala_job"), polled)
                assert polled.structuredContent is not None
                data = cast(
                    dict[str, object],
                    cast(dict[str, object], polled.structuredContent["result"])["data"],
                )
                job = cast(dict[str, object], data["job"])
                if job["state"] == "succeeded":
                    break
                await asyncio.sleep(0.01)
        handoff = cast(dict[str, object], data["handoff"])
        assert handoff["input_format"] == "deepkoala_detailed"
        assert Path(cast(str, handoff["output_path"])).is_file()
        source = cast(dict[str, object], handoff["source"])
        assert source["input_path"] is None
        assert source["input_uri"] == f"mcp://deepkoala-mcp/jobs/{job_id}/output"
        assert "input.fasta" not in json.dumps(handoff)

        deleted = await session.call_tool("delete_deepkoala_job", {"job_id": job_id})
        assert deleted.isError is False
        _validate(_tool(tools, "delete_deepkoala_job"), deleted)


@pytest.mark.asyncio
async def test_stdio_process_initializes_without_stdout_noise(
    tmp_path: Path,
    checkout: Path,
) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "DEEPKOALA_MCP_CHECKOUT": str(checkout),
            "DEEPKOALA_MCP_PYTHON": str(Path(sys.executable).resolve()),
            "DEEPKOALA_MCP_STATE_ROOT": str((tmp_path / "stdio-state").resolve()),
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
        assert server_info["name"] == "deepkoala-mcp"
        await _write(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        await _write(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
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
