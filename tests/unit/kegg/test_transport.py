"""Offline tests for the bounded KEGG HTTPS transport."""

from __future__ import annotations

import io
import ssl
import traceback
from email.message import Message
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

import kegg_mcp.kegg.transport as transport_module
from kegg_mcp.kegg.contracts import MAX_HTTP_METADATA_ITEMS, HttpMetadata
from kegg_mcp.kegg.transport import (
    PROJECT_DOCUMENTATION_URL,
    USER_AGENT,
    HttpsTransport,
    TransportError,
    TransportErrorKind,
    TransportResponse,
)


class FakeResponse:
    """Small urllib response double with bounded-read observations."""

    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        headers: Message[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers if headers is not None else Message()
        self._body = body
        self.read_sizes: list[int] = []
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self._body[:size]

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    """Record a request and return or raise one configured outcome."""

    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self.outcome = outcome
        self.requests: list[Request] = []
        self.timeouts: list[float] = []

    def open(
        self,
        fullurl: Request,
        data: bytes | None = None,
        timeout: float = 0.0,
    ) -> Any:
        del data
        self.requests.append(fullurl)
        self.timeouts.append(timeout)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def make_headers(**values: str) -> Message[str, str]:
    headers: Message[str, str] = Message()
    for name, value in values.items():
        headers[name.replace("_", "-")] = value
    return headers


def test_transport_sends_fixed_safe_headers_and_retains_only_allowlisted_metadata() -> None:
    headers = make_headers(
        Content_Length="4",
        Content_Type="text/plain; charset=utf-8",
        ETag='"example"',
        Date="Tue, 14 Jul 2026 00:00:00 GMT",
        Authorization="secret",
        Set_Cookie="private=1",
    )
    response = FakeResponse(b"body", headers=headers)
    opener = FakeOpener(response)

    result = HttpsTransport(opener=opener).request(
        "https://rest.kegg.jp/info/kegg",
        timeout_seconds=7.5,
        max_response_bytes=100,
    )

    request = opener.requests[0]
    assert request.get_method() == "GET"
    assert request.get_header("Accept") == "text/plain"
    assert request.get_header("Accept-encoding") == "identity"
    assert request.get_header("User-agent") == USER_AGENT
    assert PROJECT_DOCUMENTATION_URL in USER_AGENT
    assert opener.timeouts == [7.5]
    assert response.read_sizes == [101]
    assert response.closed is True
    assert result.status_code == 200
    assert result.body == b"body"
    assert [(item.name, item.value) for item in result.http_metadata] == [
        ("content-type", "text/plain; charset=utf-8"),
        ("etag", '"example"'),
        ("date", "Tue, 14 Jul 2026 00:00:00 GMT"),
    ]


def test_transport_rejects_excess_repeated_allowlisted_metadata_without_values() -> None:
    headers: Message[str, str] = Message()
    private_value = "private-header-marker"
    for index in range(MAX_HTTP_METADATA_ITEMS + 1):
        headers["ETag"] = f"{private_value}-{index}"

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=FakeOpener(FakeResponse(b"body", headers=headers))).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=5.0,
            max_response_bytes=100,
        )

    assert error.value.kind is TransportErrorKind.INVALID_RESPONSE
    assert error.value.transient is False
    assert private_value not in str(error.value)


def test_transport_response_rejects_excess_metadata_from_injected_transports() -> None:
    metadata = tuple(
        HttpMetadata(name="etag", value=f'"value-{index}"')
        for index in range(MAX_HTTP_METADATA_ITEMS + 1)
    )

    with pytest.raises(ValueError, match="bounded item count"):
        TransportResponse(status_code=200, body=b"body", http_metadata=metadata)


def test_default_opener_ignores_environment_proxy_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://user:secret@example.invalid:8080")
    proxy_configurations: list[dict[str, str]] = []

    def fake_proxy_handler(proxies: dict[str, str]) -> object:
        proxy_configurations.append(proxies)
        return object()

    def fake_build_opener(*handlers: object) -> FakeOpener:
        assert len(handlers) == 2
        return FakeOpener(FakeResponse(b"unused"))

    monkeypatch.setattr(transport_module, "ProxyHandler", fake_proxy_handler)
    monkeypatch.setattr(transport_module, "build_opener", fake_build_opener)

    HttpsTransport()

    assert proxy_configurations == [{}]


def test_transport_returns_http_error_status_for_client_classification() -> None:
    headers = make_headers(Content_Length="7", Content_Type="text/plain")
    error = HTTPError(
        "https://rest.kegg.jp/get/K99999",
        404,
        "Not Found",
        headers,
        io.BytesIO(b"missing"),
    )

    result = HttpsTransport(opener=FakeOpener(error)).request(
        "https://rest.kegg.jp/get/K99999",
        timeout_seconds=5.0,
        max_response_bytes=100,
    )

    assert result.status_code == 404
    assert result.body == b"missing"


