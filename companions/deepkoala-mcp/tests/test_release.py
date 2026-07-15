"""Independent-distribution and lean-scope release checks."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import deepkoala_mcp

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "src" / "deepkoala_mcp"


def test_distribution_has_no_inference_or_download_dependency() -> None:
    document = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    assert project["name"] == "deepkoala-mcp"
    assert project["version"] == deepkoala_mcp.__version__
    dependencies = " ".join(project["dependencies"]).lower()
    for forbidden in ("torch", "deepkoala", "requests", "httpx", "aiohttp"):
        assert forbidden not in dependencies


def test_source_uses_fixed_cpu_launch_without_a_network_client() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py"))
    lowered = corpus.lower()
    assert "shell=true" not in lowered
    assert re.search(r"\b(requests|httpx|aiohttp|urllib\.request)\b", lowered) is None
    assert "create_subprocess_exec" in corpus
    assert '"--device",\n        "cpu"' in corpus
    assert '"--num_workers",\n        "0"' in corpus
