"""Bounded Pillow raster validation and PNG rendering."""

from __future__ import annotations

import io
import threading
import warnings
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from kegg_render_mcp._presentation import (
    ACCEPTED_COLOR,
    UNSUPPORTED_COLOR,
)
from kegg_render_mcp._presentation import (
    block_color as _block_color,
)
from kegg_render_mcp._presentation import (
    exact_completion_text as _exact,
)
from kegg_render_mcp._presentation import (
    ratio_text as _ratio,
)
from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError
from kegg_render_mcp.module_scene import ModuleScene
from kegg_render_mcp.pathway_scene import PathwayScene
from kegg_render_mcp.svg import wrap_text_rows

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PIL_PIXEL_LIMIT_LOCK = threading.Lock()
_TEXT_ROW_HEIGHT = 16
_MAX_FOOTER_TEXT_ROWS = 4


@dataclass(frozen=True, slots=True)
class PngArtifact:
    content: bytes
    width: int
    height: int


def validate_png(payload: bytes, *, max_bytes: int, max_pixels: int) -> tuple[int, int]:
    if not payload.startswith(PNG_SIGNATURE) or len(payload) > max_bytes:
        raise _asset_error("The pathway image is not a bounded PNG.")
    with _PIL_PIXEL_LIMIT_LOCK:
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(payload)) as image:
                    if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                        raise _asset_error("The pathway image must be one static PNG frame.")
                    width, height = image.size
                    if width <= 0 or height <= 0 or width * height > max_pixels:
                        raise _asset_error("The pathway image exceeds the configured pixel limit.")
                    image.load()
        except RenderMcpError:
            raise
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as error:
            raise _asset_error("The pathway PNG could not be decoded safely.") from error
        finally:
            Image.MAX_IMAGE_PIXELS = previous
    return width, height


def render_pathway_png(
    scene: PathwayScene, *, max_pixels: int, max_output_bytes: int
) -> PngArtifact:
    width, height = scene.width, scene.height
    warning_text = "Warnings: " + " | ".join(scene.warnings)[:1000] if scene.warnings else ""
    warning_rows = wrap_text_rows(
        warning_text,
        width_chars=110,
        max_rows=_MAX_FOOTER_TEXT_ROWS,
    )
    caption_rows = wrap_text_rows(
        scene.caption,
        width_chars=110,
        max_rows=_MAX_FOOTER_TEXT_ROWS,
    )
    warning_block_height = len(warning_rows) * _TEXT_ROW_HEIGHT + 8 if warning_rows else 0
    content_y_offset = 74 + warning_block_height
    required_footer = content_y_offset + 24 + len(caption_rows) * _TEXT_ROW_HEIGHT + 16
    footer = max(220 if warning_rows else 180, required_footer)
    output_width = max(width, 760)
    output_height = height + footer
    if output_width * output_height > max_pixels:
        raise _output_error("The pathway PNG derivative exceeds the configured pixel limit.")
    with Image.open(io.BytesIO(scene.source_png)) as source:
        background = Image.new("RGBA", (output_width, output_height), "white")
        background.alpha_composite(source.convert("RGBA"), (0, 0))
        canvas = background.convert("RGB")
    draw = ImageDraw.Draw(canvas, "RGBA")
    for geometry in scene.overlays:
        fill = _rgba(ACCEPTED_COLOR, 72)
        outline = _rgba(ACCEPTED_COLOR, 255)
        if geometry.kind == "box":
            left = round(geometry.x - geometry.width / 2)
            top = round(geometry.y - geometry.height / 2)
            right = round(geometry.x + geometry.width / 2)
            bottom = round(geometry.y + geometry.height / 2)
            draw.rectangle((left, top, right, bottom), fill=fill, outline=outline, width=4)
        else:
            draw.line(geometry.points, fill=outline, width=7, joint="curve")
    font = ImageFont.load_default()
    y = height + 20
    draw.text((20, y), f"{scene.target_id}: {scene.title}", fill="#1F2937", font=font)
    _draw_evidence_swatch(
        draw,
        (20, y + 24, 42, y + 38),
        ACCEPTED_COLOR,
        geometry_kinds=scene.retained_geometry_kinds,
    )
    draw.text((50, y + 24), "Accepted annotation", fill="#1F2937", font=font)
    content_y = y + 54
    if warning_rows:
        _draw_rows(
            draw,
            warning_rows,
            (20, content_y),
            font=font,
        )
        content_y += warning_block_height
    draw.text(
        (20, content_y),
        "Unmatched graphics are not evidence of biological absence.",
        fill="#1F2937",
        font=font,
    )
    _draw_rows(draw, caption_rows, (20, content_y + 24), font=font)
    return _serialize_png(canvas, max_output_bytes)


