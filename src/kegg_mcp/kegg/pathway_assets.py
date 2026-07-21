"""Typed, bounded pathway-image and KGML retrieval contracts."""

from __future__ import annotations

import re
import struct
import zlib
from enum import StrEnum

from pydantic import ConfigDict, Field, field_validator, model_validator

from kegg_mcp.domain.annotations import JSON_SCHEMA_DIALECT, FrozenModel
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggClientLimits,
    KeggOperation,
    is_kegg_pathway_identifier,
)
from kegg_mcp.kegg.operations import PreparedRequest, ResponseParser

PATHWAY_ASSET_PARSER_VERSION = "3"
MAX_PATHWAY_ASSET_BYTES = 50_000_000
MAX_PNG_DIMENSION = 20_000
MAX_PNG_PIXELS = 40_000_000
MAX_PNG_CHUNKS = 100_000
MAX_PNG_DECOMPRESSED_BYTES = 128_000_000

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CHUNK_TYPE = re.compile(rb"^[A-Za-z]{4}$")
_ACTIVE_XML_DECLARATION = re.compile(
    r"<![ \t\r\n]*(?:DOCTYPE|ENTITY)\b",
    re.IGNORECASE,
)
_XML_WHITESPACE = frozenset(" \t\r\n")
_SUPPORTED_KGML_SYSTEM_ID = "https://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd"
_PNG_DECOMPRESSION_CHUNK_BYTES = 64 * 1024
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7_PASSES = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


class PathwayAssetKind(StrEnum):
    """KEGG GET options supported for one pathway asset."""

    IMAGE = "image"
    IMAGE_2X = "image2x"
    KGML = "kgml"


class PathwayAssetRequest(FrozenModel):
    """One validated pathway asset request without an arbitrary URL surface."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-asset-request:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    pathway_id: str = Field(min_length=7, max_length=10)
    kind: PathwayAssetKind

    @field_validator("pathway_id")
    @classmethod
    def validate_pathway_id(cls, value: str) -> str:
        if not is_kegg_pathway_identifier(value):
            raise ValueError("pathway_id must be a canonical KEGG pathway identifier")
        return value


class PathwayAssetResult(FrozenModel):
    """One bounded content-validated pathway asset with retrieval provenance."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-asset-result:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    request: PathwayAssetRequest
    content: bytes = Field(min_length=1, max_length=MAX_PATHWAY_ASSET_BYTES)
    mime_type: str = Field(pattern=r"^(?:image/png|application/xml)$", max_length=100)
    width: int | None = Field(default=None, strict=True, gt=0, le=MAX_PNG_DIMENSION)
    height: int | None = Field(default=None, strict=True, gt=0, le=MAX_PNG_DIMENSION)
    provenance: KeggBatchProvenance

    @model_validator(mode="after")
    def validate_kind_metadata(self) -> PathwayAssetResult:
        is_png = self.request.kind in {PathwayAssetKind.IMAGE, PathwayAssetKind.IMAGE_2X}
        if is_png != (self.mime_type == "image/png"):
            raise ValueError("asset kind and MIME type do not match")
        if is_png != (self.width is not None and self.height is not None):
            raise ValueError("PNG assets require dimensions and KGML assets must omit them")
        if (
            self.width is not None
            and self.height is not None
            and self.width * self.height > MAX_PNG_PIXELS
        ):
            raise ValueError("PNG pixel count exceeds the asset contract")
        return self


def prepare_pathway_asset(
    request: PathwayAssetRequest,
    limits: KeggClientLimits,
) -> PreparedRequest:
    """Prepare one bounded KEGG GET asset request."""
    path = f"/get/{request.pathway_id}/{request.kind.value}"
    if len(path.encode("ascii")) > limits.max_url_bytes:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The prepared pathway asset request exceeds the configured URL-size limit.",
            suggested_action="Use a canonical pathway identifier and the default endpoint path.",
            safe_details=(SafeDetail(name="operation", value=KeggOperation.GET.value),),
        )
    parser = (
        ResponseParser.PATHWAY_KGML_PREFLIGHT
        if request.kind is PathwayAssetKind.KGML
        else ResponseParser.PATHWAY_PNG
    )
    return PreparedRequest(
        operation=KeggOperation.GET,
        path=path,
        normalized_request_key=path,
        parser=parser,
        requested_identifiers=(request.pathway_id,),
    )


