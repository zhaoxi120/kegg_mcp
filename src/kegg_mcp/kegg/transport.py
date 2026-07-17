"""Safe synchronous HTTPS transport for the typed KEGG client."""

from __future__ import annotations

import math
import socket
import ssl
from contextlib import closing
from dataclasses import dataclass
from email.message import Message
from enum import StrEnum
from http.client import HTTPException
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

from pydantic import ValidationError

from kegg_mcp import __version__
from kegg_mcp.kegg.contracts import MAX_HTTP_METADATA_ITEMS, HttpMetadata

_ALLOWED_METADATA_HEADERS = ("content-type", "etag", "last-modified", "date")


USER_AGENT = f"kegg-mcp/{__version__}"


class TransportErrorKind(StrEnum):
    """Stable internal categories used by the client retry policy."""

    INVALID_REQUEST = "invalid_request"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    DNS = "dns"
    PERMISSION = "permission"
    TLS = "tls"
    REDIRECT_REJECTED = "redirect_rejected"
    RESPONSE_TOO_LARGE = "response_too_large"
    UNSUPPORTED_ENCODING = "unsupported_encoding"
    INVALID_RESPONSE = "invalid_response"


_SAFE_ERROR_MESSAGES = {
    TransportErrorKind.INVALID_REQUEST: "The HTTPS transport rejected an invalid request.",
    TransportErrorKind.TIMEOUT: "The HTTPS request timed out.",
    TransportErrorKind.CONNECTION: "The HTTPS request could not be completed.",
    TransportErrorKind.DNS: "The HTTPS endpoint name could not be resolved.",
    TransportErrorKind.PERMISSION: "The environment denied the HTTPS connection.",
    TransportErrorKind.TLS: "TLS validation failed for the HTTPS request.",
    TransportErrorKind.REDIRECT_REJECTED: "The HTTPS transport rejected a redirect response.",
    TransportErrorKind.RESPONSE_TOO_LARGE: "The HTTPS response exceeded the configured size limit.",
    TransportErrorKind.UNSUPPORTED_ENCODING: (
        "The HTTPS response used an unsupported content encoding."
    ),
    TransportErrorKind.INVALID_RESPONSE: "The HTTPS peer returned an invalid response.",
}


class TransportError(Exception):
    """A redacted transport failure that the client may classify for retry."""

    def __init__(self, kind: TransportErrorKind, *, transient: bool) -> None:
        self.kind = kind
        self.transient = transient
        super().__init__(_SAFE_ERROR_MESSAGES[kind])


