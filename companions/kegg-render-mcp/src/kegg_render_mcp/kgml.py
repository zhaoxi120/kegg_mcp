"""Bounded event-driven KGML parser for box and overview-line graphics."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal
from xml.parsers import expat

from kegg_render_mcp.config import RendererLimits
from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError

_KO_RE = re.compile(r"ko:(K[0-9]{5})\Z")
_PATH_RE = re.compile(r"(?:path:)?(ko[0-9]{5})\Z")
_ASCII_COORDINATE_RE = re.compile(r"[0-9]+\Z", re.ASCII)
_SUPPORTED_KGML_DTD_SYSTEM_ID = "https://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd"
KGML_PARSER_NAME = "kegg_render_safe_kgml"
KGML_PARSER_VERSION = "1.3"


@dataclass(frozen=True, slots=True)
class KgmlBoxGraphic:
    kind: Literal["box"]
    entry_id: int
    ko_ids: tuple[str, ...]
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class KgmlPolylineGraphic:
    kind: Literal["polyline"]
    entry_id: int
    ko_ids: tuple[str, ...]
    points: tuple[tuple[float, float], ...]


KgmlGraphic = KgmlBoxGraphic | KgmlPolylineGraphic


@dataclass(frozen=True, slots=True)
class KgmlDocument:
    graphics: tuple[KgmlGraphic, ...]


class _KgmlAbort(Exception):
    pass


@dataclass(slots=True)
class _EntryContext:
    depth: int
    entry_id: int
    ko_ids: tuple[str, ...]


class _KgmlEventParser:
    def __init__(self, expected_pathway_id: str, limits: RendererLimits) -> None:
        self.expected_pathway_id = expected_pathway_id
        self.limits = limits
        self.element_count = 0
        self.attribute_count = 0
        self.depth = 0
        self.root_seen = False
        self.entry: _EntryContext | None = None
        self.graphics: list[KgmlGraphic] = []
        self.polyline_point_count = 0
        self.polyline_total_length = 0.0
        self.graphic_ko_association_count = 0
        self.doctype_seen = False

    def start(self, name: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        self.element_count += 1
        self.attribute_count += len(attributes)
        self._check_limits()
        if not self.root_seen:
            self.root_seen = True
            if name != "pathway":
                raise _KgmlAbort("KGML root must be a pathway element.")
            match = _PATH_RE.fullmatch(attributes.get("name", ""))
            if match is None or match.group(1) != self.expected_pathway_id:
                raise _KgmlAbort("KGML pathway identity does not match the requested target.")
            return
        if name == "entry":
            if self.entry is not None:
                raise _KgmlAbort("KGML must not contain nested entry elements.")
            raw_entry_name = attributes.get("name", "")
            if len(raw_entry_name) > self.limits.max_ko_entry_name_characters:
                raise _KgmlAbort("A KGML entry name exceeds the configured character limit.")
            ko_ids = tuple(
                sorted(
                    {
                        match.group(1)
                        for token in raw_entry_name.split()
                        if (match := _KO_RE.fullmatch(token)) is not None
                    }
                )
            )
            if len(ko_ids) > self.limits.max_ko_ids_per_entry:
                raise _KgmlAbort("A KGML entry exceeds the configured K-number limit.")
            if not ko_ids:
                self.entry = _EntryContext(self.depth, 0, ())
                return
            try:
                entry_id = int(attributes.get("id", ""))
            except ValueError as error:
                raise _KgmlAbort("A KO-bearing KGML entry has an invalid identifier.") from error
            if entry_id < 0:
                raise _KgmlAbort("A KO-bearing KGML entry has an invalid identifier.")
            self.entry = _EntryContext(self.depth, entry_id, ko_ids)
            return
        if name == "graphics" and self.entry is not None and self.entry.ko_ids:
            graphic_type = attributes.get("type", "")
            if graphic_type not in {"line", "rectangle", "roundrectangle"}:
                return
            self._reserve_graphic_ko_associations()
            if graphic_type == "line":
                self.graphics.append(
                    KgmlPolylineGraphic(
                        kind="polyline",
                        entry_id=self.entry.entry_id,
                        ko_ids=self.entry.ko_ids,
                        points=self._polyline_points(attributes),
                    )
                )
                return
            x = _coordinate(
                attributes, "x", max_characters=self.limits.max_coordinate_token_characters
            )
            y = _coordinate(
                attributes, "y", max_characters=self.limits.max_coordinate_token_characters
            )
            width = _coordinate(
                attributes, "width", max_characters=self.limits.max_coordinate_token_characters
            )
            height = _coordinate(
                attributes, "height", max_characters=self.limits.max_coordinate_token_characters
            )
            if width <= 0 or height <= 0:
                raise _KgmlAbort("KGML graphics require finite non-negative coordinates.")
            self.graphics.append(
                KgmlBoxGraphic(
                    kind="box",
                    entry_id=self.entry.entry_id,
                    ko_ids=self.entry.ko_ids,
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                )
            )

    def _polyline_points(self, attributes: dict[str, str]) -> tuple[tuple[float, float], ...]:
        raw = attributes.get("coords", "")
        if len(raw) > self.limits.max_polyline_coordinate_characters:
            raise _KgmlAbort("A KGML line coordinate list exceeds the configured character limit.")
        coordinate_count = raw.count(",") + 1
        if coordinate_count < 4 or coordinate_count % 2 != 0:
            raise _KgmlAbort("KGML line graphics require an even coordinate list of two points.")
        point_count = coordinate_count // 2
        if point_count > self.limits.max_polyline_points:
            raise _KgmlAbort("A KGML line graphic exceeds the configured point limit.")
        if self.polyline_point_count + point_count > self.limits.max_total_polyline_points:
            raise _KgmlAbort("KGML line graphics exceed the configured total point limit.")
        values = tuple(
            _coordinate_text(
                token,
                max_characters=self.limits.max_coordinate_token_characters,
            )
            for token in raw.split(",")
        )
        points = tuple(zip(values[0::2], values[1::2], strict=True))
        length = sum(
            math.hypot(end_x - start_x, end_y - start_y)
            for (start_x, start_y), (end_x, end_y) in pairwise(points)
        )
        if length <= 0:
            raise _KgmlAbort("KGML line graphics must contain a non-degenerate segment.")
        if self.polyline_total_length + length > self.limits.max_total_polyline_length:
            raise _KgmlAbort("KGML line graphics exceed the configured total length limit.")
        self.polyline_point_count += point_count
        self.polyline_total_length += length
        return points

    def _reserve_graphic_ko_associations(self) -> None:
        if self.entry is None:
            raise _KgmlAbort("KGML graphic association has invalid entry nesting.")
        count = self.graphic_ko_association_count + len(self.entry.ko_ids)
        if count > self.limits.max_graphic_ko_associations:
            raise _KgmlAbort("KGML graphics exceed the configured K-number association limit.")
        self.graphic_ko_association_count = count

    def end(self, name: str) -> None:
        if name == "entry" and self.entry is not None and self.entry.depth == self.depth:
            self.entry = None
        self.depth -= 1

    def start_doctype(
        self,
        name: str,
        system_id: str | None,
        public_id: str | None,
        has_internal_subset: int,
    ) -> None:
        if (
            self.doctype_seen
            or name != "pathway"
            or system_id != _SUPPORTED_KGML_DTD_SYSTEM_ID
            or public_id is not None
            or has_internal_subset != 0
        ):
            raise _KgmlAbort("KGML contains an unsupported DTD declaration.")
        self.doctype_seen = True

    def prohibited_declaration(self, *args: object) -> None:
        del args
        raise _KgmlAbort("KGML entity declarations are prohibited.")

    def external_entity(self, *args: object) -> int:
        del args
        raise _KgmlAbort("KGML external entities are prohibited.")

    def _check_limits(self) -> None:
        if (
            self.element_count > self.limits.max_xml_elements
            or self.attribute_count > self.limits.max_xml_attributes
            or self.depth > self.limits.max_xml_depth
        ):
            raise _KgmlAbort("KGML structure exceeds configured parser limits.")


def parse_kgml(payload: bytes, expected_pathway_id: str, limits: RendererLimits) -> KgmlDocument:
    """Parse KGML incrementally without resolving its inert canonical DTD reference."""
    if not payload or len(payload) > limits.max_asset_bytes:
        raise _asset_error("KGML bytes are empty or exceed the configured asset limit.")
    state = _KgmlEventParser(expected_pathway_id, limits)
    parser = expat.ParserCreate(encoding="UTF-8")
    parser.buffer_text = False
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.StartElementHandler = state.start
    parser.EndElementHandler = state.end
    parser.StartDoctypeDeclHandler = state.start_doctype
    parser.EntityDeclHandler = state.prohibited_declaration
    parser.UnparsedEntityDeclHandler = state.prohibited_declaration
    parser.ExternalEntityRefHandler = state.external_entity
    try:
        for offset in range(0, len(payload), 64 * 1024):
            parser.Parse(payload[offset : offset + 64 * 1024], False)
        parser.Parse(b"", True)
    except (_KgmlAbort, expat.ExpatError, UnicodeError, ValueError) as error:
        message = str(error) if isinstance(error, _KgmlAbort) else "KGML is not safe, bounded XML."
        raise _asset_error(message) from error
    if not state.root_seen or state.depth != 0:
        raise _asset_error("KGML does not contain one complete pathway root.")
    state.graphics.sort(key=_graphic_sort_key)
    return KgmlDocument(
        graphics=tuple(state.graphics),
    )


def validate_graphic_bounds(document: KgmlDocument, width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise _asset_error("Matching PNG dimensions must be positive.")
    for graphic in document.graphics:
        if graphic.kind == "box":
            left = graphic.x - graphic.width / 2
            right = graphic.x + graphic.width / 2
            top = graphic.y - graphic.height / 2
            bottom = graphic.y + graphic.height / 2
            outside = left < 0 or top < 0 or right > width or bottom > height
        else:
            outside = any(x < 0 or y < 0 or x > width or y > height for x, y in graphic.points)
        if outside:
            raise _asset_error("KGML graphics exceed the matching PNG dimensions.")


def _coordinate(attributes: dict[str, str], name: str, *, max_characters: int) -> float:
    return _coordinate_text(attributes.get(name, ""), max_characters=max_characters)


def _coordinate_text(raw: str, *, max_characters: int) -> float:
    if len(raw) > max_characters:
        raise _KgmlAbort("KGML contains an excessive coordinate token.")
    if _ASCII_COORDINATE_RE.fullmatch(raw) is None:
        raise _KgmlAbort("KGML coordinates must be ASCII non-negative integers.")
    value = int(raw)
    if value > 1_000_000:
        raise _KgmlAbort("KGML contains an excessive coordinate.")
    return float(value)


def _graphic_sort_key(
    graphic: KgmlGraphic,
) -> tuple[int, int, float, float, tuple[str, ...]]:
    if graphic.kind == "box":
        return (graphic.entry_id, 0, graphic.x, graphic.y, graphic.ko_ids)
    first_x, first_y = graphic.points[0]
    return (graphic.entry_id, 1, first_x, first_y, graphic.ko_ids)


def _asset_error(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.ASSET_INVALID,
            message=message,
            suggested_action="Refresh the matching single-pathway asset or select another target.",
        )
    )
