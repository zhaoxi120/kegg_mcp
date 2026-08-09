"""Deterministic static SVG serialization without active or external content."""

from __future__ import annotations

import base64
import html
from dataclasses import dataclass
from xml.etree import ElementTree

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

TEXT_COLOR = "#1F2937"
BACKGROUND_COLOR = "#FFFFFF"


@dataclass(frozen=True, slots=True)
class SvgArtifact:
    content: bytes
    width: int
    height: int


def render_pathway_svg(scene: PathwayScene, *, max_bytes: int, max_nodes: int) -> SvgArtifact:
    warning_text = "Warnings: " + " | ".join(scene.warnings)[:1000] if scene.warnings else ""
    warning_rows = wrap_text_rows(warning_text, width_chars=100, max_rows=5)
    caption_rows = wrap_text_rows(scene.caption, width_chars=100, max_rows=5)
    warning_block_height = len(warning_rows) * 18 + 12 if warning_rows else 0
    content_y_offset = 98 + warning_block_height
    required_footer = content_y_offset + 30 + len(caption_rows) * 18 + 20
    footer = max(225 if warning_rows else 190, required_footer)
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
    footer_y = scene.height + 28
    lines.extend(
        [
            _text(24, footer_y, f"{scene.target_id}: {scene.title}", 19, bold=True),
            _legend(
                24,
                footer_y + 34,
                ACCEPTED_COLOR,
                "Accepted annotation",
                geometry_kinds=scene.retained_geometry_kinds,
            ),
        ]
    )
    content_y = footer_y + 70
    if warning_rows:
        lines.extend(_positioned_text(warning_rows, x=24, y=content_y, size=13))
        content_y += warning_block_height
    lines.extend(
        [
            _text(
                24,
                content_y,
                "Unmatched graphics remain unchanged; they are not evidence of biological absence.",
                14,
            ),
            *_wrapped_text(scene.caption, x=24, y=content_y + 30, width_chars=100, size=13),
            "</svg>",
        ]
    )
    content = "".join(lines).encode("utf-8")
    _validate_static_svg(content, max_bytes=max_bytes, max_nodes=max_nodes)
    return SvgArtifact(content=content, width=width, height=height)


def render_module_svg(scene: ModuleScene, *, max_bytes: int, max_nodes: int) -> SvgArtifact:
    estimated_nodes = (
        24
        + len(scene.nodes) * 4
        + len(scene.blocks) * 4
        + len(scene.optional_components) * 2
        + len(scene.reference_edges) * 2
    )
    if estimated_nodes > max_nodes:
        raise _limit_error("The MODULE SVG exceeds the configured node limit.")
    positions = {
        node.node_id: (50 + node.depth * 220, 190 + index * 58)
        for index, node in enumerate(scene.nodes)
    }
    lines = [
        _header(scene.width, scene.height, f"MODULE annotation evidence for {scene.target_id}"),
        f'<rect width="{scene.width}" height="{scene.height}" fill="{BACKGROUND_COLOR}"/>',
        _text(30, 38, f"{scene.target_id}: {scene.title}", 22, bold=True),
        _text(
            30,
            70,
            f"Exact: {_exact(scene.exact_completion)} | "
            f"Block coverage: {_ratio(scene.block_coverage)} | Status: {scene.status}",
            15,
        ),
        _text(
            30,
            100,
            (
                "AND/space/+ = all required; OR/, = alternatives; "
                "OPTIONAL/- = excluded from denominator."
            ),
            14,
        ),
    ]
    for node in scene.nodes:
        if node.parent_id is None:
            continue
        parent_x, parent_y = positions[node.parent_id]
        x, y = positions[node.node_id]
        lines.append(
            f'<path d="M {parent_x + 176} {parent_y + 20} L {x} {y + 20}" '
            'fill="none" stroke="#94A3B8" stroke-width="2"/>'
        )
    for node in scene.nodes:
        x, y = positions[node.node_id]
        color = UNSUPPORTED_COLOR if node.unsupported else "#6B7280" if node.optional else "#4B5563"
        dash = ' stroke-dasharray="7 4"' if node.optional or node.unsupported else ""
        lines.extend(
            [
                f'<rect x="{x}" y="{y}" width="176" height="40" rx="6" fill="#FFFFFF" '
                f'stroke="{color}" stroke-width="3"{dash}/>',
                _text(x + 10, y + 25, _truncate(node.label, 24), 13, bold=node.depth == 0),
            ]
        )
    panel_x = max((x for x, _ in positions.values()), default=50) + 230
    lines.append(_text(panel_x, 164, "Required block states", 17, bold=True))
    for index, block in enumerate(scene.blocks):
        y = 190 + index * 34
        color = _block_color(block.state)
        lines.extend(
            [
                f'<rect x="{panel_x}" y="{y}" width="18" height="18" fill="{color}" '
                'stroke="#374151" stroke-width="1"/>',
                _text(
                    panel_x + 28,
                    y + 15,
                    f"Block {block.block_index}: {block.state}",
                    12,
                ),
            ]
        )
    panel_y = 190 + len(scene.blocks) * 34
    if scene.optional_components:
        panel_y += 18
        lines.append(_text(panel_x, panel_y, "Optional component states", 17, bold=True))
        panel_y += 26
        for item in scene.optional_components:
            lines.append(
                _text(
                    panel_x,
                    panel_y,
                    f"Optional {item.component_index} ({item.source_module_id}): {item.state}",
                    12,
                )
            )
            panel_y += 28
    if scene.reference_edges:
        panel_y += 12
        lines.append(_text(panel_x, panel_y, "Resolved MODULE references", 17, bold=True))
        panel_y += 26
        for edge in scene.reference_edges:
            lines.append(
                _text(
                    panel_x,
                    panel_y,
                    f"{edge.source_module_id} -> {edge.target_module_id}",
                    12,
                )
            )
            panel_y += 28
    caption_y = min(scene.height - 80, max(240, 220 + len(scene.nodes) * 58))
    if scene.warnings:
        lines.append(
            _text(30, caption_y - 24, "Warnings: " + " | ".join(scene.warnings)[:1000], 12)
        )
    lines.extend(_wrapped_text(scene.caption, x=30, y=caption_y, width_chars=115, size=13))
    lines.append("</svg>")
    content = "".join(lines).encode("utf-8")
    _validate_static_svg(content, max_bytes=max_bytes, max_nodes=max_nodes)
    return SvgArtifact(content=content, width=scene.width, height=scene.height)


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
    parts.append(_text(x + 36, y, label, 13))
    return "".join(parts)


def _polyline_path(points: tuple[tuple[float, float], ...]) -> str:
    first_x, first_y = points[0]
    segments = [f"M {first_x:.2f} {first_y:.2f}"]
    segments.extend(f"L {x:.2f} {y:.2f}" for x, y in points[1:])
    return " ".join(segments)


def _wrapped_text(value: str, *, x: int, y: int, width_chars: int, size: int) -> list[str]:
    return _positioned_text(
        wrap_text_rows(value, width_chars=width_chars, max_rows=5),
        x=x,
        y=y,
        size=size,
    )


def wrap_text_rows(value: str, *, width_chars: int, max_rows: int) -> tuple[str, ...]:
    words = value.split()
    rows: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join((*current, word))
        if len(candidate) > width_chars and current:
            rows.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        rows.append(" ".join(current))
    return tuple(rows[:max_rows])


def _positioned_text(rows: tuple[str, ...], *, x: int, y: int, size: int) -> list[str]:
    return [_text(x, y + index * (size + 5), row, size) for index, row in enumerate(rows)]


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


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1] + "…"


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
