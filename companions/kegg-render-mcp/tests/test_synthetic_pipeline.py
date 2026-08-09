"""Fully synthetic FASTA-to-render integration across all three MCP servers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import cast

import pytest
from deepkoala_mcp.jobs import DeepKoalaJobManager
from deepkoala_mcp.server import create_server as create_deepkoala_server
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.server import create_server as create_core_server
from kegg_mcp.services.reference_budget import KeggMcpClient
from kegg_mcp.services.render_contracts import RENDER_INPUT_SCHEMA_VERSION, RenderInput
from kegg_mcp.services.result_store import SQLiteResultStore
from mcp.shared.memory import create_connected_server_and_client_session
from test_deepkoala_companion_handoff import (
    _build_runtime_config,
    _FakeRunner,
    _parse_handoff,
    _poll_terminal,
    _ready_probe,
    _start_job,
    _wire_data,
)
from test_mcp_server import _FakeReferenceClient

from conftest import SyntheticProvider
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.render_service import RendererService
from kegg_render_mcp.server import RendererRuntime
from kegg_render_mcp.server import create_server as create_renderer_server

_DETAILED_CSV = (
    b"name,predict_label,probability,threshold,annotate\n"
    b"protein-1,K00001,0.95,0.50,*\n"
    b"protein-2,K00002,0.65,0.70,\n"
)
_EXPECTED_ARTIFACTS = {
    "ko00010.svg",
    "ko00010.png",
    "M00001.svg",
    "M00001.png",
    "render_manifest.json",
}


def _forbid_network(*_: object, **__: object) -> None:
    raise AssertionError("the fully synthetic pipeline must not access the network")


@pytest.mark.asyncio
async def test_fasta_handoff_accepted_ko_view_flows_into_safe_renderer_output(
    tmp_path: Path,
    allowed_root: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kegg_mcp.kegg.transport.HttpsTransport.request",
        _forbid_network,
    )

    deepkoala_config = _build_runtime_config(tmp_path)
    deepkoala_runner = _FakeRunner(_DETAILED_CSV)
    deepkoala_manager = DeepKoalaJobManager(
        deepkoala_config,
        runner=deepkoala_runner,
        runtime_probe=_ready_probe,
    )
    async with create_connected_server_and_client_session(
        create_deepkoala_server(deepkoala_manager)
    ) as deepkoala_session:
        job_id = await _start_job(deepkoala_session, deepkoala_config, "synthetic-pipeline")
        completed = await _poll_terminal(deepkoala_session, job_id)
        handoff = _parse_handoff(completed)
        handoff_payload = cast(dict[str, object], _wire_data(completed)["handoff"])

    expected_fasta = deepkoala_config.input_roots[0] / "synthetic-pipeline.faa"
    assert handoff.input_path == str(expected_fasta)
    assert handoff.source.model_version == "202502"
    assert handoff.source.model_name == "full"
    assert handoff.input_format == "deepkoala_detailed"
    assert Path(handoff.annotations_path).read_bytes() == _DETAILED_CSV

    reference_client = _FakeReferenceClient()
    core_runtime = McpRuntime(
        client=cast(KeggMcpClient, reference_client),
        result_store=SQLiteResultStore(tmp_path / "core-results.sqlite3"),
        scope_id="synthetic-three-mcp-pipeline",
        allowed_roots=(str(tmp_path.resolve()),),
    )
    analysis_output = allowed_root / "analysis"
    source = cast(dict[str, object], handoff_payload["source"])
    async with create_connected_server_and_client_session(
        create_core_server(core_runtime)
    ) as core_session:
        analyzed = await core_session.call_tool(
            "analyze_ko_annotations",
            {
                "annotations": {
                    "file_path": handoff_payload["annotations_path"],
                    "input_format": handoff_payload["input_format"],
                    "source": source,
                },
                "module_ids": ["M00001"],
                "pathways": [{"pathway_id": "ko00010"}],
                "output_directory": str(analysis_output),
            },
        )
        assert analyzed.isError is False
        core_data = _wire_data(analyzed)

    output_bundle = cast(dict[str, object], core_data["output_bundle"])
    render_input_path = Path(cast(str, output_bundle["render_input"]))
    render_input = RenderInput.model_validate_json(
        render_input_path.read_text(encoding="utf-8"),
        strict=True,
    )
    assert render_input.schema_version == RENDER_INPUT_SCHEMA_VERSION == "6"
    assert render_input.evidence.accepted_ko_ids == ("K00001",)
    assert [item.module_id for item in render_input.modules] == ["M00001"]
    assert [item.pathway_id for item in render_input.pathways] == ["ko00010"]
    assert reference_client.call_log == [
        ("get", "M00001"),
        ("link", "pathway_to_ko"),
        ("get", "ko00010"),
    ]

    renderer_runtime = RendererRuntime(
        runtime_config,
        RendererService(runtime_config, synthetic_provider),
    )
    render_output = allowed_root / "rendered"
    async with create_connected_server_and_client_session(
        create_renderer_server(renderer_runtime)
    ) as renderer_session:
        rendered = await renderer_session.call_tool(
            "render_analysis_bundle",
            {
                "render_input_path": str(render_input_path),
                "formats": ["svg", "png"],
                "output_directory": str(render_output),
            },
        )
        assert rendered.isError is False
        render_data = _wire_data(rendered)

    assert render_data["target_ids"] == ["ko00010", "M00001"]
    artifacts = cast(list[dict[str, object]], render_data["artifacts"])
    assert {cast(str, item["name"]) for item in artifacts} == _EXPECTED_ARTIFACTS
    assert synthetic_provider.calls == [("ko00010", "image"), ("ko00010", "kgml")]

    resolved_output = render_output.resolve(strict=True)
    for name in _EXPECTED_ARTIFACTS:
        path = render_output / name
        assert path.is_file()
        assert not path.is_symlink()
        assert path.resolve(strict=True).is_relative_to(resolved_output)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    for name in ("ko00010.svg", "M00001.svg"):
        svg = (render_output / name).read_text(encoding="utf-8")
        assert "<svg" in svg
        assert "<script" not in svg.lower()
        assert 'href="http://' not in svg.lower()
        assert 'href="https://' not in svg.lower()
        assert "url(http" not in svg.lower()
    for name in ("ko00010.png", "M00001.png"):
        assert (render_output / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    manifest_text = (render_output / "render_manifest.json").read_text(encoding="utf-8")
    manifest = cast(dict[str, object], json.loads(manifest_text))
    provenance = cast(dict[str, object], manifest["provenance"])
    assert manifest["schema_version"] == "4"
    assert provenance["accepted_unique_ko_count"] == 1
    assert "annotation_retention" not in provenance
    assert "record_level_evidence_retained" not in provenance
    assert str(runtime_config.state_root) not in manifest_text
    assert str(render_input_path) not in manifest_text
