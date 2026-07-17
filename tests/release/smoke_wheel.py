"""Install freshly built wheels and verify them outside the source checkout."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", action="append", required=True, type=Path)
    parser.add_argument("--distribution", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--console")
    return parser


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


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
        python = virtual_environment / "bin" / "python"
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
            console = virtual_environment / "bin" / arguments.console
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


if __name__ == "__main__":
    main()
