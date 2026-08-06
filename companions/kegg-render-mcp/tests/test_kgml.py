"""Synthetic KGML identity, structure, and coordinate tests."""

from __future__ import annotations

import pytest

from conftest import KGML_DOCTYPE, synthetic_kgml
from kegg_render_mcp.config import RendererLimits
from kegg_render_mcp.contracts import ErrorCode, RenderMcpError
from kegg_render_mcp.kgml import KGML_PARSER_VERSION, parse_kgml, validate_graphic_bounds


def test_parse_kgml_preserves_multi_ko_graphics_in_deterministic_order() -> None:
    document = parse_kgml(synthetic_kgml(), "ko00010", RendererLimits())
    assert KGML_PARSER_VERSION == "1.3"
    assert tuple(item.kind for item in document.graphics) == ("box", "box")
    assert document.graphics[0].ko_ids == ("K00001", "K00002")
    assert tuple(item.entry_id for item in document.graphics) == (1, 2)


def test_parse_kgml_preserves_bounded_polyline_geometry() -> None:
    document = parse_kgml(
        _line_kgml("10,20,30,40,50,60"),
        "ko01100",
        RendererLimits(),
    )

    graphic = document.graphics[0]
    assert graphic.kind == "polyline"
    assert graphic.ko_ids == ("K00001", "K00002")
    assert graphic.points == ((10.0, 20.0), (30.0, 40.0), (50.0, 60.0))


@pytest.mark.parametrize(
    "coords",
    [
        "",
        "1,2",
        "1,2,3",
        "1,2,3,",
        "1,2,3,not-a-number",
        "1,2,3,nan",
        "1,2,3.0,4",
        "1,2,1e2,4",
        " 1,2,3,4",
        "\uff11,2,3,4",
        "-1,2,3,4",
        "1,2,1000001,4",
        "1,1,1,1",
    ],
)
def test_parse_kgml_rejects_malformed_or_degenerate_polyline(coords: str) -> None:
    with pytest.raises(RenderMcpError) as raised:
        parse_kgml(_line_kgml(coords), "ko01100", RendererLimits())

    assert raised.value.detail.code is ErrorCode.ASSET_INVALID


def test_parse_kgml_enforces_per_line_and_total_polyline_point_limits() -> None:
    with pytest.raises(RenderMcpError, match="point limit"):
        parse_kgml(
            _line_kgml("1,1,2,2,3,3"),
            "ko01100",
            RendererLimits(max_polyline_points=2),
        )


def test_parse_kgml_bounds_coordinate_characters_before_numeric_conversion() -> None:
    with pytest.raises(RenderMcpError, match="coordinate list"):
        parse_kgml(
            _line_kgml("0" * 65 + ",0,1,1"),
            "ko01100",
            RendererLimits(max_polyline_coordinate_characters=64),
        )

    with pytest.raises(RenderMcpError, match="coordinate token"):
        parse_kgml(
            _line_kgml("00000,0,1,1"),
            "ko01100",
            RendererLimits(max_coordinate_token_characters=4),
        )


@pytest.mark.parametrize("coordinate", ["1.0", "1e2", "+1", " 1", "\uff11", "0" * 17])
def test_parse_kgml_rejects_noncanonical_or_long_box_coordinates(coordinate: str) -> None:
    payload = (
        '<pathway name="path:ko00010"><entry id="1" name="ko:K00001">'
        f'<graphics type="rectangle" x="{coordinate}" y="1" width="2" height="2"/>'
        "</entry></pathway>"
    ).encode()

    with pytest.raises(RenderMcpError) as raised:
        parse_kgml(payload, "ko00010", RendererLimits())

    assert raised.value.detail.code is ErrorCode.ASSET_INVALID


def test_parse_kgml_enforces_total_polyline_length_limit() -> None:
    with pytest.raises(RenderMcpError, match="total length limit"):
        parse_kgml(
            _line_kgml("0,0,2,0", "0,0,2,0"),
            "ko01100",
            RendererLimits(max_total_polyline_length=3),
        )


def test_parse_kgml_bounds_ko_entries_and_graphic_associations() -> None:
    with pytest.raises(RenderMcpError, match="entry name"):
        parse_kgml(
            _line_kgml("0,0,1,1", entry_name="ko:K00001 " + "x" * 65),
            "ko01100",
            RendererLimits(max_ko_entry_name_characters=64),
        )

    with pytest.raises(RenderMcpError, match="K-number limit"):
        parse_kgml(
            _line_kgml(
                "0,0,1,1",
                entry_name="ko:K00001 ko:K00002 ko:K00003",
            ),
            "ko01100",
            RendererLimits(max_ko_ids_per_entry=2),
        )

    with pytest.raises(RenderMcpError, match="association limit"):
        parse_kgml(
            _line_kgml("0,0,1,1", "1,1,2,2"),
            "ko01100",
            RendererLimits(max_graphic_ko_associations=3),
        )

    with pytest.raises(RenderMcpError, match="total point limit"):
        parse_kgml(
            _line_kgml("1,1,2,2", "3,3,4,4"),
            "ko01100",
            RendererLimits(max_polyline_points=2, max_total_polyline_points=3),
        )


def test_polyline_limit_configuration_has_hard_upper_bounds() -> None:
    with pytest.raises(ValueError):
        RendererLimits(max_polyline_points=4_097)
    with pytest.raises(ValueError):
        RendererLimits(max_total_polyline_points=500_001)
    with pytest.raises(ValueError):
        RendererLimits(max_polyline_coordinate_characters=131_073)
    with pytest.raises(ValueError):
        RendererLimits(max_total_polyline_length=50_000_001)
    with pytest.raises(ValueError, match="must not exceed"):
        RendererLimits(max_polyline_points=4, max_total_polyline_points=3)

    defaults = RendererLimits()
    assert defaults.max_xml_elements == 50_000
    assert defaults.max_xml_attributes == 250_000


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


def test_polyline_points_must_fit_matching_png_dimensions() -> None:
    document = parse_kgml(_line_kgml("0,0,240,140"), "ko01100", RendererLimits())
    validate_graphic_bounds(document, 240, 140)

    with pytest.raises(RenderMcpError, match="dimensions"):
        validate_graphic_bounds(document, 239, 140)


def _line_kgml(
    *coords: str,
    entry_name: str = "ko:K00001 ko:K00002",
) -> bytes:
    graphics = "".join(
        f'<entry id="{index}" name="{entry_name}" type="gene">'
        f'<graphics name="K00001" type="line" coords="{value}"/>'
        "</entry>"
        for index, value in enumerate(coords, start=1)
    )
    return f'<pathway name="path:ko01100" title="Synthetic overview">{graphics}</pathway>'.encode()
