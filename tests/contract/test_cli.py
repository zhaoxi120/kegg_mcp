"""Command-line onboarding and redacted diagnostic contracts."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from kegg_mcp.mcp import cli


def test_doctor_json_reports_redacted_file_handoff_state(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    output = StringIO()

    exit_code = cli.main(
        ["doctor", "--json"],
        environment={
            "KEGG_MCP_ACCESS_MODE": "offline_cache",
            "KEGG_MCP_ALLOWED_ROOTS": str(shared),
        },
        stdout=output,
    )

    document = json.loads(output.getvalue())
    assert exit_code == 0
    assert document["status"] == "ok"
    assert document["configuration_valid"] is True
    assert document["access_mode"] == "offline_cache"
    assert document["network_enabled"] is False
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
        environment={"KEGG_MCP_ACCESS_MODE": "offline_cache"},
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
