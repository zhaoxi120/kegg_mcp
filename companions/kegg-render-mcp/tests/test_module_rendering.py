"""MODULE AST, completion, warning, summary, and canvas tests."""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from kegg_mcp.analysis import (
    ModuleBlockState,
    ModuleEvaluationStatus,
    ModuleExpressionKind,
    ModuleReferenceIssueKind,
)
from kegg_mcp.domain import AnalysisUnit
from kegg_mcp.services.render_contracts import (
    MODULE_RENDER_MAX_CANVAS_DIMENSION,
    MODULE_RENDER_MAX_CANVAS_PIXELS,
    RenderabilityStatus,
)
from PIL import Image, ImageChops

from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import ErrorCode, RenderMcpError
from kegg_render_mcp.module_scene import construct_module_scene
from kegg_render_mcp.raster import PNG_SIGNATURE, render_module_png
from kegg_render_mcp.render_input import load_render_input
from kegg_render_mcp.svg import ACCEPTED_COLOR, render_module_svg


def test_module_scene_preserves_and_or_grouping_and_authoritative_state(
    render_input_file: Path, runtime_config: RendererRuntimeConfig
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    scene = construct_module_scene(
        loaded.module("M00001"), analysis_unit=loaded.document.dataset.analysis_unit
    )
    kinds = {node.kind for node in scene.nodes}
    assert {"module_definition", "and", "or", "group", "ko"} <= kinds
    assert scene.status == "complete"
    assert scene.exact_completion is True
    assert scene.block_coverage == 1.0
    assert {block.state for block in scene.blocks} == {"complete"}
    assert tuple((item.source_module_id, item.state) for item in scene.optional_components) == (
        ("M00001", "absent"),
    )
    assert tuple(
        (item.source_module_id, item.target_module_id) for item in scene.reference_edges
    ) == (("M00001", "M00002"),)
    assert scene.width <= MODULE_RENDER_MAX_CANVAS_DIMENSION
    assert scene.height <= MODULE_RENDER_MAX_CANVAS_DIMENSION
    assert scene.width * scene.height <= MODULE_RENDER_MAX_CANVAS_PIXELS


def test_module_svg_uses_neutral_ast_and_evidence_colors_only_for_blocks(
    render_input_file: Path, runtime_config: RendererRuntimeConfig
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    scene = construct_module_scene(
        loaded.module("M00001"), analysis_unit=loaded.document.dataset.analysis_unit
    )
    svg = render_module_svg(scene, max_bytes=4_000_000, max_nodes=10_000)
    text = svg.content.decode()
    assert "Exact: complete" in text
    assert "Block coverage: 100.0%" in text
    assert "AND (plus)" in text
    assert "OR (,)" in text
    assert "Optional 1 (M00001)" in text
    assert "M00001 -&gt; M00002" in text
    assert "#4B5563" in text
    assert ACCEPTED_COLOR in text
    assert "biochemical topology" in text
    assert "Analysis unit: unknown" in text


def test_module_png_is_bounded_static_derivative(
    render_input_file: Path, runtime_config: RendererRuntimeConfig
) -> None:
    loaded = load_render_input(str(render_input_file), runtime_config)
    scene = construct_module_scene(
        loaded.module("M00001"), analysis_unit=loaded.document.dataset.analysis_unit
    )
    png = render_module_png(scene, max_pixels=4_000_000, max_output_bytes=4_000_000)
    assert png.content.startswith(PNG_SIGNATURE)
    assert png.width == scene.width
    assert png.height == scene.height


def test_m00001_shaped_module_uses_tight_shared_svg_and_png_layout() -> None:
    scene = construct_module_scene(
        _m00001_shaped_target(), analysis_unit=AnalysisUnit.ISOLATE_PROTEOME
    )

    assert len(scene.nodes) == 55
    assert max(node.depth for node in scene.nodes) == 7
    assert (scene.width, scene.height, scene.node_y) == (1816, 3250, 448)

    svg = render_module_svg(scene, max_bytes=4_000_000, max_nodes=10_000)
    png = render_module_png(scene, max_pixels=20_000_000, max_output_bytes=4_000_000)
    assert (svg.width, svg.height) == (scene.width, scene.height)
    assert (png.width, png.height) == (scene.width, scene.height)
    assert b'x="50.00" y="140.00"' in svg.content
    assert b'x="50" y="448" width="176" height="40"' in svg.content

    with Image.open(io.BytesIO(png.content)) as image:
        canvas = image.convert("RGB")
        content_bounds = ImageChops.difference(
            canvas, Image.new("RGB", canvas.size, "white")
        ).getbbox()
    assert content_bounds is not None
    assert scene.width - content_bounds[2] <= 50
    assert scene.height - content_bounds[3] <= 70


def test_summary_only_module_retains_not_evaluable_reason_in_graphic() -> None:
    completion = SimpleNamespace(
        evaluation_status=ModuleEvaluationStatus.NOT_EVALUABLE,
        is_complete=None,
        block_coverage=None,
    )
    target = SimpleNamespace(
        module_id="M00002",
        module_name="Oversized synthetic MODULE",
        renderability=RenderabilityStatus.SUMMARY_ONLY,
        not_renderable_reason="ast_node_limit_exceeded",
        definitions=(),
        required_block_states=(),
        optional_component_states=(),
        reference_edges=(),
        completion=completion,
        reference_issues=(),
        warnings=(SimpleNamespace(message="Unsupported token remains visible."),),
    )
    scene = construct_module_scene(
        cast(Any, target), analysis_unit=AnalysisUnit.METAGENOMIC_COMMUNITY
    )
    assert scene.nodes[0].kind == "summary_only"
    assert "ast_node_limit_exceeded" in scene.nodes[0].label
    svg = render_module_svg(scene, max_bytes=100_000, max_nodes=100)
    assert b"Summary only: ast_node_limit_exceeded" in svg.content
    assert b"Unsupported token remains visible" in svg.content
    assert b"pooled encoded potential" in svg.content


def test_unresolved_cycle_and_unsupported_nodes_remain_visible() -> None:
    leaf = SimpleNamespace(
        kind=ModuleExpressionKind.UNSUPPORTED,
        value="RM001",
        children=(),
        operators=(),
    )
    ast = SimpleNamespace(required_blocks=(leaf,))
    definition = SimpleNamespace(
        definition=SimpleNamespace(module_id="M00003"),
        parse_result=SimpleNamespace(ast=ast),
    )
    completion = SimpleNamespace(
        evaluation_status=ModuleEvaluationStatus.PARTIALLY_EVALUABLE,
        is_complete=None,
        block_coverage=None,
    )
    issue = SimpleNamespace(
        kind=ModuleReferenceIssueKind.CYCLE,
        message="M00003 reference cycle is unresolved.",
    )
    target = SimpleNamespace(
        module_id="M00003",
        module_name=None,
        renderability=RenderabilityStatus.RENDERABLE,
        not_renderable_reason=None,
        definitions=(definition,),
        required_block_states=(),
        optional_component_states=(),
        reference_edges=(),
        completion=completion,
        reference_issues=(issue,),
        warnings=(),
    )
    scene = construct_module_scene(cast(Any, target), analysis_unit=AnalysisUnit.UNKNOWN)
    assert any(node.unsupported and node.label == "RM001" for node in scene.nodes)
    svg = render_module_svg(scene, max_bytes=100_000, max_nodes=100)
    assert b"reference cycle is unresolved" in svg.content
    with_warning = render_module_png(scene, max_pixels=2_000_000, max_output_bytes=500_000)
    without_warning = render_module_png(
        replace(scene, warnings=()), max_pixels=2_000_000, max_output_bytes=500_000
    )
    assert with_warning.content != without_warning.content


def test_canvas_dimensions_fail_instead_of_clipping_nodes() -> None:
    expression = SimpleNamespace(
        kind=ModuleExpressionKind.KO,
        value="K00001",
        children=(),
        operators=(),
    )
    # A deliberately deep synthetic AST exercises the renderer's independent canvas limit.
    for _ in range(100):
        expression = SimpleNamespace(
            kind=ModuleExpressionKind.GROUP,
            value=None,
            children=(expression,),
            operators=(),
        )
    definition = SimpleNamespace(
        definition=SimpleNamespace(module_id="M00004"),
        parse_result=SimpleNamespace(ast=SimpleNamespace(required_blocks=(expression,))),
    )
    completion = SimpleNamespace(
        evaluation_status=ModuleEvaluationStatus.INCOMPLETE,
        is_complete=False,
        block_coverage=0.0,
    )
    target = SimpleNamespace(
        module_id="M00004",
        module_name=None,
        renderability=RenderabilityStatus.RENDERABLE,
        not_renderable_reason=None,
        definitions=(definition,),
        required_block_states=(),
        optional_component_states=(),
        reference_edges=(),
        completion=completion,
        reference_issues=(),
        warnings=(),
    )
    with pytest.raises(RenderMcpError) as raised:
        construct_module_scene(
            cast(Any, target), analysis_unit=AnalysisUnit.UNKNOWN, max_nodes=1000
        )
    assert raised.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED


def _m00001_shaped_target() -> Any:
    depths = (
        (1, 2)
        + (3,) * 7
        + (1, 2)
        + (3,) * 4
        + (1, 2)
        + (3,) * 5
        + (1, 2)
        + (3,) * 5
        + (1,)
        + (1, 2, 3, 4, 5, 6, 7, 7, 5, 3)
        + (1, 2)
        + (3,) * 4
        + (1, 2, 3, 3)
        + (1, 2, 3, 3)
    )
    blocks: list[SimpleNamespace] = []
    stack: list[SimpleNamespace] = []
    for index, depth in enumerate(depths, start=1):
        node = SimpleNamespace(
            kind=ModuleExpressionKind.KO,
            value=f"K{index:05d}",
            children=[],
            operators=(),
        )
        del stack[depth - 1 :]
        if stack:
            stack[-1].kind = ModuleExpressionKind.GROUP
            stack[-1].value = None
            stack[-1].children.append(node)
        else:
            blocks.append(node)
        stack.append(node)
    definition = SimpleNamespace(
        definition=SimpleNamespace(module_id="M00001"),
        parse_result=SimpleNamespace(ast=SimpleNamespace(required_blocks=blocks)),
    )
    completion = SimpleNamespace(
        evaluation_status=ModuleEvaluationStatus.INCOMPLETE,
        is_complete=False,
        block_coverage=0.0,
    )
    return SimpleNamespace(
        module_id="M00001",
        module_name="Synthetic M00001-shaped MODULE",
        renderability=RenderabilityStatus.RENDERABLE,
        not_renderable_reason=None,
        definitions=(definition,),
        required_block_states=tuple(
            SimpleNamespace(block_index=index, state=ModuleBlockState.INCOMPLETE)
            for index in range(1, 10)
        ),
        optional_component_states=(),
        reference_edges=(),
        completion=completion,
        reference_issues=(),
        warnings=(),
    )
