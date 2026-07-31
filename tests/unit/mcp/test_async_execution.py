"""Event-loop boundaries for synchronous Core MCP services."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import cast

import pytest
from anyio import CancelScope, Event, create_task_group, fail_after, sleep
from mcp.server.lowlevel.helper_types import ReadResourceContents
from pydantic import AnyUrl

import kegg_mcp.mcp.resources as resource_module
from kegg_mcp.kegg import (
    CachePolicy,
    KeggClient,
    KeggClientConfig,
    PublicAcademicAccess,
    RateLimitPolicy,
    RetryPolicy,
)
from kegg_mcp.kegg.transport import TransportError, TransportErrorKind, TransportResponse
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.tool_registry import dispatch_tool
from kegg_mcp.services.reference_budget import KeggMcpClient
from kegg_mcp.services.result_store import ResultStoreError, SQLiteResultStore


class _BlockingTransport:
    def __init__(self, *, fail_after_release: bool = False) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.worker_thread_id: int | None = None
        self.fail_after_release = fail_after_release

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del url, timeout_seconds, max_response_bytes
        self.worker_thread_id = threading.get_ident()
        self.entered.set()
        if not self.release.wait(timeout=3):
            raise AssertionError("blocking transport was not released")
        if self.fail_after_release:
            raise TransportError(TransportErrorKind.CONNECTION, transient=False)
        return TransportResponse(
            status_code=200,
            body=b"ENTRY       K00844                      KO\nNAME        Synthetic KO\n///\n",
        )


class _CountingClient:
    def __init__(self) -> None:
        self.call_count = 0

    def find(self, *_args: object, **_kwargs: object) -> None:
        self.call_count += 1
        raise AssertionError("client call must not start when result-store preflight fails")


class _PreflightFailingResultStore(SQLiteResultStore):
    def preflight_write(self) -> None:
        raise ResultStoreError("preflight_write")


def _runtime(tmp_path: Path, transport: _BlockingTransport) -> McpRuntime:
    return McpRuntime(
        client=KeggClient(
            KeggClientConfig(
                access=PublicAcademicAccess(academic_use_confirmed=True),
                cache=CachePolicy(path=str(tmp_path / "kegg.sqlite3")),
                rate_limit=RateLimitPolicy(state_root=str(tmp_path / "rate-limit")),
                retry=RetryPolicy(max_retries=0),
            ),
            transport=transport,
        ),
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id="async-execution-scope",
    )


@pytest.mark.asyncio
async def test_retained_network_tool_preflights_store_before_client_call(
    tmp_path: Path,
) -> None:
    client = _CountingClient()
    runtime = McpRuntime(
        client=cast(KeggMcpClient, client),
        result_store=_PreflightFailingResultStore(tmp_path / "results.sqlite3"),
        scope_id="preflight-failure-scope",
    )

    result = await dispatch_tool(
        "search_kegg_entries",
        {"database": "ko", "query": "glycolysis", "mode": "keyword"},
        runtime,
    )

    assert result.isError is True
    assert result.structuredContent is not None
    assert result.structuredContent["error"]["code"] == "RESULT_STORE_FAILED"
    assert client.call_count == 0


@pytest.mark.asyncio
async def test_slow_kegg_request_does_not_block_status_or_event_loop(tmp_path: Path) -> None:
    transport = _BlockingTransport()
    runtime = _runtime(tmp_path, transport)
    event_loop_thread_id = threading.get_ident()
    get_task = asyncio.create_task(
        dispatch_tool(
            "get_kegg_entries",
            {"entries": [{"database": "ko", "identifier": "K00844"}]},
            runtime,
        )
    )

    try:
        entered = await asyncio.wait_for(
            asyncio.to_thread(transport.entered.wait, 1),
            timeout=1.5,
        )
        assert entered is True

        status = await asyncio.wait_for(
            dispatch_tool("get_server_status", {}, runtime),
            timeout=1,
        )
        assert status.isError is False
        assert transport.worker_thread_id != event_loop_thread_id
    finally:
        transport.release.set()
        result = await asyncio.wait_for(get_task, timeout=2)

    assert result.isError is False


@pytest.mark.parametrize("worker_fails", [False, True])
@pytest.mark.asyncio
async def test_cancelled_request_waits_for_worker_then_propagates(
    tmp_path: Path,
    worker_fails: bool,
) -> None:
    transport = _BlockingTransport(fail_after_release=worker_fails)
    runtime = _runtime(tmp_path, transport)
    cancel_scopes: list[CancelScope] = []
    returned_results: list[object] = []
    invocation_finished = Event()

    async def invoke() -> None:
        try:
            with CancelScope() as cancel_scope:
                cancel_scopes.append(cancel_scope)
                returned_results.append(
                    await dispatch_tool(
                        "get_kegg_entries",
                        {"entries": [{"database": "ko", "identifier": "K00844"}]},
                        runtime,
                    )
                )
        finally:
            invocation_finished.set()

    async with create_task_group() as task_group:
        task_group.start_soon(invoke)
        try:
            while not cancel_scopes:
                await sleep(0)
            entered = await asyncio.wait_for(
                asyncio.to_thread(transport.entered.wait, 1),
                timeout=1.5,
            )
            assert entered is True

            cancel_scopes[0].cancel()
            await sleep(0)
            assert returned_results == []
            assert invocation_finished.is_set() is False
        finally:
            transport.release.set()

    assert returned_results == []
    assert invocation_finished.is_set() is True
    assert runtime.result_store.list_results(runtime.scope_id).total_items == 0


@pytest.mark.asyncio
async def test_cancelled_request_waiting_for_client_capacity_never_starts(tmp_path: Path) -> None:
    transport = _BlockingTransport()
    runtime = _runtime(tmp_path, transport)
    cancel_scopes: list[CancelScope] = []
    returned_results: list[object] = []

    async def invoke() -> None:
        with CancelScope() as cancel_scope:
            cancel_scopes.append(cancel_scope)
            returned_results.append(
                await dispatch_tool(
                    "get_kegg_entries",
                    {"entries": [{"database": "ko", "identifier": "K00844"}]},
                    runtime,
                )
            )

    async def wait_until_queued() -> None:
        while runtime.client_handler_limiter.statistics().tasks_waiting != 1:
            await sleep(0)

    with fail_after(2):
        async with runtime.client_handler_limiter, create_task_group() as task_group:
            task_group.start_soon(invoke)
            await asyncio.wait_for(wait_until_queued(), timeout=1)
            cancel_scopes[0].cancel()

    assert transport.entered.is_set() is False
    assert returned_results == []


@pytest.mark.asyncio
async def test_local_handoff_does_not_wait_for_kegg_client_capacity(tmp_path: Path) -> None:
    client = _CountingClient()
    runtime = McpRuntime(
        client=cast(KeggMcpClient, client),
        result_store=SQLiteResultStore(tmp_path / "results-local-handoff.sqlite3"),
        scope_id="local-handoff-scope",
        allowed_roots=(str(tmp_path),),
    )

    async with runtime.client_handler_limiter:
        result = await asyncio.wait_for(
            dispatch_tool(
                "prepare_kegg_handoff",
                {
                    "output_directory": str(tmp_path / "syntax-handoff"),
                    "handoff": {
                        "target": "syntax_ko_composition",
                        "ko_ids": ["K00001"],
                    },
                },
                runtime,
            ),
            timeout=1,
        )

    assert result.isError is False
    assert client.call_count == 0


@pytest.mark.asyncio
async def test_resource_storage_work_runs_outside_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _BlockingTransport()
    runtime = _runtime(tmp_path, transport)
    event_loop_thread_id = threading.get_ident()
    worker_thread_ids: list[int] = []

    def fake_read_resource(
        value: str,
        actual_runtime: McpRuntime,
    ) -> list[ReadResourceContents]:
        assert value == "ko-analysis://status"
        assert actual_runtime is runtime
        worker_thread_ids.append(threading.get_ident())
        return []

    monkeypatch.setattr(resource_module, "_read_resource", fake_read_resource)

    assert await resource_module.read_resource(AnyUrl("ko-analysis://status"), runtime) == []
    assert len(worker_thread_ids) == 1
    assert worker_thread_ids[0] != event_loop_thread_id
