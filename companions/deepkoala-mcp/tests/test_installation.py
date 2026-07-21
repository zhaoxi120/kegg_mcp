"""Bounded installation inspection, selection, and runtime-probe tests."""

import sys
from pathlib import Path

import pytest

from deepkoala_mcp.contracts import ErrorCode
from deepkoala_mcp.installation import (
    InstallationError,
    inspect_installation,
    probe_multi_dependencies,
    probe_runtime,
    select_installation,
)


def test_inspection_and_latest_selection_are_deterministic(checkout: Path) -> None:
    version, resources = inspect_installation(checkout)
    selected = select_installation(checkout, "full", "latest")
    assert version == "0.1-test"
    assert [(item.model_date, item.model) for item in resources] == [
        ("202401", "full"),
        ("202401", "frag"),
        ("202502", "full"),
        ("202502", "frag"),
    ]
    assert selected.resource.model_date == "202502"


def test_selection_fails_closed_for_missing_resources(checkout: Path) -> None:
    with pytest.raises(InstallationError) as captured:
        select_installation(checkout, "full", "202601")
    assert captured.value.code is ErrorCode.WEIGHTS_NOT_FOUND


def test_runtime_probe_imports_configured_environment_without_output(checkout: Path) -> None:
    result = probe_runtime(
        checkout=checkout,
        python_executable=Path(sys.executable).resolve(),
        cpu_threads=1,
    )
    assert result.runtime_ready is True
    assert isinstance(result.cuda_available, bool)


def test_runtime_probe_rejects_executable_that_cannot_run_python(checkout: Path) -> None:
    result = probe_runtime(
        checkout=checkout,
        python_executable=Path("/bin/false"),
        cpu_threads=1,
    )
    assert result.runtime_ready is False
    assert result.cuda_available is False


def test_runtime_and_dependency_probes_enable_only_compatible_local_multi(
    checkout: Path,
    tmp_path: Path,
) -> None:
    (checkout / "deepkoala" / "infer_multi.py").write_text(
        "def _run_hmmsearch(hmm_file, seq):\n    return None, None\n",
        encoding="utf-8",
    )
    executable = tmp_path / "hmmsearch"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o700)
    profiles = tmp_path / "profiles"
    profiles.mkdir(mode=0o700)
    (profiles / "K00001.hmm").write_text("HMMER3/f\n", encoding="ascii")

    runtime = probe_runtime(
        checkout=checkout,
        python_executable=Path(sys.executable).resolve(),
        cpu_threads=1,
    )
    multi_ready = probe_multi_dependencies(
        allow_multi=True,
        profiles_dir=profiles,
        hmmsearch_executable=executable,
        runtime=runtime,
    )

    assert runtime.runtime_ready is True
    assert runtime.multi_adapter_compatible is True
    assert multi_ready is True


def test_multi_probe_fails_closed_for_incompatible_interface_and_symlink(
    checkout: Path,
    tmp_path: Path,
) -> None:
    (checkout / "deepkoala" / "infer_multi.py").write_text(
        "def _run_hmmsearch(command):\n    return None, None\n",
        encoding="utf-8",
    )
    bin_target = tmp_path / "hmmer-real"
    bin_target.mkdir()
    target = bin_target / "hmmsearch"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    bin_link = tmp_path / "hmmer-link"
    bin_link.symlink_to(bin_target, target_is_directory=True)
    executable = bin_link / "hmmsearch"
    profiles = tmp_path / "profiles"
    profiles.mkdir(mode=0o700)
    (profiles / "K00001.hmm").write_text("HMMER3/f\n", encoding="ascii")

    runtime = probe_runtime(
        checkout=checkout,
        python_executable=Path(sys.executable).resolve(),
        cpu_threads=1,
    )
    multi_ready = probe_multi_dependencies(
        allow_multi=True,
        profiles_dir=profiles,
        hmmsearch_executable=executable,
        runtime=runtime,
    )

    assert runtime.runtime_ready is True
    assert runtime.multi_adapter_compatible is False
    assert multi_ready is False


def test_multi_capability_import_failure_does_not_disable_base_runtime(
    checkout: Path,
) -> None:
    (checkout / "deepkoala" / "infer_multi.py").write_text(
        "raise RuntimeError('incompatible optional module')\n",
        encoding="utf-8",
    )

    runtime = probe_runtime(
        checkout=checkout,
        python_executable=Path(sys.executable).resolve(),
        cpu_threads=1,
    )

    assert runtime.runtime_ready is True
    assert runtime.multi_adapter_compatible is False
