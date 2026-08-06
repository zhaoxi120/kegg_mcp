"""Bounded Pillow raster validation and PNG rendering."""

from __future__ import annotations

import io
import math
import threading
import warnings
from dataclasses import dataclass
from itertools import pairwise

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from kegg_render_mcp._presentation import (
    ACCEPTED_COLOR,
    UNCERTAIN_COLOR,
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
_UNCERTAIN_DASH_LENGTH = 8.0
_UNCERTAIN_DASH_PERIOD = 12.0


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
    for overlay in scene.overlays:
        geometry = overlay.geometry
        color = ACCEPTED_COLOR if overlay.state == "accepted" else UNCERTAIN_COLOR
        fill = _rgba(color, 72)
        outline = _rgba(color, 255)
        if geometry.kind == "box":
            left = round(geometry.x - geometry.width / 2)
            top = round(geometry.y - geometry.height / 2)
            right = round(geometry.x + geometry.width / 2)
            bottom = round(geometry.y + geometry.height / 2)
            if overlay.state == "uncertain":
                draw.rectangle((left, top, right, bottom), fill=fill)
                _dashed_rectangle(draw, (left, top, right, bottom), outline)
            else:
                draw.rectangle((left, top, right, bottom), fill=fill, outline=outline, width=4)
        elif overlay.state == "accepted":
            draw.line(geometry.points, fill=outline, width=7, joint="curve")
        else:
            _dashed_polyline(
                draw,
                geometry.points,
                outline,
                dash_length=_UNCERTAIN_DASH_LENGTH,
                period=_UNCERTAIN_DASH_PERIOD,
                width=7,
            )
    font = ImageFont.load_default()
    y = height + 20
    draw.text((20, y), f"{scene.target_id}: {scene.title}", fill="#1F2937", font=font)
    _draw_evidence_swatch(
        draw,
        (20, y + 24, 42, y + 38),
        ACCEPTED_COLOR,
        uncertain=False,
        geometry_kinds=scene.retained_geometry_kinds,
    )
    draw.text((50, y + 24), "Accepted annotation", fill="#1F2937", font=font)
    _draw_evidence_swatch(
        draw,
        (210, y + 24, 232, y + 38),
        UNCERTAIN_COLOR,
        uncertain=True,
        geometry_kinds=scene.retained_geometry_kinds,
    )
    draw.text((240, y + 24), "Policy-defined uncertain annotation", fill="#1F2937", font=font)
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
            f"Strict exact={_exact(scene.strict_exact_completion)}; "
            f"block={_ratio(scene.strict_block_coverage)}; {scene.strict_status}"
        ),
        fill="#1F2937",
        font=font,
    )
    draw.text(
        (30, 72),
        (
            f"Lenient exact={_exact(scene.lenient_exact_completion)}; "
            f"block={_ratio(scene.lenient_block_coverage)}; {scene.lenient_status}"
        ),
        fill="#1F2937",
        font=font,
    )
    positions = {
        node.node_id: (40 + node.depth * 205, 130 + index * 48)
        for index, node in enumerate(scene.nodes)
    }
    for node in scene.nodes:
        if node.parent_id is not None:
            parent_x, parent_y = positions[node.parent_id]
            x, y = positions[node.node_id]
            draw.line((parent_x + 160, parent_y + 15, x, y + 15), fill="#94A3B8", width=2)
    for node in scene.nodes:
        x, y = positions[node.node_id]
        color = UNSUPPORTED_COLOR if node.unsupported else "#6B7280" if node.optional else "#4B5563"
        draw.rounded_rectangle(
            (x, y, x + 160, y + 30), radius=5, fill="white", outline=color, width=3
        )
        draw.text((x + 7, y + 9), node.label[:24], fill="#1F2937", font=font)
    panel_x = max((x for x, _ in positions.values()), default=40) + 190
    draw.text((panel_x, 112), "Required blocks", fill="#1F2937", font=font)
    for index, block in enumerate(scene.blocks):
        y = 132 + index * 28
        color = _block_color(block.strict_state, block.lenient_state)
        draw.rectangle((panel_x, y, panel_x + 15, y + 15), fill=color, outline="#374151")
        draw.text(
            (panel_x + 22, y + 3),
            f"{block.block_index}: {block.strict_state}/{block.lenient_state}",
            fill="#1F2937",
            font=font,
        )
    panel_y = 132 + len(scene.blocks) * 28
    if scene.optional_components:
        panel_y += 14
        draw.text((panel_x, panel_y), "Optional component states", fill="#1F2937", font=font)
        panel_y += 20
        for item in scene.optional_components:
            draw.text(
                (panel_x, panel_y),
                f"Optional {item.component_index} ({item.source_module_id}): "
                f"{item.strict_state}/{item.lenient_state}",
                fill="#1F2937",
                font=font,
            )
            panel_y += 22
    if scene.reference_edges:
        panel_y += 10
        draw.text((panel_x, panel_y), "Resolved MODULE references", fill="#1F2937", font=font)
        panel_y += 20
        for edge in scene.reference_edges:
            draw.text(
                (panel_x, panel_y),
                f"{edge.source_module_id} -> {edge.target_module_id}",
                fill="#1F2937",
                font=font,
            )
            panel_y += 22
    if scene.warnings:
        _draw_wrapped(
            draw,
            "Warnings: " + " | ".join(scene.warnings)[:1000],
            (30, scene.height - 104),
            width=130,
            font=font,
        )
    _draw_wrapped(draw, scene.caption, (30, scene.height - 64), width=130, font=font)
    return _serialize_png(canvas, max_output_bytes)