@dataclass(frozen=True, slots=True)
class TransportResponse:
    """One bounded HTTP response with only approved metadata retained."""

    status_code: int
    body: bytes
    http_metadata: tuple[HttpMetadata, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not 100 <= self.status_code <= 599:
            raise ValueError("status_code must be a three-digit HTTP status")
        raw_metadata = cast(object, self.http_metadata)
        if not isinstance(raw_metadata, tuple):
            raise TypeError("http_metadata must be a tuple of validated metadata")
        metadata = cast(tuple[object, ...], raw_metadata)
        if not all(isinstance(item, HttpMetadata) for item in metadata):
            raise TypeError("http_metadata must be a tuple of validated metadata")
        if len(metadata) > MAX_HTTP_METADATA_ITEMS:
            raise ValueError("http_metadata exceeds the bounded item count")


class Transport(Protocol):
    """Injectable synchronous transport contract used by the KEGG client."""

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        """Execute one bounded HTTPS GET request."""
        ...


class UrlOpener(Protocol):
    """Narrow opener interface used to isolate HTTP in unit tests."""

    def open(
        self,
        fullurl: Request,
        data: bytes | None = None,
        timeout: float = ...,
    ) -> Any:
        """Open one request using urllib-compatible semantics."""
        ...


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Convert every redirect into an HTTPError for explicit rejection."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _default_opener() -> OpenerDirector:
    # An empty ProxyHandler prevents urllib from reading proxy settings from the
    # process environment. The redirect handler rejects rather than follows 3xx.
    return build_opener(ProxyHandler({}), _RejectRedirectHandler())


class HttpsTransport:
    """Bounded GET-only HTTPS transport without proxies or redirects."""

    def __init__(self, *, opener: UrlOpener | None = None) -> None:
        self._opener: UrlOpener = opener if opener is not None else _default_opener()

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        """Execute one request while discarding sensitive low-level exception context."""
        failure: TransportError
        try:
            return self._request_once(
                url,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
        except TransportError as error:
            failure = TransportError(error.kind, transient=error.transient)
        raise failure

    def _request_once(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        """Execute one GET and return its status, bounded body, and safe headers."""
        self._validate_limits(timeout_seconds, max_response_bytes)
        self._validate_url(url)
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "text/plain",
                "Accept-Encoding": "identity",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            try:
                response = self._opener.open(request, timeout=timeout_seconds)
            except HTTPError as error:
                if 300 <= error.code <= 399:
                    error.close()
                    raise TransportError(
                        TransportErrorKind.REDIRECT_REJECTED,
                        transient=False,
                    ) from None
                response = error
            except ValueError:
                raise TransportError(
                    TransportErrorKind.INVALID_REQUEST,
                    transient=False,
                ) from None

            with closing(response):
                return self._read_response(response, max_response_bytes=max_response_bytes)
        except TransportError:
            raise
        except TimeoutError:
            raise TransportError(TransportErrorKind.TIMEOUT, transient=True) from None
        except ssl.SSLError:
            raise TransportError(TransportErrorKind.TLS, transient=False) from None
        except URLError as error:
            reason = error.reason
            if isinstance(reason, TimeoutError):
                raise TransportError(TransportErrorKind.TIMEOUT, transient=True) from None
            if isinstance(reason, socket.gaierror):
                raise TransportError(TransportErrorKind.DNS, transient=False) from None
            if isinstance(reason, PermissionError):
                raise TransportError(TransportErrorKind.PERMISSION, transient=False) from None
            if isinstance(reason, (ssl.SSLError, ssl.CertificateError)):
                raise TransportError(TransportErrorKind.TLS, transient=False) from None
            raise TransportError(TransportErrorKind.CONNECTION, transient=True) from None
        except PermissionError:
            raise TransportError(TransportErrorKind.PERMISSION, transient=False) from None
        except HTTPException:
            raise TransportError(
                TransportErrorKind.INVALID_RESPONSE,
                transient=False,
            ) from None
        except OSError:
            raise TransportError(TransportErrorKind.CONNECTION, transient=True) from None

    @staticmethod
    def _validate_limits(timeout_seconds: object, max_response_bytes: object) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0.0
        ):
            raise TransportError(TransportErrorKind.INVALID_REQUEST, transient=False)
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or max_response_bytes <= 0
        ):
            raise TransportError(TransportErrorKind.INVALID_REQUEST, transient=False)

    @staticmethod
    def _validate_url(url: object) -> None:
        if not isinstance(url, str) or not url or url != url.strip():
            raise TransportError(TransportErrorKind.INVALID_REQUEST, transient=False)
        try:
            url.encode("ascii")
            parsed = urlsplit(url)
            parsed_port = parsed.port
        except (UnicodeEncodeError, ValueError):
            raise TransportError(
                TransportErrorKind.INVALID_REQUEST,
                transient=False,
            ) from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed_port is not None and not 1 <= parsed_port <= 65535)
            or "%" in url
            or "\\" in url
            or any(ord(character) < 33 or ord(character) == 127 for character in url)
            or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        ):
            raise TransportError(TransportErrorKind.INVALID_REQUEST, transient=False)

    @classmethod
    def _read_response(
        cls,
        response: Any,
        *,
        max_response_bytes: int,
    ) -> TransportResponse:
        status_code = getattr(response, "status", None)
        if status_code is None:
            status_code = response.getcode()
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise TransportError(TransportErrorKind.INVALID_RESPONSE, transient=False)
        if 300 <= status_code <= 399:
            raise TransportError(TransportErrorKind.REDIRECT_REJECTED, transient=False)

        headers = getattr(response, "headers", None)
        if not isinstance(headers, Message):
            raise TransportError(TransportErrorKind.INVALID_RESPONSE, transient=False)
        typed_headers = cast("Message[str, str]", headers)
        cls._validate_content_encoding(typed_headers)
        declared_length = cls._content_length(typed_headers)
        if declared_length is not None and declared_length > max_response_bytes:
            raise TransportError(TransportErrorKind.RESPONSE_TOO_LARGE, transient=False)

        body = response.read(max_response_bytes + 1)
        if not isinstance(body, bytes):
            raise TransportError(TransportErrorKind.INVALID_RESPONSE, transient=False)
        if len(body) > max_response_bytes:
            raise TransportError(TransportErrorKind.RESPONSE_TOO_LARGE, transient=False)
        if declared_length is not None and len(body) != declared_length:
            raise TransportError(TransportErrorKind.INVALID_RESPONSE, transient=False)

        return TransportResponse(
            status_code=status_code,
            body=body,
            http_metadata=cls._metadata(typed_headers),
        )

    @staticmethod
    def _validate_content_encoding(headers: Message) -> None:
        encoding = headers.get("Content-Encoding")
        if encoding is not None and (encoding.strip().lower() not in {"", "identity"}):
            raise TransportError(TransportErrorKind.UNSUPPORTED_ENCODING, transient=False)

    @staticmethod
    def _content_length(headers: Message) -> int | None:
        raw_length = headers.get("Content-Length")
        if raw_length is None:
            return None
        if not raw_length.isascii() or not raw_length.isdigit():
            raise TransportError(TransportErrorKind.INVALID_RESPONSE, transient=False)
        return int(raw_length)

    @staticmethod
    def _metadata(headers: Message) -> tuple[HttpMetadata, ...]:
        metadata: list[HttpMetadata] = []
        try:
            for name in _ALLOWED_METADATA_HEADERS:
                values = headers.get_all(name, failobj=[])
                for value in values:
                    metadata.append(HttpMetadata(name=name, value=value))
                    if len(metadata) > MAX_HTTP_METADATA_ITEMS:
                        raise TransportError(
                            TransportErrorKind.INVALID_RESPONSE,
                            transient=False,
                        )
        except (TypeError, ValidationError, ValueError):
            raise TransportError(
                TransportErrorKind.INVALID_RESPONSE,
                transient=False,
            ) from None
        return tuple(metadata)
