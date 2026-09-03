"""Bounded Pillow raster validation and PNG rendering."""

from __future__ import annotations

import io
import threading
import warnings
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from kegg_render_mcp._presentation import (
    ACCEPTED_COLOR,
    LEGEND_FONT_SIZE,
    LEGEND_ROW_HEIGHT,
    pathway_footer_height,
)
from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError
from kegg_render_mcp.pathway_scene import PathwayScene

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PIL_PIXEL_LIMIT_LOCK = threading.Lock()


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
    footer = pathway_footer_height(has_annotation_credit=scene.annotation_credit is not None)
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
    detail_font = ImageFont.load_default(size=LEGEND_FONT_SIZE)
    y = height + 12
    draw.text((20, y), scene.headline, fill="#1F2937", font=detail_font)
    # A one-pixel horizontal overdraw supplies a light bold weight without bundling a font.
    draw.text((21, y), scene.headline, fill="#1F2937", font=detail_font)
    detail_y = y + LEGEND_ROW_HEIGHT
    draw.text((20, detail_y), scene.coverage_summary, fill="#1F2937", font=detail_font)
    legend_y = detail_y + LEGEND_ROW_HEIGHT
    if scene.annotation_credit is not None:
        draw.text((20, legend_y), scene.annotation_credit, fill="#1F2937", font=detail_font)
        legend_y += LEGEND_ROW_HEIGHT
    _draw_evidence_swatch(
        draw,
        (20, legend_y, 42, legend_y + 14),
        ACCEPTED_COLOR,
        geometry_kinds=scene.retained_geometry_kinds,
    )
    draw.text(
        (50, legend_y - 2),
        "Accepted annotation",
        fill="#1F2937",
        font=detail_font,
    )
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
