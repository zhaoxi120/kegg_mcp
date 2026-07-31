"""Tests for bounded typed pathway PNG and KGML assets."""

from __future__ import annotations

import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter

import pytest
from pydantic import ValidationError

import kegg_mcp.kegg.client as client_module
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg import (
    PATHWAY_ASSET_PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    AccessMode,
    CacheLookupState,
    CachePolicy,
    KeggClient,
    KeggClientConfig,
    KeggRequestOptions,
    OfflineCacheAccess,
    PathwayAssetKind,
    PathwayAssetRequest,
    PathwayAssetResult,
    PublicAcademicAccess,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.cache import SQLiteKeggCache
from kegg_mcp.kegg.contracts import HttpMetadata, KeggClientLimits
from kegg_mcp.kegg.operations import ResponseParser
from kegg_mcp.kegg.pathway_assets import (
    prepare_pathway_asset,
    validate_pathway_asset_content,
)
from kegg_mcp.kegg.transport import TransportResponse

_NOW = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
_KGML_DOCTYPE = '<!DOCTYPE pathway SYSTEM "https://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd">'


def _chunk(chunk_type: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(data, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", checksum)


def _png(width: int = 2, height: int = 1, *, compressed_rows: bytes | None = None) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(rows) if compressed_rows is None else compressed_rows)
        + _chunk(b"IEND", b"")
    )


def _kgml(pathway_id: str = "ko00010", *, include_doctype: bool = True) -> bytes:
    # This inert declaration was observed in current KEGG KGML responses on 2026-07-21.
    doctype = _KGML_DOCTYPE if include_doctype else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"{doctype}"
        f'<pathway name="path:{pathway_id}" org="ko" number="00010">'
        '<entry id="1" name="ko:K00844" type="ortholog" />'
        "</pathway>"
    ).encode()


class _QueueTransport:
    def __init__(self, response: TransportResponse) -> None:
        self._response = response
        self.urls: list[str] = []

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del timeout_seconds, max_response_bytes
        self.urls.append(url)
        return self._response


class _BombTransport:
    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del url, timeout_seconds, max_response_bytes
        raise AssertionError("cached pathway asset attempted a network request")


class _NoWaitLimiter:
    def __init__(
        self,
        scope: str,
        requests_per_second: float,
        *,
        state_root: str,
    ) -> None:
        del scope, requests_per_second, state_root

    def acquire(self) -> None:
        return None


@pytest.fixture(autouse=True)
def replace_rate_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "DeploymentRateLimiter", _NoWaitLimiter)


def _config(cache_path: Path) -> KeggClientConfig:
    return KeggClientConfig(
        access=PublicAcademicAccess(academic_use_confirmed=True),
        cache=CachePolicy(path=str(cache_path), ttl_seconds=60),
        limits=KeggClientLimits(max_response_bytes=1_000_000),
    )


def test_request_rejects_arbitrary_urls_and_invalid_pathway_identifiers() -> None:
    for invalid in ("https://example.test/image.png", "../ko00010", "K00844", "map0010"):
        with pytest.raises(ValidationError):
            PathwayAssetRequest(pathway_id=invalid, kind=PathwayAssetKind.IMAGE)


@pytest.mark.parametrize(
    ("kind", "suffix", "parser"),
    [
        (PathwayAssetKind.IMAGE, "image", ResponseParser.PATHWAY_PNG),
        (PathwayAssetKind.IMAGE_2X, "image2x", ResponseParser.PATHWAY_PNG),
        (PathwayAssetKind.KGML, "kgml", ResponseParser.PATHWAY_KGML_PREFLIGHT),
    ],
)
def test_preparation_uses_one_fixed_get_path(
    kind: PathwayAssetKind,
    suffix: str,
    parser: ResponseParser,
) -> None:
    prepared = prepare_pathway_asset(
        PathwayAssetRequest(pathway_id="ko00010", kind=kind),
        KeggClientLimits(),
    )

    assert prepared.path == f"/get/ko00010/{suffix}"
    assert prepared.normalized_request_key == f"/get/ko00010/{suffix}"
    assert prepared.parser is parser
    assert prepared.requested_identifiers == ("ko00010",)