def render_module_png(scene: ModuleScene, *, max_pixels: int, max_output_bytes: int) -> PngArtifact:
    if scene.width * scene.height > max_pixels:
        raise _output_error("The MODULE PNG derivative exceeds the configured pixel limit.")
    canvas = Image.new("RGB", (scene.width, scene.height), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text((30, 28), f"{scene.target_id}: {scene.title}", fill="#1F2937", font=font)
    draw.text(
        (30, 52),
        (
            f"Exact={_exact(scene.exact_completion)}; "
            f"block={_ratio(scene.block_coverage)}; {scene.status}"
        ),
        fill="#1F2937",
        font=font,
    )
    positions = {
        node.node_id: (50 + node.depth * 220, scene.node_y + index * 48)
        for index, node in enumerate(scene.nodes)
    }
    for node in scene.nodes:
        if node.parent_id is not None:
            parent_x, parent_y = positions[node.parent_id]
            x, y = positions[node.node_id]
            draw.line((parent_x + 176, parent_y + 20, x, y + 20), fill="#94A3B8", width=2)
    for node in scene.nodes:
        x, y = positions[node.node_id]
        color = UNSUPPORTED_COLOR if node.unsupported else "#6B7280" if node.optional else "#4B5563"
        draw.rounded_rectangle(
            (x, y, x + 176, y + 40), radius=5, fill="white", outline=color, width=3
        )
        draw.text((x + 10, y + 14), node.label[:24], fill="#1F2937", font=font)
    panel_x = 50
    panel_y = 140
    populated_panel = False
    if scene.blocks:
        draw.text((panel_x, panel_y - 11), "Required blocks", fill="#1F2937", font=font)
        panel_y += 26
        for block in scene.blocks:
            color = _block_color(block.state)
            draw.rectangle(
                (panel_x, panel_y, panel_x + 18, panel_y + 18),
                fill=color,
                outline="#374151",
            )
            draw.text(
                (panel_x + 28, panel_y + 3),
                f"{block.block_index}: {block.state}",
                fill="#1F2937",
                font=font,
            )
            panel_y += 28
        populated_panel = True
    if scene.optional_components:
        if populated_panel:
            panel_y += 12
        draw.text(
            (panel_x, panel_y - 11),
            "Optional component states",
            fill="#1F2937",
            font=font,
        )
        panel_y += 26
        for item in scene.optional_components:
            draw.text(
                (panel_x, panel_y + 3),
                f"Optional {item.component_index} ({item.source_module_id}): {item.state}",
                fill="#1F2937",
                font=font,
            )
            panel_y += 28
        populated_panel = True
    if scene.reference_edges:
        if populated_panel:
            panel_y += 12
        draw.text(
            (panel_x, panel_y - 11),
            "Resolved MODULE references",
            fill="#1F2937",
            font=font,
        )
        panel_y += 26
        for edge in scene.reference_edges:
            draw.text(
                (panel_x, panel_y + 3),
                f"{edge.source_module_id} -> {edge.target_module_id}",
                fill="#1F2937",
                font=font,
            )
            panel_y += 28
    caption_y = scene.height - 110
    if scene.warnings:
        _draw_wrapped(
            draw,
            "Warnings: " + " | ".join(scene.warnings)[:1000],
            (30, caption_y - 24),
            width=130,
            font=font,
        )
    _draw_wrapped(draw, scene.caption, (30, caption_y), width=130, font=font)
    return _serialize_png(canvas, max_output_bytes)


def _serialize_png(image: Image.Image, max_bytes: int) -> PngArtifact:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    content = buffer.getvalue()
    if len(content) > max_bytes:
        raise _output_error("The PNG derivative exceeds the configured byte limit.")
    return PngArtifact(content=content, width=image.width, height=image.height)


def _draw_evidence_swatch(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: str,
    *,
    geometry_kinds: frozenset[str],
) -> None:
    rgba = _rgba(color, 255)
    if "box" in geometry_kinds or not geometry_kinds:
        draw.rectangle(box, fill=_rgba(color, 72))
        draw.rectangle(box, outline=rgba, width=2)
    if "polyline" in geometry_kinds:
        left, top, right, bottom = box
        points = ((float(left), (top + bottom) / 2), (float(right), (top + bottom) / 2))
        draw.line(points, fill=rgba, width=4)


def _rgba(hex_color: str, alpha: int) -> tuple[int, int, int, int]:
    value = hex_color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16), alpha


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    value: str,
    origin: tuple[int, int],
    *,
    width: int,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    _draw_rows(
        draw,
        wrap_text_rows(value, width_chars=width, max_rows=_MAX_FOOTER_TEXT_ROWS),
        origin,
        font=font,
    )


def _draw_rows(
    draw: ImageDraw.ImageDraw,
    rows: tuple[str, ...],
    origin: tuple[int, int],
    *,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    for index, row in enumerate(rows):
        draw.text(
            (origin[0], origin[1] + index * _TEXT_ROW_HEIGHT),
            row,
            fill="#1F2937",
            font=font,
        )


def _asset_error(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.ASSET_INVALID,
            message=message,
            suggested_action="Refresh the matching pathway PNG or select another target.",
        )
    )


def _output_error(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            message=message,
            suggested_action="Use SVG only or select a smaller render target.",
        )
    )
