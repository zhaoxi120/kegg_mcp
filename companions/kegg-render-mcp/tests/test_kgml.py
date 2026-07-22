"""Synthetic KGML identity, structure, and coordinate tests."""

from __future__ import annotations

import pytest

from conftest import KGML_DOCTYPE, synthetic_kgml
from kegg_render_mcp.config import RendererLimits
from kegg_render_mcp.contracts import ErrorCode, RenderMcpError
from kegg_render_mcp.kgml import parse_kgml, validate_graphic_bounds


def test_parse_kgml_preserves_multi_ko_graphics_in_deterministic_order() -> None:
    document = parse_kgml(synthetic_kgml(), "ko00010", RendererLimits())
    assert document.pathway_id == "ko00010"
    assert document.parser_version == "1.1"
    assert document.graphics[0].ko_ids == ("K00001", "K00002")
    assert tuple(item.entry_id for item in document.graphics) == (1, 2)


def test_parse_kgml_accepts_payload_without_a_doctype() -> None:
    payload = synthetic_kgml().replace(f"{KGML_DOCTYPE}\n".encode(), b"")

    document = parse_kgml(payload, "ko00010", RendererLimits())

    assert tuple(item.entry_id for item in document.graphics) == (1, 2)


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE pathway [<!ENTITY xxe SYSTEM "file:///etc/passwd">]><pathway/>',
        (b'<!DOCTYPE pathway SYSTEM "file:///tmp/KGML.dtd"><pathway name="path:ko00010"/>'),
        (
            b'<!DOCTYPE pathway SYSTEM "http://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd">'
            b'<pathway name="path:ko00010"/>'
        ),
        (
            b'<!DOCTYPE pathway SYSTEM "https://example.test/KGML_v0.7.2_.dtd">'
            b'<pathway name="path:ko00010"/>'
        ),
        (
            b'<!DOCTYPE pathway SYSTEM "https://www.kegg.jp/kegg/xml/KGML_v0.7.3_.dtd">'
            b'<pathway name="path:ko00010"/>'
        ),
        (
            b'<!DOCTYPE pathway PUBLIC "-//KEGG//KGML" '
            b'"https://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd">'
            b'<pathway name="path:ko00010"/>'
        ),
        (
            b'<!DOCTYPE pathway SYSTEM "https://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd" '
            b'[<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b'<pathway name="path:ko00010"/>'
        ),
        (KGML_DOCTYPE + KGML_DOCTYPE + '<pathway name="path:ko00010"/>').encode(),
        ('<pathway name="path:ko00010"/>' + KGML_DOCTYPE).encode(),
        b'<pathway name="path:ko00020"/>',
        b'<pathway name="path:ko00010"><entry id="x" name="ko:K00001"/></pathway>',
        (
            b'<pathway name="path:ko00010"><entry id="1" name="ko:K00001">'
            b'<graphics type="rectangle" x="nan" y="1" width="2" height="2"/>'
            b"</entry></pathway>"
        ),
        (
            b'<pathway name="path:ko00010"><entry id="1" name="ko:K00001">'
            b'<graphics type="rectangle" x="1" y="1" width="-2" height="2"/>'
            b"</entry></pathway>"
        ),
    ],
)
def test_parse_kgml_rejects_entities_identity_and_invalid_coordinates(payload: bytes) -> None:
    with pytest.raises(RenderMcpError) as raised:
        parse_kgml(payload, "ko00010", RendererLimits())
    assert raised.value.detail.code is ErrorCode.ASSET_INVALID


def test_parse_kgml_enforces_nesting_limit() -> None:
    payload = b'<pathway name="path:ko00010">' + b"<x>" * 20 + b"</x>" * 20 + b"</pathway>"
    with pytest.raises(RenderMcpError, match="structure"):
        parse_kgml(payload, "ko00010", RendererLimits(max_xml_depth=8))


def test_parse_kgml_aborts_during_event_stream_at_element_limit() -> None:
    payload = b'<pathway name="path:ko00010">' + b'<entry id="1" name="none"/>' * 20 + b"</pathway>"
    with pytest.raises(RenderMcpError, match="structure"):
        parse_kgml(payload, "ko00010", RendererLimits(max_xml_elements=5))


def test_graphics_must_fit_matching_png_dimensions() -> None:
    document = parse_kgml(synthetic_kgml(), "ko00010", RendererLimits())
    validate_graphic_bounds(document, 240, 140)
    with pytest.raises(RenderMcpError, match="dimensions"):
        validate_graphic_bounds(document, 100, 60)
