"""Contracts for installing repository Skills into a Codex workspace."""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = PROJECT_ROOT / "scripts" / "install-skills.py"
ARCHIVE_COMMIT = "a" * 40
SKILL_NAMES = {
    "deepkoala-annotation",
    "kegg-ko-analysis",
    "kegg-pathway-rendering",
}
MARKER_NAME = ".kegg-mcp-skill-install.json"


class InjectedBaseException(BaseException):
    """Non-standard termination used to exercise transaction recovery."""


def _load_installer_module(installer: Path = INSTALLER) -> Any:
    module_name = f"kegg_skill_installer_test_{abs(hash(str(installer)))}"
    spec = importlib.util.spec_from_file_location(module_name, installer)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


INSTALLER_MODULE = _load_installer_module()


def _cleanup_prepared(prepared: Any) -> None:
    shutil.rmtree(prepared.root, ignore_errors=True)


def _managed_target_identities(workspace: Path) -> dict[str, tuple[int, int]]:
    skill_root = workspace / ".agents" / "skills"
    return {
        name: ((skill_root / name).stat().st_dev, (skill_root / name).stat().st_ino)
        for name in SKILL_NAMES
    }


def _transaction_directories(workspace: Path) -> tuple[Path, ...]:
    return tuple((workspace / ".agents" / "skills").glob(".kegg-mcp-skill-install-*"))


def _source_tree_digest(source: Path) -> str:
    calculate = INSTALLER_MODULE._source_tree_digest
    result: object = calculate(source)
    assert isinstance(result, str)
    return result


def _distribution_versions() -> dict[str, str]:
    paths = {
        "kegg-mcp": PROJECT_ROOT / "pyproject.toml",
        "deepkoala-mcp": PROJECT_ROOT / "companions" / "deepkoala-mcp" / "pyproject.toml",
        "kegg-render-mcp": PROJECT_ROOT / "companions" / "kegg-render-mcp" / "pyproject.toml",
    }
    return {
        distribution: str(tomllib.loads(path.read_text(encoding="utf-8"))["project"]["version"])
        for distribution, path in paths.items()
    }


def _copy_source_tree(source: Path) -> None:
    source.mkdir(parents=True)
    (source / "scripts").mkdir()
    shutil.copy2(INSTALLER, source / "scripts" / INSTALLER.name)
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / ".agents", source / ".agents")
    for companion in ("deepkoala-mcp", "kegg-render-mcp"):
        companion_root = source / "companions" / companion
        companion_root.mkdir(parents=True)
        shutil.copy2(
            PROJECT_ROOT / "companions" / companion / "pyproject.toml",
            companion_root / "pyproject.toml",
        )


