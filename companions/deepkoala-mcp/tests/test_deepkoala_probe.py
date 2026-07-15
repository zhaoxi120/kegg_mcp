from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest

from deepkoala_mcp.deepkoala import (
    DeepKoalaInstallation,
    DeepKoalaProbeError,
    probe_deepkoala_installation,
    recheck_artifact_identities,
)

_FAKE_UTILS = """\
import os

def resolve_device(requested):
    if os.environ.get("UNSAFE_PROBE_SECRET") is not None:
        raise RuntimeError("ambient environment was inherited")
    if pathlib_check := os.environ.get("PYTHONPATH"):
        if os.path.realpath(pathlib_check) != os.path.realpath(os.getcwd()):
            raise RuntimeError("checkout path was not controlled")
    return "cpu" if requested == "auto" else requested
"""


def _make_checkout(
    root: Path,
    *,
    version: str = "0.1-beta",
    init_source: str = "",
    utils_source: str = _FAKE_UTILS,
) -> Path:
    checkout = root / "official-deepkoala"
    package = checkout / "deepkoala"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(init_source, encoding="utf-8")
    (package / "utils.py").write_text(utils_source, encoding="utf-8")
    (checkout / "resources").mkdir()
    (checkout / "pyproject.toml").write_text(
        f'[project]\nname = "deepkoala"\nversion = "{version}"\n',
        encoding="utf-8",
    )
    return checkout


def _write_artifacts(
    checkout: Path,
    date: str,
    *,
    model: str = "full",
    weight: bytes = b"fake-weight",
    config: bytes = b'{"K00001": {"index": 0, "threshold": 0.5}}',
) -> tuple[Path, Path]:
    resource_date = checkout / "resources" / date
    resource_date.mkdir()
    weight_path = resource_date / f"weights_{model}.pt"
    config_path = resource_date / f"ko_config_{model}.json"
    weight_path.write_bytes(weight)
    config_path.write_bytes(config)
    return weight_path, config_path


def _installation(checkout: Path) -> DeepKoalaInstallation:
    return DeepKoalaInstallation(
        python_executable=Path(sys.executable).resolve(),
        checkout=checkout.resolve(),
    )


@pytest.mark.asyncio
async def test_probe_resolves_latest_and_returns_path_free_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401", weight=b"old")
    weight_path, config_path = _write_artifacts(checkout, "202412", weight=b"new")
    (checkout / "resources" / "202413").mkdir()
    (checkout / "resources" / "not-a-date").mkdir()
    monkeypatch.setenv("UNSAFE_PROBE_SECRET", "must-not-reach-probe")

    result = await probe_deepkoala_installation(_installation(checkout))

    assert result.requested_model == "full"
    assert result.requested_date == "latest"
    assert result.resolved_date == "202412"
    assert result.requested_device == "auto"
    assert result.resolved_device == "cpu"
    assert result.source_version == "0.1-beta"
    assert result.weight_source == "github_bundled"
    assert result.weight_artifact.basename == "weights_full.pt"
    assert result.weight_artifact.size_bytes == weight_path.stat().st_size
    assert result.weight_artifact.sha256 == hashlib.sha256(b"new").hexdigest()
    assert result.config_artifact.basename == "ko_config_full.json"
    assert result.config_artifact.size_bytes == config_path.stat().st_size
    assert result.python_artifact.basename == "configured-python"
    assert result.python_artifact.size_bytes > 0
    assert result.source_artifact.basename == "deepkoala-source-tree"
    assert result.source_artifact.size_bytes > 0
    serialized = json.dumps(asdict(result), sort_keys=True)
    assert str(tmp_path) not in serialized
    assert str(Path(sys.executable).resolve()) not in serialized


@pytest.mark.asyncio
async def test_probe_supports_explicit_fragment_resources_and_device(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path, init_source='__version__ = "not-the-source-version"\n')
    weight_path, _ = _write_artifacts(
        checkout,
        "202405",
        model="frag",
        weight=b"fragment-weight",
    )

    result = await probe_deepkoala_installation(
        _installation(checkout),
        model="frag",
        date="202405",
        device="cpu",
    )

    assert result.requested_model == "frag"
    assert result.requested_date == "202405"
    assert result.resolved_date == "202405"
    assert result.requested_device == "cpu"
    assert result.resolved_device == "cpu"
    assert result.source_version == "0.1-beta"
    assert result.weight_artifact.basename == weight_path.name


