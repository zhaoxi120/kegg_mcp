"""Offline release and distribution policy checks."""

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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNED_RELEASE_FILES = (
    PROJECT_ROOT / "CHANGELOG.md",
    PROJECT_ROOT / "docs" / "mcp-benchmark-review.md",
    PROJECT_ROOT / "docs" / "installation.md",
    PROJECT_ROOT / "docs" / "release-readiness.md",
    PROJECT_ROOT / "docs" / "skill-evaluation.md",
    PROJECT_ROOT / "docs" / "troubleshooting.md",
    PROJECT_ROOT / "companions" / "deepkoala-mcp" / "README.md",
    PROJECT_ROOT / "tests" / "live" / "README.md",
    PROJECT_ROOT / "examples" / "README.md",
    PROJECT_ROOT / "examples" / "plain-ko" / "ko-list.txt",
    PROJECT_ROOT / "examples" / "plain-ko" / "clean-ko-list.txt",
    PROJECT_ROOT / "examples" / "config" / "offline.env.example",
    PROJECT_ROOT / "examples" / "config" / "public-academic.env.example",
    PROJECT_ROOT / "examples" / "config" / "licensed.env.example",
)
FORBIDDEN_DISTRIBUTION_SUFFIXES = {
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
FORBIDDEN_ARCHIVE_PARTS = {
    ".git",
    ".kegg-cache",
    ".pytest_cache",
    ".pyright",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "results",
}
CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
PYTHON_REQUIRES = ">=3.11,<3.12"


def _project_table() -> dict[str, object]:
    document = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cast(dict[str, object], document["project"])


def _candidate_files() -> tuple[Path, ...]:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(
        path
        for relative in completed.stdout.split("\0")
        if relative
        if (path := PROJECT_ROOT / relative).is_file()
    )


def _assert_safe_archive_name(name: str) -> None:
    path = PurePosixPath(name)
    assert not path.is_absolute()
    assert ".." not in path.parts
    assert not FORBIDDEN_ARCHIVE_PARTS.intersection(path.parts)
    assert path.suffix.lower() not in FORBIDDEN_DISTRIBUTION_SUFFIXES


def test_project_metadata_declares_buildable_stdio_package() -> None:
    project = _project_table()
    scripts = cast(dict[str, str], project["scripts"])

    assert project["name"] == "kegg-mcp"
    assert project["version"] == "0.2.0"
    assert project["requires-python"] == PYTHON_REQUIRES
    assert project["license"] == "MIT"
    assert scripts == {"kegg-mcp": "kegg_mcp.mcp.cli:main"}
    lock_text = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")
    lock_header = lock_text.splitlines()[:5]
    assert 'requires-python = "==3.11.*"' in lock_header
    lock_document = tomllib.loads(lock_text)
    locked_packages = cast(list[dict[str, object]], lock_document["package"])
    locked_project = next(
        package for package in locked_packages if package.get("name") == project["name"]
    )
    assert locked_project["version"] == project["version"]


def test_release_documents_and_synthetic_examples_are_english_and_bounded() -> None:
    assert all(path.is_file() for path in OWNED_RELEASE_FILES)

    for path in OWNED_RELEASE_FILES:
        content = path.read_text(encoding="utf-8")
        assert not CJK_CHARACTER.search(content), path
        assert path.stat().st_size <= 128 * 1024, path
        assert "SQLite format 3" not in content


def test_access_examples_require_explicit_rights_and_use_no_secrets() -> None:
    offline = (PROJECT_ROOT / "examples/config/offline.env.example").read_text(encoding="utf-8")
    academic = (PROJECT_ROOT / "examples/config/public-academic.env.example").read_text(
        encoding="utf-8"
    )
    licensed = (PROJECT_ROOT / "examples/config/licensed.env.example").read_text(encoding="utf-8")

    assert "KEGG_MCP_ACCESS_MODE=offline_cache" in offline
    assert "CONFIRMED=true" not in offline
    assert "KEGG_MCP_ACCESS_MODE=public_academic" in academic
    assert "KEGG_MCP_ACADEMIC_USE_CONFIRMED=true" in academic
    assert "KEGG_MCP_ACCESS_MODE=licensed" in licensed
    assert "KEGG_MCP_LICENSED_USE_CONFIRMED=true" in licensed
    assert "https://kegg.example.edu/api" in licensed
    assert "rest.kegg.jp" not in licensed
    assert not re.search(r"(?im)^(?:password|secret|token|api_key)=\S+", licensed)


def test_ko_examples_are_inputs_without_kegg_payloads_or_biological_claims() -> None:
    example_root = PROJECT_ROOT / "examples"
    files = tuple(path for path in example_root.rglob("*") if path.is_file())

    assert files
    assert all(path.suffix.lower() not in FORBIDDEN_DISTRIBUTION_SUFFIXES for path in files)
    assert all(path.stat().st_size <= 128 * 1024 for path in files)
    assert (example_root / "plain-ko/ko-list.txt").read_text(encoding="utf-8").splitlines() == [
        " K00001",
        "ko:K00002",
        "K00001",
        "NOT_A_KO",
    ]
    assert (example_root / "plain-ko/clean-ko-list.txt").read_text(
        encoding="utf-8"
    ).splitlines() == ["K00001", "K00002", "K00003"]


def test_candidate_tree_contains_no_tracked_release_blocking_binary() -> None:
    candidate_files = _candidate_files()
    assert candidate_files

    for path in candidate_files:
        assert path.suffix.lower() not in FORBIDDEN_DISTRIBUTION_SUFFIXES, path
        assert path.stat().st_size <= 5 * 1024 * 1024, path

    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.zh-CN.md" in ignore
    assert all(not path.name.endswith(".zh-CN.md") for path in candidate_files)


def test_rights_and_release_status_are_prominent() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for required in ("public_academic", "licensed", "offline_cache"):
        assert required in installation
    assert "Python 3.11.x" in installation
    assert "https://www.kegg.jp/kegg/rest/" in installation
    assert "https://www.kegg.jp/kegg/legal.html" in installation
    offline_licensed = (
        "KEGG_MCP_ACCESS_MODE=offline_cache\n"
        "KEGG_MCP_LICENSED_ENDPOINT=https://kegg.example.edu/api\n"
        "KEGG_MCP_LICENSED_USE_CONFIRMED=true"
    )
    assert offline_licensed in installation
    assert "network access remains disabled" in installation
    assert "Ordinary tool calls do not expose a" in installation
    assert "KEGG_MCP_ALLOWED_ROOTS" in installation
    assert "KEGG_MCP_ACCESS_MODE=public_academic" in readme
    assert "KEGG_MCP_ACADEMIC_USE_CONFIRMED=true" in readme
    assert "export KEGG_MCP_ACCESS_MODE=offline_cache" in readme
    assert "KEGG_MCP_ACCESS_MODE: offline_cache" in ci
    assert "probe connectivity once" in installation
    assert "Retrieve only KO entry `K00844`" in installation
    assert "Current status:" in readiness
    assert "exact commit" in readiness
    normalized_changelog = re.sub(r"\s+", " ", changelog.lower())
    assert "## [0.2.0] - 2026-07-15" in changelog
    assert "## [unreleased]" in normalized_changelog


def test_skill_evaluation_record_distinguishes_static_tests_from_forward_review() -> None:
    record = (PROJECT_ROOT / "docs/skill-evaluation.md").read_text(encoding="utf-8")

    assert "not a runtime LLM evaluation" in record
    assert "independent forward/manual review" in record
    assert record.count("Observed route: passed.") == 6
    assert "exact v0.2.0 candidate" in re.sub(r"\s+", " ", record)


def test_distribution_boundary_is_explicit() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")

    for document in (readme, installation, readiness):
        assert "Python wheel" in document
        assert "repository-scoped Skill" in document
        assert "tag source archive" in document
    normalized_readme = re.sub(r"\s+", " ", readme)
    normalized_installation = re.sub(r"\s+", " ", installation)
    normalized_readiness = re.sub(r"\s+", " ", readiness)
    assert "installing the wheel alone does not make the Skill available" in normalized_readme
    assert "does not install this repository-scoped Skill" in normalized_installation
    assert "do not install that Skill" in normalized_readiness


def test_offline_build_produces_auditable_safe_archives(tmp_path: Path) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "release builds require the locked uv toolchain"

    source = tmp_path / "source"
    output = tmp_path / "dist"
    shutil.copytree(
        PROJECT_ROOT,
        source,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".pytest_cache",
            ".ruff_cache",
            ".pyright",
            "__pycache__",
            "build",
            "dist",
            "results",
            "*.egg-info",
            "*.sqlite3*",
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
    expected_license = (PROJECT_ROOT / "LICENSE").read_bytes()

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = tuple(wheel.namelist())
        for name in names:
            _assert_safe_archive_name(name)
            assert wheel.getinfo(name).file_size <= 5 * 1024 * 1024
        assert "kegg_mcp/mcp/server.py" in names
        assert "kegg_mcp/mcp/cli.py" in names
        entry_points_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entry_points = wheel.read(entry_points_name).decode("utf-8")
        assert "kegg-mcp = kegg_mcp.mcp.cli:main" in entry_points
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = wheel.read(metadata_name).decode("utf-8")
        assert "License-Expression: MIT" in metadata.splitlines()
        assert "Requires-Python: <3.12,>=3.11" in metadata.splitlines()
        license_names = tuple(
            name for name in names if PurePosixPath(name).name == "LICENSE" and ".dist-info" in name
        )
        assert len(license_names) == 1
        assert wheel.read(license_names[0]) == expected_license
        assert b"Permission is hereby granted, free of charge" in expected_license
        assert all(".agents" not in PurePosixPath(name).parts for name in names)
        assert all("docs" not in PurePosixPath(name).parts for name in names)
        assert all("examples" not in PurePosixPath(name).parts for name in names)
        assert all("companions" not in PurePosixPath(name).parts for name in names)
        assert all("deepkoala_mcp" not in PurePosixPath(name).parts for name in names)
        assert all(b"/lab/zhaoxi/" not in wheel.read(name) for name in names)

    with tarfile.open(sdists[0], mode="r:gz") as sdist:
        members = tuple(sdist.getmembers())
        for member in members:
            _assert_safe_archive_name(member.name)
            assert member.size <= 5 * 1024 * 1024
        regular_members = tuple(member for member in members if member.isfile())
        license_members = tuple(
            member for member in regular_members if PurePosixPath(member.name).name == "LICENSE"
        )
        assert len(license_members) == 1
        packaged_license = sdist.extractfile(license_members[0])
        assert packaged_license is not None
        assert packaged_license.read() == expected_license
        for member in regular_members:
            assert "companions" not in PurePosixPath(member.name).parts
            assert "deepkoala_mcp" not in PurePosixPath(member.name).parts
            extracted = sdist.extractfile(member)
            assert extracted is not None
            assert b"/lab/zhaoxi/" not in extracted.read()
