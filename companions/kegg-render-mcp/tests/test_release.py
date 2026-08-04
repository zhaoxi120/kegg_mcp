"""Independent distribution and synthetic-only release boundary tests."""

from __future__ import annotations

import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import kegg_render_mcp

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "src" / "kegg_render_mcp"


def test_distribution_declares_compatible_core_without_annotation_or_browser_stack() -> None:
    document = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    assert project["name"] == "kegg-render-mcp"
    assert project["version"] == kegg_render_mcp.__version__
    assert "Operating System :: MacOS :: MacOS X" in project["classifiers"]
    assert "Operating System :: POSIX :: Linux" in project["classifiers"]
    assert not any("Windows" in classifier for classifier in project["classifiers"])
    dependencies = " ".join(project["dependencies"]).lower()
    assert "anyio>=4.10,<5" in dependencies
    assert "kegg-mcp>=0.5,<0.9" in dependencies
    for forbidden in ("deepkoala", "torch", "selenium", "playwright", "cairosvg"):
        assert forbidden not in dependencies


def test_source_has_no_subprocess_shell_or_independent_network_client() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py"))
    lowered = corpus.lower()
    assert "shell=true" not in lowered
    assert "subprocess" not in lowered
    assert re.search(r"\b(requests|httpx|aiohttp|urllib\.request)\b", lowered) is None
    assert "from kegg_mcp.kegg import" in corpus


def test_package_contains_license_and_no_static_kegg_payload() -> None:
    assert (PROJECT / "LICENSE").is_file()
    forbidden_suffixes = {".kgml", ".sqlite", ".sqlite3", ".db", ".pt", ".pth"}
    assert not [
        path
        for path in PROJECT.rglob("*")
        if ".venv" not in path.parts and path.suffix.lower() in forbidden_suffixes
    ]
    assert not [path for path in PROJECT.rglob("*.png") if ".venv" not in path.parts]


def test_wheel_and_sdist_audit_excludes_payloads_and_other_implementations(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(tmp_path),
            str(PROJECT),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    sdist = next(tmp_path.glob("*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(sdist) as archive:
        sdist_names = archive.getnames()
    expected_sdist_tests = {
        path.relative_to(PROJECT).as_posix()
        for path in (PROJECT / "tests").glob("*.py")
        if path.name != "test_synthetic_pipeline.py"
    }
    assert all(
        any(name.endswith(f"/{relative}") for name in sdist_names)
        for relative in expected_sdist_tests
    )
    assert not any(name.endswith("/tests/test_synthetic_pipeline.py") for name in sdist_names)
    for names in (wheel_names, sdist_names):
        lowered = "\n".join(names).lower()
        assert "deepkoala_mcp" not in lowered
        assert "/kegg_mcp/" not in lowered
        for suffix in (".png", ".kgml", ".sqlite", ".sqlite3", ".db", ".pt", ".pth"):
            assert not any(name.lower().endswith(suffix) for name in names)