def _serialize_png(image: Image.Image, max_bytes: int) -> PngArtifact:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=False, compress_level=9)
    content = buffer.getvalue()
    if len(content) > max_bytes:
        raise _output_error("The PNG derivative exceeds the configured byte limit.")
    return PngArtifact(content=content, width=image.width, height=image.height)


def _dashed_rectangle(
    draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], color: tuple[int, int, int, int]
) -> None:
    left, top, right, bottom = box
    for start in range(left, right, 10):
        draw.line((start, top, min(start + 5, right), top), fill=color, width=3)
        draw.line((start, bottom, min(start + 5, right), bottom), fill=color, width=3)
    for start in range(top, bottom, 10):
        draw.line((left, start, left, min(start + 5, bottom)), fill=color, width=3)
        draw.line((right, start, right, min(start + 5, bottom)), fill=color, width=3)


def _dashed_polyline(
    draw: ImageDraw.ImageDraw,
    points: tuple[tuple[float, float], ...],
    color: tuple[int, int, int, int],
    *,
    dash_length: float,
    period: float,
    width: int,
) -> None:
    for (start_x, start_y), (end_x, end_y) in pairwise(points):
        distance = math.hypot(end_x - start_x, end_y - start_y)
        offset = 0.0
        while offset < distance:
            stop = min(offset + dash_length, distance)
            start_fraction = offset / distance
            stop_fraction = stop / distance
            draw.line(
                (
                    start_x + (end_x - start_x) * start_fraction,
                    start_y + (end_y - start_y) * start_fraction,
                    start_x + (end_x - start_x) * stop_fraction,
                    start_y + (end_y - start_y) * stop_fraction,
                ),
                fill=color,
                width=width,
            )
            offset += period


def _draw_evidence_swatch(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: str,
    *,
    uncertain: bool,
    geometry_kinds: frozenset[str],
) -> None:
    rgba = _rgba(color, 255)
    if "box" in geometry_kinds or not geometry_kinds:
        draw.rectangle(box, fill=_rgba(color, 72))
        if uncertain:
            _dashed_rectangle(draw, box, rgba)
        else:
            draw.rectangle(box, outline=rgba, width=2)
    if "polyline" in geometry_kinds:
        left, top, right, bottom = box
        points = ((float(left), (top + bottom) / 2), (float(right), (top + bottom) / 2))
        if uncertain:
            _dashed_polyline(
                draw,
                points,
                rgba,
                dash_length=_UNCERTAIN_DASH_LENGTH,
                period=_UNCERTAIN_DASH_PERIOD,
                width=4,
            )
        else:
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