@pytest.mark.asyncio
@pytest.mark.parametrize("date", ["2024", "202400", "202413", "../../escape"])
async def test_probe_rejects_invalid_resource_dates(tmp_path: Path, date: str) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await probe_deepkoala_installation(_installation(checkout), date=date)

    assert captured.value.code == "resource_date_invalid"
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.asyncio
async def test_probe_requires_explicit_resource_date_to_exist(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await probe_deepkoala_installation(_installation(checkout), date="202402")

    assert captured.value.code == "resource_date_missing"
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.asyncio
async def test_latest_ignores_symlinked_resource_dates(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")
    external = tmp_path / "external"
    external.mkdir()
    (checkout / "resources" / "202412").symlink_to(external, target_is_directory=True)

    result = await probe_deepkoala_installation(_installation(checkout))

    assert result.resolved_date == "202401"


@pytest.mark.asyncio
async def test_probe_rejects_symlinked_artifacts(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    weight_path, _ = _write_artifacts(checkout, "202401")
    external = tmp_path / "outside-weight.pt"
    external.write_bytes(b"outside")
    weight_path.unlink()
    weight_path.symlink_to(external)

    with pytest.raises(DeepKoalaProbeError) as captured:
        await probe_deepkoala_installation(_installation(checkout))

    assert captured.value.code == "artifact_invalid"
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.asyncio
async def test_probe_rejects_non_official_project_metadata(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")
    (checkout / "pyproject.toml").write_text(
        '[project]\nname = "another-package"\nversion = "1.0"\n',
        encoding="utf-8",
    )

    with pytest.raises(DeepKoalaProbeError) as captured:
        await probe_deepkoala_installation(_installation(checkout))

    assert captured.value.code == "source_metadata_invalid"


@pytest.mark.asyncio
async def test_probe_verifies_import_origin_and_redacts_failure_details(tmp_path: Path) -> None:
    checkout = _make_checkout(
        tmp_path,
        init_source=f"__file__ = {str(tmp_path / 'outside.py')!r}\n",
    )
    _write_artifacts(checkout, "202401")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await probe_deepkoala_installation(_installation(checkout))

    assert captured.value.code == "device_probe_failed"
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.asyncio
async def test_probe_bounds_subprocess_output(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path, init_source='print("X" * 4096)\n')
    _write_artifacts(checkout, "202401")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await probe_deepkoala_installation(_installation(checkout))

    assert captured.value.code == "device_probe_output_invalid"
    assert "X" not in str(captured.value)


@pytest.mark.asyncio
async def test_probe_enforces_timeout_without_returning_child_error(tmp_path: Path) -> None:
    checkout = _make_checkout(
        tmp_path,
        utils_source="import time\ntime.sleep(1)\ndef resolve_device(requested): return 'cpu'\n",
    )
    _write_artifacts(checkout, "202401")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await probe_deepkoala_installation(
            _installation(checkout),
            timeout_seconds=0.02,
        )

    assert captured.value.code == "device_probe_timeout"
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are unavailable")
@pytest.mark.asyncio
async def test_probe_reaps_inherited_pipe_descendant_after_leader_exit(tmp_path: Path) -> None:
    checkout = _make_checkout(
        tmp_path,
        init_source="""
import pathlib
import subprocess
import sys

child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])
(pathlib.Path.cwd() / 'probe-descendant.pid').write_text(str(child.pid), encoding='ascii')
""",
    )
    _write_artifacts(checkout, "202401")

    async with asyncio.timeout(2):
        result = await probe_deepkoala_installation(
            _installation(checkout),
            timeout_seconds=1,
        )

    descendant_pid = int((checkout / "probe-descendant.pid").read_text(encoding="ascii"))
    assert result.resolved_device == "cpu"
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


@pytest.mark.asyncio
async def test_recheck_detects_artifact_replacement(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    weight_path, _ = _write_artifacts(checkout, "202401")
    installation = _installation(checkout)
    result = await probe_deepkoala_installation(installation)
    weight_path.write_bytes(b"replacement")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await recheck_artifact_identities(installation, result)

    assert captured.value.code == "artifacts_changed"
    assert str(tmp_path) not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name", ["cli.py", "utils.py"])
async def test_recheck_detects_source_replacement(tmp_path: Path, source_name: str) -> None:
    checkout = _make_checkout(tmp_path)
    (checkout / "deepkoala" / "cli.py").write_text("VALUE = 1\n", encoding="utf-8")
    _write_artifacts(checkout, "202401")
    installation = _installation(checkout)
    result = await probe_deepkoala_installation(installation)
    (checkout / "deepkoala" / source_name).write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await recheck_artifact_identities(installation, result)

    assert captured.value.code == "artifacts_changed"


@pytest.mark.asyncio
async def test_recheck_detects_configured_interpreter_replacement(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")
    interpreter_link = tmp_path / "configured-python"
    interpreter_link.symlink_to(Path(sys.executable).resolve())
    installation = DeepKoalaInstallation(
        python_executable=interpreter_link.absolute(),
        checkout=checkout.resolve(),
    )
    result = await probe_deepkoala_installation(installation)
    replacement = tmp_path / "replacement-python"
    replacement.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    replacement.chmod(0o700)
    interpreter_link.unlink()
    interpreter_link.symlink_to(replacement)

    with pytest.raises(DeepKoalaProbeError) as captured:
        await recheck_artifact_identities(installation, result)

    assert captured.value.code == "artifacts_changed"


@pytest.mark.asyncio
async def test_recheck_accepts_unchanged_artifacts(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")
    installation = _installation(checkout)
    result = await probe_deepkoala_installation(installation)

    assert await recheck_artifact_identities(installation, result) is None


@pytest.mark.asyncio
async def test_recheck_rejects_forged_artifact_coordinates(tmp_path: Path) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")
    installation = _installation(checkout)
    result = await probe_deepkoala_installation(installation)
    forged = replace(result, requested_model="../../outside")

    with pytest.raises(DeepKoalaProbeError) as captured:
        await recheck_artifact_identities(installation, forged)

    assert captured.value.code == "artifacts_changed"


def test_installation_requires_absolute_paths(tmp_path: Path) -> None:
    with pytest.raises(DeepKoalaProbeError) as captured:
        DeepKoalaInstallation(
            python_executable=Path("python"),
            checkout=tmp_path,
        )

    assert captured.value.code == "configuration_invalid"


@pytest.mark.asyncio
async def test_companion_process_never_imports_deepkoala_or_torch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checkout = _make_checkout(tmp_path)
    _write_artifacts(checkout, "202401")
    original_import: Callable[..., Any] = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> Any:
        if name == "torch" or name == "deepkoala" or name.startswith("deepkoala."):
            raise AssertionError(f"unexpected companion import: {name}")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    result = await probe_deepkoala_installation(_installation(checkout))

    assert result.resolved_device == "cpu"
