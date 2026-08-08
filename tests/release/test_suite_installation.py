"""Local-only contracts for the all-in-one Codex suite installer."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = PROJECT_ROOT / "scripts" / "install-suite.py"
LAUNCHER = PROJECT_ROOT / "scripts" / "run-installed-mcp.py"
EXAMPLE_DEPLOYMENT_CONFIG = PROJECT_ROOT / "examples" / "config" / "kegg-mcp-suite.toml"
RENDERER_CONFIG = (
    PROJECT_ROOT / "companions" / "kegg-render-mcp" / "src" / "kegg_render_mcp" / "config.py"
)
SKILL_NAMES = {
    "deepkoala-annotation",
    "kegg-ko-analysis",
    "kegg-pathway-rendering",
}


def _load_installer_module(installer: Path = INSTALLER) -> Any:
    module_name = f"kegg_suite_installer_test_{abs(hash(str(installer)))}"
    spec = importlib.util.spec_from_file_location(module_name, installer)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    prior_bytecode_policy = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = prior_bytecode_policy
    return module


INSTALLER_MODULE = _load_installer_module()
LAUNCHER_MODULE = _load_installer_module(LAUNCHER)
SERVER_NAMES = set(INSTALLER_MODULE.SERVER_NAMES)


def _literal_assignment(path: Path, name: str) -> object:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in module.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} is not one literal assignment in {path}")


def _mkdir(path: Path, *, private: bool = False) -> Path:
    path.mkdir(parents=True)
    path.chmod(0o700 if private else 0o755)
    return path


def _deployment_paths(tmp_path: Path) -> dict[str, Path]:
    private = _mkdir(tmp_path / "private", private=True)
    shared = _mkdir(tmp_path / "shared")
    input_root = _mkdir(shared / "input")
    output_root = _mkdir(shared / "output")
    external_python = tmp_path / "deepkoala-python"
    external_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    external_python.chmod(0o700)
    hmmsearch = tmp_path / "hmmsearch"
    hmmsearch.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hmmsearch.chmod(0o700)
    return {
        "private": private,
        "shared": shared,
        "input": input_root,
        "output": output_root,
        "python": external_python,
        "profiles": _mkdir(tmp_path / "profiles"),
        "hmmsearch": hmmsearch,
        "rate": _mkdir(private / "rate", private=True),
        "deep_state": _mkdir(private / "deep-state", private=True),
        "render_state": _mkdir(private / "render-state", private=True),
    }


def _deployment_toml(paths: dict[str, Path], *, extras: dict[str, str] | None = None) -> str:
    extra = extras or {}
    return f"""\
