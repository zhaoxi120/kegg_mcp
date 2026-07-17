"""Offline end-to-end contract tests for DeepKOALA companion handoffs."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from deepkoala_mcp.config import DeepKoalaRuntimeConfig
from deepkoala_mcp.contracts import ImportHandoff, SourceProvenance
from deepkoala_mcp.installation import RuntimeProbeResult
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.runner import ProcessOutcome, RunnerPlan
from deepkoala_mcp.server import create_server as create_deepkoala_server
from mcp import types
from mcp.client.session import ClientSession
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl, ValidationError

from kegg_mcp.importers import import_deepkoala_detailed
from kegg_mcp.kegg import (
    GetRequest,
    GetResult,
    KeggClientConfig,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    OfflineCacheAccess,
)
from kegg_mcp.mcp.contracts import NormalizeKoAnnotationsInput
from kegg_mcp.mcp.input_validation import validate_tool_input
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.server import create_server as create_core_server
from kegg_mcp.services.result_store import SQLiteResultStore

_SMALL_DETAILED_CSV = (
    b"name,predict_label,probability,threshold,annotate\n"
    b"protein-1,K00001,0.95,0.50,*\n"
    b"protein-2,K00002,0.65,0.70,\n"
)


class _FakeRunner:
    """Write synthetic valid detailed output after an optional test-controlled gate."""

    def __init__(self, payload: bytes, *, gated: bool = False) -> None:
        self.payload = payload
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not gated:
            self.release.set()

    async def run(self, plan: RunnerPlan) -> ProcessOutcome:
        self.started.set()
        await self.release.wait()
        plan.output_path.write_bytes(self.payload)
        return ProcessOutcome(return_code=0)


class _OfflineOnlyKeggClient:
    """A structural core client that proves normalization never reaches KEGG."""

    def __init__(self) -> None:
        self._config = KeggClientConfig(access=OfflineCacheAccess())

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del request, options
        raise AssertionError("normalization must not call KEGG GET")

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        del request, options
        raise AssertionError("normalization must not call KEGG LINK")


def _build_runtime_config(root: Path) -> DeepKoalaRuntimeConfig:
    checkout = root / "deepkoala-checkout"
    package = checkout / "deepkoala"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "utils.py").write_text(
        "def resolve_device(value): return value\n",
        encoding="utf-8",
    )
    (package / "cli.py").write_text("# synthetic offline CLI\n", encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "deepkoala"\nversion = "0.1-test"\n',
        encoding="utf-8",
    )
    resources = checkout / "resources" / "202502"
    resources.mkdir(parents=True)
    for model in ("full", "frag"):
        (resources / f"weights_{model}.pt").write_bytes(model.encode("ascii"))
        (resources / f"ko_config_{model}.json").write_text("{}", encoding="utf-8")
    inputs = root / "inputs"
    outputs = root / "outputs"
    inputs.mkdir()
    outputs.mkdir()
    return DeepKoalaRuntimeConfig(
        checkout=checkout.resolve(),
        python_executable=Path(sys.executable).resolve(),
        state_root=(root / "companion-state").resolve(),
        input_roots=(inputs.resolve(),),
        output_roots=(outputs.resolve(),),
        max_timeout_seconds=30,
    )


def _ready_probe(
    *,
    checkout: Path,
    python_executable: Path,
    cpu_threads: int,
) -> RuntimeProbeResult:
    del checkout, python_executable, cpu_threads
    return RuntimeProbeResult(runtime_ready=True, cuda_available=False)


def _core_runtime(root: Path) -> McpRuntime:
    return McpRuntime(
        client=_OfflineOnlyKeggClient(),
        result_store=SQLiteResultStore(root / "core-results.sqlite3"),
        scope_id="deepkoala-cross-server-contract",
        allowed_roots=(str(root.resolve()),),
    )


def _run_arguments(config: DeepKoalaRuntimeConfig, name: str) -> dict[str, object]:
    fasta = config.input_roots[0] / f"{name}.faa"
    fasta.write_text(">protein-1\nMPEPTIDE\n>protein-2\nMPEPTIDE\n", encoding="ascii")
    return {
        "fasta_path": str(fasta),
        "output_directory": str(config.output_roots[0] / name),
    }


def _wire_payload(result: types.CallToolResult) -> dict[str, object]:
    """Force the same JSON boundary used between independent MCP processes."""
    assert result.structuredContent is not None
    return cast(
        dict[str, object],
        json.loads(json.dumps(result.structuredContent, allow_nan=False)),
    )


def _wire_data(result: types.CallToolResult) -> dict[str, object]:
    payload = _wire_payload(result)
    assert payload["ok"] is True
    wrapped = cast(dict[str, object], payload["result"])
    return cast(dict[str, object], wrapped["data"])


def _parse_handoff(result: types.CallToolResult) -> ImportHandoff:
    raw = _wire_data(result)["handoff"]
    if raw is None:
        raise ValueError("a running or unsuccessful job has no import handoff")
    return ImportHandoff.model_validate_json(json.dumps(raw, allow_nan=False))


async def _start_job(
    session: ClientSession,
    config: DeepKoalaRuntimeConfig,
    name: str,
) -> str:
    started = await session.call_tool("run_deepkoala_job", _run_arguments(config, name))
    assert started.isError is False
    job = cast(dict[str, object], _wire_data(started)["job"])
    return cast(str, job["job_id"])


async def _poll_terminal(session: ClientSession, job_id: str) -> types.CallToolResult:
    async with asyncio.timeout(5):
        while True:
            result = await session.call_tool("get_deepkoala_job", {"job_id": job_id})
            assert result.isError is False
            job = cast(dict[str, object], _wire_data(result)["job"])
            if job["state"] != "running":
                assert job["state"] == "succeeded"
                return result
            await asyncio.sleep(0.01)


def _core_arguments(
    handoff: ImportHandoff,
    *,
    text: str | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {
        "input_format": handoff.input_format,
        "source": handoff.source.model_dump(mode="json"),
    }
    if text is None:
        arguments["file_path"] = handoff.annotations_path
    else:
        arguments["text"] = text
    validate_tool_input(NormalizeKoAnnotationsInput, arguments)
    return arguments


async def _normalize_once(root: Path, arguments: dict[str, object]) -> dict[str, object]:
    server = create_core_server(_core_runtime(root))
    with patch(
        "kegg_mcp.services.normalization.import_deepkoala_detailed",
        wraps=import_deepkoala_detailed,
    ) as importer:
        async with create_connected_server_and_client_session(server) as session:
            result = await session.call_tool("normalize_ko_annotations", arguments)
        assert result.isError is False
        assert importer.call_count == 1
    return _wire_data(result)


def _require_resource_schema(payload: dict[str, object]) -> None:
    if payload.get("schema_version") != "1":
        raise ValueError("unsupported or missing DeepKOALA resource schema_version")
    if payload.get("artifact") != "annotations" or payload.get("encoding") != "base64":
        raise ValueError("unexpected DeepKOALA annotation resource envelope")


async def _read_annotation_resource(session: ClientSession, uri: str) -> str:
    resource = await session.read_resource(AnyUrl(uri))
    item = cast(types.TextResourceContents, resource.contents[0])
    if item.mimeType == "text/csv":
        return item.text
    notice = cast(dict[str, object], json.loads(item.text))
    _require_resource_schema(notice)
    total_bytes = cast(int, notice["total_bytes"])
    next_uri = cast(str | None, notice["next_uri"])
    expected_offset = 0
    chunks: list[bytes] = []
    while next_uri is not None:
        page_resource = await session.read_resource(AnyUrl(next_uri))
        page_item = cast(types.TextResourceContents, page_resource.contents[0])
        page = cast(dict[str, object], json.loads(page_item.text))
        _require_resource_schema(page)
        if page.get("offset") != expected_offset or page.get("total_bytes") != total_bytes:
            raise ValueError("non-contiguous DeepKOALA annotation resource page")
        chunk = base64.b64decode(cast(str, page["content_base64"]), validate=True)
        if len(chunk) != page["returned_bytes"]:
            raise ValueError("DeepKOALA annotation resource byte count mismatch")
        chunks.append(chunk)
        expected_offset += len(chunk)
        next_uri = cast(str | None, page["next_uri"])
    payload = b"".join(chunks)
    if len(payload) != total_bytes:
        raise ValueError("incomplete DeepKOALA annotation resource")
    return payload.decode("utf-8", errors="strict")


@pytest.mark.asyncio
async def test_shared_file_handoff_crosses_real_mcp_json_boundary_once(
    tmp_path: Path,
) -> None:
    config = _build_runtime_config(tmp_path)
    runner = _FakeRunner(_SMALL_DETAILED_CSV, gated=True)
    manager = DeepKoalaJobManager(config, runner=runner, runtime_probe=_ready_probe)
    server = create_deepkoala_server(manager)

    async with create_connected_server_and_client_session(server) as session:
        job_id = await _start_job(session, config, "shared-filesystem")
        await asyncio.wait_for(runner.started.wait(), timeout=2)
        running = await session.call_tool("get_deepkoala_job", {"job_id": job_id})
        assert _wire_data(running)["handoff"] is None
        with pytest.raises(ValueError, match="has no import handoff"):
            _parse_handoff(running)

        runner.release.set()
        completed = await _poll_terminal(session, job_id)
        raw_handoff = cast(dict[str, object], _wire_data(completed)["handoff"])
        raw_source = cast(dict[str, object], raw_handoff["source"])
        assert cast(str, raw_source["annotation_date"]).endswith("Z")
        handoff = _parse_handoff(completed)
        assert handoff.schema_version == "1"
        assert handoff.source.annotation_date.isoformat().endswith("+00:00")
        assert Path(handoff.annotations_path).read_bytes() == _SMALL_DETAILED_CSV
        assert Path(handoff.report_path).is_file()

        normalized = await _normalize_once(tmp_path, _core_arguments(handoff))
        summary = cast(dict[str, object], normalized["import_summary"])
        assert summary["emitted_records"] == 2
        provenance = cast(dict[str, object], normalized["provenance"])
        source = cast(dict[str, object], cast(list[object], provenance["source_preview"])[0])
        assert source["source_name"] == "deepkoala"
        assert source["input_path"] == str(config.input_roots[0] / "shared-filesystem.faa")

        stable_annotations = Path(handoff.annotations_path)
        deleted = await session.call_tool("delete_deepkoala_job", {"job_id": job_id})
        assert deleted.isError is False
        assert stable_annotations.is_file()
        with pytest.raises(McpError, match="ARTIFACT_NOT_FOUND"):
            await session.read_resource(AnyUrl(handoff.annotations_resource_uri))


@pytest.mark.parametrize("large", [False, True], ids=["direct-text", "paged-base64"])
@pytest.mark.asyncio
async def test_resource_fallback_reconstructs_inline_core_input_with_offset_timestamp(
    tmp_path: Path,
    large: bool,
) -> None:
    rows = 2 if not large else 3_000
    payload = b"name,predict_label,probability,threshold,annotate\n" + b"".join(
        f"protein-{index},K00001,0.95,0.50,*\n".encode("ascii") for index in range(rows)
    )
    config = _build_runtime_config(tmp_path)
    runner = _FakeRunner(payload)
    manager = DeepKoalaJobManager(config, runner=runner, runtime_probe=_ready_probe)
    server = create_deepkoala_server(manager)

    async with create_connected_server_and_client_session(server) as session:
        job_id = await _start_job(session, config, f"resource-{large}")
        completed = await _poll_terminal(session, job_id)
        handoff = _parse_handoff(completed)
        annotation_text = await _read_annotation_resource(
            session,
            handoff.annotations_resource_uri,
        )
        assert annotation_text.encode("utf-8") == payload

        source = handoff.source.model_dump(mode="json")
        utc_text = cast(str, source["annotation_date"])
        parsed = datetime.fromisoformat(utc_text.replace("Z", "+00:00"))
        source["annotation_date"] = parsed.astimezone(timezone(timedelta(hours=9))).isoformat()
        offset_handoff = handoff.model_copy(
            update={
                "source": SourceProvenance.model_validate_json(json.dumps(source, allow_nan=False))
            }
        )
        arguments = _core_arguments(offset_handoff, text=annotation_text)
        normalized = await _normalize_once(tmp_path, arguments)
        summary = cast(dict[str, object], normalized["import_summary"])
        assert summary["emitted_records"] == rows
        provenance = cast(dict[str, object], normalized["provenance"])
        first_source = cast(
            dict[str, object],
            cast(list[object], provenance["source_preview"])[0],
        )
        assert cast(str, first_source["annotation_date"]).endswith("+09:00")


@pytest.mark.asyncio
async def test_handoff_and_resource_versions_fail_closed(tmp_path: Path) -> None:
    config = _build_runtime_config(tmp_path)
    runner = _FakeRunner(
        b"name,predict_label,probability,threshold,annotate\n"
        + b"".join(
            f"protein-{index},K00001,0.95,0.50,*\n".encode("ascii") for index in range(3_000)
        )
    )
    manager = DeepKoalaJobManager(config, runner=runner, runtime_probe=_ready_probe)
    server = create_deepkoala_server(manager)
    async with create_connected_server_and_client_session(server) as session:
        job_id = await _start_job(session, config, "version-errors")
        completed = await _poll_terminal(session, job_id)
        raw_handoff = cast(dict[str, object], _wire_data(completed)["handoff"])
        for value in (None, "2"):
            malformed = dict(raw_handoff)
            if value is None:
                malformed.pop("schema_version")
            else:
                malformed["schema_version"] = value
            with pytest.raises(ValidationError):
                ImportHandoff.model_validate_json(json.dumps(malformed))

        handoff = _parse_handoff(completed)
        resource = await session.read_resource(AnyUrl(handoff.annotations_resource_uri))
        notice = cast(
            dict[str, object],
            json.loads(cast(types.TextResourceContents, resource.contents[0]).text),
        )
        _require_resource_schema(notice)
        for value in (None, "2"):
            malformed_notice = dict(notice)
            if value is None:
                malformed_notice.pop("schema_version")
            else:
                malformed_notice["schema_version"] = value
            with pytest.raises(ValueError, match="schema_version"):
                _require_resource_schema(malformed_notice)
