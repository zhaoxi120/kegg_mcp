"""Independent-distribution and lean-scope release checks."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

import deepkoala_mcp

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "src" / "deepkoala_mcp"
FORBIDDEN_ARCHIVE_SUFFIXES = {
    ".bin",
    ".ckpt",
    ".csv",
    ".db",
    ".faa",
    ".fasta",
    ".fna",
    ".hmm",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_ARCHIVE_PARTS = {
    ".git",
    ".pytest_cache",
    ".pyright",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "resources",
    "state",
}


def _assert_safe_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    assert not path.is_absolute()
    assert ".." not in path.parts
    assert not FORBIDDEN_ARCHIVE_PARTS.intersection(path.parts)
    assert path.suffix.lower() not in FORBIDDEN_ARCHIVE_SUFFIXES


def _private_workspace_marker() -> bytes:
    return os.fsencode(PROJECT.parents[2]) + b"/"


def test_distribution_has_no_inference_or_download_dependency() -> None:
    document = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))
    project = document["project"]
    assert project["name"] == "deepkoala-mcp"
    assert project["version"] == deepkoala_mcp.__version__
    assert "anyio>=4.10,<5" in project["dependencies"]
    dependencies = " ".join(project["dependencies"]).lower()
    for forbidden in ("torch", "deepkoala", "requests", "httpx", "aiohttp"):
        assert forbidden not in dependencies


def test_source_uses_validated_device_launch_without_a_network_client() -> None:
    corpus = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py"))
    lowered = corpus.lower()
    assert "shell=true" not in lowered
    assert re.search(r"\b(requests|httpx|aiohttp|urllib\.request)\b", lowered) is None
    assert "create_subprocess_exec" in corpus
    assert '"--device",\n        plan.device' in corpus
    assert '"--device",\n        "auto"' not in corpus
    assert '"--num_workers",\n        "0"' in corpus
    assert '"CUDA_VISIBLE_DEVICES": ""' not in corpus
    assert "PR_SET_PDEATHSIG" in corpus
    assert "os.fork()" in corpus
    assert "pass_fds=parent_guard.passed_fds" in corpus


def test_offline_build_archives_only_the_companion(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    source = tmp_path / "source"
    output = tmp_path / "dist"
    shutil.copytree(
        PROJECT,
        source,
        ignore=shutil.ignore_patterns(
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            ".pyright",
            "__pycache__",
            "build",
            "dist",
            "*.egg-info",
        ),
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
    expected_license = (PROJECT / "LICENSE").read_bytes()

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = tuple(wheel.namelist())
        assert "deepkoala_mcp/cli.py" in names
        assert "deepkoala_mcp/runner.py" in names
        assert all("tests" not in PurePosixPath(name).parts for name in names)
        for name in names:
            _assert_safe_archive_name(name)
            assert wheel.getinfo(name).file_size <= 5 * 1024 * 1024
            assert _private_workspace_marker() not in wheel.read(name)
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = wheel.read(metadata_name).decode("utf-8").lower()
        assert "requires-dist: torch" not in metadata
        assert "requires-dist: deepkoala" not in metadata
        license_names = tuple(
            name for name in names if PurePosixPath(name).name == "LICENSE" and ".dist-info" in name
        )
        assert len(license_names) == 1
        assert wheel.read(license_names[0]) == expected_license

    with tarfile.open(sdists[0], mode="r:gz") as sdist:
        members = tuple(sdist.getmembers())
        license_members = tuple(
            member
            for member in members
            if member.isfile() and PurePosixPath(member.name).name == "LICENSE"
        )
        assert len(license_members) == 1
        packaged_license = sdist.extractfile(license_members[0])
        assert packaged_license is not None
        assert packaged_license.read() == expected_license
        for member in members:
            _assert_safe_archive_name(member.name)
            assert member.size <= 5 * 1024 * 1024
            if member.isfile():
                extracted = sdist.extractfile(member)
                assert extracted is not None
                assert _private_workspace_marker() not in extracted.read()
