"""Small CLI for stdio serving and redacted deployment diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO, TypedDict

from deepkoala_mcp import __version__
from deepkoala_mcp.config import load_runtime_config
from deepkoala_mcp.contracts import CompanionRouteState
from deepkoala_mcp.installation import (
    InstallationError,
    classify_readiness_route,
    inspect_installation,
    probe_multi_dependencies,
    probe_runtime,
)
from deepkoala_mcp.server import main as run_stdio


class _DoctorDocument(TypedDict):
    status: str
    server_version: str
    route_state: str
    configuration_valid: bool
    checkout_ready: bool
    runtime_ready: bool
    cuda_available: bool
    model_resources_ready: bool
    allow_multi: bool
    multi_ready: bool
    state_root_ready: bool
    input_root_count: int | None
    output_root_count: int | None
    downloads_enabled: bool
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
    try:
        config = load_runtime_config(environment)
    except (OSError, ValueError):
        return (
            _document(
                route_state="runner_misconfigured",
                issue="The explicit DeepKOALA companion configuration is missing or unsafe.",
                next_action="Correct the required companion environment variables and paths.",
            ),
            2,
        )

    checkout_ready = False
    resources_ready = False
    try:
        _, resources = inspect_installation(config.checkout)
        checkout_ready = True
        resources_ready = any(item.model in config.allowed_models for item in resources)
    except InstallationError:
        pass
    runtime = probe_runtime(
        checkout=config.checkout,
        python_executable=config.python_executable,
        cpu_threads=config.cpu_threads,
    )
    multi_ready = probe_multi_dependencies(
        allow_multi=config.allow_multi,
        profiles_dir=config.profiles_dir,
        hmmsearch_executable=config.hmmsearch_executable,
        runtime=runtime,
    )
    route = classify_readiness_route(
        checkout_ready=checkout_ready,
        runtime_ready=runtime.runtime_ready,
        model_resources_ready=resources_ready,
        allow_multi=config.allow_multi,
        multi_ready=multi_ready,
    )
    ready = route.route_state is CompanionRouteState.LOCAL_READY
    return (
        _document(
            route_state=route.route_state.value,
            issue=route.issue,
            next_action=route.next_action,
            configuration_valid=True,
            checkout_ready=checkout_ready,
            runtime_ready=runtime.runtime_ready,
            cuda_available=runtime.cuda_available,
            model_resources_ready=resources_ready,
            allow_multi=config.allow_multi,
            multi_ready=multi_ready,
            state_root_ready=True,
            input_root_count=len(config.input_roots),
            output_root_count=len(config.output_roots),
        ),
        0 if ready else 2,
    )


def _document(
    *,
    route_state: str,
    issue: str | None,
    next_action: str,
    configuration_valid: bool = False,
    checkout_ready: bool = False,
    runtime_ready: bool = False,
    cuda_available: bool = False,
    model_resources_ready: bool = False,
    allow_multi: bool = False,
    multi_ready: bool = False,
    state_root_ready: bool = False,
    input_root_count: int | None = None,
    output_root_count: int | None = None,
) -> _DoctorDocument:
    return {
        "status": "ok" if route_state == "local_ready" else "action_required",
        "server_version": __version__,
        "route_state": route_state,
        "configuration_valid": configuration_valid,
        "checkout_ready": checkout_ready,
        "runtime_ready": runtime_ready,
        "cuda_available": cuda_available,
        "model_resources_ready": model_resources_ready,
        "allow_multi": allow_multi,
        "multi_ready": multi_ready,
        "state_root_ready": state_root_ready,
        "input_root_count": input_root_count,
        "output_root_count": output_root_count,
        "downloads_enabled": False,
        "private_paths": "redacted",
        "issue": issue,
        "next_action": next_action,
    }


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
    """Dispatch stdio serving or a no-inference deployment diagnostic."""
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