def validate_pathway_asset_content(
    request: PathwayAssetRequest,
    content: bytes,
    *,
    content_type: str | None,
) -> tuple[str, int | None, int | None]:
    """Validate one bounded asset and return canonical MIME type and dimensions."""
    if not content or len(content) > MAX_PATHWAY_ASSET_BYTES:
        raise ValueError("pathway asset bytes are outside the supported bounds")
    if request.kind is PathwayAssetKind.KGML:
        _validate_content_type(content_type, allowed=("application/xml", "text/xml", "text/plain"))
        _validate_kgml_preflight(content)
        return "application/xml", None, None
    _validate_content_type(content_type, allowed=("image/png", "application/octet-stream"))
    width, height = _validate_png(content)
    return "image/png", width, height


def _validate_content_type(content_type: str | None, *, allowed: tuple[str, ...]) -> None:
    if content_type is None:
        return
    media_type = content_type.partition(";")[0].strip().lower()
    if media_type not in allowed:
        raise ValueError("pathway asset response has an incompatible content type")


def _validate_png(content: bytes) -> tuple[int, int]:
    if not content.startswith(_PNG_SIGNATURE):
        raise ValueError("pathway image does not have a PNG signature")
    offset = len(_PNG_SIGNATURE)
    chunk_count = 0
    width: int | None = None
    height: int | None = None
    saw_idat = False
    saw_iend = False
    saw_plte = False
    idat_closed = False
    color_type: int | None = None
    bit_depth: int | None = None
    idat_validator: _PngIdatValidator | None = None
    while offset < len(content):
        chunk_count += 1
        if chunk_count > MAX_PNG_CHUNKS or offset + 12 > len(content):
            raise ValueError("pathway PNG chunk structure exceeds the supported bounds")
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        if _PNG_CHUNK_TYPE.fullmatch(chunk_type) is None or chunk_type[2] & 0x20:
            raise ValueError("pathway PNG contains an invalid chunk type")
        data_start = offset + 8
        data_end = data_start + length
        crc_end = data_end + 4
        if data_end < data_start or crc_end > len(content):
            raise ValueError("pathway PNG contains a truncated chunk")
        expected_crc = struct.unpack(">I", content[data_end:crc_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(content[data_start:data_end], actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            raise ValueError("pathway PNG contains an invalid chunk checksum")
        if chunk_count == 1:
            if chunk_type != b"IHDR" or length != 13:
                raise ValueError("pathway PNG must begin with one IHDR chunk")
            parsed_width, parsed_height = struct.unpack(">II", content[data_start : data_start + 8])
            parsed_bit_depth, parsed_color_type, compression, filtering, interlace = content[
                data_start + 8 : data_end
            ]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                parsed_width <= 0
                or parsed_height <= 0
                or parsed_width > MAX_PNG_DIMENSION
                or parsed_height > MAX_PNG_DIMENSION
                or parsed_width * parsed_height > MAX_PNG_PIXELS
                or parsed_bit_depth not in valid_depths.get(parsed_color_type, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                raise ValueError("pathway PNG IHDR is outside the supported image contract")
            width, height = parsed_width, parsed_height
            bit_depth, color_type = parsed_bit_depth, parsed_color_type
            idat_validator = _PngIdatValidator(
                _png_scanline_data_lengths(
                    parsed_width,
                    parsed_height,
                    parsed_bit_depth,
                    parsed_color_type,
                    interlace,
                )
            )
        elif chunk_type == b"IHDR":
            raise ValueError("pathway PNG contains multiple IHDR chunks")
        elif chunk_type == b"PLTE":
            if saw_plte or saw_idat or color_type in {0, 4}:
                raise ValueError("pathway PNG contains an invalid PLTE chunk")
            if length == 0 or length % 3 != 0 or length > 768:
                raise ValueError("pathway PNG contains an invalid PLTE chunk")
            if color_type == 3 and bit_depth is not None and length // 3 > 2**bit_depth:
                raise ValueError("pathway PNG palette exceeds its indexed bit depth")
            saw_plte = True
        elif chunk_type == b"IDAT":
            if idat_closed or idat_validator is None:
                raise ValueError("pathway PNG IDAT chunks must be contiguous after IHDR")
            if color_type == 3 and not saw_plte:
                raise ValueError("indexed pathway PNG images require a PLTE chunk")
            idat_validator.feed_compressed(content[data_start:data_end])
            saw_idat = True
        else:
            if saw_idat and chunk_type != b"IEND":
                idat_closed = True
            if not chunk_type[0] & 0x20 and chunk_type != b"IEND":
                raise ValueError("pathway PNG contains an unsupported critical chunk")
        if chunk_type == b"IEND":
            if length != 0 or crc_end != len(content):
                raise ValueError("pathway PNG has an invalid terminal IEND chunk")
            saw_iend = True
        offset = crc_end
    if width is None or height is None or not saw_idat or not saw_iend or idat_validator is None:
        raise ValueError("pathway PNG is missing required chunks")
    if color_type == 3 and not saw_plte:
        raise ValueError("indexed pathway PNG images require a PLTE chunk")
    idat_validator.finish()
    return width, height


class _PngIdatValidator:
    """Stream-validate one bounded zlib stream and its PNG scanline framing."""

    def __init__(self, row_data_lengths: tuple[int, ...]) -> None:
        self._row_data_lengths = row_data_lengths
        self._expected_bytes = sum(length + 1 for length in row_data_lengths)
        if self._expected_bytes > MAX_PNG_DECOMPRESSED_BYTES:
            raise ValueError("pathway PNG decompressed scanlines exceed the supported bound")
        self._decompressor = zlib.decompressobj()
        self._row_index = 0
        self._row_offset = 0
        self._output_bytes = 0
        self._finished = False

    def feed_compressed(self, content: bytes) -> None:
        if self._finished or (self._decompressor.eof and content):
            raise ValueError("pathway PNG contains trailing compressed image data")
        pending = content
        while pending:
            pending_length = len(pending)
            try:
                output = self._decompressor.decompress(
                    pending,
                    _PNG_DECOMPRESSION_CHUNK_BYTES,
                )
            except zlib.error as error:
                raise ValueError(
                    "pathway PNG contains an invalid compressed IDAT stream"
                ) from error
            self._feed_output(output)
            if self._decompressor.unused_data:
                raise ValueError("pathway PNG contains trailing compressed image data")
            pending = self._decompressor.unconsumed_tail
            if pending and len(pending) == pending_length and not output:
                raise ValueError("pathway PNG compressed stream made no bounded progress")
        self._drain_output()

    def finish(self) -> None:
        self._drain_output()
        if (
            not self._decompressor.eof
            or self._decompressor.unused_data
            or self._decompressor.unconsumed_tail
        ):
            raise ValueError("pathway PNG contains an incomplete compressed IDAT stream")
        if (
            self._output_bytes != self._expected_bytes
            or self._row_index != len(self._row_data_lengths)
            or self._row_offset != 0
        ):
            raise ValueError("pathway PNG decompressed scanlines do not match IHDR dimensions")
        self._finished = True

    def _drain_output(self) -> None:
        while True:
            try:
                output = self._decompressor.decompress(
                    b"",
                    _PNG_DECOMPRESSION_CHUNK_BYTES,
                )
            except zlib.error as error:
                raise ValueError(
                    "pathway PNG contains an invalid compressed IDAT stream"
                ) from error
            if not output:
                return
            self._feed_output(output)

    def _feed_output(self, content: bytes) -> None:
        self._output_bytes += len(content)
        if self._output_bytes > self._expected_bytes:
            raise ValueError("pathway PNG decompressed output exceeds its scanline bound")
        position = 0
        while position < len(content):
            if self._row_index >= len(self._row_data_lengths):
                raise ValueError("pathway PNG contains excess decompressed scanline data")
            row_size = self._row_data_lengths[self._row_index] + 1
            if self._row_offset == 0:
                if content[position] > 4:
                    raise ValueError("pathway PNG contains an invalid scanline filter")
                position += 1
                self._row_offset = 1
            consumed = min(row_size - self._row_offset, len(content) - position)
            position += consumed
            self._row_offset += consumed
            if self._row_offset == row_size:
                self._row_index += 1
                self._row_offset = 0


def _png_scanline_data_lengths(
    width: int,
    height: int,
    bit_depth: int,
    color_type: int,
    interlace: int,
) -> tuple[int, ...]:
    bits_per_pixel = bit_depth * _PNG_CHANNELS[color_type]

    def row_bytes(pixel_width: int) -> int:
        return (pixel_width * bits_per_pixel + 7) // 8

    if interlace == 0:
        return (row_bytes(width),) * height
    rows: list[int] = []
    for x_start, y_start, x_step, y_step in _ADAM7_PASSES:
        pass_width = _png_pass_size(width, x_start, x_step)
        pass_height = _png_pass_size(height, y_start, y_step)
        if pass_width and pass_height:
            rows.extend((row_bytes(pass_width),) * pass_height)
    return tuple(rows)


def _png_pass_size(size: int, start: int, step: int) -> int:
    return 0 if size <= start else (size - start + step - 1) // step


def _validate_kgml_preflight(content: bytes) -> None:
    try:
        text = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("KGML must be canonical UTF-8 XML") from exc
    supported_start, has_pathway_root = _scan_kgml_prolog(text)
    declarations = _ACTIVE_XML_DECLARATION.finditer(text)
    first_declaration = next(declarations, None)
    if first_declaration is not None and (
        first_declaration.start() != supported_start or next(declarations, None) is not None
    ):
        raise ValueError("KGML contains an unsupported DTD or entity declaration")
    if not has_pathway_root:
        raise ValueError("KGML must begin with an obvious pathway root tag")


def _scan_kgml_prolog(text: str) -> tuple[int | None, bool]:
    """Scan the bounded KGML prolog in linear time without resolving its DTD."""
    position = 1 if text.startswith("\ufeff") else 0
    position, misc_is_closed = _skip_kgml_misc(text, position)
    if not misc_is_closed:
        return None, False

    doctype_start: int | None = None
    doctype_end = _match_supported_kgml_doctype(text, position)
    if doctype_end is not None:
        doctype_start = position
        position = doctype_end

    position, misc_is_closed = _skip_kgml_misc(text, position)
    if not misc_is_closed:
        return None, False
    root_is_present = text.startswith("<pathway", position)
    boundary_position = position + len("<pathway")
    root_is_bounded = boundary_position < len(text) and (
        text[boundary_position] in _XML_WHITESPACE or text[boundary_position] in "/>"
    )
    if not (root_is_present and root_is_bounded):
        return None, False
    return doctype_start, True


def _skip_kgml_misc(text: str, start: int) -> tuple[int, bool]:
    """Skip XML whitespace, processing instructions, and comments once each is closed."""
    position = start
    while True:
        position = _skip_xml_whitespace(text, position)
        if text.startswith("<?", position):
            end = text.find("?>", position + 2)
            if end < 0:
                return position, False
            position = end + 2
            continue
        if text.startswith("<!--", position):
            end = text.find("-->", position + 4)
            if end < 0:
                return position, False
            position = end + 3
            continue
        return position, True


def _match_supported_kgml_doctype(text: str, start: int) -> int | None:
    """Return the end of the one inert canonical KEGG declaration, if present."""
    position = start
    for token in ("<!DOCTYPE", "pathway", "SYSTEM"):
        if not text.startswith(token, position):
            return None
        position += len(token)
        next_position = _skip_xml_whitespace(text, position)
        if next_position == position:
            return None
        position = next_position

    if position >= len(text) or text[position] not in "\"'":
        return None
    quote = text[position]
    position += 1
    if not text.startswith(_SUPPORTED_KGML_SYSTEM_ID, position):
        return None
    position += len(_SUPPORTED_KGML_SYSTEM_ID)
    if position >= len(text) or text[position] != quote:
        return None
    position = _skip_xml_whitespace(text, position + 1)
    if position >= len(text) or text[position] != ">":
        return None
    return position + 1


def _skip_xml_whitespace(text: str, start: int) -> int:
    position = start
    while position < len(text) and text[position] in _XML_WHITESPACE:
        position += 1
    return position


__all__ = [
    "MAX_PATHWAY_ASSET_BYTES",
    "MAX_PNG_DECOMPRESSED_BYTES",
    "MAX_PNG_DIMENSION",
    "MAX_PNG_PIXELS",
    "PATHWAY_ASSET_PARSER_VERSION",
    "PathwayAssetKind",
    "PathwayAssetRequest",
    "PathwayAssetResult",
    "prepare_pathway_asset",
    "validate_pathway_asset_content",
]
