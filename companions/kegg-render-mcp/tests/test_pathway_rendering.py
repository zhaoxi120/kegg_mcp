"""Regular pathway overlay semantics and bounded SVG/PNG tests."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest
from kegg_mcp.analysis import PathwayReferenceScope
from kegg_mcp.domain import AnalysisUnit, EvidenceMode
from kegg_mcp.services.render_contracts import PathwayRenderTarget, RenderabilityStatus
from PIL import Image

from conftest import SyntheticProvider
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import ErrorCode, RenderMcpError
from kegg_render_mcp.pathway_scene import construct_pathway_scene
from kegg_render_mcp.raster import PNG_SIGNATURE, render_pathway_png, validate_png
from kegg_render_mcp.render_input import load_render_input
from kegg_render_mcp.svg import ACCEPTED_COLOR, UNCERTAIN_COLOR, render_pathway_svg


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
        limits=runtime_config.limits,
    )
    assert tuple(item.state for item in scene.overlays) == ("uncertain", "accepted")
    assert synthetic_provider.calls == [("ko00010", "image"), ("ko00010", "kgml")]
    assert "not pathway presence" in scene.caption
    assert "ko00010 - Synthetic pathway" in scene.caption
    assert scene.retained_box_graphic_count == 2
    assert scene.retained_polyline_graphic_count == 0
    assert scene.mapped_detected_ko_ids == ("K00001", "K00002")
    assert scene.box_overlay_count == 2
    assert scene.polyline_overlay_count == 0


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
        limits=runtime_config.limits,
    )

    assert tuple(item.state for item in scene.overlays) == ("accepted",)


@pytest.mark.asyncio
async def test_pathway_warns_without_recomputing_partially_unmapped_coverage(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    provider = SyntheticProvider()
    provider.kgml = b"""<pathway name="path:ko00010" title="Partial geometry">
  <entry id="1" name="ko:K00001" type="gene">
    <graphics name="K00001" type="rectangle" x="60" y="50" width="60" height="24"/>
  </entry>
</pathway>"""

    target = loaded.pathway("ko00010")
    scene = await construct_pathway_scene(
        loaded,
        target,
        provider,
        limits=runtime_config.limits,
    )

    assert scene.mapped_detected_ko_ids == ("K00001",)
    assert any("1 of 2" in warning and "not recomputed" in warning for warning in scene.warnings)


@pytest.mark.asyncio
async def test_pathway_fails_closed_when_detected_evidence_has_no_retained_geometry(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    provider = SyntheticProvider()
    provider.kgml = b"""<pathway name="path:ko00010" title="Unsupported geometry">
  <entry id="1" name="ko:K00001 ko:K00002" type="gene">
    <graphics name="K00001..." type="circle" x="60" y="50" width="20" height="20"/>
  </entry>
