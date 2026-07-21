"""Bounded event-driven KGML parser for regular pathway graphics."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from xml.parsers import expat

from kegg_render_mcp.config import RendererLimits
from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError

_KO_RE = re.compile(r"ko:(K[0-9]{5})\Z")
_PATH_RE = re.compile(r"(?:path:)?(ko[0-9]{5})\Z")
_SUPPORTED_KGML_DTD_SYSTEM_ID = "https://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd"


@dataclass(frozen=True, slots=True)
class KgmlGraphic:
    entry_id: int
    ko_ids: tuple[str, ...]
    graphic_type: str
    name: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class KgmlDocument:
    pathway_id: str
    title: str
    graphics: tuple[KgmlGraphic, ...]
    parser_name: str = "kegg_render_safe_kgml"
    parser_version: str = "1.1"


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
        self.text_bytes = 0
        self.depth = 0
        self.root_seen = False
        self.root_closed = False
        self.title = ""
        self.entry: _EntryContext | None = None
        self.graphics: list[KgmlGraphic] = []
        self.doctype_seen = False

    def start(self, name: str, attributes: dict[str, str]) -> None:
        self.depth += 1
        self.element_count += 1
        self.attribute_count += len(attributes)
        self.text_bytes += sum(
            len(key.encode("utf-8")) + len(value.encode("utf-8"))
            for key, value in attributes.items()
        )
        self._check_limits()
        if self.root_closed:
            raise _KgmlAbort("KGML contains content after the root pathway element.")
        if not self.root_seen:
            self.root_seen = True
            if name != "pathway":
                raise _KgmlAbort("KGML root must be a pathway element.")
            match = _PATH_RE.fullmatch(attributes.get("name", ""))
            if match is None or match.group(1) != self.expected_pathway_id:
                raise _KgmlAbort("KGML pathway identity does not match the requested target.")
            self.title = attributes.get("title", "")[:1000]
            return
        if name == "entry":
            if self.entry is not None:
                raise _KgmlAbort("KGML must not contain nested entry elements.")
            ko_ids = tuple(
                sorted(
                    {
                        match.group(1)
                        for token in attributes.get("name", "").split()
                        if (match := _KO_RE.fullmatch(token)) is not None
                    }
                )
            )
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
            if graphic_type not in {"rectangle", "roundrectangle"}:
                return
            x = _coordinate(attributes, "x")
            y = _coordinate(attributes, "y")
            width = _coordinate(attributes, "width")
            height = _coordinate(attributes, "height")
            if width <= 0 or height <= 0 or x < 0 or y < 0:
                raise _KgmlAbort("KGML graphics require finite non-negative coordinates.")
            self.graphics.append(
                KgmlGraphic(
                    entry_id=self.entry.entry_id,
                    ko_ids=self.entry.ko_ids,
                    graphic_type=graphic_type,
                    name=attributes.get("name", "")[:256],
                    x=x,
                    y=y,
                    width=width,
                    height=height,
                )
            )

    def end(self, name: str) -> None:
        if name == "entry" and self.entry is not None and self.entry.depth == self.depth:
            self.entry = None
        if name == "pathway" and self.depth == 1:
            self.root_closed = True
        self.depth -= 1
        if self.depth < 0:
            raise _KgmlAbort("KGML element nesting is invalid.")

    def text(self, value: str) -> None:
        self.text_bytes += len(value.encode("utf-8"))
        self._check_limits()

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
            or self.text_bytes > self.limits.max_asset_bytes
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
    parser.CharacterDataHandler = state.text
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
    if not state.root_seen or not state.root_closed or state.depth != 0:
        raise _asset_error("KGML does not contain one complete pathway root.")
    state.graphics.sort(key=lambda item: (item.entry_id, item.x, item.y, item.ko_ids))
    return KgmlDocument(
        pathway_id=expected_pathway_id,
        title=state.title,
        graphics=tuple(state.graphics),
    )


def validate_graphic_bounds(document: KgmlDocument, width: int, height: int) -> None:
    for graphic in document.graphics:
        left = graphic.x - graphic.width / 2
        right = graphic.x + graphic.width / 2
        top = graphic.y - graphic.height / 2
        bottom = graphic.y + graphic.height / 2
        if left < 0 or top < 0 or right > width or bottom > height:
            raise _asset_error("KGML graphics exceed the matching PNG dimensions.")


def _coordinate(attributes: dict[str, str], name: str) -> float:
    try:
        value = float(attributes.get(name, ""))
    except ValueError as error:
        raise _KgmlAbort("KGML contains a non-numeric coordinate.") from error
    if not math.isfinite(value) or value > 1_000_000:
        raise _KgmlAbort("KGML contains an excessive or non-finite coordinate.")
    return value


def _asset_error(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.ASSET_INVALID,
            message=message,
            suggested_action="Refresh the matching single-pathway asset or select another target.",
        )
    )
