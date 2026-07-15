"""Repository-wide pytest controls for explicitly authorized live KEGG tests."""

from __future__ import annotations

import os
import time
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from email.message import Message
from itertools import pairwise
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)

import pytest

from kegg_mcp.kegg import (
    AccessMode,
    CachePolicy,
    KeggClient,
    KeggClientConfig,
    KeggClientLimits,
    RetryPolicy,
)
from kegg_mcp.kegg.contracts import KeggOperation
from kegg_mcp.kegg.transport import (
    HttpsTransport,
    TransportError,
    TransportResponse,
)
from kegg_mcp.mcp.config import load_runtime_config

_LIVE_MARKER = "live_kegg"
_MAX_REQUESTS_PER_OPERATION = 30
_MAX_TOTAL_REQUESTS = 120
_MIN_START_GAP_SECONDS = 0.95


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register explicit, local-only controls for the live compatibility campaign."""
    group = parser.getgroup("kegg-live")
    group.addoption(
        "--run-kegg-live",
        action="store_true",
        default=False,
        help="Run explicitly authorized live KEGG compatibility tests.",
    )
    group.addoption(
        "--kegg-live-use-env-proxy",
        action="store_true",
        default=False,
        help=(
            "Use process proxy settings for the test-only KEGG opener; this does not change "
            "the production transport default."
        ),
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Keep live tests offline unless both the CLI gate and access gate are explicit."""
    live_items = [item for item in items if item.get_closest_marker(_LIVE_MARKER) is not None]
    if not live_items:
        return

    if not bool(config.getoption("run_kegg_live")):
        skip = pytest.mark.skip(reason="requires the explicit --run-kegg-live option")
        for item in live_items:
            item.add_marker(skip)
        return

    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        raise pytest.UsageError("live KEGG tests must run in one non-xdist process")

    try:
        runtime = load_runtime_config(os.environ)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    if runtime.kegg.access.mode is AccessMode.OFFLINE_CACHE:
        raise pytest.UsageError(
            "live KEGG tests require an explicitly confirmed public_academic or licensed mode"
        )


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Preserve the production transport's redirect rejection in proxy test mode."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True, slots=True)
class _RequestStart:
    operation: KeggOperation
    monotonic_seconds: float


class _LiveCampaignCircuitOpen(RuntimeError):
    """Stop additional wire attempts after a rate-limit or repeated transport failure."""


class _RecordingTransport:
    """Bound and record real wire attempts without exposing endpoints or response bodies."""

    def __init__(self, *, opener: OpenerDirector | None = None) -> None:
        self._inner = HttpsTransport(opener=opener)
        self._starts: list[_RequestStart] = []
        self._counts: Counter[KeggOperation] = Counter()
        self._consecutive_transport_failures = 0
        self._circuit_reason: str | None = None

    @property
    def starts(self) -> tuple[_RequestStart, ...]:
        return tuple(self._starts)

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        if self._circuit_reason is not None:
            raise _LiveCampaignCircuitOpen(self._circuit_reason)

        path_parts = urlsplit(url).path.split("/")
        if len(path_parts) < 3:
            raise AssertionError("live KEGG request did not contain a bounded operation path")
        operation = KeggOperation(path_parts[1])
        if self._counts[operation] >= _MAX_REQUESTS_PER_OPERATION:
            raise _LiveCampaignCircuitOpen("per-operation live request budget exhausted")
        if len(self._starts) >= _MAX_TOTAL_REQUESTS:
            raise _LiveCampaignCircuitOpen("global live request budget exhausted")

        self._counts[operation] += 1
        self._starts.append(_RequestStart(operation=operation, monotonic_seconds=time.monotonic()))
        try:
            response = self._inner.request(
                url,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
        except TransportError:
            self._consecutive_transport_failures += 1
            if self._consecutive_transport_failures >= 3:
                self._circuit_reason = "three consecutive live transport failures"
            raise

        if response.status_code == 429:
            self._circuit_reason = "the live endpoint returned HTTP 429"
        if response.status_code >= 500:
            self._consecutive_transport_failures += 1
            if self._consecutive_transport_failures >= 3:
                self._circuit_reason = "three consecutive live server failures"
        else:
            self._consecutive_transport_failures = 0
        return response


def _proxy_opener(enabled: bool) -> OpenerDirector | None:
    if not enabled:
        return None
    return build_opener(ProxyHandler(), _RejectRedirectHandler())


@pytest.fixture(scope="session")
def live_kegg_client(pytestconfig: pytest.Config) -> Iterator[KeggClient]:
    """Provide one bounded client and remove every live payload after the test session."""
    runtime = load_runtime_config(os.environ)
    if runtime.kegg.access.mode is AccessMode.OFFLINE_CACHE:
        raise pytest.UsageError("the live KEGG fixture cannot use offline_cache mode")

    use_proxy = bool(pytestconfig.getoption("kegg_live_use_env_proxy"))
    transport = _RecordingTransport(opener=_proxy_opener(use_proxy))
    with TemporaryDirectory(prefix="kegg-mcp-live-") as directory:
        client = KeggClient(
            KeggClientConfig(
                access=runtime.kegg.access,
                limits=KeggClientLimits(
                    requests_per_second=1.0,
                    timeout_seconds=30.0,
                    max_identifiers=1,
                    relation_batch_size=1,
                ),
                retry=RetryPolicy(
                    max_retries=0,
                    initial_backoff_seconds=0.0,
                    max_backoff_seconds=0.0,
                    jitter_seconds=0.0,
                ),
                cache=CachePolicy(
                    path=os.path.join(directory, "kegg.sqlite3"),
                    ttl_seconds=60,
                ),
            ),
            transport=transport,
        )
        yield client

    starts = transport.starts
    gaps = tuple(
        current.monotonic_seconds - previous.monotonic_seconds
        for previous, current in pairwise(starts)
    )
    assert all(gap >= _MIN_START_GAP_SECONDS for gap in gaps)
    assert len(starts) <= _MAX_TOTAL_REQUESTS
    assert all(
        count <= _MAX_REQUESTS_PER_OPERATION
        for count in Counter(start.operation for start in starts).values()
    )
