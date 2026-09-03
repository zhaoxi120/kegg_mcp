"""Deterministic static SVG serialization without active or external content."""

from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from xml.etree import ElementTree

from kegg_render_mcp._presentation import (
    ACCEPTED_COLOR,
    LEGEND_FONT_SIZE,
    LEGEND_ROW_HEIGHT,
    pathway_footer_height,
)
from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError
from kegg_render_mcp.pathway_scene import PathwayScene

TEXT_COLOR = "#1F2937"
BACKGROUND_COLOR = "#FFFFFF"


@dataclass(frozen=True, slots=True)
class SvgArtifact:
    content: bytes
    width: int
    height: int


def render_pathway_svg(scene: PathwayScene, *, max_bytes: int, max_nodes: int) -> SvgArtifact:
    footer = pathway_footer_height(has_annotation_credit=scene.annotation_credit is not None)
    width = max(scene.width, 760)
    height = scene.height + footer
    encoded = base64.b64encode(scene.source_png).decode("ascii")
    lines = [
        _header(width, height, f"KEGG annotation evidence for {scene.target_id}"),
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND_COLOR}"/>',
        (
            f'<image x="0" y="0" width="{scene.width}" height="{scene.height}" '
            f'href="data:image/png;base64,{encoded}"/>'
        ),
    ]
    for geometry in scene.overlays:
        if geometry.kind == "box":
            left = geometry.x - geometry.width / 2
            top = geometry.y - geometry.height / 2
            lines.append(
                f'<rect x="{left:.2f}" y="{top:.2f}" width="{geometry.width:.2f}" '
                f'height="{geometry.height:.2f}" rx="3" fill="{ACCEPTED_COLOR}" '
                f'fill-opacity="0.28" stroke="{ACCEPTED_COLOR}" stroke-width="4"/>'
            )
        else:
            lines.append(
                f'<path d="{_polyline_path(geometry.points)}" fill="none" '
                f'stroke="{ACCEPTED_COLOR}" stroke-width="7" stroke-linecap="round" '
                'stroke-linejoin="round"/>'
            )
    headline_y = scene.height + 24
    lines.append(_text(24, headline_y, scene.headline, LEGEND_FONT_SIZE, bold=True))
    detail_y = headline_y + LEGEND_ROW_HEIGHT
    lines.append(_text(24, detail_y, scene.coverage_summary, LEGEND_FONT_SIZE))
    legend_y = detail_y + LEGEND_ROW_HEIGHT
    if scene.annotation_credit is not None:
        lines.append(_text(24, legend_y, scene.annotation_credit, LEGEND_FONT_SIZE))
        legend_y += LEGEND_ROW_HEIGHT
    lines.extend(
        [
            _legend(
                24,
                legend_y,
                ACCEPTED_COLOR,
                "Accepted annotation",
                geometry_kinds=scene.retained_geometry_kinds,
            ),
            "</svg>",
        ]
    )
    content = "".join(lines).encode("utf-8")
    _validate_static_svg(content, max_bytes=max_bytes, max_nodes=max_nodes)
    return SvgArtifact(content=content, width=width, height=height)


def _header(width: int, height: int, title: str) -> str:
    safe_title = _xml_text(title)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="{width}" '
        f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{html.escape(safe_title, quote=True)}">'
        f"<title>{html.escape(safe_title)}</title>"
    )


def _text(x: float, y: float, value: str, size: int, *, bold: bool = False) -> str:
    weight = ' font-weight="700"' if bold else ""
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" fill="{TEXT_COLOR}" font-family="sans-serif" '
        f'font-size="{size}"{weight}>{html.escape(_xml_text(value))}</text>'
    )


def _legend(
    x: int,
    y: int,
    color: str,
    label: str,
    *,
    geometry_kinds: frozenset[str],
) -> str:
    parts: list[str] = []
    if "box" in geometry_kinds or not geometry_kinds:
        parts.append(
            f'<rect x="{x}" y="{y - 16}" width="26" height="18" fill="{color}" '
            f'fill-opacity="0.28" stroke="{color}" stroke-width="3"/>'
        )
    if "polyline" in geometry_kinds:
        parts.append(
            f'<path d="M {x} {y - 7} L {x + 26} {y - 7}" fill="none" stroke="{color}" '
            f'stroke-width="4" stroke-linecap="round"/>'
        )
    parts.append(_text(x + 36, y, label, LEGEND_FONT_SIZE))
    return "".join(parts)


def _polyline_path(points: tuple[tuple[float, float], ...]) -> str:
    first_x, first_y = points[0]
    segments = [f"M {first_x:.2f} {first_y:.2f}"]
    segments.extend(f"L {x:.2f} {y:.2f}" for x, y in points[1:])
    return " ".join(segments)


def _validate_static_svg(content: bytes, *, max_bytes: int, max_nodes: int) -> None:
    if len(content) > max_bytes:
        raise _limit_error("The SVG exceeds the configured serialized-byte limit.")
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as error:
        raise _invariant_error() from error
    if sum(1 for _ in root.iter()) > max_nodes:
        raise _limit_error("The SVG exceeds the configured node limit.")
    allowed_tags = {"svg", "title", "rect", "image", "text", "path"}
    for element in root.iter():
        if element.tag.rpartition("}")[2] not in allowed_tags:
            raise _invariant_error()
        for qualified_name, value in element.attrib.items():
            name = qualified_name.rpartition("}")[2].lower()
            lowered = value.strip().lower()
            if name.startswith("on") or name in {"src", "style"}:
                raise _invariant_error()
            if name == "href" and not lowered.startswith("data:image/png;base64,"):
                raise _invariant_error()


def _xml_text(value: str) -> str:
    return "".join(
        character if _is_xml_character(ord(character)) else "\ufffd" for character in value
    )


def _is_xml_character(codepoint: int) -> bool:
    return (
        codepoint in {0x09, 0x0A, 0x0D}
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _limit_error(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            message=message,
            suggested_action="Select fewer or smaller render targets.",
        )
    )


def _invariant_error() -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.INTERNAL_ERROR,
            message="The renderer rejected an unsafe generated SVG structure.",
            suggested_action="Retry the bounded request and inspect renderer status if it recurs.",
        )
    )
