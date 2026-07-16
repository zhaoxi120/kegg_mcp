"""Release and distribution policy checks."""

from __future__ import annotations

import ast
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
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "CHANGELOG.md",
    PROJECT_ROOT / "SECURITY.md",
    PROJECT_ROOT / "docs" / "development-plan.md",
    PROJECT_ROOT / "docs" / "mcp-benchmark-review.md",
    PROJECT_ROOT / "docs" / "installation.md",
    PROJECT_ROOT / "docs" / "mcp-server.md",
    PROJECT_ROOT / "docs" / "release-readiness.md",
    PROJECT_ROOT / "docs" / "repository-review-decisions.md",
    PROJECT_ROOT / "docs" / "services-results-reporting.md",
    PROJECT_ROOT / "docs" / "skill-evaluation.md",
    PROJECT_ROOT / "docs" / "troubleshooting.md",
    PROJECT_ROOT / "docs" / "visualization-extension-plan.md",
    PROJECT_ROOT / "companions" / "deepkoala-mcp" / "README.md",
    PROJECT_ROOT / "companions" / "kegg-render-mcp" / "README.md",
    PROJECT_ROOT / "tests" / "live" / "README.md",
    PROJECT_ROOT / "examples" / "README.md",
    PROJECT_ROOT / "examples" / "plain-ko" / "ko-list.txt",
    PROJECT_ROOT / "examples" / "plain-ko" / "clean-ko-list.txt",
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
    ".kgml",
    ".onnx",
    ".png",
    ".pt",
    ".pth",
    ".safetensors",
    ".sqlite",
    ".sqlite3",
}
FORBIDDEN_ARCHIVE_PARTS = {
    ".git",
    ".kegg-cache",
    ".kegg-render",
    ".pytest_cache",
    ".pyright",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "analysis-results",
    "pathway-assets",
    "render-output",
    "renderer-state",
    "results",
}
CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
PYTHON_REQUIRES = ">=3.11,<3.12"
VERSION_SUFFIXED_IDENTIFIER = re.compile(r"(?:V[0-9]+|_v[0-9]+)\Z")


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
    assert project["version"] == "0.3.0"
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


