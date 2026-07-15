"""MCP discovery, schema, execution, resource, and stdio contracts."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
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
from pydantic import AnyUrl

from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.server import TOOL_NAMES, create_server

_CLI = """\
import argparse
import csv
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--input_path', required=True)
p.add_argument('--output_path', required=True)
p.add_argument('--model')
p.add_argument('--date')
p.add_argument('--device')
p.add_argument('--detail', action='store_true')
p.add_argument('--batch_size', type=int, default=32)
p.add_argument('--num_workers', type=int, default=2)
p.add_argument('--topk', type=int, default=1)
args = p.parse_args()
names = [
    line[1:].split()[0]
    for line in Path(args.input_path).read_text(encoding='utf-8').splitlines()
    if line.startswith('>')
]
with Path(args.output_path).open('w', newline='', encoding='utf-8') as stream:
    writer = csv.writer(stream)
    writer.writerow(['name', 'predict_label', 'probability', 'threshold', 'annotate'])
    for name in names:
        writer.writerow([name, 'K00001', '0.9', '0.5', '*'])
print(f'Processed {len(names)} sequences, annotated {len(names)}.')
"""


def _checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "deepkoala"
    package = checkout / "deepkoala"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "utils.py").write_text(
        "def resolve_device(requested):\n    return 'cpu' if requested == 'auto' else requested\n",
        encoding="utf-8",
    )
    (package / "cli.py").write_text(_CLI, encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "deepkoala"\nversion = "0.1-beta"\n',
        encoding="utf-8",
    )
    resources = checkout / "resources" / "202502"
    resources.mkdir(parents=True)
    for model in ("full", "frag"):
        (resources / f"weights_{model}.pt").write_bytes(model.encode())
        (resources / f"ko_config_{model}.json").write_text("{}", encoding="utf-8")
    return checkout.resolve()


def _manager(tmp_path: Path) -> DeepKoalaJobManager:
    return DeepKoalaJobManager(
        DeepKoalaRuntimeConfig(
            checkout=_checkout(tmp_path),
            python_executable=Path(sys.executable).resolve(),
            state_root=(tmp_path / "state").resolve(),
        )
    )


def _tool(tools: list[types.Tool], name: str) -> types.Tool:
    return next(tool for tool in tools if tool.name == name)


def _validate(tool: types.Tool, result: types.CallToolResult) -> None:
    assert tool.outputSchema is not None
    assert result.structuredContent is not None
    Draft202012Validator(tool.outputSchema).validate(  # pyright: ignore[reportUnknownMemberType]
        result.structuredContent
    )


@pytest.mark.asyncio
async def test_discovery_declares_six_tools_annotations_and_resources(tmp_path: Path) -> None:
    server = create_server(_manager(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools

        assert tuple(tool.name for tool in tools) == TOOL_NAMES
        for tool in tools:
            assert tool.inputSchema.get("additionalProperties") is False
            assert tool.outputSchema is not None
            assert tool.annotations is not None
        for name in ("get_deepkoala_runner_status", "get_deepkoala_job"):
            annotations = _tool(tools, name).annotations
            assert annotations is not None
            assert annotations.readOnlyHint is True
            assert annotations.idempotentHint is True
            assert annotations.openWorldHint is False
        prepare = _tool(tools, "prepare_deepkoala_job")
        assert prepare.annotations is not None
        assert prepare.annotations.idempotentHint is False
        assert prepare.annotations.openWorldHint is True
        submit = _tool(tools, "submit_deepkoala_job")
        assert submit.annotations is not None
        assert submit.annotations.idempotentHint is True
        cancel = _tool(tools, "cancel_deepkoala_job")
        assert cancel.annotations is not None
        assert cancel.annotations.destructiveHint is True
        assert cancel.annotations.idempotentHint is True
        delete = _tool(tools, "delete_deepkoala_job")
        assert delete.annotations is not None
        assert delete.annotations.destructiveHint is True
        assert delete.annotations.idempotentHint is False
        assert delete.description is not None
        assert "idempotent" not in delete.description.lower()

        prepare_schema = prepare.inputSchema
        assert prepare_schema["properties"]["fasta_text"]["anyOf"][0]["maxLength"] == 5_000_000
        assert prepare_schema["properties"]["multi"]["const"] is False
        assert prepare_schema["properties"]["topk"]["anyOf"][0]["maximum"] == 10
        input_validator = Draft202012Validator(prepare_schema)
        assert input_validator.is_valid({"fasta_text": ">a\nM\n"})  # pyright: ignore[reportUnknownMemberType]
        assert input_validator.is_valid({"fasta_path": "/allowed/a.faa"})  # pyright: ignore[reportUnknownMemberType]
        assert not input_validator.is_valid({})  # pyright: ignore[reportUnknownMemberType]
        assert not input_validator.is_valid(  # pyright: ignore[reportUnknownMemberType]
            {"fasta_text": ">a\nM\n", "fasta_path": "/allowed/a.faa"}
        )

        assert prepare.outputSchema is not None
        output_validator = Draft202012Validator(prepare.outputSchema)
        assert not output_validator.is_valid(  # pyright: ignore[reportUnknownMemberType]
            {"ok": True, "result": None, "error": None}
        )
        assert not output_validator.is_valid(  # pyright: ignore[reportUnknownMemberType]
            {"ok": False, "result": None, "error": None}
        )
        assert not output_validator.is_valid(  # pyright: ignore[reportUnknownMemberType]
            {"ok": True}
        )

        resources = (await session.list_resources()).resources
        assert {str(resource.uri) for resource in resources} == {"deepkoala-job://status"}
        templates = (await session.list_resource_templates()).resourceTemplates
        assert {template.uriTemplate for template in templates} == {
            "deepkoala-job://jobs/{job_id}/{section}",
            "deepkoala-job://jobs/{job_id}/{section}/{offset}/{limit}",
        }


@pytest.mark.asyncio
async def test_status_prepare_submit_poll_resource_and_delete_are_schema_valid(
    tmp_path: Path,
) -> None:
    server = create_server(_manager(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        status = await session.call_tool("get_deepkoala_runner_status", {})
        _validate(_tool(tools, "get_deepkoala_runner_status"), status)
        assert status.structuredContent is not None
        status_data = status.structuredContent["result"]["data"]
        assert status_data["ready"] is True
        assert status_data["limits"]["max_sequences"] == 100_000
        assert status_data["limits"]["max_residues"] == 5_000_000
        assert status_data["limits"]["max_sequence_length"] == 100_000
        assert status_data["limits"]["max_header_length"] == 1_024
        assert str(tmp_path) not in json.dumps(status.structuredContent)

        prepared = await session.call_tool(
            "prepare_deepkoala_job",
            {"fasta_text": ">seq1\nMKTAYIAK\n", "device": "cpu"},
        )
        _validate(_tool(tools, "prepare_deepkoala_job"), prepared)
        assert prepared.isError is False
        assert prepared.structuredContent is not None
        plan = prepared.structuredContent["result"]["data"]
        assert plan["state"] == "prepared"

        submitted = await session.call_tool(
            "submit_deepkoala_job",
            {
                "plan_id": plan["plan_id"],
                "notice_sha256": plan["notice_sha256"],
                "acknowledged": True,
            },
        )
        _validate(_tool(tools, "submit_deepkoala_job"), submitted)
        assert submitted.structuredContent is not None
        job_id = submitted.structuredContent["result"]["data"]["job"]["job_id"]

        result: types.CallToolResult | None = None
        for _ in range(100):
            result = await session.call_tool("get_deepkoala_job", {"job_id": job_id})
            assert result.structuredContent is not None
            if result.structuredContent["result"]["data"]["job"]["state"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        assert result is not None
        _validate(_tool(tools, "get_deepkoala_job"), result)
        assert result.structuredContent is not None
        job = result.structuredContent["result"]["data"]["job"]
        assert job["state"] == "succeeded"
        assert job["diagnostics_truncated"] is False
        handoff = result.structuredContent["result"]["data"]["handoff"]
        assert handoff["input_format"] == "deepkoala_detailed"

        resource = await session.read_resource(AnyUrl(job["result_uri"]))
        content = resource.contents[0]
        assert isinstance(content, types.TextResourceContents)
        assert "name,predict_label,probability,threshold,annotate" in content.text

        deleted = await session.call_tool("delete_deepkoala_job", {"job_id": job_id})
        _validate(_tool(tools, "delete_deepkoala_job"), deleted)
        assert deleted.isError is False


@pytest.mark.asyncio
async def test_schema_errors_reject_multi_and_unknown_fields_without_input_echo(
    tmp_path: Path,
) -> None:
    server = create_server(_manager(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        invalid = await session.call_tool(
            "prepare_deepkoala_job",
            {
                "fasta_text": ">secret-header\nMKTAYIAK\n",
                "multi": True,
                "unexpected": "private-value",
            },
        )

        assert invalid.isError is True
        assert invalid.structuredContent is not None
        assert invalid.structuredContent["error"]["code"] == "INVALID_REQUEST"
        serialized = json.dumps(invalid.structuredContent)
        assert "secret-header" not in serialized
        assert "private-value" not in serialized


@pytest.mark.asyncio
async def test_large_resource_requires_binary_safe_verified_pagination(tmp_path: Path) -> None:
    server = create_server(_manager(tmp_path))
    fasta = "".join(f">sequence-{index}\nMPEPTIDE\n" for index in range(3_000))
    async with create_connected_server_and_client_session(server) as session:
        prepared = await session.call_tool(
            "prepare_deepkoala_job",
            {"fasta_text": fasta, "device": "cpu"},
        )
        assert prepared.structuredContent is not None
        plan = prepared.structuredContent["result"]["data"]
        submitted = await session.call_tool(
            "submit_deepkoala_job",
            {
                "plan_id": plan["plan_id"],
                "notice_sha256": plan["notice_sha256"],
                "acknowledged": True,
            },
        )
        assert submitted.structuredContent is not None
        job_id = submitted.structuredContent["result"]["data"]["job"]["job_id"]

        result: types.CallToolResult | None = None
        for _ in range(100):
            result = await session.call_tool("get_deepkoala_job", {"job_id": job_id})
            assert result.structuredContent is not None
            if result.structuredContent["result"]["data"]["job"]["state"] == "succeeded":
                break
            await asyncio.sleep(0.02)
        assert result is not None
        assert result.structuredContent is not None
        job = result.structuredContent["result"]["data"]["job"]
        assert job["state"] == "succeeded"

        direct = await session.read_resource(AnyUrl(job["result_uri"]))
        direct_content = direct.contents[0]
        assert isinstance(direct_content, types.TextResourceContents)
        pagination = json.loads(direct_content.text)
        assert pagination["kind"] == "artifact_requires_pagination"
        assert pagination["sha256"] == job["output_sha256"]

        page = await session.read_resource(AnyUrl(pagination["next_uri"]))
        page_content = page.contents[0]
        assert isinstance(page_content, types.TextResourceContents)
        envelope = json.loads(page_content.text)
        decoded = base64.b64decode(envelope["content_base64"], validate=True)
        assert envelope["offset"] == 0
        assert envelope["returned_bytes"] == len(decoded)
        assert envelope["total_bytes"] == job["output_bytes"]
        assert envelope["next_uri"] is None
        assert hashlib.sha256(decoded).hexdigest() == job["output_sha256"]

        deleted = await session.call_tool("delete_deepkoala_job", {"job_id": job_id})
        assert deleted.isError is False


@pytest.mark.asyncio
async def test_stdio_process_initializes_without_protocol_noise(tmp_path: Path) -> None:
    checkout = _checkout(tmp_path)
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
        start_new_session=os.name == "posix",
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        await _write_stdio_message(
            process,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "deepkoala-mcp-test", "version": "0.1.0"},
                },
            },
        )
        initialized = await _read_stdio_response(process, request_id=1)
        initialized_result = cast(dict[str, object], initialized["result"])
        server_info = cast(dict[str, object], initialized_result["serverInfo"])
        assert server_info["name"] == "deepkoala-mcp"

        await _write_stdio_message(
            process,
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        await _write_stdio_message(
            process,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        listed = await _read_stdio_response(process, request_id=2)
        listed_result = cast(dict[str, object], listed["result"])
        tools = cast(list[dict[str, object]], listed_result["tools"])
        assert tuple(tool["name"] for tool in tools) == TOOL_NAMES
    finally:
        await _stop_stdio_process(process)


async def _write_stdio_message(
    process: asyncio.subprocess.Process,
    payload: dict[str, object],
) -> None:
    assert process.stdin is not None
    encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
    process.stdin.write(encoded)
    async with asyncio.timeout(5):
        await process.stdin.drain()


async def _read_stdio_response(
    process: asyncio.subprocess.Process,
    *,
    request_id: int,
) -> dict[str, object]:
    assert process.stdout is not None
    async with asyncio.timeout(5):
        line = await process.stdout.readline()
    assert line, "stdio server closed before returning a JSON-RPC response"
    payload = cast(dict[str, object], json.loads(line.decode("utf-8")))
    assert payload.get("jsonrpc") == "2.0"
    assert payload.get("id") == request_id
    assert "error" not in payload
    assert isinstance(payload.get("result"), dict)
    return payload


async def _stop_stdio_process(process: asyncio.subprocess.Process) -> None:
    if process.stdin is not None:
        process.stdin.close()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, TimeoutError):
            async with asyncio.timeout(1):
                await process.stdin.wait_closed()
    try:
        async with asyncio.timeout(2):
            await _poll_stdio_process_exit(process)
    except TimeoutError:
        if os.name == "posix":
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        else:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        async with asyncio.timeout(2):
            await _poll_stdio_process_exit(process)

    if process.stdout is not None:
        remaining = await process.stdout.read()
        for line in remaining.splitlines():
            payload = json.loads(line.decode("utf-8"))
            assert isinstance(payload, dict), "stdout contained non-protocol content"
    if process.stderr is not None:
        stderr = await process.stderr.read()
        assert b"Traceback (most recent call last)" not in stderr


async def _poll_stdio_process_exit(process: asyncio.subprocess.Process) -> None:
    while process.returncode is None:
        await asyncio.sleep(0.01)
