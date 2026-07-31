"""Small command-line facade for stdio serving and side-effect-free diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TextIO, TypedDict

from kegg_mcp import __version__
from kegg_mcp.domain.errors import KeggMcpError
from kegg_mcp.kegg import AccessMode
from kegg_mcp.kegg.cache import SQLiteKeggCache
from kegg_mcp.kegg.rate_limit import UnsupportedRuntimePlatformError
from kegg_mcp.mcp.config import load_runtime_config
from kegg_mcp.mcp.server import main as run_stdio
from kegg_mcp.services.result_store import ResultStoreError, SQLiteResultStore


class _DoctorDocument(TypedDict):
    status: str
    server_version: str
    configuration_valid: bool
    access_mode: str | None
    network_enabled: bool | None
    file_handoff_enabled: bool | None
    allowed_root_count: int | None
    allowed_root_paths: str
    network_probe: str
    storage_probe: str
    issues: list[str]
    next_actions: list[str]


class _CleanupDocument(TypedDict):
    status: str
    operation: str
    expired_results: int | None
    expired_bytes: int | None
    remaining_results: int | None
    remaining_bytes: int | None
    issue: str | None


class _CacheDocument(TypedDict, total=False):
    status: str
    operation: str
    entry_count: int
    expired_entry_count: int
    payload_bytes: int
    database_bytes: int
    max_entries: int
    max_payload_bytes: int
    max_database_bytes: int
    expired_entries: int
    expired_payload_bytes: int
    remaining_entries: int
    remaining_payload_bytes: int
    issue: str | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kegg-mcp",
        description="Local stdio MCP server for cautious KEGG analysis of KO annotations.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="Run the stdio MCP server (the default).")
    doctor = commands.add_parser(
        "doctor",
        help="Inspect redacted local configuration without network or database access.",
    )
    doctor.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    cleanup = commands.add_parser(
        "cleanup",
        help="Run an explicit local retained-result cleanup operation.",
    )
    cleanup.add_argument(
        "--expired",
        action="store_true",
        required=True,
        help="Delete only TTL-expired retained results; active results are not quota-evicted.",
    )
    cleanup.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    cache = commands.add_parser(
        "cache",
        help="Inspect or explicitly clean the bounded local KEGG response cache.",
    )
    cache_commands = cache.add_subparsers(dest="cache_command", required=True)
    cache_status = cache_commands.add_parser(
        "status",
        help="Report redacted cache counts and configured capacity.",
    )
    cache_status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    cache_cleanup = cache_commands.add_parser(
        "cleanup",
        help="Delete only TTL-expired KEGG response rows.",
    )
    cache_cleanup.add_argument("--expired", action="store_true", required=True)
    cache_cleanup.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    return parser


def _doctor_document(environment: Mapping[str, str] | None) -> tuple[_DoctorDocument, int]:
    try:
        config = load_runtime_config(environment)
    except UnsupportedRuntimePlatformError:
        return (
            {
                "status": "error",
                "server_version": __version__,
                "configuration_valid": False,
                "access_mode": None,
                "network_enabled": None,
                "file_handoff_enabled": None,
                "allowed_root_count": None,
                "allowed_root_paths": "redacted",
                "network_probe": "not_run",
                "storage_probe": "not_run",
                "issues": [
                    "This host cannot provide the POSIX advisory locking required for safe "
                    "deployment-wide KEGG rate limiting."
                ],
                "next_actions": [
                    "On native Windows, run kegg-mcp under WSL; otherwise use a supported POSIX "
                    "environment.",
                ],
            },
            2,
        )
    except ValueError:
        return (
            {
                "status": "error",
                "server_version": __version__,
                "configuration_valid": False,
                "access_mode": None,
                "network_enabled": None,
                "file_handoff_enabled": None,
                "allowed_root_count": None,
                "allowed_root_paths": "redacted",
                "network_probe": "not_run",
                "storage_probe": "not_run",
                "issues": [
                    "The environment does not satisfy the documented KEGG MCP configuration."
                ],
                "next_actions": [
                    "Review docs/installation.md and correct the named deployment variables.",
                    "Rerun kegg-mcp doctor before starting an MCP client.",
                ],
            },
            2,
        )

    root_count = len(config.allowed_roots)
    network_enabled = config.kegg.access.mode is not AccessMode.OFFLINE_CACHE
    next_actions = (
        ["Call probe_kegg_connectivity from an MCP client before the first live analysis."]
        if network_enabled
        else [
            "Use an authorized live profile to populate missing KEGG cache entries; offline mode "
            "never contacts KEGG."
        ]
    )
    if root_count == 0:
        next_actions.append("Set KEGG_MCP_ALLOWED_ROOTS to enable file handoff and output bundles.")
    return (
        {
            "status": "ok",
            "server_version": __version__,
            "configuration_valid": True,
            "access_mode": config.kegg.access.mode.value,
            "network_enabled": network_enabled,
            "file_handoff_enabled": root_count > 0,
            "allowed_root_count": root_count,
            "allowed_root_paths": "redacted",
            "network_probe": "not_run",
            "storage_probe": "not_run",
            "issues": [],
            "next_actions": next_actions,
        },
        0,
    )


def _write_doctor_text(document: _DoctorDocument, stream: TextIO) -> None:
    stream.write("KEGG MCP doctor\n")
    stream.write(f"status: {document['status']}\n")
    stream.write(f"server version: {document['server_version']}\n")
    stream.write(f"configuration valid: {str(document['configuration_valid']).lower()}\n")
    if document["configuration_valid"]:
        stream.write(f"access mode: {document['access_mode']}\n")
        stream.write(f"network enabled: {str(document['network_enabled']).lower()}\n")
        stream.write(
            f"file handoff enabled: {str(document['file_handoff_enabled']).lower()} "
            f"({document['allowed_root_count']} configured roots; paths redacted)\n"
        )
    for issue in document.get("issues", []):
        stream.write(f"issue: {issue}\n")
    for action in document["next_actions"]:
        stream.write(f"next: {action}\n")


def _cleanup_document(environment: Mapping[str, str] | None) -> tuple[_CleanupDocument, int]:
    try:
        config = load_runtime_config(environment)
        summary = SQLiteResultStore(config.result_store_path).cleanup_expired()
    except (ValueError, ResultStoreError):
        return (
            {
                "status": "error",
                "operation": "expired_results",
                "expired_results": None,
                "expired_bytes": None,
                "remaining_results": None,
                "remaining_bytes": None,
                "issue": "The local retained-result store could not be cleaned safely.",
            },
            2,
        )
    return (
        {
            "status": "ok",
            "operation": "expired_results",
            "expired_results": summary.expired_results,
            "expired_bytes": summary.expired_bytes,
            "remaining_results": summary.remaining_results,
            "remaining_bytes": summary.remaining_bytes,
            "issue": None,
        },
        0,
    )


def _write_cleanup_text(document: _CleanupDocument, stream: TextIO) -> None:
    stream.write("KEGG MCP retained-result cleanup\n")
    stream.write(f"status: {document['status']}\n")
    stream.write(f"operation: {document['operation']}\n")
    if document["status"] == "ok":
        stream.write(f"expired results deleted: {document['expired_results']}\n")
        stream.write(f"expired bytes deleted: {document['expired_bytes']}\n")
        stream.write(f"remaining results: {document['remaining_results']}\n")
        stream.write(f"remaining bytes: {document['remaining_bytes']}\n")
    elif document["issue"] is not None:
        stream.write(f"issue: {document['issue']}\n")


def _cache_document(
    environment: Mapping[str, str] | None,
    *,
    operation: str,
) -> tuple[_CacheDocument, int]:
    try:
        config = load_runtime_config(environment)
        policy = config.kegg.cache
        cache = SQLiteKeggCache(
            policy.path,
            max_entries=policy.max_entries,
            max_payload_bytes=policy.max_payload_bytes,
            max_database_bytes=policy.max_database_bytes,
        )
        now = datetime.now(UTC)
        if operation == "status":
            status = cache.status(now=now)
            return (
                {
                    "status": "ok",
                    "operation": "cache_status",
                    "entry_count": status.entry_count,
                    "expired_entry_count": status.expired_entry_count,
                    "payload_bytes": status.payload_bytes,
                    "database_bytes": status.database_bytes,
                    "max_entries": status.max_entries,
                    "max_payload_bytes": status.max_payload_bytes,
                    "max_database_bytes": status.max_database_bytes,
                    "issue": None,
                },
                0,
            )
        summary = cache.cleanup_expired(now=now)
        return (
            {
                "status": "ok",
                "operation": "expired_cache_entries",
                "expired_entries": summary.expired_entries,
                "expired_payload_bytes": summary.expired_payload_bytes,
                "remaining_entries": summary.remaining_entries,
                "remaining_payload_bytes": summary.remaining_payload_bytes,
                "database_bytes": summary.database_bytes,
                "issue": None,
            },
            0,
        )
    except (ValueError, KeggMcpError):
        return (
            {
                "status": "error",
                "operation": f"cache_{operation}",
                "issue": "The local KEGG response cache could not be accessed safely.",
            },
            2,
        )


def _write_cache_text(document: _CacheDocument, stream: TextIO) -> None:
    stream.write("KEGG MCP response cache\n")
    for name, value in document.items():
        stream.write(f"{name.replace('_', ' ')}: {value}\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    """Dispatch the stdio command or a diagnostic subcommand."""
    arguments = _parser().parse_args(argv)
    if arguments.command in {None, "serve"}:
        run_stdio()
        return 0

    if arguments.command == "cleanup":
        document, exit_code = _cleanup_document(environment)
        stream = stdout or sys.stdout
        if arguments.json:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        else:
            _write_cleanup_text(document, stream)
        return exit_code

    if arguments.command == "cache":
        document, exit_code = _cache_document(
            environment,
            operation=arguments.cache_command,
        )
        stream = stdout or sys.stdout
        if arguments.json:
            json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        else:
            _write_cache_text(document, stream)
        return exit_code

    document, exit_code = _doctor_document(environment)
    stream = stdout or sys.stdout
    if arguments.json:
        json.dump(document, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    else:
        _write_doctor_text(document, stream)
    return exit_code


__all__ = ["main"]
