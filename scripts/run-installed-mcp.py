#!/usr/bin/env python3
"""Launch exactly one installed KEGG MCP process from a private deployment manifest."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from typing import NoReturn, cast

SCHEMA_VERSION = 1
SERVER_NAMES = ("deepkoala-mcp", "kegg-mcp", "kegg-render-mcp")
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ENVIRONMENT_ENTRIES = 128


def _fail(message: str) -> NoReturn:
    print(f"kegg-mcp installed launcher failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def _read_manifest(path: Path) -> dict[str, object]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if not hasattr(os, "O_NOFOLLOW"):
        _fail("the platform lacks no-follow filesystem controls")
    try:
        descriptor = os.open(path, flags | os.O_NOFOLLOW)
    except OSError:
        _fail("the private deployment manifest is unavailable")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("the private deployment manifest is not a regular file")
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            _fail("the private deployment manifest ownership or mode is unsafe")
        if metadata.st_size <= 0 or metadata.st_size > MAX_MANIFEST_BYTES:
            _fail("the private deployment manifest size is invalid")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(payload) > MAX_MANIFEST_BYTES:
        _fail("the private deployment manifest size is invalid")
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail("the private deployment manifest is invalid")
    if not isinstance(document, dict):
        _fail("the private deployment manifest must be an object")
    return cast(dict[str, object], document)


def _server_configuration(
    document: dict[str, object], server_name: str, install_root: Path
) -> tuple[Path, dict[str, str]]:
    if set(document) != {"schema_version", "commands", "environments"}:
        _fail("the private deployment manifest has unknown or missing fields")
    schema_version = document["schema_version"]
    if type(schema_version) is not int or schema_version != SCHEMA_VERSION:
        _fail("the private deployment manifest schema is unsupported")
    commands = document["commands"]
    environments = document["environments"]
    if not isinstance(commands, dict):
        _fail("the installed command map is invalid")
    typed_commands = cast(dict[str, object], commands)
    if set(typed_commands) != set(SERVER_NAMES):
        _fail("the installed command map is invalid")
    if not isinstance(environments, dict):
        _fail("the installed environment map is invalid")
    typed_environments = cast(dict[str, object], environments)
    if set(typed_environments) != set(SERVER_NAMES):
        _fail("the installed environment map is invalid")

    raw_command = typed_commands.get(server_name)
    raw_environment = typed_environments.get(server_name)
    if not isinstance(raw_command, str):
        _fail("the selected server configuration is invalid")
    if not isinstance(raw_environment, dict):
        _fail("the selected server configuration is invalid")
    typed_environment = cast(dict[object, object], raw_environment)
    if len(typed_environment) > MAX_ENVIRONMENT_ENTRIES or not all(
        isinstance(key, str)
        and key
        and "=" not in key
        and "\x00" not in key
        and isinstance(value, str)
        and "\x00" not in value
        for key, value in typed_environment.items()
    ):
        _fail("the selected server environment is invalid")

    command = Path(raw_command)
    if not command.is_absolute() or ".." in command.parts:
        _fail("the installed server command is invalid")
    try:
        resolved_command = command.resolve(strict=True)
        resolved_root = install_root.resolve(strict=True)
    except OSError:
        _fail("the installed server command is unavailable")
    if not resolved_command.is_relative_to(resolved_root / "runtimes"):
        _fail("the installed server command escapes the runtime root")
    metadata = resolved_command.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved_command, os.X_OK):
        _fail("the installed server command is not executable")
    return resolved_command, cast(dict[str, str], typed_environment)


def main(argv: list[str] | None = None) -> NoReturn:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or arguments[0] not in SERVER_NAMES:
        _fail("select exactly one supported MCP server")
    server_name = arguments[0]
    deployment_root = Path(__file__).resolve().parent
    install_root = deployment_root.parent
    document = _read_manifest(deployment_root / "deployment.json")
    command, configured_environment = _server_configuration(document, server_name, install_root)
    environment = os.environ.copy()
    for key in tuple(environment):
        if key.startswith(("KEGG_MCP_", "KEGG_RENDER_MCP_", "DEEPKOALA_MCP_")) or key in {
            "PYTHONHOME",
            "PYTHONPATH",
            "VIRTUAL_ENV",
        }:
            environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    environment.update(configured_environment)
    try:
        os.execve(command, [str(command)], environment)
    except OSError:
        _fail("the installed MCP process could not be started")


if __name__ == "__main__":
    main()
