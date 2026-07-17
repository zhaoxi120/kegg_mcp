"""Redacted DeepKOALA companion diagnostic and stdio dispatch tests."""

from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from deepkoala_mcp import cli
from deepkoala_mcp.config import (
    ALLOWED_ROOTS_ENV,
    CHECKOUT_ENV,
    PYTHON_ENV,
    STATE_ROOT_ENV,
    DeepKoalaRuntimeConfig,
)


def _environment(config: DeepKoalaRuntimeConfig) -> dict[str, str]:
    return {
        CHECKOUT_ENV: str(config.checkout),
        PYTHON_ENV: str(config.python_executable),
        STATE_ROOT_ENV: str(config.state_root),
        ALLOWED_ROOTS_ENV: str(config.allowed_roots[0]),
    }


def test_doctor_reports_ready_without_exposing_paths(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runtime_config.state_root.mkdir(mode=0o700)
    output = StringIO()

    exit_code = cli.main(
        ["doctor", "--json"],
        environment=_environment(runtime_config),
        stdout=output,
    )

    document = json.loads(output.getvalue())
    assert exit_code == 0
    assert document["status"] == "ok"
    assert document["route_state"] == "local_ready"
    assert document["configuration_valid"] is True
    assert document["downloads_required"] is False
    assert document["core_handoff_check"] == "operator_required"
    assert document["private_paths"] == "redacted"
    assert str(runtime_config.checkout) not in output.getvalue()
    assert str(runtime_config.state_root) not in output.getvalue()


def test_doctor_distinguishes_missing_checkout_without_mutation(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    output = StringIO()
    environment = _environment(runtime_config)
    environment[CHECKOUT_ENV] = str(runtime_config.checkout.parent / "missing")

    exit_code = cli.main(["doctor", "--json"], environment=environment, stdout=output)

    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["route_state"] == "deepkoala_checkout_missing"
    assert document["downloads_required"] is True
    assert not runtime_config.state_root.exists()
    assert environment[CHECKOUT_ENV] not in output.getvalue()


def test_doctor_distinguishes_invalid_checkout_from_missing_resources(
    runtime_config: DeepKoalaRuntimeConfig,
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    invalid_checkout = tmp_path / "invalid-checkout"
    invalid_checkout.mkdir()
    environment = _environment(runtime_config)
    environment[CHECKOUT_ENV] = str(invalid_checkout)
    environment[STATE_ROOT_ENV] = str(state)
    output = StringIO()

    exit_code = cli.main(["doctor", "--json"], environment=environment, stdout=output)

    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["route_state"] == "runner_misconfigured"
    assert document["checkout_ready"] is False
    assert document["model_resources_ready"] is False
    assert str(invalid_checkout) not in output.getvalue()


def test_doctor_reports_missing_python_after_checkout_validation(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    runtime_config.state_root.mkdir(mode=0o700)
    environment = _environment(runtime_config)
    environment[PYTHON_ENV] = str(runtime_config.checkout / "missing-python")
    output = StringIO()

    exit_code = cli.main(["doctor", "--json"], environment=environment, stdout=output)

    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["route_state"] == "deepkoala_python_missing"
    assert document["checkout_ready"] is True
    assert document["python_ready"] is False
    assert document["downloads_required"] is True


def test_doctor_reports_missing_resources_without_changing_checkout(
    runtime_config: DeepKoalaRuntimeConfig,
) -> None:
    for resource in (runtime_config.checkout / "resources").rglob("*"):
        if resource.is_file():
            resource.unlink()
    runtime_config.state_root.mkdir(mode=0o700)
    environment = _environment(runtime_config)
    environment[PYTHON_ENV] = sys.executable
    output = StringIO()

    exit_code = cli.main(["doctor", "--json"], environment=environment, stdout=output)

    document = json.loads(output.getvalue())
    assert exit_code == 2
    assert document["route_state"] == "model_resources_missing"
    assert document["checkout_ready"] is True
    assert document["model_resources_ready"] is False
    assert document["downloads_required"] is True


def test_default_and_serve_commands_dispatch_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "run_stdio", lambda: calls.append("stdio"))

    assert cli.main([]) == 0
    assert cli.main(["serve"]) == 0
    assert calls == ["stdio", "stdio"]
