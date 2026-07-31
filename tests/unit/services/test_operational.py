"""Tests for redacted operational status and connectivity classification."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import NoReturn

import pytest

from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError
from kegg_mcp.kegg import InfoRequest, KeggClientConfig, KeggRequestOptions
from kegg_mcp.services.models import ConnectivityState
from kegg_mcp.services.operational import probe_kegg_connectivity_service

_NOW = datetime(2026, 7, 31, 3, 0, 0, tzinfo=UTC)


class _LocalStorageFailingClient:
    def __init__(self, error_code: ErrorCode) -> None:
        self._config = KeggClientConfig()
        self._error_code = error_code
        self.call_count = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def info(
        self,
        request: InfoRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> NoReturn:
        del request, options
        self.call_count += 1
        raise KeggMcpError(
            ErrorDetail(
                code=self._error_code,
                message="The local KEGG state could not be used safely.",
                recoverable=True,
                suggested_action="Repair the configured local state and retry.",
            )
        )


@pytest.mark.parametrize(
    "error_code",
    (ErrorCode.CACHE_FAILED, ErrorCode.LOCAL_STATE_FAILED),
)
def test_connectivity_probe_classifies_local_state_failures_separately(
    error_code: ErrorCode,
) -> None:
    client = _LocalStorageFailingClient(error_code)

    result = probe_kegg_connectivity_service(client, now=_NOW)

    assert result.state is ConnectivityState.LOCAL_STORAGE_FAILURE
    assert result.error_code is error_code
    assert result.probed_at == _NOW
    assert result.suggested_action == "Repair the configured local state and retry."
    assert client.call_count == 1