def test_png_validation_checks_structure_crc_dimensions_and_content_type() -> None:
    content = _png(width=3, height=2)
    result = validate_pathway_asset_content(
        PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.IMAGE),
        content,
        content_type="image/png; charset=binary",
    )

    assert result == ("image/png", 3, 2)
    with pytest.raises(ValueError, match="checksum"):
        validate_pathway_asset_content(
            PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.IMAGE),
            content[:-5] + bytes([content[-5] ^ 1]) + content[-4:],
            content_type="image/png",
        )


def test_png_validation_rejects_invalid_idat_stream_and_scanline_output() -> None:
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.IMAGE)

    with pytest.raises(ValueError, match="compressed IDAT"):
        validate_pathway_asset_content(
            request,
            _png(compressed_rows=b"not-a-zlib-stream"),
            content_type="image/png",
        )
    with pytest.raises(ValueError, match="scanlines"):
        validate_pathway_asset_content(
            request,
            _png(compressed_rows=zlib.compress(b"\x00\x00")),
            content_type="image/png",
        )
    with pytest.raises(ValueError, match="scanline filter"):
        validate_pathway_asset_content(
            request,
            _png(compressed_rows=zlib.compress(b"\x05" + b"\x00" * 6)),
            content_type="image/png",
        )


def test_kgml_preflight_accepts_supported_inert_doctype_and_defers_xml_structure() -> None:
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.KGML)

    assert validate_pathway_asset_content(
        request,
        _kgml(),
        content_type="application/xml",
    ) == ("application/xml", None, None)
    assert validate_pathway_asset_content(
        request,
        _kgml("ko00020"),
        content_type="application/xml",
    ) == ("application/xml", None, None)
    assert validate_pathway_asset_content(
        request,
        _kgml(include_doctype=False),
        content_type="application/xml",
    ) == ("application/xml", None, None)
    assert validate_pathway_asset_content(
        request,
        b"<pathway><not-yet-parsed",
        content_type="application/xml",
    ) == ("application/xml", None, None)
    with pytest.raises(ValueError, match="pathway root"):
        validate_pathway_asset_content(
            request,
            b"<not-pathway/>",
            content_type="application/xml",
        )
    with pytest.raises(ValueError, match="UTF-8"):
        validate_pathway_asset_content(
            request,
            b"\xff",
            content_type="application/xml",
        )


@pytest.mark.parametrize(
    "payload",
    [
        '\u00a0<pathway name="path:ko00010"/>',
        '<pathway\u00a0name="path:ko00010"/>',
        '<pathwayX name="path:ko00010"/>',
        (
            "<!DOCTYPE\u00a0pathway SYSTEM "
            '"https://www.kegg.jp/kegg/xml/KGML_v0.7.2_.dtd">'
            '<pathway name="path:ko00010"/>'
        ),
    ],
)
def test_kgml_preflight_rejects_non_xml_whitespace_and_unbounded_root_names(
    payload: str,
) -> None:
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.KGML)

    with pytest.raises(ValueError):
        validate_pathway_asset_content(
            request,
            payload.encode(),
            content_type="application/xml",
        )


def test_kgml_preflight_rejects_long_invalid_prolog_without_regex_backtracking() -> None:
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.KGML)
    payload = b" " * 16_384 + b"X"

    started = perf_counter()
    with pytest.raises(ValueError, match="pathway root"):
        validate_pathway_asset_content(
            request,
            payload,
            content_type="application/xml",
        )

    assert perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "payload",
    [
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
        (_KGML_DOCTYPE + _KGML_DOCTYPE + '<pathway name="path:ko00010"/>').encode(),
        ('<pathway name="path:ko00010"/>' + _KGML_DOCTYPE).encode(),
    ],
)
def test_kgml_preflight_rejects_unsupported_or_misplaced_declarations(
    payload: bytes,
) -> None:
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.KGML)

    with pytest.raises(ValueError, match="DTD or entity"):
        validate_pathway_asset_content(
            request,
            payload,
            content_type="application/xml",
        )