def test_candidate_version_and_release_matrix_is_consistent() -> None:
    core_version = str(_project_table()["version"])
    deepkoala_project = tomllib.loads(
        (PROJECT_ROOT / "companions/deepkoala-mcp/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    renderer_project = tomllib.loads(
        (PROJECT_ROOT / "companions/kegg-render-mcp/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")
    security = (PROJECT_ROOT / "SECURITY.md").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    matrix_rows = (
        f"| `kegg-mcp` | `{core_version}` | Unreleased candidate |",
        f"| `deepkoala-mcp` | `{deepkoala_project['version']}` | Unreleased candidate |",
        f"| `kegg-render-mcp` | `{renderer_project['version']}` | Unreleased candidate |",
    )
    for document in (readme, readiness):
        assert all(row in document for row in matrix_rows)
        normalized = re.sub(r"\s+", " ", document.lower())
        assert "only published github release is core `v0.1.0`" in normalized

    assert "Current candidate versions and publication status" in installation
    assert "release-readiness checklist" in installation
    for document in (readme, installation, readiness):
        assert "Linux" in document
        assert "Python 3.11.x" in document

    assert "kegg-mcp>=0.3,<0.4" in renderer_project["dependencies"]
    assert "| 0.3.x | Current unreleased candidate" in security
    assert "| 0.2.x | Never published" in security
    assert "| 0.1.x | Supported GitHub release |" in security
    assert "## [0.2.0] - Unpublished candidate (2026-07-15)" in changelog
    assert "## [0.2.0] - 2026-07-15" not in changelog


def test_release_documents_and_synthetic_examples_are_english_and_bounded() -> None:
    assert all(path.is_file() for path in OWNED_RELEASE_FILES)

    for path in OWNED_RELEASE_FILES:
        content = path.read_text(encoding="utf-8")
        assert not CJK_CHARACTER.search(content), path
        assert path.stat().st_size <= 128 * 1024, path
        assert "SQLite format 3" not in content


def test_access_examples_record_rights_and_use_no_secrets() -> None:
    academic = (PROJECT_ROOT / "examples/config/public-academic.env.example").read_text(
        encoding="utf-8"
    )
    licensed = (PROJECT_ROOT / "examples/config/licensed.env.example").read_text(encoding="utf-8")

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
        assert path.name not in {"render_input.json", "render_manifest.json"}, path
        assert path.stat().st_size <= 5 * 1024 * 1024, path
        if path.suffix.lower() == ".svg":
            assert "data:image/png;base64," not in path.read_text(encoding="utf-8"), path

    ignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "*.zh-CN.md" in ignore
    assert ".kegg-render/" in ignore
    assert "renderer-state/" in ignore
    assert "analysis-results/" in ignore
    assert "pathway-assets/" in ignore
    assert "*.kgml" in ignore
    assert "*.png" in ignore
    assert "render_input.json" in ignore
    assert "render_manifest.json" in ignore
    assert all(not path.name.endswith(".zh-CN.md") for path in candidate_files)


def test_refactored_orchestration_hotspots_remain_bounded() -> None:
    limits = {
        "src/kegg_mcp/mcp/server.py": 250,
        "src/kegg_mcp/services/primitives.py": 150,
        "src/kegg_mcp/kegg/client.py": 500,
        "companions/kegg-render-mcp/src/kegg_render_mcp/artifacts.py": 650,
        "companions/kegg-render-mcp/src/kegg_render_mcp/render_service.py": 400,
        "companions/deepkoala-mcp/src/deepkoala_mcp/jobs.py": 700,
    }

    for relative_path, maximum_lines in limits.items():
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert len(content.splitlines()) <= maximum_lines, relative_path


def test_python_identifiers_do_not_embed_contract_versions() -> None:
    for path in _candidate_files():
        if path.suffix != ".py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        identifiers: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                identifiers.add(node.name)
            elif isinstance(node, ast.Name):
                identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.add(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.add(node.arg)
        offenders = sorted(name for name in identifiers if VERSION_SUFFIXED_IDENTIFIER.search(name))
        assert offenders == [], f"{path.relative_to(PROJECT_ROOT)}: {offenders}"


def test_visualization_extension_has_an_independent_synthetic_release_boundary() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    server_doc = (PROJECT_ROOT / "docs/mcp-server.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")
    renderer_readme = (PROJECT_ROOT / "companions" / "kegg-render-mcp" / "README.md").read_text(
        encoding="utf-8"
    )
    renderer_project_path = PROJECT_ROOT / "companions" / "kegg-render-mcp" / "pyproject.toml"
    renderer_lock = PROJECT_ROOT / "companions" / "kegg-render-mcp" / "uv.lock"
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert renderer_project_path.is_file()
    assert renderer_lock.is_file()
    renderer_project = tomllib.loads(renderer_project_path.read_text(encoding="utf-8"))["project"]
    assert renderer_project["name"] == "kegg-render-mcp"
    assert "kegg-mcp>=0.3,<0.4" in renderer_project["dependencies"]
    assert renderer_project["scripts"] == {"kegg-render-mcp": "kegg_render_mcp.server:main"}
    lock_document = tomllib.loads(renderer_lock.read_text(encoding="utf-8"))
    locked_packages = cast(list[dict[str, object]], lock_document["package"])
    locked_core = next(package for package in locked_packages if package["name"] == "kegg-mcp")
    locked_renderer = next(
        package for package in locked_packages if package["name"] == "kegg-render-mcp"
    )
    assert locked_core["version"] == "0.3.0"
    assert locked_renderer["version"] == renderer_project["version"]

    for document in (readme, installation, server_doc, readiness, renderer_readme):
        normalized = re.sub(r"\s+", " ", document)
        assert "render_input.json" in normalized
        assert "version 2" in normalized
        assert "separate" in normalized or "independent" in normalized
    for document in (readme, installation, server_doc, readiness):
        assert "AnalysisExecutionProvenance` version 2" in re.sub(r"\s+", " ", document)

    renderer_job = ci.split("validate-renderer-companion:", maxsplit=1)[1]
    for command in (
        "uv sync --frozen",
        "uv run --frozen ruff check .",
        "uv run --frozen ruff format --check .",
        "uv run --frozen pyright",
        "uv run --frozen pytest",
        "uv build --no-sources",
    ):
        assert command in renderer_job
    assert "companions/kegg-render-mcp/uv.lock" in renderer_job
    assert "KEGG_MCP_RUN_LIVE_TESTS" not in renderer_job
    assert "push:" not in ci

    normalized_readiness = re.sub(r"\s+", " ", readiness.lower())
    normalized_renderer = re.sub(r"\s+", " ", renderer_readme.lower())
    assert "synthetic" in normalized_readiness
    assert "no live kegg requests" in normalized_readiness
    assert "no kegg payload" in normalized_renderer
    assert "global and overview" in normalized_renderer


def test_rights_and_release_status_are_prominent() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for required in ("public_academic", "licensed"):
        assert required in installation
    assert "offline_cache" not in installation
    assert "Python 3.11.x" in installation
    assert "https://www.kegg.jp/kegg/rest/" in installation
    assert "https://www.kegg.jp/kegg/legal.html" in installation
    assert "KEGG_MCP_ALLOWED_ROOTS" in installation
    assert "confirmed `public_academic`" in re.sub(r"\s+", " ", readme)
    assert "KEGG_MCP_ACCESS_MODE: public_academic" in ci
    assert 'KEGG_MCP_ACADEMIC_USE_CONFIRMED: "true"' in ci
    assert 'KEGG_MCP_LIVE_REQUESTS_PER_OPERATION: "30"' in ci
    assert "120 live KEGG requests" in ci
    assert "push:" not in ci
    assert "probe connectivity once" in installation
    assert "Retrieve only KO entry `K00844`" in installation
    assert "Current status:" in readiness
    assert "exact commit" in readiness
    normalized_changelog = re.sub(r"\s+", " ", changelog.lower())
    assert "## [0.2.0] - Unpublished candidate (2026-07-15)" in changelog
    assert "## [unreleased]" in normalized_changelog


def test_skill_evaluation_record_distinguishes_static_tests_from_forward_review() -> None:
    record = (PROJECT_ROOT / "docs/skill-evaluation.md").read_text(encoding="utf-8")

    assert "not a runtime LLM evaluation" in record
    assert "independent forward/manual review" in record
    assert record.count("Observed route: passed.") == 6
    assert "exact v0.2.0 candidate" in re.sub(r"\s+", " ", record)
    assert "separate nine-route forward review" in re.sub(r"\s+", " ", record)


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
    assert "installing the wheel alone does not make either Skill available" in normalized_readme
    assert "does not install either repository-scoped Skill" in normalized_installation
    assert "do not install either Skill" in normalized_readiness


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
