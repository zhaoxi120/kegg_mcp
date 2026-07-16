"""Regular pathway overlay semantics and bounded SVG/PNG tests."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest
from kegg_mcp.analysis import PathwayReferenceScope
from kegg_mcp.domain import AnalysisUnit, EvidenceMode
from kegg_mcp.services.render_contracts import RenderabilityStatus

from conftest import SyntheticProvider
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import ErrorCode, RenderMcpError
from kegg_render_mcp.pathway_scene import construct_pathway_scene
from kegg_render_mcp.raster import PNG_SIGNATURE, render_pathway_png, validate_png
from kegg_render_mcp.render_input import load_render_input
from kegg_render_mcp.svg import render_pathway_svg


@pytest.mark.asyncio
async def test_accepted_precedes_uncertain_on_multi_ko_graphic(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    scene = await construct_pathway_scene(
        loaded,
        loaded.pathway("ko00010"),
        synthetic_provider,
        max_asset_bytes=runtime_config.limits.max_asset_bytes,
        max_pixels=runtime_config.limits.max_pixels,
        limits=runtime_config.limits,
    )
    assert tuple(item.state for item in scene.overlays) == ("accepted", "uncertain")
    assert synthetic_provider.calls == [("ko00010", "image"), ("ko00010", "kgml")]
    assert "not pathway presence" in scene.caption


@pytest.mark.asyncio
async def test_strict_pathway_does_not_color_uncertain_evidence(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    strict_target = loaded.pathway("ko00010").model_copy(
        update={"evidence_mode": EvidenceMode.STRICT}
    )
    scene = await construct_pathway_scene(
        loaded,
        strict_target,
        synthetic_provider,
        max_asset_bytes=runtime_config.limits.max_asset_bytes,
        max_pixels=runtime_config.limits.max_pixels,
        limits=runtime_config.limits,
    )
    assert tuple(item.state for item in scene.overlays) == ("accepted",)


@pytest.mark.asyncio
async def test_pathway_colors_only_authoritative_detected_evidence(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    target = loaded.pathway("ko00010").model_copy(
        update={
            "detected_ko_ids": ("K00001",),
            "coverage_numerator": 1,
            "coverage_ratio": 1 / 3,
        }
    )
    scene = await construct_pathway_scene(
        loaded,
        target,
        synthetic_provider,
        max_asset_bytes=runtime_config.limits.max_asset_bytes,
        max_pixels=runtime_config.limits.max_pixels,
        limits=runtime_config.limits,
    )

    assert tuple(item.state for item in scene.overlays) == ("accepted",)


@pytest.mark.asyncio
async def test_pathway_svg_is_static_accessible_and_evidence_calibrated(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    scene = await construct_pathway_scene(
        loaded,
        loaded.pathway("ko00010"),
        synthetic_provider,
        max_asset_bytes=runtime_config.limits.max_asset_bytes,
        max_pixels=runtime_config.limits.max_pixels,
        limits=runtime_config.limits,
    )
    svg = render_pathway_svg(scene, max_bytes=4_000_000, max_nodes=10_000)
    text = svg.content.decode()
    assert "Accepted annotation" in text
    assert "Policy-defined uncertain annotation" in text
    assert "not evidence of biological absence" in text
    assert "<script" not in text.lower()
    assert "javascript:" not in text.lower()
    assert "https://" not in text.lower()
    assert "data:image/png;base64," in text
    assert "Warnings:" in text
    warned = render_pathway_svg(
        replace(scene, warnings=("Synthetic stale-reference warning.",)),
        max_bytes=4_000_000,
        max_nodes=10_000,
    )
    assert "Warnings: Synthetic stale-reference warning." in warned.content.decode()

    node_count = sum(1 for _ in ElementTree.fromstring(svg.content).iter())
    render_pathway_svg(scene, max_bytes=4_000_000, max_nodes=node_count)
    with pytest.raises(RenderMcpError) as bounded:
        render_pathway_svg(scene, max_bytes=4_000_000, max_nodes=node_count - 1)
    assert bounded.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED


@pytest.mark.asyncio
async def test_pathway_caption_preserves_community_analysis_unit(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    community_document = loaded.document.model_copy(
        update={
            "dataset": loaded.document.dataset.model_copy(
                update={"analysis_unit": AnalysisUnit.METAGENOMIC_COMMUNITY}
            )
        }
    )
    community_input = replace(loaded, document=community_document)
    scene = await construct_pathway_scene(
        community_input,
        community_input.pathway("ko00010"),
        synthetic_provider,
        max_asset_bytes=runtime_config.limits.max_asset_bytes,
        max_pixels=runtime_config.limits.max_pixels,
        limits=runtime_config.limits,
    )

    assert scene.analysis_unit == "metagenomic_community"
    assert "pooled encoded potential" in scene.caption


@pytest.mark.asyncio
async def test_pathway_png_contains_bounded_raster_derivative(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    scene = await construct_pathway_scene(
        loaded,
        loaded.pathway("ko00010"),
        synthetic_provider,
        max_asset_bytes=runtime_config.limits.max_asset_bytes,
        max_pixels=runtime_config.limits.max_pixels,
        limits=runtime_config.limits,
    )
    png = render_pathway_png(
        scene, max_asset_bytes=2_000_000, max_pixels=2_000_000, max_output_bytes=4_000_000
    )
    assert png.content.startswith(PNG_SIGNATURE)
    assert (png.width, png.height) == (760, 360)
    assert validate_png(png.content, max_bytes=4_000_000, max_pixels=2_000_000) == (760, 360)
    warned = render_pathway_png(
        replace(scene, warnings=("Synthetic stale-reference warning.",)),
        max_asset_bytes=2_000_000,
        max_pixels=2_000_000,
        max_output_bytes=4_000_000,
    )
    assert warned.height == 360
    assert warned.content != png.content
    without_warning = render_pathway_png(
        replace(scene, warnings=()),
        max_asset_bytes=2_000_000,
        max_pixels=2_000_000,
        max_output_bytes=4_000_000,
    )
    assert without_warning.height == 320
    assert without_warning.content != png.content


def test_png_validation_rejects_signature_truncation_and_pixel_limit() -> None:
    for payload in (b"", PNG_SIGNATURE + b"broken"):
        with pytest.raises(RenderMcpError) as raised:
            validate_png(payload, max_bytes=1000, max_pixels=1000)
        assert raised.value.detail.code is ErrorCode.ASSET_INVALID


@pytest.mark.asyncio
async def test_svg_allows_url_like_text_and_replaces_invalid_xml_characters(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    scene = await construct_pathway_scene(
        loaded,
        loaded.pathway("ko00010"),
        synthetic_provider,
        max_asset_bytes=runtime_config.limits.max_asset_bytes,
        max_pixels=runtime_config.limits.max_pixels,
        limits=runtime_config.limits,
    )
    artifact = render_pathway_svg(
        replace(
            scene,
            title="Reference text https://example.invalid",
            warnings=("The literal onload= token is harmless text.\x01",),
        ),
        max_bytes=4_000_000,
        max_nodes=10_000,
    )
    text = artifact.content.decode()

    assert "https://example.invalid" in text
    assert "onload= token" in text
    assert "\ufffd" in text


@pytest.mark.asyncio
async def test_global_or_overview_target_is_rejected_before_asset_retrieval(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
    synthetic_provider: SyntheticProvider,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    base = loaded.pathway("ko00010")
    target = base.model_copy(
        update={
            "pathway_id": "ko01100",
            "pathway_class": ("Metabolism; Global and overview maps",),
            "reference_scope": PathwayReferenceScope.GLOBAL_OR_OVERVIEW,
            "renderability": RenderabilityStatus.SUMMARY_ONLY,
            "not_renderable_reason": "global_or_overview_pathway_unsupported",
        }
    )
    with pytest.raises(RenderMcpError) as raised:
        await construct_pathway_scene(
            loaded,
            target,
            synthetic_provider,
            max_asset_bytes=2_000_000,
            max_pixels=2_000_000,
            limits=runtime_config.limits,
        )
    assert raised.value.detail.code is ErrorCode.TARGET_NOT_RENDERABLE
    assert synthetic_provider.calls == []


@pytest.mark.asyncio
async def test_mismatched_asset_identity_is_rejected(
    render_input_file: Path, runtime_config: RendererRuntimeConfig
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    provider = SyntheticProvider(pathway_id="ko00020")
    with pytest.raises(RenderMcpError, match="identities"):
        await construct_pathway_scene(
            loaded,
            loaded.pathway("ko00010"),
            provider,
            max_asset_bytes=2_000_000,
            max_pixels=2_000_000,
            limits=runtime_config.limits,
        )