def test_client_retrieves_validates_and_reuses_one_png_asset(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    transport = _QueueTransport(
        TransportResponse(
            status_code=200,
            body=_png(),
            http_metadata=(HttpMetadata(name="content-type", value="image/png"),),
        )
    )
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.IMAGE)
    network = KeggClient(
        _config(cache_path),
        transport=transport,
        clock=lambda: _NOW,
    ).get_pathway_asset(request)
    cached = KeggClient(
        _config(cache_path),
        transport=_BombTransport(),
        clock=lambda: _NOW + timedelta(seconds=1),
    ).get_pathway_asset(
        request,
        options=KeggRequestOptions(refresh=False, cache_only=True),
    )

    assert transport.urls == ["https://rest.kegg.jp/get/ko00010/image"]
    assert network.mime_type == "image/png"
    assert (network.width, network.height) == (2, 1)
    assert network.provenance.origin is ResponseOrigin.NETWORK
    assert network.provenance.parser_name == "pathway_png"
    assert network.provenance.parser_version == PATHWAY_ASSET_PARSER_VERSION
    assert PathwayAssetResult.model_validate_json(network.model_dump_json()) == network
    assert cached.content == network.content
    assert cached.provenance.origin is ResponseOrigin.CACHE


def test_client_reuses_one_explicitly_allowed_stale_asset(tmp_path: Path) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.IMAGE)
    KeggClient(
        _config(cache_path),
        transport=_QueueTransport(
            TransportResponse(
                status_code=200,
                body=_png(),
                http_metadata=(HttpMetadata(name="content-type", value="image/png"),),
            )
        ),
        clock=lambda: _NOW,
    ).get_pathway_asset(request)

    stale = KeggClient(
        _config(cache_path),
        transport=_BombTransport(),
        clock=lambda: _NOW + timedelta(seconds=61),
    ).get_pathway_asset(
        request,
        options=KeggRequestOptions(
            refresh=False,
            cache_only=True,
            allow_stale=True,
        ),
    )

    assert stale.provenance.origin is ResponseOrigin.CACHE
    assert stale.provenance.cache_lookup_state is CacheLookupState.STALE_HIT
    assert stale.provenance.is_stale is True


def test_client_reports_cached_asset_content_failure_without_network(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.IMAGE)
    limits = _config(cache_path).limits
    prepared = prepare_pathway_asset(request, limits)
    SQLiteKeggCache(cache_path).write(
        prepared.operation,
        prepared.normalized_request_key,
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        body=b"not-a-png",
        retrieved_at=_NOW,
        expires_at=_NOW + timedelta(seconds=60),
        parser_version=PATHWAY_ASSET_PARSER_VERSION,
        database_release=None,
        http_metadata=(HttpMetadata(name="content-type", value="image/png"),),
    )

    with pytest.raises(KeggMcpError) as caught:
        KeggClient(
            _config(cache_path),
            transport=_BombTransport(),
            clock=lambda: _NOW + timedelta(seconds=1),
        ).get_pathway_asset(
            request,
            options=KeggRequestOptions(refresh=False, cache_only=True),
        )

    assert caught.value.detail.code is ErrorCode.CACHE_FAILED
    assert {item.value for item in caught.value.detail.safe_details} == {"pathway_asset_validation"}


def test_offline_client_forces_refresh_requests_to_the_read_only_asset_cache(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    request = PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.IMAGE)
    KeggClient(
        _config(cache_path),
        transport=_QueueTransport(
            TransportResponse(
                status_code=200,
                body=_png(),
                http_metadata=(HttpMetadata(name="content-type", value="image/png"),),
            )
        ),
        clock=lambda: _NOW,
    ).get_pathway_asset(request)
    live_config = _config(cache_path)
    offline_config = live_config.model_copy(update={"access": OfflineCacheAccess()})

    result = KeggClient(
        offline_config,
        transport=_BombTransport(),
        clock=lambda: _NOW + timedelta(seconds=1),
    ).get_pathway_asset(request, options=KeggRequestOptions(refresh=True))

    assert result.provenance.access_mode is AccessMode.OFFLINE_CACHE
    assert result.provenance.origin is ResponseOrigin.CACHE


def test_client_rejects_active_kgml_declarations_before_caching(tmp_path: Path) -> None:
    transport = _QueueTransport(
        TransportResponse(
            status_code=200,
            body=(
                b'<!DOCTYPE pathway [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                b'<pathway name="path:ko00010"/>'
            ),
            http_metadata=(HttpMetadata(name="content-type", value="application/xml"),),
        )
    )
    client = KeggClient(
        _config(tmp_path / "kegg.sqlite3"),
        transport=transport,
        clock=lambda: _NOW,
    )

    with pytest.raises(KeggMcpError) as caught:
        client.get_pathway_asset(
            PathwayAssetRequest(pathway_id="ko00010", kind=PathwayAssetKind.KGML)
        )

    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