def _nested_archive(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "outer-workspace"
    source = workspace / ".mcp" / "kegg_mcp"
    _copy_source_tree(source)
    return workspace, source


def _git_checkout(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "install-workspace"
    workspace.mkdir(parents=True)
    source = tmp_path / "source-checkout"
    _copy_source_tree(source)
    commands = (
        ["git", "init", "--quiet"],
        ["git", "config", "user.name", "Skill Installer Test"],
        ["git", "config", "user.email", "skill-installer@example.invalid"],
        ["git", "add", "--all"],
        ["git", "commit", "--quiet", "-m", "Create clean Skill source"],
    )
    for command in commands:
        subprocess.run(command, cwd=source, check=True, capture_output=True, text=True, timeout=10)
    return workspace, source


def _run_installer(
    workspace: Path,
    source: Path,
    *,
    source_commit: str | None = ARCHIVE_COMMIT,
    source_tree_sha256: str | None = "auto",
    expected_core_version: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if expected_core_version is None:
        expected_core_version = _distribution_versions()["kegg-mcp"]
    command = [
        sys.executable,
        str(source / "scripts" / INSTALLER.name),
        "--workspace",
        str(workspace),
        "--expected-core-version",
        expected_core_version,
    ]
    if source_commit is not None:
        command.extend(("--source-commit", source_commit))
    if source_tree_sha256 == "auto":
        source_tree_sha256 = None if (source / ".git").is_dir() else _source_tree_digest(source)
    if source_tree_sha256 is not None:
        command.extend(("--source-tree-sha256", source_tree_sha256))
    return subprocess.run(
        command,
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_installer_closes_nested_checkout_discovery_and_records_versions(tmp_path: Path) -> None:
    workspace, source = _nested_archive(tmp_path)

    completed = _run_installer(workspace, source)

    assert completed.returncode == 0, completed.stderr
    assert "Installed 3 and updated 0 managed Skills" in completed.stdout
    assert f"source_commit={ARCHIVE_COMMIT}" in completed.stdout
    expected_tree_digest = _source_tree_digest(source)
    assert f"source_tree_sha256={expected_tree_digest}" in completed.stdout
    skill_root = workspace / ".agents" / "skills"
    assert {path.name for path in skill_root.iterdir()} == SKILL_NAMES
    for name in SKILL_NAMES:
        destination = skill_root / name
        assert (destination / "SKILL.md").read_bytes() == (
            source / ".agents" / "skills" / name / "SKILL.md"
        ).read_bytes()
        marker = json.loads((destination / MARKER_NAME).read_text(encoding="utf-8"))
        assert marker == {
            "content_sha256": marker["content_sha256"],
            "installer": "kegg-mcp-skill-installer",
            "schema_version": 1,
            "skill_name": name,
            "source_commit": ARCHIVE_COMMIT,
            "source_kind": "tag_source_archive",
            "source_tree_sha256": expected_tree_digest,
            "versions": _distribution_versions(),
        }
        serialized_marker = json.dumps(marker)
        assert str(workspace) not in serialized_marker
        assert str(source) not in serialized_marker

    repeated = _run_installer(workspace, source)
    assert repeated.returncode == 0, repeated.stderr
    assert "Installed 0 and updated 3 managed Skills" in repeated.stdout


def test_installer_derives_commit_from_an_exact_git_checkout(tmp_path: Path) -> None:
    workspace, source = _git_checkout(tmp_path)

    completed = _run_installer(workspace, source, source_commit=None)

    assert completed.returncode == 0, completed.stderr
    expected_commit = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout.strip()
    marker = json.loads(
        (workspace / ".agents" / "skills" / "kegg-ko-analysis" / MARKER_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert marker["source_commit"] == expected_commit
    assert marker["source_kind"] == "git_checkout"


def test_installer_refuses_to_manage_source_skills_in_the_same_checkout(
    tmp_path: Path,
) -> None:
    _, source = _git_checkout(tmp_path)

    completed = _run_installer(source, source, source_commit=None)

    assert completed.returncode == 2
    assert "ERROR [skill_target_conflict]" in completed.stderr
    skill_root = source / ".agents" / "skills"
    assert {path.name for path in skill_root.iterdir()} == SKILL_NAMES
    assert not tuple(skill_root.glob(f"*/{MARKER_NAME}"))


def test_git_checkout_rejects_tracked_and_untracked_source_changes(tmp_path: Path) -> None:
    cases = (
        "tracked_skill",
        "staged_skill",
        "tracked_version",
        "tracked_installer",
        "untracked",
        "ignored",
    )
    for case in cases:
        workspace, source = _git_checkout(tmp_path / case)
        if case in {"tracked_skill", "staged_skill"}:
            skill_file = source / ".agents" / "skills" / "kegg-ko-analysis" / "SKILL.md"
            skill_file.write_text(
                skill_file.read_text(encoding="utf-8") + "\nlocal change\n",
                encoding="utf-8",
            )
            if case == "staged_skill":
                subprocess.run(
                    ["git", "add", str(skill_file)],
                    cwd=source,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
        elif case == "tracked_version":
            version_file = source / "companions" / "deepkoala-mcp" / "pyproject.toml"
            version_file.write_text(
                version_file.read_text(encoding="utf-8") + "\n# local change\n",
                encoding="utf-8",
            )
        elif case == "tracked_installer":
            installer = source / "scripts" / "install-skills.py"
            installer.write_text(
                installer.read_text(encoding="utf-8") + "\n# local change\n",
                encoding="utf-8",
            )
        else:
            extra = source / ".agents" / "skills" / "kegg-ko-analysis" / f"{case}.local"
            if case == "ignored":
                exclude = source / ".git" / "info" / "exclude"
                exclude.write_text("*.local\n", encoding="utf-8")
            extra.write_text("not in HEAD\n", encoding="utf-8")

        completed = _run_installer(workspace, source, source_commit=None)

        assert completed.returncode == 2
        assert "ERROR [skill_source_modified]" in completed.stderr
        assert not (workspace / ".agents").exists()


def test_installer_refuses_unknown_skill_directory_before_copying_any_skill(
    tmp_path: Path,
) -> None:
    workspace, source = _nested_archive(tmp_path)
    unknown = workspace / ".agents" / "skills" / "kegg-ko-analysis"
    unknown.mkdir(parents=True)
    sentinel = unknown / "owner-content.txt"
    sentinel.write_text("preserve me\n", encoding="utf-8")

    completed = _run_installer(workspace, source)

    assert completed.returncode == 2
    assert "ERROR [skill_target_conflict]" in completed.stderr
    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"
    assert {path.name for path in unknown.parent.iterdir()} == {"kegg-ko-analysis"}


def test_installer_refuses_to_replace_a_modified_managed_skill(tmp_path: Path) -> None:
    workspace, source = _nested_archive(tmp_path)
    first = _run_installer(workspace, source)
    assert first.returncode == 0, first.stderr
    modified = workspace / ".agents" / "skills" / "kegg-pathway-rendering" / "SKILL.md"
    modified.write_text(modified.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8")
    preserved = modified.read_bytes()

    repeated = _run_installer(workspace, source)

    assert repeated.returncode == 2
    assert "ERROR [skill_target_modified]" in repeated.stderr
    assert modified.read_bytes() == preserved


def test_installer_rejects_every_invalid_marker_shape_and_type(tmp_path: Path) -> None:
    cases = (
        "extra_key",
        "schema_bool",
        "source_kind",
        "source_kind_type",
        "source_commit",
        "versions",
        "content_digest",
        "source_tree_digest",
    )
    for case in cases:
        workspace, source = _nested_archive(tmp_path / case)
        first = _run_installer(workspace, source)
        assert first.returncode == 0, first.stderr
        marker_path = workspace / ".agents" / "skills" / "kegg-ko-analysis" / MARKER_NAME
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if case == "extra_key":
            marker["unexpected"] = True
        elif case == "schema_bool":
            marker["schema_version"] = True
        elif case == "source_kind":
            marker["source_kind"] = "archive"
        elif case == "source_kind_type":
            marker["source_kind"] = []
        elif case == "source_commit":
            marker["source_commit"] = "abc"
        elif case == "versions":
            marker["versions"] = {"kegg-mcp": 1}
        elif case == "content_digest":
            marker["content_sha256"] = "A" * 64
        else:
            marker["source_tree_sha256"] = None
        marker_path.write_text(json.dumps(marker), encoding="utf-8")

        repeated = _run_installer(workspace, source)

        assert repeated.returncode == 2
        assert "ERROR [skill_target_conflict]" in repeated.stderr


def test_tree_digest_covers_file_mode_and_empty_directories(tmp_path: Path) -> None:
    tree = tmp_path / "digest-tree"
    tree.mkdir()
    file_path = tree / "file.txt"
    file_path.write_text("content\n", encoding="utf-8")
    calculate = INSTALLER_MODULE._tree_digest

    baseline: str = calculate(tree)
    file_path.chmod(0o664)
    non_executable_mode: str = calculate(tree)
    assert non_executable_mode == baseline

    file_path.chmod(0o755)
    executable: str = calculate(tree)
    assert executable != baseline

    file_path.chmod(0o644)
    (tree / "empty-directory").mkdir()
    with_empty_directory: str = calculate(tree)
    assert with_empty_directory != baseline


def test_source_digest_v2_golden_vector(tmp_path: Path) -> None:
    source = tmp_path / "golden-source"
    files = {
        ".agents/skills/example/SKILL.md": b"name: example\n",
        "pyproject.toml": b"core\n",
        "companions/deepkoala-mcp/pyproject.toml": b"deep\n",
        "companions/kegg-render-mcp/pyproject.toml": b"render\n",
        "scripts/install-skills.py": b"#!/usr/bin/env python3\n",
    }
    for relative, content in files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (source / ".agents" / "skills" / "example" / "empty").mkdir()
    (source / "scripts" / "install-skills.py").chmod(0o755)

    assert (
        _source_tree_digest(source)
        == "b7c168dc0ddf273ea5bdac6199cc28532c5b233d94f1e4eebd1419b7b7a42da2"
    )


def test_archive_and_stage_normalize_unhashed_permission_metadata(tmp_path: Path) -> None:
    workspace, source = _nested_archive(tmp_path)
    skill_directory = source / ".agents" / "skills" / "kegg-ko-analysis"
    skill_file = skill_directory / "SKILL.md"
    baseline_digest = _source_tree_digest(source)
    skill_directory.chmod(0o700)
    skill_file.chmod(0o666)
    assert _source_tree_digest(source) == baseline_digest
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        baseline_digest,
        _distribution_versions()["kegg-mcp"],
    )
    try:
        snapshot_skill = prepared.root / ".agents" / "skills" / "kegg-ko-analysis"
        assert snapshot_skill.stat().st_mode & 0o777 == 0o755
        assert (snapshot_skill / "SKILL.md").stat().st_mode & 0o777 == 0o644
        module._install(workspace, prepared)
        installed_skill = workspace / ".agents" / "skills" / "kegg-ko-analysis"
        assert installed_skill.stat().st_mode & 0o777 == 0o755
        assert (installed_skill / "SKILL.md").stat().st_mode & 0o777 == 0o644
    finally:
        _cleanup_prepared(prepared)


def test_git_object_snapshot_prevents_worktree_copy_race(tmp_path: Path) -> None:
    workspace, source = _git_checkout(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepare = module._prepare_source
    install = module._install
    prepared = prepare(None, None, _distribution_versions()["kegg-mcp"])
    skill_relative = Path(".agents/skills/kegg-ko-analysis/SKILL.md")
    expected = subprocess.run(
        ["git", "show", f"HEAD:{skill_relative.as_posix()}"],
        cwd=source,
        check=True,
        capture_output=True,
        timeout=5,
    ).stdout
    (source / skill_relative).write_text("changed after snapshot\n", encoding="utf-8")
    try:
        install(workspace, prepared)
        assert (
            workspace / ".agents" / "skills" / "kegg-ko-analysis" / "SKILL.md"
        ).read_bytes() == expected
    finally:
        shutil.rmtree(prepared.root, ignore_errors=True)


def test_archive_snapshot_isolated_from_source_change_after_preparation(tmp_path: Path) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepare = module._prepare_source
    install = module._install
    prepared = prepare(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )
    changed = source / ".agents" / "skills" / "kegg-ko-analysis" / "SKILL.md"
    original = changed.read_bytes()
    changed.write_text(changed.read_text(encoding="utf-8") + "\nrace\n", encoding="utf-8")

    try:
        install(workspace, prepared)
        installed = workspace / ".agents" / "skills" / "kegg-ko-analysis" / "SKILL.md"
        assert installed.read_bytes() == original
        assert prepared.root != source
    finally:
        _cleanup_prepared(prepared)


def test_archive_snapshot_prevents_digest_to_skill_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    source_skill = source / ".agents" / "skills" / "kegg-ko-analysis" / "SKILL.md"
    original = source_skill.read_bytes()
    trusted_digest = _source_tree_digest(source)
    calculate_digest = module._source_tree_digest
    changed = False

    def digest_then_change(root: Path) -> str:
        nonlocal changed
        result: str = calculate_digest(root)
        if not changed:
            source_skill.write_bytes(original + b"\nrace after aggregate digest\n")
            changed = True
        return result

    monkeypatch.setattr(module, "_source_tree_digest", digest_then_change)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        trusted_digest,
        _distribution_versions()["kegg-mcp"],
    )
    try:
        assert prepared.root != source
        assert (
            prepared.root / ".agents" / "skills" / "kegg-ko-analysis" / "SKILL.md"
        ).read_bytes() == original
        module._install(workspace, prepared)
        assert (
            workspace / ".agents" / "skills" / "kegg-ko-analysis" / "SKILL.md"
        ).read_bytes() == original
    finally:
        _cleanup_prepared(prepared)


def test_symlink_swap_cannot_redirect_the_anchored_install_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )
    original_assert = module._assert_root_binding
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    swapped = False

    def race(root: object) -> None:
        nonlocal swapped
        if not swapped:
            skills = workspace / ".agents" / "skills"
            skills.rename(workspace / ".agents" / "skills-original")
            skills.symlink_to(attacker, target_is_directory=True)
            swapped = True
        original_assert(root)

    monkeypatch.setattr(module, "_assert_root_binding", race)
    try:
        with pytest.raises(module.InstallError) as raised:
            module._install(workspace, prepared)

        assert raised.value.code == "install_root_changed"
        assert not tuple(attacker.iterdir())
        assert not tuple((workspace / ".agents" / "skills-original").iterdir())
    finally:
        _cleanup_prepared(prepared)


def test_replace_failure_rolls_back_and_removes_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )
    install = module._install
    install(workspace, prepared)
    original_rename = module._rename_no_replace
    calls = 0

    def fail_once(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("injected replacement failure")
        original_rename(*args, **kwargs)

    monkeypatch.setattr(module, "_rename_no_replace", fail_once)
    try:
        with pytest.raises(module.InstallError) as raised:
            install(workspace, prepared)

        assert raised.value.code == "installation_failed"
        skill_root = workspace / ".agents" / "skills"
        assert {path.name for path in skill_root.iterdir()} == SKILL_NAMES
    finally:
        _cleanup_prepared(prepared)


@pytest.mark.parametrize("failure_call", (1, 2))
def test_post_rename_exception_restores_original_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )
    module._install(workspace, prepared)
    original_identities = _managed_target_identities(workspace)
    original_rename = module._rename_no_replace
    calls = 0

    def fail_after_successful_rename(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        original_rename(*args, **kwargs)
        if calls == failure_call:
            raise OSError("injected post-rename failure")

    monkeypatch.setattr(module, "_rename_no_replace", fail_after_successful_rename)
    try:
        with pytest.raises(module.InstallError) as raised:
            module._install(workspace, prepared)
        assert raised.value.code == "installation_failed"
        assert _managed_target_identities(workspace) == original_identities
        assert not _transaction_directories(workspace)
    finally:
        _cleanup_prepared(prepared)


def test_new_install_verification_failure_removes_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )

    def fail_verification(*args: object, **kwargs: object) -> None:
        raise OSError("injected installed-target verification failure")

    monkeypatch.setattr(module, "_verify_installed_target", fail_verification)
    try:
        with pytest.raises(module.InstallError) as raised:
            module._install(workspace, prepared)
        assert raised.value.code == "installation_failed"
        assert not tuple((workspace / ".agents" / "skills").iterdir())
    finally:
        _cleanup_prepared(prepared)


@pytest.mark.parametrize(
    "failure_type",
    (KeyboardInterrupt, InjectedBaseException),
    ids=("keyboard-interrupt", "base-exception"),
)
def test_base_exception_after_install_rename_restores_original_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_type: type[BaseException],
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )
    module._install(workspace, prepared)
    original_identities = _managed_target_identities(workspace)

    def interrupt_verification(*args: object, **kwargs: object) -> None:
        raise failure_type("injected termination")

    monkeypatch.setattr(module, "_verify_installed_target", interrupt_verification)
    try:
        with pytest.raises(failure_type):
            module._install(workspace, prepared)
        assert _managed_target_identities(workspace) == original_identities
        assert not _transaction_directories(workspace)
    finally:
        _cleanup_prepared(prepared)


def test_partial_transaction_creation_closes_fds_and_removes_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    module = _load_installer_module()
    root = module._anchor_install_root(workspace)
    original_open = module._open_child_directory
    opened_descriptors: list[int] = []
    calls = 0

    def fail_backup_open(parent_fd: int, name: str) -> tuple[int, tuple[int, int]]:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected backup open failure")
        result: tuple[int, tuple[int, int]] = original_open(parent_fd, name)
        opened_descriptors.append(result[0])
        return result

    monkeypatch.setattr(module, "_open_child_directory", fail_backup_open)
    try:
        with pytest.raises(OSError, match="injected backup open failure"):
            module._create_transaction(root)
        assert not _transaction_directories(workspace)
        for descriptor in opened_descriptors:
            with pytest.raises(OSError):
                module.os.fstat(descriptor)
    finally:
        root.close()


def test_success_cleanup_failure_is_classified_and_preserves_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )
    module._install(workspace, prepared)
    capsys.readouterr()

    def fail_cleanup(*args: object, **kwargs: object) -> None:
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(module, "_remove_transaction", fail_cleanup)
    try:
        with pytest.raises(module.InstallError) as raised:
            module._install(workspace, prepared)
        assert raised.value.code == "installation_cleanup_failed"
        assert ".agents/skills/.kegg-mcp-skill-install-" in str(raised.value)
        assert str(workspace) not in str(raised.value)
        assert "Installed" not in capsys.readouterr().out
        transactions = _transaction_directories(workspace)
        assert len(transactions) == 1
        assert {path.name for path in (transactions[0] / "backup").iterdir()} == SKILL_NAMES
    finally:
        _cleanup_prepared(prepared)


def test_rollback_failure_preserves_backups_and_relative_recovery_guide(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace, source = _nested_archive(tmp_path)
    module = _load_installer_module(source / "scripts" / INSTALLER.name)
    prepared = module._prepare_source(
        ARCHIVE_COMMIT,
        _source_tree_digest(source),
        _distribution_versions()["kegg-mcp"],
    )
    install = module._install
    install(workspace, prepared)
    original_rename = module._rename_no_replace
    calls = 0

    def fail_replace_and_rollback(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls in {4, 5}:
            raise OSError("injected rollback failure")
        original_rename(*args, **kwargs)

    monkeypatch.setattr(module, "_rename_no_replace", fail_replace_and_rollback)
    try:
        with pytest.raises(module.InstallError) as raised:
            install(workspace, prepared)

        assert raised.value.code == "installation_rollback_failed"
        message = str(raised.value)
        assert ".agents/skills/.kegg-mcp-skill-install-" in message
        assert str(workspace) not in message
        transactions = _transaction_directories(workspace)
        assert len(transactions) == 1
        backup_names = {path.name for path in (transactions[0] / "backup").iterdir()}
        assert backup_names == {"deepkoala-annotation", "kegg-ko-analysis"}
    finally:
        _cleanup_prepared(prepared)


def test_archive_commit_and_wheel_version_guards_fail_before_install(tmp_path: Path) -> None:
    workspace, source = _nested_archive(tmp_path)

    missing_commit = _run_installer(
        workspace,
        source,
        source_commit=None,
        source_tree_sha256=_source_tree_digest(source),
    )
    assert missing_commit.returncode == 2
    assert "ERROR [source_commit_unavailable]" in missing_commit.stderr
    assert not (workspace / ".agents").exists()

    commit_without_tree_digest = _run_installer(
        workspace,
        source,
        source_tree_sha256=None,
    )
    assert commit_without_tree_digest.returncode == 2
    assert "ERROR [source_tree_sha256_required]" in commit_without_tree_digest.stderr
    assert not (workspace / ".agents").exists()

    mismatched_tree = _run_installer(
        workspace,
        source,
        source_tree_sha256="0" * 64,
    )
    assert mismatched_tree.returncode == 2
    assert "ERROR [source_tree_sha256_mismatch]" in mismatched_tree.stderr
    assert not (workspace / ".agents").exists()

    mismatch = _run_installer(workspace, source, expected_core_version="invalid-version")
    assert mismatch.returncode == 2
    assert "ERROR [version_mismatch]" in mismatch.stderr
    assert not (workspace / ".agents").exists()


def test_installer_is_standard_library_only_and_has_no_download_commands() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots = {
        alias.name.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_roots.update(
        node.module.partition(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )
    assert imported_roots <= sys.stdlib_module_names | {"__future__"}
    assert imported_roots.isdisjoint({"ftplib", "http", "httpx", "requests", "socket", "urllib"})
    normalized_source = source.casefold()
    for forbidden in (
        "pip install",
        "uv sync",
        "curl ",
        "wget ",
        "http://",
        "https://",
        "git fetch",
        "git pull",
        "git clone",
        "urlopen",
    ):
        assert forbidden not in normalized_source


def test_installation_docs_separate_skill_and_mcp_discovery_failures() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    installation = (PROJECT_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
    troubleshooting = (PROJECT_ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    corpus = "\n".join((readme, installation, troubleshooting))

    for required in (
        "scripts/install-skills.py",
        "nested checkout",
        "--expected-core-version",
        "--source-tree-sha256",
        "MCP discovery",
        "Skill discovery",
        "skill_not_installed",
        "skill_not_discovered",
        "mcp_not_registered",
        "mcp_not_startable",
        "mcp_runtime_unready",
        "handoff_root_mismatch",
        "model_resources_missing",
        "skill_source_modified",
        "source_tree_sha256_required",
        "source_tree_sha256_mismatch",
        "installation_rollback_failed",
        "installation_cleanup_failed",
    ):
        assert required in corpus
    assert "https://learn.chatgpt.com/docs/build-skills#where-to-save-skills" in installation
    assert "2026-07-18" in installation
    assert "does not download dependencies, models, weights, or KEGG data" in installation
    for required_control in ("/proc/self/fd", "O_DIRECTORY", "O_NOFOLLOW", "renameat2"):
        assert required_control in installation
