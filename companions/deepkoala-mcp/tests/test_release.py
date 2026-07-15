"""Offline distribution and release-boundary checks for the companion."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath
from typing import cast

COMPANION_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = COMPANION_ROOT.parents[1]
FORBIDDEN_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".db",
    ".faa",
    ".fasta",
    ".fna",
    ".h3f",
    ".h3i",
    ".h3m",
    ".h3p",
    ".hmm",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_PARTS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
}
CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
LOCAL_HOME_MARKER = "/" + "lab" + "/zhaoxi/"
MANUAL_CHECKOUT_MARKER = "/tmp/" + "deepkoala-cpu-debug"
KEGG_HOST_MARKER = "rest" + ".kegg.jp"


def _project_table() -> dict[str, object]:
    document = tomllib.loads((COMPANION_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cast(dict[str, object], document["project"])


def _assert_safe_member(name: str) -> None:
    member = PurePosixPath(name)
    assert not member.is_absolute()
    assert ".." not in member.parts
    assert not FORBIDDEN_PARTS.intersection(member.parts)
    assert member.suffix.lower() not in FORBIDDEN_SUFFIXES


def test_metadata_and_lock_define_an_independent_lightweight_distribution() -> None:
    document = tomllib.loads((COMPANION_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = _project_table()
    dependencies = cast(list[str], project["dependencies"])
    scripts = cast(dict[str, str], project["scripts"])
    tool = cast(dict[str, object], document["tool"])
    pyright = cast(dict[str, object], tool["pyright"])

    assert project["name"] == "deepkoala-mcp"
    assert project["version"] == "0.1.0"
    assert project["requires-python"] == ">=3.11,<3.12"
    assert project["license"] == "MIT"
    assert scripts == {"deepkoala-mcp": "deepkoala_mcp.server:main"}
    assert dependencies == ["mcp>=1.27,<2", "pydantic>=2.12,<3"]
    assert pyright["include"] == ["src", "tests"]
    assert pyright["venv"] == ".venv"
    assert pyright["venvPath"] == "."
    assert not any(
        forbidden in dependency.lower()
        for dependency in dependencies
        for forbidden in ("deepkoala", "kegg-mcp", "torch")
    )

    lock_text = (COMPANION_ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'requires-python = "==3.11.*"' in lock_text.splitlines()[:5]
    lock = tomllib.loads(lock_text)
    packages = cast(list[dict[str, object]], lock["package"])
    locked_project = next(package for package in packages if package.get("name") == project["name"])
    assert locked_project["version"] == project["version"]
    assert not any(
        package.get("name") in {"deepkoala", "kegg-mcp", "torch"} for package in packages
    )


def test_companion_owned_text_is_english_bounded_and_contains_no_local_artifacts() -> None:
    files = tuple(
        path
        for path in COMPANION_ROOT.rglob("*")
        if path.is_file()
        and not FORBIDDEN_PARTS.intersection(path.relative_to(COMPANION_ROOT).parts)
    )
    assert files
    for path in files:
        assert path.suffix.lower() not in FORBIDDEN_SUFFIXES, path
        assert path.stat().st_size <= 5 * 1024 * 1024, path
        if path.suffix in {".md", ".py", ".toml"} or path.name == "LICENSE":
            text = path.read_text(encoding="utf-8")
            assert not CJK_CHARACTER.search(text), path
            assert LOCAL_HOME_MARKER not in text, path

    tests_text = "\n".join(
        path.read_text(encoding="utf-8") for path in (COMPANION_ROOT / "tests").glob("*.py")
    )
    assert MANUAL_CHECKOUT_MARKER not in tests_text
    assert KEGG_HOST_MARKER not in tests_text


def test_offline_build_contains_only_the_companion(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "release builds require the locked uv toolchain"

    source = tmp_path / "source"
    output = tmp_path / "dist"
    shutil.copytree(
        COMPANION_ROOT,
        source,
        ignore=shutil.ignore_patterns(*FORBIDDEN_PARTS, "*.egg-info"),
    )
    environment = os.environ.copy()
    environment["UV_OFFLINE"] = "1"
    environment["UV_PYTHON_DOWNLOADS"] = "never"
    subprocess.run(
        [
            uv,
            "build",
            "--offline",
            "--no-python-downloads",
            "--no-sources",
            "--no-progress",
            "--no-create-gitignore",
            "--out-dir",
            str(output),
        ],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    wheels = tuple(output.glob("*.whl"))
    sdists = tuple(output.glob("*.tar.gz"))
    assert len(wheels) == 1
    assert len(sdists) == 1
    expected_license = (COMPANION_ROOT / "LICENSE").read_bytes()

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = tuple(wheel.namelist())
        for name in names:
            _assert_safe_member(name)
            assert wheel.getinfo(name).file_size <= 5 * 1024 * 1024
        assert "deepkoala_mcp/server.py" in names
        assert all("kegg_mcp" not in PurePosixPath(name).parts for name in names)
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        assert "deepkoala-mcp = deepkoala_mcp.server:main" in wheel.read(entry_points_name).decode(
            "utf-8"
        )
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = wheel.read(metadata_name).decode("utf-8")
        assert "License-Expression: MIT" in metadata.splitlines()
        assert "Requires-Dist: torch" not in metadata
        assert "Requires-Dist: deepkoala" not in metadata
        assert "Requires-Dist: kegg-mcp" not in metadata
        license_names = tuple(
            name for name in names if PurePosixPath(name).name == "LICENSE" and ".dist-info" in name
        )
        assert len(license_names) == 1
        assert wheel.read(license_names[0]) == expected_license

    with tarfile.open(sdists[0], mode="r:gz") as sdist:
        members = tuple(sdist.getmembers())
        for member in members:
            _assert_safe_member(member.name)
            assert member.size <= 5 * 1024 * 1024
        regular_members = tuple(member for member in members if member.isfile())
        assert any(PurePosixPath(member.name).name == "LICENSE" for member in regular_members)
        assert all("kegg_mcp" not in PurePosixPath(member.name).parts for member in regular_members)
