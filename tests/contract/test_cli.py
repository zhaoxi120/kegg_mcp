"""Command-line onboarding and redacted diagnostic contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path

import pytest

from kegg_mcp.kegg.cache import SQLiteKeggCache
from kegg_mcp.kegg.contracts import (
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    KeggOperation,
    RetrievalEndpointClass,
)
from kegg_mcp.mcp import cli
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    ResultStoreLimits,
    SQLiteResultStore,
)


def test_doctor_json_reports_redacted_file_handoff_state(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    output = StringIO()

    exit_code = cli.main(
        ["doctor", "--json"],
        environment={
            "KEGG_MCP_ALLOWED_ROOTS": str(shared),
        },
        stdout=output,
    )

    document = json.loads(output.getvalue())
    assert exit_code == 0
    assert document["status"] == "ok"
    assert document["configuration_valid"] is True
    assert document["access_mode"] == "public_academic"
    assert document["network_enabled"] is True
    assert document["file_handoff_enabled"] is True
    assert document["allowed_root_count"] == 1
    assert document["allowed_root_paths"] == "redacted"
    assert document["network_probe"] == "not_run"
    assert document["storage_probe"] == "not_run"
    assert str(shared) not in output.getvalue()


def test_doctor_reports_disabled_file_handoff_without_roots() -> None:
    output = StringIO()

    exit_code = cli.main(
        ["doctor"],
        environment={},
        stdout=output,
    )

    text = output.getvalue()
    assert exit_code == 0
    assert "file handoff enabled: false (0 configured roots; paths redacted)" in text
    assert "Set KEGG_MCP_ALLOWED_ROOTS" in text


def test_doctor_accepts_explicit_academic_user_test_profile() -> None:
    output = StringIO()

    exit_code = cli.main(
        ["doctor", "--json"],
        environment={
            "KEGG_MCP_ACCESS_MODE": "public_academic",
            "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "true",
        },
        stdout=output,
    )

    document = json.loads(output.getvalue())
    assert exit_code == 0
    assert document["access_mode"] == "public_academic"
    assert document["network_enabled"] is True
    assert document["network_probe"] == "not_run"
    assert document["next_actions"] == [
        "Call probe_kegg_connectivity from an MCP client before the first live analysis.",
        "Set KEGG_MCP_ALLOWED_ROOTS to enable file handoff and output bundles.",
    ]


def test_doctor_rejects_invalid_configuration_without_echoing_values() -> None:
    private_endpoint = "https://private.example.test/operator-secret"
    output = StringIO()

    exit_code = cli.main(
        ["doctor", "--json"],
        environment={
            "KEGG_MCP_ACCESS_MODE": "licensed",
            "KEGG_MCP_LICENSED_ENDPOINT": private_endpoint,
        },
        stdout=output,
    )

    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["status"] == "error"
    assert document["configuration_valid"] is False
    assert document["network_probe"] == "not_run"
    assert document["storage_probe"] == "not_run"
    assert private_endpoint not in output.getvalue()
    assert "operator-secret" not in output.getvalue()


def test_default_and_serve_commands_preserve_stdio_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "run_stdio", lambda: calls.append("stdio"))

    assert cli.main([]) == 0
    assert cli.main(["serve"]) == 0
    assert calls == ["stdio", "stdio"]


def test_cleanup_expired_is_explicit_bounded_and_path_redacted(tmp_path: Path) -> None:
    store_path = tmp_path / "private-results.sqlite3"
    now = datetime.now(UTC)
    store = SQLiteResultStore(store_path, limits=ResultStoreLimits(retention_seconds=1))
    store.create(
        "orphan-scope",
        (ResultArtifactInput(section="detail", mime_type="text/plain", content=b"private"),),
        now=now - timedelta(seconds=2),
    )
    output = StringIO()

    exit_code = cli.main(
        ["cleanup", "--expired", "--json"],
        environment={"KEGG_MCP_RESULT_STORE_PATH": str(store_path)},
        stdout=output,
    )

    document = json.loads(output.getvalue())
    assert exit_code == 0
    assert document == {
        "expired_bytes": 7,
        "expired_results": 1,
        "issue": None,
        "operation": "expired_results",
        "remaining_bytes": 0,
        "remaining_results": 0,
        "status": "ok",
    }
    assert str(store_path) not in output.getvalue()


def test_cache_status_and_cleanup_are_explicit_and_redacted(tmp_path: Path) -> None:
    cache_path = tmp_path / "private-kegg-cache.sqlite3"
    now = datetime.now(UTC)
    SQLiteKeggCache(cache_path).write(
        KeggOperation.GET,
        "/get/K00001",
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
        body=b"expired",
        retrieved_at=now - timedelta(days=8),
        expires_at=now - timedelta(days=1),
        parser_version="1",
        database_release=None,
    )
    environment = {"KEGG_MCP_CACHE_PATH": str(cache_path)}
    status_output = StringIO()

    assert (
        cli.main(
            ["cache", "status", "--json"],
            environment=environment,
            stdout=status_output,
        )
        == 0
    )
    status = json.loads(status_output.getvalue())
    assert status["status"] == "ok"
    assert status["operation"] == "cache_status"
    assert status["entry_count"] == 1
    assert status["expired_entry_count"] == 1
    assert str(cache_path) not in status_output.getvalue()
    assert PUBLIC_KEGG_ENDPOINT_FINGERPRINT not in status_output.getvalue()

    cleanup_output = StringIO()
    assert (
        cli.main(
            ["cache", "cleanup", "--expired", "--json"],
            environment=environment,
            stdout=cleanup_output,
        )
        == 0
    )
    cleanup = json.loads(cleanup_output.getvalue())
    assert cleanup["operation"] == "expired_cache_entries"
    assert cleanup["expired_entries"] == 1
    assert cleanup["remaining_entries"] == 0
    assert str(cache_path) not in cleanup_output.getvalue()
