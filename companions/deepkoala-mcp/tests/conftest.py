"""Small local fixtures for companion tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from deepkoala_mcp.config import DeepKoalaRuntimeConfig


def build_checkout(root: Path, *, cli_source: str = "# test CLI\n") -> Path:
    """Create the bounded official-layout surface inspected by the companion."""
    checkout = root / "deepkoala-checkout"
    package = checkout / "deepkoala"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "utils.py").write_text("def resolve_device(value): return value\n", encoding="utf-8")
    (package / "cli.py").write_text(cli_source, encoding="utf-8")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "deepkoala"\nversion = "0.1-test"\n',
        encoding="utf-8",
    )
    for date in ("202401", "202502"):
        resources = checkout / "resources" / date
        resources.mkdir(parents=True)
        for model in ("full", "frag"):
            (resources / f"weights_{model}.pt").write_bytes(model.encode("ascii"))
            (resources / f"ko_config_{model}.json").write_text("{}", encoding="utf-8")
    return checkout.resolve()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    return build_checkout(tmp_path)


@pytest.fixture
def runtime_config(tmp_path: Path, checkout: Path) -> DeepKoalaRuntimeConfig:
    allowed = tmp_path / "inputs"
    allowed.mkdir()
    return DeepKoalaRuntimeConfig(
        checkout=checkout,
        python_executable=Path(sys.executable).resolve(),
        state_root=(tmp_path / "state").resolve(),
        allowed_roots=(allowed.resolve(),),
        cpu_threads=2,
        max_queue_size=2,
        default_timeout_seconds=30,
        plan_ttl_seconds=30,
        retention_seconds=30,
    )