</pathway>"""

    with pytest.raises(RenderMcpError, match="no safely retained") as raised:
        await construct_pathway_scene(
            loaded,
            loaded.pathway("ko00010"),
            provider,
            limits=runtime_config.limits,
        )

    assert raised.value.detail.code is ErrorCode.ASSET_INVALID


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
        limits=runtime_config.limits,
    )
    svg = render_pathway_svg(scene, max_bytes=4_000_000, max_nodes=10_000)
    text = svg.content.decode()
    assert ACCEPTED_COLOR == "#FF0000"
    assert ACCEPTED_COLOR in text
    assert UNCERTAIN_COLOR in text
    assert "#0057FF" not in text
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
    long_warned = render_pathway_svg(
        replace(scene, warnings=(("Synthetic bounded warning text. " * 80).strip(),)),
        max_bytes=4_000_000,
        max_nodes=10_000,
    )
    assert long_warned.height > warned.height

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
        limits=runtime_config.limits,
    )

    assert "analysis unit: metagenomic_community" in scene.caption
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
        limits=runtime_config.limits,
    )
    png = render_pathway_png(scene, max_pixels=2_000_000, max_output_bytes=4_000_000)
    assert png.content.startswith(PNG_SIGNATURE)
    assert (png.width, png.height) == (760, 360)
    assert validate_png(png.content, max_bytes=4_000_000, max_pixels=2_000_000) == (760, 360)
    with Image.open(io.BytesIO(png.content)) as rendered:
        rgb = rendered.convert("RGB")
        assert rgb.getpixel((30, 50)) == tuple(bytes.fromhex(ACCEPTED_COLOR.removeprefix("#")))
        assert rgb.getpixel((130, 78)) == tuple(bytes.fromhex(UNCERTAIN_COLOR.removeprefix("#")))
        assert rgb.getpixel((137, 78)) != tuple(bytes.fromhex(UNCERTAIN_COLOR.removeprefix("#")))
        accepted_box = rgb.crop((34, 41, 86, 59))
        accepted_bytes = accepted_box.tobytes()
        assert any(
            max(accepted_bytes[offset : offset + 3]) < 80
            for offset in range(0, len(accepted_bytes), 3)
        )
    warned = render_pathway_png(
        replace(scene, warnings=("Synthetic stale-reference warning.",)),
        max_pixels=2_000_000,
        max_output_bytes=4_000_000,
    )
    assert warned.height == 360
    assert warned.content != png.content
    without_warning = render_pathway_png(
        replace(scene, warnings=()),
        max_pixels=2_000_000,
        max_output_bytes=4_000_000,
    )
    assert without_warning.height == 320
    assert without_warning.content != png.content
    long_warning = render_pathway_png(
        replace(scene, warnings=(("Synthetic bounded warning text. " * 80).strip(),)),
        max_pixels=2_000_000,
        max_output_bytes=4_000_000,
    )
    assert long_warning.height > warned.height


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
async def test_renderable_global_target_uses_tagged_polyline_overlays(
    render_input_file: Path,
    runtime_config: RendererRuntimeConfig,
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    base = loaded.pathway("ko00010")
    target = PathwayRenderTarget.model_validate(
        {
            **base.model_dump(mode="python"),
            "pathway_id": "ko01100",
            "pathway_name": "Synthetic overview",
            "pathway_class": ("ENTRY: Global Pathway",),
            "reference_scope": PathwayReferenceScope.GLOBAL_OR_OVERVIEW,
            "renderability": RenderabilityStatus.RENDERABLE,
            "not_renderable_reason": None,
        },
    )
    provider = SyntheticProvider(pathway_id="ko01100")
    provider.kgml = _synthetic_overview_kgml()

    scene = await construct_pathway_scene(
        loaded,
        target,
        provider,
        limits=runtime_config.limits,
    )

    assert provider.calls == [("ko01100", "image"), ("ko01100", "kgml")]
    assert tuple(overlay.geometry.kind for overlay in scene.overlays) == (
        "polyline",
        "polyline",
        "polyline",
        "polyline",
        "polyline",
    )
    assert tuple(overlay.state for overlay in scene.overlays) == (
        "uncertain",
        "uncertain",
        "uncertain",
        "accepted",
        "accepted",
    )
    assert scene.retained_box_graphic_count == 0
    assert scene.retained_polyline_graphic_count == 5
    assert scene.mapped_detected_ko_ids == ("K00001", "K00002")
    assert scene.box_overlay_count == 0
    assert scene.polyline_overlay_count == 5
    assert any("KEGG contextual colors and arrowheads" in warning for warning in scene.warnings)
    assert "without inferring direction or activity" in scene.caption

    svg = render_pathway_svg(scene, max_bytes=4_000_000, max_nodes=10_000)
    root = ElementTree.fromstring(svg.content)
    paths = [element for element in root.iter() if element.tag.rpartition("}")[2] == "path"]
    overlay_paths = [path for path in paths if path.attrib.get("stroke-width") == "7"]
    legend_paths = [path for path in paths if path.attrib.get("stroke-width") == "4"]
    rectangles = [element for element in root.iter() if element.tag.rpartition("}")[2] == "rect"]
    assert len(overlay_paths) == 5
    assert len(legend_paths) == 2
    assert len(rectangles) == 1
    assert all(path.attrib["d"].startswith("M ") for path in overlay_paths)
    assert sum("stroke-dasharray" in path.attrib for path in overlay_paths) == 3
    assert sum("stroke-dasharray" in path.attrib for path in legend_paths) == 1
    assert overlay_paths[-1].attrib["stroke"] == ACCEPTED_COLOR

    png = render_pathway_png(
        scene,
        max_pixels=2_000_000,
        max_output_bytes=4_000_000,
    )
    with Image.open(io.BytesIO(png.content)) as rendered:
        rgb = rendered.convert("RGB")
        assert rgb.getpixel((50, 10)) == tuple(bytes.fromhex(ACCEPTED_COLOR.removeprefix("#")))
        assert rgb.getpixel((12, 130)) == tuple(bytes.fromhex(UNCERTAIN_COLOR.removeprefix("#")))
        assert rgb.getpixel((21, 130)) == (255, 255, 255)
        assert rgb.getpixel((12, 30)) == tuple(bytes.fromhex(UNCERTAIN_COLOR.removeprefix("#")))
        assert rgb.getpixel((60, 30)) == tuple(bytes.fromhex(ACCEPTED_COLOR.removeprefix("#")))


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
            limits=runtime_config.limits,
        )


def _synthetic_overview_kgml() -> bytes:
    return b"""<pathway name="path:ko01100" title="Synthetic overview">
  <entry id="1" name="ko:K00001 ko:K00002" type="gene">
    <graphics name="K00001..." type="line" coords="10,10,110,10"/>
  </entry>
  <entry id="2" name="ko:K00002" type="gene">
    <graphics name="K00002" type="line" coords="10,130,110,130"/>
  </entry>
  <entry id="3" name="ko:K00002" type="gene">
    <graphics name="K00002" type="line" coords="10,10,110,10"/>
  </entry>
  <entry id="4" name="ko:K00002" type="gene">
    <graphics name="K00002" type="line" coords="10,30,110,30"/>
  </entry>
  <entry id="5" name="ko:K00001" type="gene">
    <graphics name="K00001" type="line" coords="50,30,100,30"/>
  </entry>
</pathway>"""
