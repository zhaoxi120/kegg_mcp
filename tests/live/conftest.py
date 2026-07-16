"""Safety controls for the opt-in KEGG compatibility campaign."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from itertools import pairwise
from tempfile import TemporaryDirectory

import pytest

from kegg_mcp.kegg import (
    CachePolicy,
    KeggClient,
    KeggClientConfig,
    KeggClientLimits,
    RetryPolicy,
)
from kegg_mcp.kegg.transport import HttpsTransport, TransportError, TransportResponse
from kegg_mcp.mcp.config import load_runtime_config

_DEFAULT_REQUESTS_PER_OPERATION = 5
_MAX_REQUESTS_PER_OPERATION = 30
_OPERATION_COUNT = 4
_MIN_START_GAP_SECONDS = 0.95


class _BoundedLiveTransport:
    """Enforce the wire budget and stop after any failed response."""

    def __init__(self, *, max_requests: int) -> None:
        self._inner = HttpsTransport()
        self._max_requests = max_requests
        self._starts: list[float] = []
        self._circuit_open = False

    @property
    def starts(self) -> tuple[float, ...]:
        return tuple(self._starts)

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        if self._circuit_open:
            raise RuntimeError("the live KEGG circuit is open")
        if len(self._starts) >= self._max_requests:
            raise RuntimeError("the live KEGG request budget is exhausted")
        self._starts.append(time.monotonic())
        try:
            response = self._inner.request(
                url,
                timeout_seconds=timeout_seconds,
                max_response_bytes=max_response_bytes,
            )
        except TransportError:
            self._circuit_open = True
            raise
        if response.status_code != 200:
            self._circuit_open = True
        return response


@pytest.fixture(scope="session")
def live_requests_per_operation() -> int:
    """Return the bounded request count configured for each operation."""
    raw_value = os.environ.get(
        "KEGG_MCP_LIVE_REQUESTS_PER_OPERATION",
        str(_DEFAULT_REQUESTS_PER_OPERATION),
    )
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise pytest.UsageError("KEGG_MCP_LIVE_REQUESTS_PER_OPERATION must be an integer") from exc
    if not 1 <= value <= _MAX_REQUESTS_PER_OPERATION:
        raise pytest.UsageError("KEGG_MCP_LIVE_REQUESTS_PER_OPERATION must be between 1 and 30")
    return value


@pytest.fixture(scope="session")
def live_kegg_client(live_requests_per_operation: int) -> Iterator[KeggClient]:
    """Provide one one-request-per-second client backed by a temporary cache."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        raise pytest.UsageError("live KEGG tests must run in one non-xdist process")
    runtime = load_runtime_config(os.environ)

    max_requests = live_requests_per_operation * _OPERATION_COUNT
    transport = _BoundedLiveTransport(max_requests=max_requests)
    with TemporaryDirectory(prefix="kegg-mcp-live-") as directory:
        yield KeggClient(
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

    starts = transport.starts
    assert len(starts) <= max_requests
    assert all(
        current - previous >= _MIN_START_GAP_SECONDS for previous, current in pairwise(starts)
    )
