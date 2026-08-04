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
OWNED_RELEASE_DOCUMENTS = tuple(
    sorted(
        {
            PROJECT_ROOT / "README.md",
            PROJECT_ROOT / "tests" / "live" / "README.md",
            PROJECT_ROOT / "examples" / "README.md",
            *(PROJECT_ROOT / "docs").rglob("*.md"),
            *(PROJECT_ROOT / "companions").glob("*/README.md"),
        }
        - set((PROJECT_ROOT / "docs").rglob("*.zh-CN.md"))
    )
)
OWNED_RELEASE_FILES = (
    *OWNED_RELEASE_DOCUMENTS,
    PROJECT_ROOT / "examples" / "plain-ko" / "ko-list.txt",
    PROJECT_ROOT / "examples" / "plain-ko" / "clean-ko-list.txt",
    PROJECT_ROOT / "examples" / "config" / "public-academic.env.example",
    PROJECT_ROOT / "examples" / "config" / "licensed.env.example",
    PROJECT_ROOT / "examples" / "config" / "kegg-mcp-suite.toml",
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


def _workflow_job(workflow: str, name: str) -> str:
    marker = f"\n  {name}:\n"
    assert marker in workflow
    remainder = workflow.split(marker, maxsplit=1)[1]
    next_job = re.search(r"\n  [a-z][a-z0-9-]*:\n", remainder)
    return remainder if next_job is None else remainder[: next_job.start()]


def _release_files() -> tuple[Path, ...]:
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


def _private_workspace_marker() -> bytes:
    return os.fsencode(PROJECT_ROOT.parent) + b"/"


def test_project_metadata_declares_buildable_stdio_package() -> None:
    project = _project_table()
    scripts = cast(dict[str, str], project["scripts"])

    assert project["name"] == "kegg-mcp"
    assert project["version"] == "0.8.0"
    assert project["readme"] == "docs/core-package.md"
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


def test_document_ownership_and_release_gates_are_explicit() -> None:
    retired = (
        PROJECT_ROOT / "docs" / "development-plan.md",
        PROJECT_ROOT / "docs" / "visualization-extension-plan.md",
    )
    current = (
        PROJECT_ROOT / "docs" / "architecture.md",
        PROJECT_ROOT / "docs" / "visualization-architecture.md",
        PROJECT_ROOT / "docs" / "core-package.md",
        PROJECT_ROOT / "docs" / "manual-component-deployment.md",
    )
    assert not any(path.exists() for path in retired)
    assert all(path.is_file() for path in current)

    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")

    for relative in (
        "docs/architecture.md",
        "docs/visualization-architecture.md",
        "docs/core-package.md",
        "docs/manual-component-deployment.md",
        "docs/skill-evaluation.md",
    ):
        assert relative in readme
    assert "manual-component-deployment.md" in installation
    assert "skill-evaluation.md#release-review-matrix" in readiness


def test_distribution_versions_and_compatibility_are_consistent() -> None:
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
    matrix_rows = (
        f"| `kegg-mcp` | `{core_version}` |",
        f"| `deepkoala-mcp` | `{deepkoala_project['version']}` |",
        f"| `kegg-render-mcp` | `{renderer_project['version']}` |",
    )
    assert all(row in readiness for row in matrix_rows)

    assert "release-readiness checklist" in installation
    for document in (readme, installation, readiness):
        assert "Linux" in document
        assert "Python 3.11.x" in document

    assert "kegg-mcp>=0.5,<0.9" in renderer_project["dependencies"]
    assert "Distribution boundary" in readiness


def test_platform_classifiers_match_the_supported_component_matrix() -> None:
    core = _project_table()
    renderer = tomllib.loads(
        (PROJECT_ROOT / "companions/kegg-render-mcp/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    deepkoala = tomllib.loads(
        (PROJECT_ROOT / "companions/deepkoala-mcp/pyproject.toml").read_text(encoding="utf-8")
    )["project"]
    macos = "Operating System :: MacOS :: MacOS X"
    linux = "Operating System :: POSIX :: Linux"

    for project in (core, renderer):
        classifiers = cast(list[str], project["classifiers"])
        assert macos in classifiers
        assert linux in classifiers
        assert not any("Windows" in classifier for classifier in classifiers)

    deepkoala_classifiers = cast(list[str], deepkoala["classifiers"])
    assert linux in deepkoala_classifiers
    assert macos in deepkoala_classifiers
    assert not any("Windows" in classifier for classifier in deepkoala_classifiers)


def test_v08_reference_and_handoff_boundaries_are_release_gated() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (PROJECT_ROOT / "docs/architecture.md").read_text(encoding="utf-8")
    server = (PROJECT_ROOT / "docs/mcp-server.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")
    normalized = re.sub(r"\s+", " ", "\n".join((readme, architecture, server, readiness)))

    for required in (
        "eighteen Core tools",
        'projection="references"',
        "write_kegg_reference_bundle",
        "prepare_kegg_handoff",
        "reference_snapshot.json",
        "reference_manifest.json",
        "handoff_manifest.json",
        "does not retrieve or summarize papers",
        "do not export cache payloads or mirror KEGG",
        "do not issue a KEGG request, upload, start a browser, execute an external tool",
        'order_semantics="caller_supplied_genomic_order"',
    ):
        assert required in normalized


def test_release_documents_and_synthetic_examples_are_english_and_bounded() -> None:
    assert all(path.is_file() for path in OWNED_RELEASE_FILES)

    for path in OWNED_RELEASE_FILES:
        content = path.read_text(encoding="utf-8")
        assert not CJK_CHARACTER.search(content), path
        assert path.stat().st_size <= 128 * 1024, path
        assert "SQLite format 3" not in content

    document_corpus = "\n".join(
        path.read_text(encoding="utf-8") for path in OWNED_RELEASE_DOCUMENTS
    )
    for stale_contract in (
        "CPU-only",
        "acknowledged=true",
        "five real requests",
        "for 20 total",
        "prior `v1` rows",
        "process-wide request rate",
        "process-wide rate limiter",
        "process-wide no-burst rate limit",
        "prepare_deepkoala_job",
        "submit_deepkoala_job",
        "kegg-visualization",
    ):
        assert stale_contract not in document_corpus


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


def test_release_tree_contains_no_tracked_release_blocking_binary() -> None:
    release_files = _release_files()
    assert release_files

    for path in release_files:
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
    assert all(not path.name.endswith(".zh-CN.md") for path in release_files)


def test_orchestration_hotspots_remain_bounded() -> None:
    limits = {
        "src/kegg_mcp/analysis/comparison.py": 550,
        "src/kegg_mcp/analysis/comparison_contracts.py": 450,
        "src/kegg_mcp/mcp/server.py": 250,
        "src/kegg_mcp/services/__init__.py": 20,
        "src/kegg_mcp/kegg/client.py": 500,
        "companions/kegg-render-mcp/src/kegg_render_mcp/artifacts.py": 650,
        "companions/kegg-render-mcp/src/kegg_render_mcp/render_service.py": 400,
        "companions/deepkoala-mcp/src/deepkoala_mcp/jobs.py": 700,
    }

    for relative_path, maximum_lines in limits.items():
        content = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert len(content.splitlines()) <= maximum_lines, relative_path


def test_python_identifiers_do_not_embed_contract_versions() -> None:
    for path in _release_files():
        if path.suffix != ".py":
            continue
        assert not VERSION_SUFFIXED_IDENTIFIER.search(path.stem), path.relative_to(PROJECT_ROOT)
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


def test_renderer_has_an_independent_synthetic_release_boundary() -> None:
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
    assert "kegg-mcp>=0.5,<0.9" in renderer_project["dependencies"]
    assert renderer_project["scripts"] == {"kegg-render-mcp": "kegg_render_mcp.server:main"}
    lock_document = tomllib.loads(renderer_lock.read_text(encoding="utf-8"))
    locked_packages = cast(list[dict[str, object]], lock_document["package"])
    locked_core = next(package for package in locked_packages if package["name"] == "kegg-mcp")
    locked_renderer = next(
        package for package in locked_packages if package["name"] == "kegg-render-mcp"
    )
    assert locked_core["version"] == _project_table()["version"]
    assert locked_renderer["version"] == renderer_project["version"]

    for document in (installation, server_doc, readiness, renderer_readme):
        normalized = re.sub(r"\s+", " ", document)
        assert "render_input.json" in normalized
        assert "version 3" in normalized
        assert "separate" in normalized or "independent" in normalized
    for document in (installation, server_doc, readiness):
        assert "AnalysisExecutionProvenance` version 3" in re.sub(r"\s+", " ", document)

    renderer_job = _workflow_job(ci, "validate-renderer-companion")
    for command in (
        "uv sync --locked",
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


def test_deepkoala_companion_has_an_independent_build_gate() -> None:
    project_path = PROJECT_ROOT / "companions/deepkoala-mcp/pyproject.toml"
    lock_path = PROJECT_ROOT / "companions/deepkoala-mcp/uv.lock"
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert project_path.is_file()
    assert lock_path.is_file()
    project = tomllib.loads(project_path.read_text(encoding="utf-8"))["project"]
    assert project["name"] == "deepkoala-mcp"
    assert project["scripts"] == {"deepkoala-mcp": "deepkoala_mcp.cli:main"}

    companion_job = _workflow_job(ci, "validate-deepkoala-companion")
    for command in (
        "uv sync --locked",
        "uv run --frozen ruff check .",
        "uv run --frozen ruff format --check .",
        "uv run --frozen pyright",
        "uv run --frozen pytest",
        "uv build --no-sources",
    ):
        assert command in companion_job
    assert "companions/deepkoala-mcp/uv.lock" in companion_job
    assert "KEGG_MCP_RUN_LIVE_TESTS" not in companion_job


def test_ci_clean_installs_fresh_wheels_outside_the_checkout() -> None:
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    smoke_path = PROJECT_ROOT / "tests/release/smoke_wheel.py"
    smoke = smoke_path.read_text(encoding="utf-8")

    for job_name in (
        "validate",
        "validate-deepkoala-companion",
        "validate-renderer-companion",
        "validate-macos-core",
        "validate-macos-deepkoala-companion",
        "validate-macos-renderer",
    ):
        assert "uv sync --locked" in _workflow_job(ci, job_name)
    assert "uv sync --frozen" not in ci
    assert smoke_path.is_file()
    for distribution, version in (
        ("kegg-mcp", "0.8.0"),
        ("deepkoala-mcp", "0.5.0"),
        ("kegg-render-mcp", "0.3.2"),
    ):
        assert f"--distribution {distribution}" in ci
        assert f"--expected-version {version}" in ci
    for job_name in (
        "validate",
        "validate-deepkoala-companion",
        "validate-renderer-companion",
        "validate-macos-core",
        "validate-macos-deepkoala-companion",
        "validate-macos-renderer",
        "validate-windows-unsupported",
    ):
        job = _workflow_job(ci, job_name)
        assert "Smoke-test installed" in job
        assert "tests/release/smoke_wheel.py" in job
    assert "uv build --no-sources --wheel" in ci
    for isolation_marker in (
        '"-I"',
        "module_path.is_relative_to(environment_path)",
        "environment.pop(name, None)",
        "cwd=root",
        'environment_root / "Scripts" / "python.exe"',
    ):
        assert isolation_marker in smoke


def test_ci_has_bounded_apple_silicon_evidence_and_native_windows_diagnostics() -> None:
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    smoke = (PROJECT_ROOT / "tests/release/smoke_wheel.py").read_text(encoding="utf-8")
    macos_core = _workflow_job(ci, "validate-macos-core")
    macos_deepkoala = _workflow_job(ci, "validate-macos-deepkoala-companion")
    macos_renderer = _workflow_job(ci, "validate-macos-renderer")
    windows = _workflow_job(ci, "validate-windows-unsupported")

    checkout = "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
    setup_uv = "astral-sh/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990"
    for job in (macos_core, macos_deepkoala, macos_renderer, windows):
        assert job.count(checkout) == 1
        assert job.count(setup_uv) == 1
        assert 'python-version: "3.11"' in job
        assert 'version: "0.11.28"' in job
        assert "KEGG_MCP_RUN_LIVE_TESTS" not in job
        assert "KEGG_MCP_ACADEMIC_USE_CONFIRMED" not in job

    assert "runs-on: macos-14" in macos_core
    assert "platform.machine() == 'arm64'" in macos_core
    assert "KEGG_MCP_ACCESS_MODE: offline_cache" in macos_core
    assert "tests/unit" in macos_core
    assert "tests/contract" in macos_core
    assert "tests/integration" in macos_core
    assert "--ignore=tests/integration/test_deepkoala_companion_handoff.py" in macos_core
    assert "--distribution kegg-mcp" in macos_core
    assert "--console kegg-mcp" in macos_core

    assert "runs-on: macos-14" in macos_renderer
    assert "platform.machine() == 'arm64'" in macos_renderer
    assert "working-directory: companions/kegg-render-mcp" in macos_renderer
    assert "uv sync --locked --all-groups" in macos_renderer
    assert "uv run --frozen pytest" in macos_renderer
    assert "--distribution kegg-render-mcp" in macos_renderer

    assert "runs-on: macos-14" in macos_deepkoala
    assert "working-directory: companions/deepkoala-mcp" in macos_deepkoala
    assert "platform.machine() == 'arm64'" in macos_deepkoala
    for command in (
        "uv sync --locked",
        "uv run --frozen ruff check .",
        "uv run --frozen ruff format --check .",
        "uv run --frozen pyright",
        "uv run --frozen pytest",
        "uv build --no-sources",
        "--distribution deepkoala-mcp",
    ):
        assert command in macos_deepkoala

    assert "validate-macos-intel-components:" not in ci
    assert "macos-15-intel" not in ci

    assert "runs-on: windows-latest" in windows
    assert "uv sync" not in windows
    assert "pytest" not in windows
    assert "deepkoala" not in windows.lower()
    assert "--native-windows-probe core" in windows
    assert "--native-windows-probe renderer" in windows
    assert "--console kegg-mcp" in windows
    assert "--console kegg-render-mcp" not in windows
    for marker in (
        'choices=("core", "renderer")',
        '"configuration_valid": False',
        '"allowed_root_paths": "redacted"',
        "UNSUPPORTED_PLATFORM_DIAGNOSTIC",
        '_venv_console(environment_root, "kegg-render-mcp")',
        "core console did not fail closed on native Windows",
        "renderer console did not fail closed on native Windows",
        '"native Windows"',
        '"WSL"',
    ):
        assert marker in smoke


def test_rights_and_release_status_are_prominent() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")
    ci = (PROJECT_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    for required in ("public_academic", "licensed", "offline_cache"):
        assert required in installation
    assert "Python 3.11.x" in installation
    assert "https://www.kegg.jp/kegg/rest/" in installation
    assert "https://www.kegg.jp/kegg/legal.html" in installation
    assert "KEGG_MCP_ALLOWED_ROOTS" in installation
    assert "confirmed `public_academic`" in re.sub(r"\s+", " ", readme)
    assert "KEGG_MCP_ACCESS_MODE: public_academic" in ci
    assert 'KEGG_MCP_ACADEMIC_USE_CONFIRMED: "true"' in ci
    assert 'KEGG_MCP_LIVE_REQUESTS_PER_OPERATION: "20"' in ci
    assert "120 live KEGG requests" in ci
    assert "push:" not in ci
    assert "probe connectivity once" in installation
    assert "Retrieve only KO entry `K00844`" in installation
    assert "Current status:" in readiness
    assert "exact merged commit" in readiness


def test_skill_evaluation_separates_deterministic_checks_from_manual_release_review() -> None:
    record = (PROJECT_ROOT / "docs/skill-evaluation.md").read_text(encoding="utf-8")

    assert "not a runtime LLM evaluation" in record
    assert "Independent forward/manual reviews" in record
    assert "tests/skill/" in record
    normalized = re.sub(r"\s+", " ", record)
    assert "exact release candidate" in normalized
    assert "All focused routes must pass before release" in normalized
    for scenario in (
        "Protein FASTA without KO assignments",
        "DeepKOALA detailed CSV",
        "Plain K-number column",
        "Two KO sets",
        "Activity claim from one K number",
        "Existing `render_input.json`",
        "Combined FASTA-to-graphics request",
    ):
        assert scenario in record


def test_distribution_boundary_is_explicit() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs/installation.md").read_text(encoding="utf-8")
    readiness = (PROJECT_ROOT / "docs/release-readiness.md").read_text(encoding="utf-8")

    for document in (readme, installation, readiness):
        assert "Python wheel" in document
        assert "repository-scoped Skill" in document
        assert "suite installer" in document
    normalized_readme = re.sub(r"\s+", " ", readme)
    normalized_installation = re.sub(r"\s+", " ", installation)
    normalized_readiness = re.sub(r"\s+", " ", readiness)
    assert "Installing a wheel alone does not make repository-scoped Skills available" in (
        normalized_readme
    )
    assert "No component wheel installs repository-scoped Skills" in normalized_installation
    assert "Renderer Python wheel installs the compatible Core distribution" in (
        normalized_installation
    )
    assert "neither registers nor starts the Core stdio server" in normalized_installation
    assert "does not install either companion or any repository-scoped Skill" in (
        normalized_readiness
    )


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
        assert all(_private_workspace_marker() not in wheel.read(name) for name in names)

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
            assert _private_workspace_marker() not in extracted.read()