schema_version = 1
{extra.get("root", "")}
[kegg]
access_mode = "public_academic"
academic_use_confirmed = true
rate_limit_root = {json.dumps(str(paths["rate"]))}
{extra.get("kegg", "")}
[core]
result_store_path = {json.dumps(str(paths["private"] / "core-results.sqlite3"))}
allowed_roots = [{json.dumps(str(paths["shared"]))}]
{extra.get("core", "")}
[deepkoala]
state_root = {json.dumps(str(paths["deep_state"]))}
input_roots = [{json.dumps(str(paths["input"]))}]
output_roots = [{json.dumps(str(paths["output"]))}]
allowed_models = ["full", "frag"]
cpu_threads = 2
{extra.get("deepkoala", "")}
[renderer]
state_root = {json.dumps(str(paths["render_state"]))}
allowed_roots = [{json.dumps(str(paths["shared"]))}]
offline_allow_stale = false
{extra.get("renderer", "")}
"""


def _write_config(
    tmp_path: Path,
    *,
    extras: dict[str, str] | None = None,
    enable_multi: bool = False,
) -> tuple[Path, dict[str, Path]]:
    paths = _deployment_paths(tmp_path)
    selected_extras = dict(extras or {})
    if enable_multi:
        selected_extras["deepkoala"] = (
            selected_extras.get("deepkoala", "")
            + "allow_multi = true\n"
            + f"profiles_dir = {json.dumps(str(paths['profiles']))}\n"
            + f"hmmsearch_executable = {json.dumps(str(paths['hmmsearch']))}\n"
        )
    config = paths["private"] / "deployment.toml"
    config.write_text(_deployment_toml(paths, extras=selected_extras), encoding="utf-8")
    config.chmod(0o600)
    return config, paths


def test_installer_and_launcher_share_deployment_manifest_contract() -> None:
    assert tuple(INSTALLER_MODULE.SERVER_NAMES) == tuple(LAUNCHER_MODULE.SERVER_NAMES)
    assert INSTALLER_MODULE.DEPLOYMENT_MANIFEST_SCHEMA_VERSION == LAUNCHER_MODULE.SCHEMA_VERSION


def test_cross_distribution_deployment_constants_remain_aligned() -> None:
    from deepkoala_mcp.contracts import RunDeepKoalaInput

    from kegg_mcp.mcp.config import RATE_LIMIT_ROOT_ENV as CORE_RATE_LIMIT_ROOT_ENV

    assert (
        INSTALLER_MODULE.RATE_LIMIT_ROOT_ENV
        == CORE_RATE_LIMIT_ROOT_ENV
        == _literal_assignment(RENDERER_CONFIG, "RATE_LIMIT_ROOT_ENV")
    )
    assert (
        RunDeepKoalaInput.model_fields["model_date"].default
        == INSTALLER_MODULE.DEFAULT_DEEPKOALA_MODEL_DATE
    )
    assert INSTALLER_MODULE.DEEPKOALA_REVISION == ("bebbe0c43f50a26488f7092f6b355aae870a4ed9")


@pytest.mark.parametrize(
    ("host_platform", "machine", "macos_version", "expected_profile"),
    [
        ("linux", "x86_64", "", "linux"),
        ("linux", "aarch64", "", "linux"),
        ("darwin", "arm64", "14.0", "darwin-arm64"),
    ],
)
def test_host_platform_profiles_cover_linux_and_native_apple_silicon(
    monkeypatch: pytest.MonkeyPatch,
    host_platform: str,
    machine: str,
    macos_version: str,
    expected_profile: str,
) -> None:
    monkeypatch.setattr(INSTALLER_MODULE.sys, "platform", host_platform)
    monkeypatch.setattr(INSTALLER_MODULE.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        INSTALLER_MODULE.platform,
        "mac_ver",
        lambda: (macos_version, ("", "", ""), ""),
    )

    profile = INSTALLER_MODULE._host_platform_profile()

    assert profile.name == expected_profile


@pytest.mark.parametrize(
    ("host_platform", "machine", "macos_version"),
    [
        ("darwin", "x86_64", "14.0"),
        ("darwin", "private-machine-value", "14.0"),
        ("darwin", "arm64", "13.6"),
        ("darwin", "arm64", "private-version-value"),
        ("win32", "AMD64", ""),
        ("freebsd14", "arm64", ""),
    ],
)
def test_host_platform_rejects_unsupported_native_targets_with_a_static_error(
    monkeypatch: pytest.MonkeyPatch,
    host_platform: str,
    machine: str,
    macos_version: str,
) -> None:
    monkeypatch.setattr(INSTALLER_MODULE.sys, "platform", host_platform)
    monkeypatch.setattr(INSTALLER_MODULE.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        INSTALLER_MODULE.platform,
        "mac_ver",
        lambda: (macos_version, ("", "", ""), ""),
    )

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._host_platform_profile()

    assert raised.value.code == "platform_unsupported"
    assert str(raised.value) == INSTALLER_MODULE.UNSUPPORTED_PLATFORM_MESSAGE
    assert machine not in str(raised.value)
    if macos_version:
        assert macos_version not in str(raised.value)


def test_tracked_example_config_is_accepted_by_the_real_installer(tmp_path: Path) -> None:
    paths = _deployment_paths(tmp_path)
    core_state = _mkdir(paths["private"] / "core", private=True)
    replacements = {
        "/absolute/private/path/to/kegg-suite/rate-limit": str(paths["rate"]),
        "/absolute/private/path/to/kegg-suite/core/results.sqlite3": str(
            core_state / "results.sqlite3"
        ),
        "/absolute/shared/path/to/kegg-suite/inputs": str(paths["input"]),
        "/absolute/shared/path/to/kegg-suite/analysis": str(paths["output"]),
        "/absolute/private/path/to/kegg-suite/deepkoala-state": str(paths["deep_state"]),
        "/absolute/private/path/to/kegg-suite/renderer-state": str(paths["render_state"]),
    }
    rendered = EXAMPLE_DEPLOYMENT_CONFIG.read_text(encoding="utf-8").replace(
        "academic_use_confirmed = false",
        "academic_use_confirmed = true",
        1,
    )
    for placeholder, value in replacements.items():
        rendered = rendered.replace(placeholder, value)
    config_path = paths["private"] / "tracked-example.toml"
    config_path.write_text(rendered, encoding="utf-8")
    config_path.chmod(0o600)

    config = INSTALLER_MODULE._load_deployment_config(config_path)

    assert config.kegg.rate_limit_root == paths["rate"].resolve()
    assert config.core.result_store_path == (core_state / "results.sqlite3").resolve()
    assert config.deepkoala.input_roots == (paths["input"].resolve(),)
    assert config.deepkoala.output_roots == (paths["output"].resolve(),)
    assert config.renderer.allowed_roots == (paths["output"].resolve(),)


def _rewrite_deepkoala_config(config: Path, paths: dict[str, Path], deepkoala_extra: str) -> None:
    config.write_text(
        _deployment_toml(paths, extras={"deepkoala": deepkoala_extra}),
        encoding="utf-8",
    )
    config.chmod(0o600)


def _copy_suite_source(source: Path) -> None:
    source.mkdir(parents=True)
    for relative in (
        "pyproject.toml",
        "uv.lock",
        "companions/deepkoala-mcp/pyproject.toml",
        "companions/deepkoala-mcp/uv.lock",
        "companions/kegg-render-mcp/pyproject.toml",
        "companions/kegg-render-mcp/uv.lock",
        "scripts/install-suite.py",
        "scripts/run-installed-mcp.py",
    ):
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    shutil.copytree(PROJECT_ROOT / ".agents" / "skills", source / ".agents" / "skills")


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _materialize_suite_artifacts(
    tmp_path: Path,
    *,
    enable_multi: bool = False,
) -> tuple[Any, Any, Any, Path, Path, dict[str, Path]]:
    config_path, paths = _write_config(tmp_path, enable_multi=enable_multi)
    config = INSTALLER_MODULE._load_deployment_config(config_path)
    source = tmp_path / "snapshot"
    _copy_suite_source(source)
    snapshot = INSTALLER_MODULE.SourceSnapshot(
        root=source,
        versions=INSTALLER_MODULE._project_versions(source),
    )
    install_parent = _mkdir(tmp_path / "install-parent", private=True)
    install_root = _mkdir(install_parent / "suite", private=True)
    for runtime, executable in (
        ("core", "kegg-mcp"),
        ("deepkoala", "deepkoala-mcp"),
        ("renderer", "kegg-render-mcp"),
    ):
        _write_executable(install_root / "runtimes" / runtime / "bin" / "python")
        _write_executable(install_root / "runtimes" / runtime / "bin" / executable)
    request = INSTALLER_MODULE.InstallRequest(
        install_root=install_root,
        marketplace_name="kegg-mcp-test",
        uv=paths["python"],
        codex=paths["python"],
        git=Path(shutil.which("git") or "/usr/bin/git").resolve(),
        python=paths["python"],
        platform_profile=INSTALLER_MODULE._host_platform_profile(),
        allow_locked_dependency_downloads=False,
        dry_run=False,
        allow_deepkoala_install=True,
    )
    environments = INSTALLER_MODULE._deployment_environments(config, install_root)
    launcher = INSTALLER_MODULE._materialize_deployment(request, snapshot, environments)
    marketplace_root = INSTALLER_MODULE._materialize_plugin(
        request,
        snapshot,
        launcher,
        config,
    )
    plugin_root = marketplace_root / "plugins" / "kegg-mcp"
    return request, snapshot, config, plugin_root, launcher, paths


def _suite_install_inputs(tmp_path: Path) -> tuple[Any, Any, Any, dict[str, Path]]:
    config_path, paths = _write_config(tmp_path)
    config = INSTALLER_MODULE._load_deployment_config(config_path)
    source = tmp_path / "transaction-snapshot"
    _copy_suite_source(source)
    snapshot = INSTALLER_MODULE.SourceSnapshot(
        root=source,
        versions=INSTALLER_MODULE._project_versions(source),
    )
    install_parent = _mkdir(tmp_path / "transaction-install-parent", private=True)
    request = INSTALLER_MODULE.InstallRequest(
        install_root=install_parent / "suite",
        marketplace_name="kegg-mcp-test",
        uv=paths["python"],
        codex=paths["python"],
        git=Path(shutil.which("git") or "/usr/bin/git").resolve(),
        python=paths["python"],
        platform_profile=INSTALLER_MODULE._host_platform_profile(),
        allow_locked_dependency_downloads=False,
        dry_run=False,
        allow_deepkoala_install=True,
    )
    return request, snapshot, config, paths


def test_deployment_config_requires_owner_only_file(tmp_path: Path) -> None:
    config, _ = _write_config(tmp_path)
    config.chmod(0o640)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config)

    assert raised.value.code == "deployment_path_invalid"


def test_deployment_config_requires_owner_only_parent(tmp_path: Path) -> None:
    config, paths = _write_config(tmp_path)
    paths["private"].chmod(0o750)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config)

    assert raised.value.code == "deployment_path_invalid"


def test_deployment_config_rejects_file_replacement_between_path_check_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _write_config(tmp_path)
    real_open = os.open
    replaced = False

    def replacing_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if Path(path) == config and not replaced:
            replaced = True
            config.unlink()
            config.write_text("schema_version = 1\n", encoding="utf-8")
            config.chmod(0o600)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(INSTALLER_MODULE.os, "open", replacing_open)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config)

    assert replaced is True
    assert raised.value.code == "deployment_config_invalid"


def test_deployment_config_rejects_boolean_schema_version(tmp_path: Path) -> None:
    config, _ = _write_config(tmp_path)
    document = config.read_text(encoding="utf-8").replace(
        "schema_version = 1", "schema_version = true", 1
    )
    config.write_text(document, encoding="utf-8")
    config.chmod(0o600)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config)

    assert raised.value.code == "deployment_config_invalid"


def test_public_academic_install_requires_first_use_confirmation(tmp_path: Path) -> None:
    config, _ = _write_config(tmp_path)
    document = config.read_text(encoding="utf-8").replace(
        "academic_use_confirmed = true", "academic_use_confirmed = false", 1
    )
    config.write_text(document, encoding="utf-8")
    config.chmod(0o600)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config)

    assert raised.value.code == "deployment_config_invalid"


def test_deployment_config_rejects_nonwritable_private_state(tmp_path: Path) -> None:
    config, paths = _write_config(tmp_path)
    paths["deep_state"].chmod(0o500)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config)

    assert raised.value.code == "deployment_path_invalid"


def test_deepkoala_multi_defaults_off_without_external_resources(tmp_path: Path) -> None:
    config_path, paths = _write_config(tmp_path)
    paths["profiles"].rmdir()
    paths["hmmsearch"].unlink()

    config = INSTALLER_MODULE._load_deployment_config(config_path)
    environment = INSTALLER_MODULE._deployment_environments(config, tmp_path / "installed")[
        "deepkoala-mcp"
    ]

    assert config.deepkoala.allow_multi is False
    assert config.deepkoala.profiles_dir is None
    assert config.deepkoala.hmmsearch_executable is None
    assert environment["DEEPKOALA_MCP_ALLOWED_DEVICES"] == ",".join(
        INSTALLER_MODULE._host_platform_profile().deepkoala_allowed_devices
    )
    assert environment["DEEPKOALA_MCP_ALLOW_MULTI"] == "false"
    assert "DEEPKOALA_MCP_PROFILES_DIR" not in environment
    assert "DEEPKOALA_MCP_HMMSEARCH_EXECUTABLE" not in environment


@pytest.mark.parametrize(
    ("profile_name", "expected_devices"),
    [("linux", "cpu,cuda"), ("darwin-arm64", "cpu,mps")],
)
def test_deployment_environment_emits_platform_specific_deepkoala_devices(
    tmp_path: Path,
    profile_name: str,
    expected_devices: str,
) -> None:
    config_path, _ = _write_config(tmp_path)
    config = INSTALLER_MODULE._load_deployment_config(config_path)
    profiles = {
        "linux": INSTALLER_MODULE.LINUX_PLATFORM_PROFILE,
        "darwin-arm64": INSTALLER_MODULE.DARWIN_ARM64_PLATFORM_PROFILE,
    }

    environments = INSTALLER_MODULE._deployment_environments(
        config,
        tmp_path / "installed",
        profiles[profile_name],
    )

    assert set(environments) == SERVER_NAMES
    assert environments["deepkoala-mcp"]["DEEPKOALA_MCP_ALLOWED_DEVICES"] == expected_devices


def test_deepkoala_multi_opt_in_emits_only_private_runtime_configuration(
    tmp_path: Path,
) -> None:
    config_path, paths = _write_config(tmp_path, enable_multi=True)

    config = INSTALLER_MODULE._load_deployment_config(config_path)
    environment = INSTALLER_MODULE._deployment_environments(config, tmp_path / "installed")[
        "deepkoala-mcp"
    ]

    assert config.deepkoala.allow_multi is True
    assert config.deepkoala.profiles_dir == paths["profiles"]
    assert config.deepkoala.hmmsearch_executable == paths["hmmsearch"]
    assert environment["DEEPKOALA_MCP_ALLOW_MULTI"] == "true"
    assert environment["DEEPKOALA_MCP_PROFILES_DIR"] == str(paths["profiles"])
    assert environment["DEEPKOALA_MCP_HMMSEARCH_EXECUTABLE"] == str(paths["hmmsearch"])


@pytest.mark.parametrize("present", ["neither", "profiles", "hmmsearch"])
def test_deepkoala_multi_requires_both_external_paths(tmp_path: Path, present: str) -> None:
    config_path, paths = _write_config(tmp_path)
    fields = ["allow_multi = true"]
    if present == "profiles":
        fields.append(f"profiles_dir = {json.dumps(str(paths['profiles']))}")
    elif present == "hmmsearch":
        fields.append(f"hmmsearch_executable = {json.dumps(str(paths['hmmsearch']))}")
    _rewrite_deepkoala_config(config_path, paths, "\n".join(fields))

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config_path)

    assert raised.value.code == "deployment_config_invalid"


def test_deepkoala_multi_paths_are_rejected_without_opt_in(tmp_path: Path) -> None:
    config_path, paths = _write_config(tmp_path)
    _rewrite_deepkoala_config(
        config_path,
        paths,
        f"profiles_dir = {json.dumps(str(paths['profiles']))}\n"
        f"hmmsearch_executable = {json.dumps(str(paths['hmmsearch']))}",
    )

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config_path)

    assert raised.value.code == "deployment_config_invalid"


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "profiles_symlink",
        "profiles_permissions",
        "hmmsearch_symlink",
        "hmmsearch_permissions",
        "hmmsearch_not_executable",
    ],
)
def test_deepkoala_multi_rejects_unsafe_external_paths(tmp_path: Path, unsafe_path: str) -> None:
    config_path, paths = _write_config(tmp_path, enable_multi=True)
    if unsafe_path == "profiles_symlink":
        paths["profiles"].rmdir()
        paths["profiles"].symlink_to(paths["private"], target_is_directory=True)
    elif unsafe_path == "profiles_permissions":
        paths["profiles"].chmod(0o775)
    elif unsafe_path == "hmmsearch_symlink":
        paths["hmmsearch"].unlink()
        paths["hmmsearch"].symlink_to(paths["python"])
    elif unsafe_path == "hmmsearch_permissions":
        paths["hmmsearch"].chmod(0o720)
    else:
        paths["hmmsearch"].chmod(0o600)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config_path)

    assert raised.value.code == "deployment_path_invalid"


@pytest.mark.parametrize("resource", ["profiles", "hmmsearch"])
@pytest.mark.parametrize("overlap", ["deep_state", "input", "output"])
def test_deepkoala_multi_resources_must_not_overlap_private_or_handoff_roots(
    tmp_path: Path, resource: str, overlap: str
) -> None:
    config_path, paths = _write_config(tmp_path)
    profiles = paths[overlap] if resource == "profiles" else paths["profiles"]
    hmmsearch = paths["hmmsearch"]
    if resource == "hmmsearch":
        hmmsearch = _write_executable(paths[overlap] / "hmmsearch")
    _rewrite_deepkoala_config(
        config_path,
        paths,
        "allow_multi = true\n"
        f"profiles_dir = {json.dumps(str(profiles))}\n"
        f"hmmsearch_executable = {json.dumps(str(hmmsearch))}",
    )

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config_path)

    assert raised.value.code == "deployment_path_invalid"


@pytest.mark.parametrize("section", ["root", "kegg", "core", "deepkoala", "renderer"])
def test_deployment_config_rejects_unknown_fields(tmp_path: Path, section: str) -> None:
    config, _ = _write_config(tmp_path, extras={section: "unexpected = true"})

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._load_deployment_config(config)

    assert raised.value.code == "deployment_config_invalid"
    assert "unknown fields" in str(raised.value)


def test_source_snapshot_uses_a_complete_source_tree_without_git_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _copy_suite_source(source)

    snapshot = INSTALLER_MODULE._prepare_source_snapshot(source)

    assert snapshot.root == source.resolve()
    assert snapshot.versions == INSTALLER_MODULE._project_versions(source)


def test_source_snapshot_rejects_an_incomplete_source_tree(tmp_path: Path) -> None:
    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._prepare_source_snapshot(tmp_path)

    assert raised.value.code == "source_snapshot_invalid"


def test_generated_plugin_contains_exactly_three_skills_and_three_mcp_servers(
    tmp_path: Path,
) -> None:
    request, snapshot, _, plugin_root, launcher, _ = _materialize_suite_artifacts(tmp_path)

    manifest = json.loads(
        (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    mcp_document = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    marketplace = json.loads(
        (plugin_root.parents[1] / ".agents" / "plugins" / "marketplace.json").read_text(
            encoding="utf-8"
        )
    )
    skill_root = plugin_root / "skills"

    assert manifest["name"] == "kegg-mcp"
    assert manifest["version"] == snapshot.versions["kegg-mcp"]
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert {path.name for path in skill_root.iterdir()} == SKILL_NAMES
    for skill_name in SKILL_NAMES:
        assert (skill_root / skill_name / "SKILL.md").read_bytes() == (
            snapshot.root / ".agents" / "skills" / skill_name / "SKILL.md"
        ).read_bytes()
    assert set(mcp_document) == {"mcpServers"}
    assert set(mcp_document["mcpServers"]) == SERVER_NAMES
    assert marketplace == {
        "name": request.marketplace_name,
        "interface": {"displayName": "Local KEGG MCP Suite"},
        "plugins": [
            {
                "name": "kegg-mcp",
                "source": {"source": "local", "path": "./plugins/kegg-mcp"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                "category": "Education & Research",
            }
        ],
    }
    for server_name, server in mcp_document["mcpServers"].items():
        assert set(server) == {"args", "command", "cwd"}
        assert Path(server["command"]).is_absolute()
        assert Path(server["command"]).is_relative_to(request.install_root / "runtimes")
        assert server["args"] == ["-I", str(launcher), server_name]
        assert server["cwd"] == "."


def test_private_deployment_values_do_not_enter_generated_plugin_metadata(
    tmp_path: Path,
) -> None:
    request, _, config, plugin_root, _, paths = _materialize_suite_artifacts(
        tmp_path, enable_multi=True
    )

    public_metadata = "\n".join(
        (
            (plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"),
            (plugin_root / ".mcp.json").read_text(encoding="utf-8"),
        )
    )
    assert not (plugin_root / "install-provenance.json").exists()
    deployment_path = request.install_root / "deployment" / "deployment.json"
    private_manifest = deployment_path.read_text(encoding="utf-8")
    private_values = {
        str(request.install_root / "deepkoala" / "source"),
        str(request.install_root / "deepkoala" / "venv" / "bin" / "python"),
        str(config.deepkoala.state_root),
        str(config.renderer.state_root),
        str(config.kegg.rate_limit_root),
        str(config.core.result_store_path),
        str(paths["input"]),
        str(paths["output"]),
        str(paths["profiles"]),
        str(paths["hmmsearch"]),
    }

    assert all(value not in public_metadata for value in private_values)
    assert all(value in private_manifest for value in private_values)
    assert '"DEEPKOALA_MCP_ALLOW_MULTI": "true"' in private_manifest
    assert stat.S_IMODE(deployment_path.stat().st_mode) == 0o600
    assert (
        "environments"
        not in json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"][
            "kegg-mcp"
        ]
    )


def test_offline_environment_omits_unrequested_licensed_namespace_confirmation(
    tmp_path: Path,
) -> None:
    config_path, paths = _write_config(tmp_path)
    configured = INSTALLER_MODULE._load_deployment_config(config_path)
    offline = INSTALLER_MODULE.DeploymentConfig(
        kegg=INSTALLER_MODULE.KeggAccessConfig(
            mode="offline_cache",
            academic_use_confirmed=False,
            licensed_endpoint=None,
            licensed_use_confirmed=False,
            cache_path=paths["private"] / "cache.sqlite3",
            rate_limit_root=configured.kegg.rate_limit_root,
        ),
        core=configured.core,
        deepkoala=configured.deepkoala,
        renderer=configured.renderer,
    )

    environments = INSTALLER_MODULE._deployment_environments(offline, tmp_path / "installed")
    core = environments["kegg-mcp"]
    renderer = environments["kegg-render-mcp"]

    assert core["KEGG_MCP_ACCESS_MODE"] == "offline_cache"
    assert renderer["KEGG_RENDER_MCP_ACCESS_MODE"] == "offline_cache"
    assert core["KEGG_MCP_CACHE_PATH"] == str(offline.kegg.cache_path)
    assert renderer["KEGG_RENDER_MCP_CACHE_PATH"] == str(offline.kegg.cache_path)
    assert core[INSTALLER_MODULE.RATE_LIMIT_ROOT_ENV] == str(offline.kegg.rate_limit_root)
    assert renderer[INSTALLER_MODULE.RATE_LIMIT_ROOT_ENV] == str(offline.kegg.rate_limit_root)
    assert "KEGG_MCP_LICENSED_USE_CONFIRMED" not in core
    assert "KEGG_RENDER_MCP_LICENSED_USE_CONFIRMED" not in renderer


@pytest.mark.parametrize("allow_downloads", [False, True])
def test_runtime_install_uses_three_locked_environments_and_never_downloads_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    allow_downloads: bool,
) -> None:
    original, snapshot, _, _ = _suite_install_inputs(tmp_path)
    request = INSTALLER_MODULE.InstallRequest(
        install_root=original.install_root,
        marketplace_name=original.marketplace_name,
        uv=original.uv,
        codex=original.codex,
        git=original.git,
        python=original.python,
        platform_profile=original.platform_profile,
        allow_locked_dependency_downloads=allow_downloads,
        dry_run=False,
        allow_deepkoala_install=True,
    )
    request.install_root.mkdir(mode=0o700)
    invocations: list[tuple[tuple[str, ...], Path | None, dict[str, str]]] = []

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert environment is not None
        captured = dict(environment)
        invocations.append((tuple(argv), cwd, captured))
        Path(captured["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True)
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)
    monkeypatch.setenv("UV_PROJECT", str(tmp_path / "hostile-project"))
    monkeypatch.setenv("UV_NO_SYNC", "1")
    monkeypatch.setenv("UV_OVERRIDE", str(tmp_path / "hostile-overrides.txt"))
    monkeypatch.setenv("PIP_INDEX_URL", "https://example.invalid/simple")

    INSTALLER_MODULE._install_runtimes(request, snapshot)

    assert len(invocations) == 3
    assert {Path(env["UV_PROJECT_ENVIRONMENT"]).name for _, _, env in invocations} == {
        "core",
        "deepkoala",
        "renderer",
    }
    assert {cwd for _, cwd, _ in invocations} == {
        snapshot.root,
        snapshot.root / "companions" / "deepkoala-mcp",
        snapshot.root / "companions" / "kegg-render-mcp",
    }
    for argv, _, environment in invocations:
        assert argv[1] == "sync"
        assert {"--locked", "--no-dev", "--no-editable", "--no-python-downloads"} <= set(argv)
        assert environment["UV_PYTHON_DOWNLOADS"] == "never"
        assert environment["UV_NO_SYSTEM_CONFIG"] == "1"
        assert environment["XDG_CONFIG_HOME"] == str(request.install_root / "uv-config")
        assert "UV_NO_CONFIG" not in environment
        assert "UV_PROJECT" not in environment
        assert "UV_NO_SYNC" not in environment
        assert "UV_OVERRIDE" not in environment
        assert "PIP_INDEX_URL" not in environment
        assert ("--offline" in argv) is (not allow_downloads)
        assert (environment.get("UV_OFFLINE") == "1") is (not allow_downloads)


def test_first_install_requires_explicit_deepkoala_confirmation(tmp_path: Path) -> None:
    original, snapshot, config, _ = _suite_install_inputs(tmp_path)
    request = INSTALLER_MODULE.InstallRequest(
        install_root=original.install_root,
        marketplace_name=original.marketplace_name,
        uv=original.uv,
        codex=original.codex,
        git=original.git,
        python=original.python,
        platform_profile=original.platform_profile,
        allow_locked_dependency_downloads=False,
        dry_run=False,
        allow_deepkoala_install=False,
    )

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._perform_install(request, config, snapshot)

    assert raised.value.code == "deepkoala_install_confirmation_required"
    assert not request.install_root.exists()


def test_managed_deepkoala_install_uses_official_repository_and_bundled_202502(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, config, _ = _suite_install_inputs(tmp_path)
    request.install_root.mkdir(mode=0o700)
    commands: list[tuple[str, ...]] = []

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        command: tuple[str, ...] = tuple(argv)
        commands.append(command)
        if len(command) > 1 and command[1] == "init":
            checkout = Path(command[-1])
            resource_root = checkout / "resources" / "202502"
            resource_root.mkdir(parents=True)
            (checkout / "requirements.txt").write_text("torch\n", encoding="utf-8")
            for model in config.deepkoala.allowed_models:
                (resource_root / f"weights_{model}.pt").write_bytes(b"weights")
                (resource_root / f"ko_config_{model}.json").write_text("{}", encoding="utf-8")
        elif "rev-parse" in command:
            return subprocess.CompletedProcess(
                list(command),
                0,
                stdout=f"{INSTALLER_MODULE.DEEPKOALA_REVISION}\n",
                stderr="",
            )
        elif "venv" in command:
            subprocess.run(
                [sys.executable, *command[1:]],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        return subprocess.CompletedProcess(list(command), 0, stdout="", stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)

    INSTALLER_MODULE._install_managed_deepkoala(request, config.deepkoala)

    assert commands[0][1:3] == ("init", "--quiet")
    assert commands[1][3:8] == ("fetch", "--quiet", "--depth", "1", "--no-tags")
    assert commands[1][-2] == INSTALLER_MODULE.DEEPKOALA_REPOSITORY
    assert commands[1][-1] == INSTALLER_MODULE.DEEPKOALA_REVISION
    assert commands[2][3:] == (
        "checkout",
        "--quiet",
        "--detach",
        "FETCH_HEAD",
    )
    assert commands[3][3:] == ("rev-parse", "--verify", "HEAD")
    assert commands[4][1:4] == ("-I", "-m", "venv")
    assert "--copies" in commands[4]
    assert commands[5][1:5] == ("-I", "-m", "pip", "--isolated")
    assert INSTALLER_MODULE.DEFAULT_DEEPKOALA_MODEL_DATE == "202502"
    serialized = " ".join(part for command in commands for part in command).lower()
    assert "clone" not in serialized
    assert " origin " not in f" {serialized} "
    assert "hmmer" not in serialized
    assert "kofam" not in serialized
    assert "multi" not in serialized
    managed_python = request.install_root / "deepkoala" / "venv" / "bin" / "python"
    assert not managed_python.is_symlink()
    identity = subprocess.run(
        [
            str(managed_python),
            "-I",
            "-c",
            "import sys;print(sys.prefix != sys.base_prefix)",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert identity.stdout.strip() == "True"


def test_managed_deepkoala_install_rejects_a_revision_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, config, _ = _suite_install_inputs(tmp_path)
    request.install_root.mkdir(mode=0o700)

    def mismatched_revision(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        command = tuple(argv)
        if len(command) > 1 and command[1] == "init":
            Path(command[-1]).mkdir(parents=True)
        output = "0" * 40 if "rev-parse" in command else ""
        return subprocess.CompletedProcess(list(command), 0, stdout=output, stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", mismatched_revision)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._install_managed_deepkoala(request, config.deepkoala)

    assert raised.value.code == "deepkoala_install_failed"
    assert "release pin" in str(raised.value)


def test_runtime_verification_requires_deepkoala_local_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, config, _ = _suite_install_inputs(tmp_path)
    environments = INSTALLER_MODULE._deployment_environments(config, request.install_root)

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        command = tuple(argv)
        if "deepkoala-mcp" in command[0]:
            output = json.dumps(
                {"configuration_valid": True, "route_state": "model_resources_unavailable"}
            )
            return subprocess.CompletedProcess(list(command), 2, stdout=output, stderr="")
        return subprocess.CompletedProcess(
            list(command),
            0,
            stdout=json.dumps({"configuration_valid": True}),
            stderr="",
        )

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._verify_runtime_configuration(request, environments)

    assert raised.value.code == "runtime_configuration_invalid"


@pytest.mark.parametrize("profile_name", ["linux", "darwin-arm64"])
def test_runtime_verification_accepts_cpu_only_local_ready_for_each_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile_name: str,
) -> None:
    original, _, config, _ = _suite_install_inputs(tmp_path)
    profiles = {
        "linux": INSTALLER_MODULE.LINUX_PLATFORM_PROFILE,
        "darwin-arm64": INSTALLER_MODULE.DARWIN_ARM64_PLATFORM_PROFILE,
    }
    profile = profiles[profile_name]
    request = replace(original, platform_profile=profile)
    environments = INSTALLER_MODULE._deployment_environments(
        config,
        request.install_root,
        profile,
    )

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        command = tuple(argv)
        if "deepkoala-mcp" in command[0]:
            output = json.dumps(
                {
                    "configuration_valid": True,
                    "route_state": "local_ready",
                    "allowed_devices": list(profile.deepkoala_allowed_devices),
                    "cuda_available": False,
                    "mps_available": False,
                }
            )
        else:
            output = json.dumps({"configuration_valid": True})
        return subprocess.CompletedProcess(list(command), 0, stdout=output, stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)

    INSTALLER_MODULE._verify_runtime_configuration(request, environments)


@pytest.mark.parametrize(
    "invalid_contract",
    ["devices", "mps_availability_type", "cuda_availability_missing"],
)
def test_darwin_runtime_verification_requires_exact_mps_doctor_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_contract: str,
) -> None:
    original, _, config, _ = _suite_install_inputs(tmp_path)
    profile = INSTALLER_MODULE.DARWIN_ARM64_PLATFORM_PROFILE
    request = replace(original, platform_profile=profile)
    environments = INSTALLER_MODULE._deployment_environments(
        config,
        request.install_root,
        profile,
    )

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        command = tuple(argv)
        if "deepkoala-mcp" in command[0]:
            document: dict[str, object] = {
                "configuration_valid": True,
                "route_state": "local_ready",
                "allowed_devices": ["cpu", "mps"],
                "cuda_available": False,
                "mps_available": False,
            }
            if invalid_contract == "devices":
                document["allowed_devices"] = ["cpu", "cuda"]
            elif invalid_contract == "mps_availability_type":
                document["mps_available"] = "false"
            else:
                document.pop("cuda_available", None)
            output = json.dumps(document)
        else:
            output = json.dumps({"configuration_valid": True})
        return subprocess.CompletedProcess(list(command), 0, stdout=output, stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._verify_runtime_configuration(request, environments)

    assert raised.value.code == "runtime_configuration_invalid"


def test_subprocess_environment_removes_tool_and_python_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hostile = {
        "GIT_CONFIG_GLOBAL": "/hostile/gitconfig",
        "GIT_DIR": "/hostile/git",
        "PIP_CONFIG_FILE": "/hostile/pip.ini",
        "UV_PROJECT": "/hostile/project",
        "UV_NO_SOURCES": "1",
        "UV_NO_SYNC": "1",
        "PYTHONHOME": "/hostile/python",
        "PYTHONPATH": "/hostile/imports",
        "VIRTUAL_ENV": "/hostile/venv",
        "KEGG_MCP_ACCESS_MODE": "licensed",
    }
    for key, value in hostile.items():
        monkeypatch.setenv(key, value)

    environment = INSTALLER_MODULE._safe_subprocess_environment(
        {"UV_PROJECT_ENVIRONMENT": "/explicit/runtime", "UV_NO_SYSTEM_CONFIG": "1"}
    )

    assert all(key not in environment for key in hostile)
    assert environment["UV_PROJECT_ENVIRONMENT"] == "/explicit/runtime"
    assert environment["UV_NO_SYSTEM_CONFIG"] == "1"
    assert environment["PYTHONNOUSERSITE"] == "1"


@pytest.mark.parametrize(
    ("profile_name", "runtime_platform", "machine", "macos_version"),
    [
        ("linux", "linux", "x86_64", ""),
        ("darwin-arm64", "darwin", "arm64", "14.0"),
    ],
)
def test_python_preflight_requires_a_native_runtime_matching_the_host_profile(
    monkeypatch: pytest.MonkeyPatch,
    profile_name: str,
    runtime_platform: str,
    machine: str,
    macos_version: str,
) -> None:
    profiles = {
        "linux": INSTALLER_MODULE.LINUX_PLATFORM_PROFILE,
        "darwin-arm64": INSTALLER_MODULE.DARWIN_ARM64_PLATFORM_PROFILE,
    }

    def runtime_metadata(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        output = json.dumps([[3, 11], "CPython", runtime_platform, machine, macos_version])
        return subprocess.CompletedProcess(list(argv), 0, stdout=output, stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", runtime_metadata)

    INSTALLER_MODULE._validate_python(Path("/absolute/python"), profiles[profile_name])


def test_darwin_python_preflight_rejects_rosetta_without_echoing_machine_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_machine_value = "x86_64-private-value"

    def translated_runtime(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        output = json.dumps([[3, 11], "CPython", "darwin", private_machine_value, "14.0"])
        return subprocess.CompletedProcess(list(argv), 0, stdout=output, stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", translated_runtime)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._validate_python(
            Path("/absolute/python"),
            INSTALLER_MODULE.DARWIN_ARM64_PLATFORM_PROFILE,
        )

    assert raised.value.code == "python_runtime_unsupported"
    assert "Rosetta" in str(raised.value)
    assert private_machine_value not in str(raised.value)


@pytest.mark.parametrize("private_version", ["13.6.9", "private-version-value"])
def test_darwin_python_preflight_rejects_unsupported_or_invalid_macos_version(
    monkeypatch: pytest.MonkeyPatch,
    private_version: str,
) -> None:
    def runtime_metadata(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        output = json.dumps([[3, 11], "CPython", "darwin", "arm64", private_version])
        return subprocess.CompletedProcess(list(argv), 0, stdout=output, stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", runtime_metadata)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._validate_python(
            Path("/absolute/python"),
            INSTALLER_MODULE.DARWIN_ARM64_PLATFORM_PROFILE,
        )

    assert raised.value.code == "python_runtime_unsupported"
    assert "macOS 14 or later" in str(raised.value)
    assert private_version not in str(raised.value)


def test_uv_preflight_requires_uv_identity_and_sync_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        command = tuple(argv)
        calls.append(command)
        if command[-1] == "--version":
            output = "uv 0.11.28\n"
        else:
            output = "\n".join(INSTALLER_MODULE.UV_REQUIRED_SYNC_OPTIONS)
        return subprocess.CompletedProcess(list(command), 0, stdout=output, stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)

    INSTALLER_MODULE._validate_uv(Path("/absolute/uv"))

    assert calls == [("/absolute/uv", "--version"), ("/absolute/uv", "sync", "--help")]


def test_uv_preflight_rejects_an_executable_that_is_not_uv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def not_uv(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, environment
        return subprocess.CompletedProcess(list(argv), 0, stdout="", stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", not_uv)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._validate_uv(Path("/not-uv"))

    assert raised.value.code == "uv_runtime_unsupported"


def test_local_command_runner_enforces_time_and_output_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(INSTALLER_MODULE, "DEFAULT_COMMAND_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(INSTALLER_MODULE.InstallError) as timed_out:
        INSTALLER_MODULE._run_command([sys.executable, "-c", "import time;time.sleep(1)"])
    assert timed_out.value.code == "tool_execution_failed"

    monkeypatch.setattr(INSTALLER_MODULE, "DEFAULT_COMMAND_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(INSTALLER_MODULE, "MAX_COMMAND_OUTPUT_BYTES", 1_024)
    with pytest.raises(INSTALLER_MODULE.InstallError) as oversized:
        INSTALLER_MODULE._run_command(
            [sys.executable, "-c", "import sys;sys.stdout.write('x'*2048)"]
        )
    assert oversized.value.code == "tool_execution_failed"


def test_installed_launcher_replaces_ambient_suite_configuration_and_executes_one_server(
    tmp_path: Path,
) -> None:
    install_root = _mkdir(tmp_path / "installed", private=True)
    deployment = _mkdir(install_root / "deployment", private=True)
    runtime_bin = _mkdir(install_root / "runtimes" / "core" / "bin", private=True)
    output = tmp_path / "selected-environment.json"
    command = runtime_bin / "capture-environment"
    command.write_text(
        f"#!{sys.executable}\n"
        "import json, os\n"
        "with open(os.environ['TEST_OUTPUT'], 'w', encoding='utf-8') as stream:\n"
        "    json.dump({\n"
        "        'mode': os.environ.get('KEGG_MCP_ACCESS_MODE'),\n"
        "        'ambient': os.environ.get('KEGG_MCP_UNDECLARED'),\n"
        "        'pythonpath': os.environ.get('PYTHONPATH'),\n"
        "        'safe_path': os.environ.get('PYTHONSAFEPATH'),\n"
        "    }, stream)\n",
        encoding="utf-8",
    )
    command.chmod(0o700)
    launcher = deployment / "run-installed-mcp.py"
    shutil.copy2(PROJECT_ROOT / "scripts" / "run-installed-mcp.py", launcher)
    launcher.chmod(0o700)
    commands = {name: str(command) for name in SERVER_NAMES}
    environments: dict[str, dict[str, str]] = {name: {} for name in SERVER_NAMES}
    environments["kegg-mcp"] = {
        "KEGG_MCP_ACCESS_MODE": "offline_cache",
        "TEST_OUTPUT": str(output),
    }
    manifest = deployment / "deployment.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": INSTALLER_MODULE.DEPLOYMENT_MANIFEST_SCHEMA_VERSION,
                "commands": commands,
                "environments": environments,
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    environment = os.environ.copy()
    environment["KEGG_MCP_ACCESS_MODE"] = "public_academic"
    environment["KEGG_MCP_UNDECLARED"] = "must-not-leak"
    environment["PYTHONPATH"] = str(tmp_path / "ambient-imports")

    result = subprocess.run(
        [sys.executable, str(launcher), "kegg-mcp"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "mode": "offline_cache",
        "ambient": None,
        "pythonpath": None,
        "safe_path": "1",
    }


def test_codex_verification_requires_exact_transports_not_only_matching_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _, _ = _suite_install_inputs(tmp_path)
    launcher = request.install_root / "deployment" / "run-installed-mcp.py"
    entries: dict[str, dict[str, object]] = {}
    for server_name in SERVER_NAMES:
        runtime = INSTALLER_MODULE.RUNTIME_COMMANDS[server_name][0]
        entries[server_name] = {
            "name": server_name,
            "enabled": True,
            "transport": {
                "type": "stdio",
                "command": str(INSTALLER_MODULE._runtime_python(request.install_root, runtime)),
                "args": ["-I", str(launcher), server_name],
                "env": None,
                "env_vars": [],
            },
        }

    def mcp_entries(_: Any) -> dict[str, dict[str, object]]:
        return entries

    monkeypatch.setattr(INSTALLER_MODULE, "_codex_mcp_entries", mcp_entries)

    assert INSTALLER_MODULE._codex_mcp_bindings_match(request) is True

    foreign_transport = entries["kegg-mcp"]["transport"]
    assert isinstance(foreign_transport, dict)
    foreign_transport["command"] = "/absolute/foreign/kegg-mcp"
    assert INSTALLER_MODULE._codex_mcp_bindings_match(request) is False


def test_codex_plugin_readiness_requires_the_exact_generated_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, snapshot, _, _ = _suite_install_inputs(tmp_path)
    expected_version = snapshot.versions["kegg-mcp"]
    installed = {
        "pluginId": f"kegg-mcp@{request.marketplace_name}",
        "name": "kegg-mcp",
        "marketplaceName": request.marketplace_name,
        "version": expected_version,
        "installed": True,
        "enabled": True,
    }

    def json_command(*_: object, **__: object) -> dict[str, object]:
        return {"installed": [installed]}

    monkeypatch.setattr(INSTALLER_MODULE, "_json_command", json_command)

    assert INSTALLER_MODULE._plugin_is_ready(request, expected_version) is True
    installed["version"] = "0.0.0"
    assert INSTALLER_MODULE._plugin_is_ready(request, expected_version) is False


def test_codex_plugin_cache_requires_the_exact_generated_skill_bundle(tmp_path: Path) -> None:
    _, snapshot, _, plugin_root, _, _ = _materialize_suite_artifacts(tmp_path)
    cached_root = tmp_path / "codex-cache" / snapshot.versions["kegg-mcp"]
    shutil.copytree(plugin_root, cached_root)
    entries = {
        server_name: {"transport": {"cwd": str(cached_root)}} for server_name in SERVER_NAMES
    }

    assert (
        INSTALLER_MODULE._codex_plugin_cache_matches(
            plugin_root,
            snapshot.versions["kegg-mcp"],
            entries,
        )
        is True
    )

    cached_manifest = cached_root / ".codex-plugin" / "plugin.json"
    cached_manifest.write_text("{}\n", encoding="utf-8")
    assert (
        INSTALLER_MODULE._codex_plugin_cache_matches(
            plugin_root,
            snapshot.versions["kegg-mcp"],
            entries,
        )
        is False
    )
    shutil.copy2(plugin_root / ".codex-plugin" / "plugin.json", cached_manifest)

    cached_skill = cached_root / "skills" / "kegg-ko-analysis" / "SKILL.md"
    cached_skill.write_text(cached_skill.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert (
        INSTALLER_MODULE._codex_plugin_cache_matches(
            plugin_root,
            snapshot.versions["kegg-mcp"],
            entries,
        )
        is False
    )


def test_plugin_registration_verifies_version_bindings_and_cached_skills(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, snapshot, _, plugin_root, launcher, _ = _materialize_suite_artifacts(tmp_path)
    marketplace_root = plugin_root.parents[1]
    cached_root = tmp_path / "codex-cache" / snapshot.versions["kegg-mcp"]
    shutil.copytree(plugin_root, cached_root)
    entries: dict[str, dict[str, object]] = {}
    for server_name in SERVER_NAMES:
        runtime = INSTALLER_MODULE.RUNTIME_COMMANDS[server_name][0]
        entries[server_name] = {
            "name": server_name,
            "enabled": True,
            "transport": {
                "type": "stdio",
                "command": str(INSTALLER_MODULE._runtime_python(request.install_root, runtime)),
                "args": ["-I", str(launcher), server_name],
                "env": None,
                "env_vars": [],
                "cwd": str(cached_root),
            },
        }
    installed = {
        "pluginId": f"kegg-mcp@{request.marketplace_name}",
        "name": "kegg-mcp",
        "marketplaceName": request.marketplace_name,
        "version": snapshot.versions["kegg-mcp"],
        "installed": True,
        "enabled": True,
    }
    commands: list[tuple[str, ...]] = []

    def run_command(
        argv: tuple[str, ...] | list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        commands.append(command)
        return subprocess.CompletedProcess(list(command), 0, stdout="{}", stderr="")

    def json_command(*_: object, **__: object) -> dict[str, object]:
        return {"installed": [installed]}

    def mcp_entries(_: Any) -> dict[str, dict[str, object]]:
        return entries

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)
    monkeypatch.setattr(INSTALLER_MODULE, "_json_command", json_command)
    monkeypatch.setattr(INSTALLER_MODULE, "_codex_mcp_entries", mcp_entries)
    journal = INSTALLER_MODULE.RegistrationJournal()

    INSTALLER_MODULE._register_plugin(request, marketplace_root, journal)

    assert commands == [
        (
            str(request.codex),
            "plugin",
            "marketplace",
            "add",
            str(marketplace_root),
            "--json",
        ),
        (
            str(request.codex),
            "plugin",
            "add",
            f"kegg-mcp@{request.marketplace_name}",
            "--json",
        ),
    ]
    assert journal == INSTALLER_MODULE.RegistrationJournal(
        marketplace_attempted=True,
        marketplace_added=True,
        plugin_attempted=True,
        plugin_added=True,
    )


def test_successful_transaction_publishes_complete_generated_suite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, snapshot, config, _ = _suite_install_inputs(tmp_path)
    directory_sync_states: list[tuple[bool, bool]] = []
    sync_directory = INSTALLER_MODULE._fsync_directory

    def no_op(*_: object) -> None:
        return None

    def register(
        actual_request: Any,
        marketplace_root: Path,
        journal: Any,
    ) -> None:
        assert actual_request is request
        assert marketplace_root == request.install_root / "marketplace"
        assert (marketplace_root / ".agents" / "plugins" / "marketplace.json").is_file()
        journal.marketplace_attempted = True
        journal.marketplace_added = True
        journal.plugin_attempted = True
        journal.plugin_added = True

    def record_directory_sync(path: Path) -> None:
        sync_directory(path)
        directory_sync_states.append(
            (
                (path / "installation.json").exists(),
                (path / ".incomplete").exists(),
            )
        )

    monkeypatch.setattr(INSTALLER_MODULE, "_install_runtimes", no_op)
    monkeypatch.setattr(INSTALLER_MODULE, "_install_managed_deepkoala", no_op)
    monkeypatch.setattr(INSTALLER_MODULE, "_verify_distribution_versions", no_op)
    monkeypatch.setattr(INSTALLER_MODULE, "_verify_runtime_configuration", no_op)
    monkeypatch.setattr(INSTALLER_MODULE, "_register_plugin", register)
    monkeypatch.setattr(INSTALLER_MODULE, "_fsync_directory", record_directory_sync)

    INSTALLER_MODULE._perform_install(request, config, snapshot)

    assert not (request.install_root / ".incomplete").exists()
    assert not (request.install_root / ".complete").exists()
    installation = json.loads(
        (request.install_root / "installation.json").read_text(encoding="utf-8")
    )
    assert directory_sync_states == [
        (False, False),
        (False, True),
        (True, True),
        (True, False),
    ]
    assert installation["status"] == "complete"
    assert set(installation["servers"]) == SERVER_NAMES
    assert set(installation["skills"]) == SKILL_NAMES
    assert installation["deepkoala_repository"] == INSTALLER_MODULE.DEEPKOALA_REPOSITORY
    assert installation["deepkoala_revision"] == INSTALLER_MODULE.DEEPKOALA_REVISION
    assert installation["deepkoala_default_model_date"] == "202502"
    assert set(installation) == {
        "deepkoala_default_model_date",
        "deepkoala_repository",
        "deepkoala_revision",
        "distribution_versions",
        "marketplace",
        "plugin",
        "schema_version",
        "servers",
        "skills",
        "status",
    }
    assert stat.S_IMODE((request.install_root / "installation.json").stat().st_mode) == 0o600


def test_install_summary_requires_a_new_task_without_requesting_reinstallation(
    tmp_path: Path,
) -> None:
    _, snapshot, _, _ = _suite_install_inputs(tmp_path)

    installed = INSTALLER_MODULE._safe_summary(snapshot, dry_run=False)
    validated = INSTALLER_MODULE._safe_summary(snapshot, dry_run=True)

    assert installed["status"] == "installed"
    assert installed["new_task_required"] is True
    assert installed["current_task_reload_supported"] is False
    assert installed["repeat_installation_required"] is False
    assert installed["next_action"] == "open_new_codex_task"
    assert installed["deepkoala_revision"] == INSTALLER_MODULE.DEEPKOALA_REVISION
    assert validated["status"] == "validated"
    assert validated["new_task_required"] is False
    assert validated["current_task_reload_supported"] is False
    assert validated["repeat_installation_required"] is False
    assert validated["next_action"] == "run_confirmed_install"
    assert validated["deepkoala_revision"] == INSTALLER_MODULE.DEEPKOALA_REVISION


@pytest.mark.parametrize(
    ("concurrent_plugin_visible", "expected_code", "install_root_preserved"),
    [(False, "plugin_registration_failed", False), (True, "installation_rollback_failed", True)],
)
def test_failed_plugin_add_never_removes_an_unproven_visible_plugin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrent_plugin_visible: bool,
    expected_code: str,
    install_root_preserved: bool,
) -> None:
    request, snapshot, config, paths = _suite_install_inputs(tmp_path)
    commands: list[tuple[str, ...]] = []
    plugin_present = concurrent_plugin_visible
    marketplace_present = True

    def no_op(*_: object) -> None:
        return None

    monkeypatch.setattr(INSTALLER_MODULE, "_install_runtimes", no_op)
    monkeypatch.setattr(INSTALLER_MODULE, "_install_managed_deepkoala", no_op)
    monkeypatch.setattr(INSTALLER_MODULE, "_verify_distribution_versions", no_op)
    monkeypatch.setattr(INSTALLER_MODULE, "_verify_runtime_configuration", no_op)

    def materialize_deployment(*_: object) -> Path:
        deployment = request.install_root / "deployment"
        deployment.mkdir(mode=0o700)
        return _write_executable(deployment / "run-installed-mcp.py")

    def materialize_plugin(*_: object) -> Path:
        marketplace = request.install_root / "marketplace"
        marketplace.mkdir(mode=0o700)
        return marketplace

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal marketplace_present, plugin_present
        del cwd, environment
        command = tuple(argv)
        commands.append(command)
        operation = command[1:]
        returncode = 0
        document: object = {}
        if operation[:2] == ("plugin", "add"):
            returncode = 1
        elif operation == ("plugin", "list", "--json"):
            installed: list[object] = []
            if plugin_present:
                installed.append(
                    {
                        "pluginId": f"kegg-mcp@{request.marketplace_name}",
                        "name": "kegg-mcp",
                        "marketplaceName": request.marketplace_name,
                    }
                )
            document = {"installed": installed}
        elif operation[:3] == ("plugin", "marketplace", "remove"):
            marketplace_present = False
        elif operation == ("plugin", "marketplace", "list", "--json"):
            marketplaces: list[object] = []
            if marketplace_present:
                marketplaces.append(
                    {
                        "name": request.marketplace_name,
                        "root": str(request.install_root / "marketplace"),
                    }
                )
            document = {"marketplaces": marketplaces}
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            stdout=json.dumps(document),
            stderr="simulated failure" if returncode else "",
        )

    monkeypatch.setattr(INSTALLER_MODULE, "_materialize_deployment", materialize_deployment)
    monkeypatch.setattr(INSTALLER_MODULE, "_materialize_plugin", materialize_plugin)
    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)

    with pytest.raises(INSTALLER_MODULE.InstallError) as raised:
        INSTALLER_MODULE._perform_install(request, config, snapshot)

    selector = f"kegg-mcp@{request.marketplace_name}"
    marketplace_root = request.install_root / "marketplace"
    assert raised.value.code == expected_code
    expected_commands = [
        (
            str(request.codex),
            "plugin",
            "marketplace",
            "add",
            str(marketplace_root),
            "--json",
        ),
        (str(request.codex), "plugin", "add", selector, "--json"),
        (
            str(request.codex),
            "plugin",
            "marketplace",
            "list",
            "--json",
        ),
        (str(request.codex), "plugin", "list", "--json"),
    ]
    if not concurrent_plugin_visible:
        expected_commands.extend(
            [
                (
                    str(request.codex),
                    "plugin",
                    "marketplace",
                    "remove",
                    request.marketplace_name,
                    "--json",
                ),
                (
                    str(request.codex),
                    "plugin",
                    "marketplace",
                    "list",
                    "--json",
                ),
            ]
        )
    assert commands == expected_commands
    assert request.install_root.exists() is install_root_preserved
    rollback_marker = request.install_root / ".rollback-required"
    assert rollback_marker.exists() is install_root_preserved
    if install_root_preserved:
        marker = json.loads(rollback_marker.read_text(encoding="utf-8"))
        assert marker["managed_marketplace"] == request.marketplace_name
        assert marker["managed_plugin"] == "kegg-mcp"
        assert marker["initial_failure_code"] == "plugin_registration_failed"
        assert marker["registration_stage"] == "plugin_add_attempted"
        assert stat.S_IMODE(rollback_marker.stat().st_mode) == 0o600
        serialized = json.dumps(marker)
        assert all(str(path) not in serialized for path in paths.values())
        assert not (request.install_root / "installation.json").exists()


@pytest.mark.parametrize("plugin_remove_succeeds", [True, False])
def test_rollback_removes_only_a_confirmed_successful_registration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_remove_succeeds: bool,
) -> None:
    request, _, _, _ = _suite_install_inputs(tmp_path)
    marketplace_root = _mkdir(request.install_root / "marketplace", private=True)
    plugin_present = True
    marketplace_present = True
    commands: list[tuple[str, ...]] = []

    def marketplaces(_: Any) -> dict[str, str | None]:
        return {request.marketplace_name: str(marketplace_root)} if marketplace_present else {}

    def plugin_installed(_: Any) -> bool:
        return plugin_present

    def run_command(
        argv: tuple[str, ...] | list[str],
        *,
        cwd: Path | None = None,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal marketplace_present, plugin_present
        del cwd, environment
        command = tuple(argv)
        commands.append(command)
        if command[1:3] == ("plugin", "remove"):
            if plugin_remove_succeeds:
                plugin_present = False
                returncode = 0
            else:
                returncode = 1
        elif command[1:4] == ("plugin", "marketplace", "remove"):
            marketplace_present = False
            returncode = 0
        else:
            raise AssertionError(f"unexpected command: {command!r}")
        return subprocess.CompletedProcess(list(command), returncode, stdout="{}", stderr="")

    monkeypatch.setattr(INSTALLER_MODULE, "_codex_marketplaces", marketplaces)
    monkeypatch.setattr(INSTALLER_MODULE, "_plugin_is_installed", plugin_installed)
    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", run_command)
    journal = INSTALLER_MODULE.RegistrationJournal(
        marketplace_attempted=True,
        marketplace_added=True,
        plugin_attempted=True,
        plugin_added=True,
    )

    assert INSTALLER_MODULE._rollback_codex(request, journal) is plugin_remove_succeeds
    assert commands[0][1:3] == ("plugin", "remove")
    assert any(command[1:4] == ("plugin", "marketplace", "remove") for command in commands) is (
        plugin_remove_succeeds
    )
    assert marketplace_present is (not plugin_remove_succeeds)


def test_rollback_refuses_a_replaced_marketplace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _, _, _ = _suite_install_inputs(tmp_path)
    _mkdir(request.install_root / "marketplace", private=True)
    foreign_root = _mkdir(tmp_path / "foreign-marketplace", private=True)

    def marketplaces(_: Any) -> dict[str, str | None]:
        return {request.marketplace_name: str(foreign_root)}

    def plugin_installed(_: Any) -> bool:
        return True

    monkeypatch.setattr(INSTALLER_MODULE, "_codex_marketplaces", marketplaces)
    monkeypatch.setattr(INSTALLER_MODULE, "_plugin_is_installed", plugin_installed)

    def unexpected_command(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("rollback must not mutate a replaced marketplace")

    monkeypatch.setattr(INSTALLER_MODULE, "_run_command", unexpected_command)
    journal = INSTALLER_MODULE.RegistrationJournal(
        marketplace_attempted=True,
        marketplace_added=True,
        plugin_attempted=True,
        plugin_added=True,
    )

    assert INSTALLER_MODULE._rollback_codex(request, journal) is False
