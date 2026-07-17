"""Redacted DeepKOALA companion diagnostic and stdio dispatch tests."""

from __future__ import annotations

import json
from io import StringIO

import pytest

from deepkoala_mcp import cli
from deepkoala_mcp.config import (
    CHECKOUT_ENV,
    INPUT_ROOTS_ENV,
    OUTPUT_ROOTS_ENV,
    PYTHON_ENV,
    STATE_ROOT_ENV,
    DeepKoalaRuntimeConfig,
)


def _environment(config: DeepKoalaRuntimeConfig) -> dict[str, str]:
    return {
        CHECKOUT_ENV: str(config.checkout),
        PYTHON_ENV: str(config.python_executable),
        STATE_ROOT_ENV: str(config.state_root),
        INPUT_ROOTS_ENV: str(config.input_roots[0]),
        OUTPUT_ROOTS_ENV: str(config.output_roots[0]),
    }


def test_doctor_reports_ready_without_exposing_paths(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    output = StringIO()
    exit_code = cli.main(
        ["doctor", "--json"],
        environment=_environment(runtime_config),
        stdout=output,
    )
    document = json.loads(output.getvalue())
    assert exit_code == 0
    assert document["route_state"] == "local_ready"
    assert document["runtime_ready"] is True
    assert document["downloads_enabled"] is False
    assert document["input_root_count"] == 1
    assert document["output_root_count"] == 1
    assert document["private_paths"] == "redacted"
    assert str(runtime_config.checkout) not in output.getvalue()
    assert str(runtime_config.state_root) not in output.getvalue()


def test_doctor_rejects_bin_false_runtime(runtime_config: DeepKoalaRuntimeConfig) -> None:
    output = StringIO()
    environment = _environment(runtime_config)
    environment[PYTHON_ENV] = "/bin/false"
    exit_code = cli.main(["doctor", "--json"], environment=environment, stdout=output)
    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["route_state"] == "deepkoala_runtime_unavailable"
    assert document["runtime_ready"] is False
    assert document["downloads_enabled"] is False


def test_doctor_reports_invalid_configuration_without_mutation(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    output = StringIO()
    environment = _environment(runtime_config)
    environment[CHECKOUT_ENV] = str(runtime_config.checkout.parent / "missing")
    exit_code = cli.main(["doctor", "--json"], environment=environment, stdout=output)
    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["route_state"] == "runner_misconfigured"
    assert document["configuration_valid"] is False
    assert not runtime_config.state_root.exists()
    assert environment[CHECKOUT_ENV] not in output.getvalue()


def test_doctor_reports_missing_allowlisted_resources(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    for resource in (runtime_config.checkout / "resources").rglob("*"):
        if resource.is_file():
            resource.unlink()
    output = StringIO()
    exit_code = cli.main(
        ["doctor", "--json"],
        environment=_environment(runtime_config),
        stdout=output,
    )
    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["route_state"] == "model_resources_unavailable"
    assert document["checkout_ready"] is True
    assert document["model_resources_ready"] is False


def test_default_and_serve_commands_dispatch_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "run_stdio", lambda: calls.append("stdio"))
    assert cli.main([]) == 0
    assert cli.main(["serve"]) == 0
    assert calls == ["stdio", "stdio"]
