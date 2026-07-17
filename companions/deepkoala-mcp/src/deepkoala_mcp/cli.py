"""Small CLI for stdio serving and redacted deployment diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO, TypedDict

from deepkoala_mcp import __version__
from deepkoala_mcp.config import (
    ALLOWED_ROOTS_ENV,
    CHECKOUT_ENV,
    PYTHON_ENV,
    STATE_ROOT_ENV,
)
from deepkoala_mcp.contracts import ErrorCode
from deepkoala_mcp.installation import InstallationError, inspect_installation
from deepkoala_mcp.server import main as run_stdio


class _DoctorDocument(TypedDict):
    status: str
    server_version: str
    route_state: str
    configuration_valid: bool
    checkout_ready: bool
    python_ready: bool
    model_resources_ready: bool
    state_root_ready: bool
    fasta_allowed_root_count: int | None
    core_handoff_check: str
    downloads_required: bool
    private_paths: str
    issue: str | None
    next_action: str


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deepkoala-mcp",
        description="Local stdio companion for bounded DeepKOALA execution.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run the stdio MCP server (the default).")
    doctor = commands.add_parser(
        "doctor",
        help="Inspect redacted local configuration without inference or downloads.",
    )
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def _doctor_document(environment: Mapping[str, str]) -> tuple[_DoctorDocument, int]:
    checkout = _configured_directory(environment.get(CHECKOUT_ENV))
    python = _configured_executable(environment.get(PYTHON_ENV))
    state = _configured_private_state_root(environment.get(STATE_ROOT_ENV))
    root_count = _allowed_root_count(environment.get(ALLOWED_ROOTS_ENV))
    checkout_layout_ready = False
    resources = False
    checkout_issue = False
    if checkout is not None:
        try:
            _, installed = inspect_installation(checkout)
            checkout_layout_ready = True
            resources = bool(installed)
        except InstallationError as error:
            checkout_layout_ready = error.code is ErrorCode.WEIGHTS_NOT_FOUND
            checkout_issue = not checkout_layout_ready

    route_state: str
    issue: str | None
    next_action: str
    downloads_required = False
    if checkout is None:
        route_state = "deepkoala_checkout_missing"
        issue = "The configured official DeepKOALA checkout is missing or unsafe."
        next_action = "Authorize installation or correct the checkout configuration."
        downloads_required = True
    elif checkout_issue:
        route_state = "runner_misconfigured"
        issue = "The configured directory is not a usable official DeepKOALA checkout."
        next_action = "Correct the checkout configuration before starting the companion."
    elif python is None:
        route_state = "deepkoala_python_missing"
        issue = "The configured DeepKOALA Python executable is missing or unusable."
        next_action = "Authorize environment setup or correct the interpreter configuration."
        downloads_required = True
    elif not resources:
        route_state = "model_resources_missing"
        issue = "No complete supported local model resource pair is available."
        next_action = "Authorize the required model download or select installed resources."
        downloads_required = True
    elif state is None:
        route_state = "state_root_missing"
        issue = "The private owner-only state root is missing or unsafe."
        next_action = "Authorize creation or repair of the private state root."
    elif root_count is None:
        route_state = "runner_misconfigured"
        issue = "The optional FASTA allowed-root configuration is invalid."
        next_action = "Correct the companion allowed-root configuration before path intake."
    else:
        route_state = "local_ready"
        issue = None
        next_action = (
            "Verify that the core KEGG_MCP_ALLOWED_ROOTS includes the companion state root, then "
            "call get_deepkoala_runner_status."
        )

    ready = route_state == "local_ready"
    return (
        {
            "status": "ok" if ready else "action_required",
            "server_version": __version__,
            "route_state": route_state,
            "configuration_valid": ready,
            "checkout_ready": checkout_layout_ready,
            "python_ready": python is not None,
            "model_resources_ready": resources,
            "state_root_ready": state is not None,
            "fasta_allowed_root_count": root_count,
            "core_handoff_check": "operator_required",
            "downloads_required": downloads_required,
            "private_paths": "redacted",
            "issue": issue,
            "next_action": next_action,
        },
        0 if ready else 2,
    )


def _configured_directory(value: str | None) -> Path | None:
    path = _absolute_path(value)
    if path is None:
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _configured_executable(value: str | None) -> Path | None:
    path = _absolute_path(value)
    if path is None:
        return None
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    return resolved if resolved.is_file() and os.access(resolved, os.X_OK) else None


def _configured_private_state_root(value: str | None) -> Path | None:
    path = _absolute_path(value)
    if path is None:
        return None
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        return None
    return resolved


def _allowed_root_count(value: str | None) -> int | None:
    if value is None or value == "":
        return 0
    parts = value.split(os.pathsep)
    if any(not part for part in parts):
        return None
    roots = {_configured_directory(part) for part in parts}
    if None in roots:
        return None
    return len(roots)


def _absolute_path(value: str | None) -> Path | None:
    if value is None or not value or "\x00" in value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        return None
    return path


def _write_text(document: _DoctorDocument, stream: TextIO) -> None:
    stream.write("DeepKOALA MCP doctor\n")
    stream.write(f"status: {document['status']}\n")
    stream.write(f"route state: {document['route_state']}\n")
    if document["issue"] is not None:
        stream.write(f"issue: {document['issue']}\n")
    stream.write(f"next: {document['next_action']}\n")
    stream.write("private paths: redacted\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Dispatch stdio serving or a side-effect-free deployment diagnostic."""
    arguments = _parser().parse_args(argv)
    if arguments.command in {None, "serve"}:
        run_stdio()
        return 0
    document, exit_code = _doctor_document(os.environ if environment is None else environment)
    stream = stdout or sys.stdout
    if arguments.json:
        json.dump(document, stream, ensure_ascii=True, indent=2, sort_keys=True)
        stream.write("\n")
    else:
        _write_text(document, stream)
    return exit_code


__all__ = ["main"]
