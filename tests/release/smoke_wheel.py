"""Install freshly built wheels and verify them outside the source checkout."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

_IMPORT_PROBE = """
from importlib import import_module, metadata
from pathlib import Path
import sys

distribution_name, module_name, expected_version = sys.argv[1:]
installed_version = metadata.version(distribution_name)
if installed_version != expected_version:
    raise SystemExit(f"unexpected installed version: {installed_version}")
module = import_module(module_name)
module_path = Path(module.__file__).resolve()
environment_path = Path(sys.prefix).resolve()
if not module_path.is_relative_to(environment_path):
    raise SystemExit(f"module imported outside isolated environment: {module_path}")
print(f"{distribution_name} {installed_version}: {module_path}")
"""

_RENDERER_NATIVE_WINDOWS_PROBE = """
from kegg_render_mcp._platform import (
    UNSUPPORTED_PLATFORM_DIAGNOSTIC,
    UnsupportedRendererPlatformError,
    validate_renderer_platform,
)
from kegg_render_mcp.config import load_runtime_config

for probe in (validate_renderer_platform, lambda: load_runtime_config({})):
    try:
        probe()
    except UnsupportedRendererPlatformError as error:
        if str(error) != UNSUPPORTED_PLATFORM_DIAGNOSTIC:
            raise SystemExit("renderer exposed a non-static unsupported-platform diagnostic")
    else:
        raise SystemExit("renderer accepted an unsupported native Windows platform")

for marker in ("unsupported platform", "native Windows", "WSL"):
    if marker not in UNSUPPORTED_PLATFORM_DIAGNOSTIC:
        raise SystemExit(f"renderer diagnostic is missing {marker!r}")
"""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", action="append", required=True, type=Path)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--console")
    parser.add_argument(
        "--native-windows-probe",
        choices=("core", "renderer"),
        help="Verify the installed wheel's bounded native-Windows rejection contract.",
    )
    return parser


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _venv_python(environment_root: Path) -> Path:
    """Return the interpreter path for the current platform's venv layout."""
    if os.name == "nt":
        return environment_root / "Scripts" / "python.exe"
    return environment_root / "bin" / "python"


def _venv_console(environment_root: Path, name: str) -> Path:
    """Return one installed console entry point without searching the host PATH."""
    if os.name == "nt":
        return environment_root / "Scripts" / f"{name}.exe"
    return environment_root / "bin" / name


def _probe_core_native_windows(
    console: Path,
    *,
    cwd: Path,
    environment: dict[str, str],
) -> None:
    completed = subprocess.run(
        [str(console), "doctor", "--json"],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if completed.returncode != 2:
        raise SystemExit("core doctor did not reject native Windows with exit code 2")
    try:
        raw_document: object = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit("core doctor did not emit valid JSON") from error
    if not isinstance(raw_document, dict):
        raise SystemExit("core doctor JSON must be an object")
    document = cast(dict[str, object], raw_document)
    expected = {
        "status": "error",
        "configuration_valid": False,
        "allowed_root_paths": "redacted",
        "network_probe": "not_run",
        "storage_probe": "not_run",
    }
    if any(document.get(name) != value for name, value in expected.items()):
        raise SystemExit("core doctor JSON does not match the unsupported-platform contract")
    raw_issues = document.get("issues")
    raw_actions = document.get("next_actions")
    if not isinstance(raw_issues, list):
        raise SystemExit("core doctor JSON is missing the safe locking issue")
    issues = cast(list[object], raw_issues)
    if not any(isinstance(issue, str) and "POSIX" in issue for issue in issues):
        raise SystemExit("core doctor JSON is missing the safe locking issue")
    if not isinstance(raw_actions, list):
        raise SystemExit("core doctor JSON is missing the WSL remediation")
    actions = cast(list[object], raw_actions)
    if not any(
        isinstance(action, str) and "native Windows" in action and "WSL" in action
        for action in actions
    ):
        raise SystemExit("core doctor JSON is missing the WSL remediation")
    startup = subprocess.run(
        [str(console)],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if startup.returncode != 2 or startup.stdout:
        raise SystemExit("core console did not fail closed on native Windows")
    diagnostic = startup.stderr.strip()
    if not diagnostic.startswith("kegg-mcp startup failed:") or any(
        marker not in diagnostic for marker in ("native Windows", "WSL")
    ):
        raise SystemExit("core console did not emit the static WSL diagnostic")


def _probe_native_windows(
    kind: str,
    *,
    environment_root: Path,
    python: Path,
    cwd: Path,
    environment: dict[str, str],
    console_name: str | None,
) -> None:
    if os.name != "nt":
        raise SystemExit("--native-windows-probe is valid only on native Windows")
    if kind == "core":
        if console_name is None:
            raise SystemExit("the core native-Windows probe requires --console")
        _probe_core_native_windows(
            _venv_console(environment_root, console_name),
            cwd=cwd,
            environment=environment,
        )
        return
    renderer_console = _venv_console(environment_root, "kegg-render-mcp")
    completed = subprocess.run(
        [str(renderer_console)],
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    if completed.returncode != 2 or completed.stdout:
        raise SystemExit("renderer console did not fail closed on native Windows")
    diagnostic = completed.stderr.strip()
    if not diagnostic.startswith("kegg-render-mcp startup failed: unsupported platform:") or any(
        marker not in diagnostic for marker in ("native Windows", "WSL")
    ):
        raise SystemExit("renderer console did not emit the static WSL diagnostic")
    _run(
        [str(python), "-I", "-c", _RENDERER_NATIVE_WINDOWS_PROBE],
        cwd=cwd,
        environment=environment,
    )


def main() -> None:
    arguments = _parser().parse_args()
    wheels = tuple(path.resolve(strict=True) for path in arguments.wheel)
    if len(wheels) != len(set(wheels)) or any(path.suffix != ".whl" for path in wheels):
        raise SystemExit("--wheel values must be unique wheel files")
    target_prefix = arguments.distribution.lower().replace("-", "_") + "-"
    expected_prefix = target_prefix + arguments.expected_version.lower() + "-"
    if not any(path.name.lower().startswith(expected_prefix) for path in wheels):
        raise SystemExit("the expected target wheel was not provided")
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv is required for the isolated wheel smoke test")
    environment = _clean_environment()

    with tempfile.TemporaryDirectory(prefix="kegg-wheel-smoke-") as temporary:
        root = Path(temporary)
        virtual_environment = root / "venv"
        _run(
            [uv, "venv", "--python", sys.executable, str(virtual_environment)],
            cwd=root,
            environment=environment,
        )
        python = _venv_python(virtual_environment)
        _run(
            [uv, "pip", "install", "--python", str(python), *(str(path) for path in wheels)],
            cwd=root,
            environment=environment,
        )
        _run(
            [
                str(python),
                "-I",
                "-c",
                _IMPORT_PROBE,
                arguments.distribution,
                arguments.module,
                arguments.expected_version,
            ],
            cwd=root,
            environment=environment,
        )
        if arguments.console is not None:
            console = _venv_console(virtual_environment, arguments.console)
            completed = subprocess.run(
                [str(console), "--version"],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            output = (completed.stdout + completed.stderr).strip()
            if arguments.expected_version not in output:
                raise SystemExit("console --version did not report the installed wheel version")
            print(output)
        if arguments.native_windows_probe is not None:
            _probe_native_windows(
                arguments.native_windows_probe,
                environment_root=virtual_environment,
                python=python,
                cwd=root,
                environment=environment,
                console_name=arguments.console,
            )


if __name__ == "__main__":
    main()