def test_transport_rejects_redirect_without_leaking_destination() -> None:
    destination = "https://secret.example.invalid/private"
    redirect = HTTPError(
        "https://rest.kegg.jp/info/kegg",
        302,
        destination,
        make_headers(Location=destination),
        io.BytesIO(b""),
    )

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=FakeOpener(redirect)).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=5.0,
            max_response_bytes=100,
        )

    assert error.value.kind is TransportErrorKind.REDIRECT_REJECTED
    assert error.value.transient is False
    assert destination not in str(error.value)


def test_transport_rejects_declared_oversize_before_reading_body() -> None:
    response = FakeResponse(b"small", headers=make_headers(Content_Length="101"))

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=FakeOpener(response)).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=5.0,
            max_response_bytes=100,
        )

    assert error.value.kind is TransportErrorKind.RESPONSE_TOO_LARGE
    assert error.value.transient is False
    assert response.read_sizes == []


def test_transport_reads_only_limit_plus_one_and_rejects_actual_oversize() -> None:
    response = FakeResponse(b"123456")

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=FakeOpener(response)).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=5.0,
            max_response_bytes=5,
        )

    assert error.value.kind is TransportErrorKind.RESPONSE_TOO_LARGE
    assert response.read_sizes == [6]


@pytest.mark.parametrize(
    ("headers", "expected_kind"),
    [
        (make_headers(Content_Encoding="gzip"), TransportErrorKind.UNSUPPORTED_ENCODING),
        (make_headers(Content_Length="invalid"), TransportErrorKind.INVALID_RESPONSE),
        (make_headers(Content_Length="6"), TransportErrorKind.INVALID_RESPONSE),
    ],
)
def test_transport_rejects_unsupported_or_inconsistent_response_metadata(
    headers: Message[str, str],
    expected_kind: TransportErrorKind,
) -> None:
    response = FakeResponse(b"12345", headers=headers)

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=FakeOpener(response)).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=5.0,
            max_response_bytes=100,
        )

    assert error.value.kind is expected_kind
    assert error.value.transient is False


@pytest.mark.parametrize(
    ("outcome", "expected_kind", "transient"),
    [
        (TimeoutError("private timeout detail"), TransportErrorKind.TIMEOUT, True),
        (URLError("private connection detail"), TransportErrorKind.CONNECTION, True),
        (ssl.SSLError("private TLS detail"), TransportErrorKind.TLS, False),
        (
            URLError(ssl.SSLError("private TLS detail")),
            TransportErrorKind.TLS,
            False,
        ),
    ],
)
def test_transport_classifies_failures_without_exposing_cause(
    outcome: BaseException,
    expected_kind: TransportErrorKind,
    transient: bool,
) -> None:
    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=FakeOpener(outcome)).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=5.0,
            max_response_bytes=100,
        )

    assert error.value.kind is expected_kind
    assert error.value.transient is transient
    assert "private" not in str(error.value)
    assert "rest.kegg.jp" not in str(error.value)


def test_transport_traceback_suppresses_sensitive_low_level_failure_context() -> None:
    secret = "credential-that-must-not-appear"

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=FakeOpener(URLError(f"https://user:{secret}@private"))).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=5.0,
            max_response_bytes=100,
        )

    rendered = "".join(
        traceback.format_exception(type(error.value), error.value, error.value.__traceback__)
    )
    assert secret not in rendered
    assert error.value.__context__ is None
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "url",
    [
        "http://rest.kegg.jp/info/kegg",
        "https://user:secret@rest.kegg.jp/info/kegg",
        "https://rest.kegg.jp/info/kegg?secret=value",
        "https://rest.kegg.jp/info/kegg#fragment",
        "https://rest.kegg.jp/info/../private",
        "https://rest.kegg.jp/info/%2e%2e/private",
        "https://rest.kegg.jp:70000/info/kegg",
        "https://rest.kegg.jp/info/kegg\n",
        "https://rest.kegg.jp/info/keggé",
    ],
)
def test_transport_rejects_unsafe_urls_before_opening(url: str) -> None:
    opener = FakeOpener(FakeResponse(b"unused"))

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=opener).request(
            url,
            timeout_seconds=5.0,
            max_response_bytes=100,
        )

    assert error.value.kind is TransportErrorKind.INVALID_REQUEST
    assert error.value.transient is False
    assert opener.requests == []


@pytest.mark.parametrize(
    ("timeout", "maximum"),
    [
        (0.0, 1),
        (float("nan"), 1),
        (cast(float, True), 1),
        (1.0, 0),
        (1.0, cast(int, True)),
    ],
)
def test_transport_rejects_invalid_limits_without_opening(
    timeout: float,
    maximum: int,
) -> None:
    opener = FakeOpener(FakeResponse(b"unused"))

    with pytest.raises(TransportError) as error:
        HttpsTransport(opener=opener).request(
            "https://rest.kegg.jp/info/kegg",
            timeout_seconds=timeout,
            max_response_bytes=maximum,
        )

    assert error.value.kind is TransportErrorKind.INVALID_REQUEST
    assert opener.requests == []
