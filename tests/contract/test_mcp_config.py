"""Environment configuration contracts for public and licensed KEGG modes."""

import os
from pathlib import Path

import pytest

from kegg_mcp.kegg import AccessMode, RetrievalEndpointClass, endpoint_fingerprint
from kegg_mcp.mcp.config import load_runtime_config


def test_empty_environment_defaults_to_confirmed_public_academic_access() -> None:
    config = load_runtime_config({"HOME": "/tmp/test-home"})
    assert config.kegg.access.mode is AccessMode.PUBLIC_ACADEMIC
    assert config.kegg.access.academic_use_confirmed is True


def test_public_academic_defaults_confirmation_but_rejects_explicit_false() -> None:
    with pytest.raises(ValueError, match="ACADEMIC_USE_CONFIRMED"):
        load_runtime_config(
            {
                "KEGG_MCP_ACCESS_MODE": "public_academic",
                "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "false",
            }
        )
    config = load_runtime_config({"KEGG_MCP_ACCESS_MODE": "public_academic"})
    assert config.kegg.access.mode is AccessMode.PUBLIC_ACADEMIC


def test_explicit_offline_cache_mode_reuses_the_public_namespace_without_network() -> None:
    config = load_runtime_config({"KEGG_MCP_ACCESS_MODE": "offline_cache"})

    assert config.kegg.access.mode is AccessMode.OFFLINE_CACHE
    assert config.kegg.access.retrieval_endpoint_class is RetrievalEndpointClass.PUBLIC_ACADEMIC


def test_offline_licensed_cache_selection_requires_the_authorized_endpoint_pair() -> None:
    endpoint = "https://licensed.example.test/kegg"
    config = load_runtime_config(
        {
            "KEGG_MCP_ACCESS_MODE": "offline_cache",
            "KEGG_MCP_LICENSED_ENDPOINT": endpoint,
            "KEGG_MCP_LICENSED_USE_CONFIRMED": "true",
        }
    )

    assert config.kegg.access.mode is AccessMode.OFFLINE_CACHE
    assert config.kegg.access.retrieval_endpoint_class is RetrievalEndpointClass.LICENSED
    assert config.kegg.access.endpoint == endpoint
    assert config.kegg.access.endpoint_fingerprint == endpoint_fingerprint(endpoint)
    with pytest.raises(ValueError, match="both licensed endpoint and confirmation"):
        load_runtime_config(
            {
                "KEGG_MCP_ACCESS_MODE": "offline_cache",
                "KEGG_MCP_LICENSED_ENDPOINT": endpoint,
            }
        )


def test_licensed_live_mode_requires_endpoint_and_confirmation() -> None:
    with pytest.raises(ValueError, match="LICENSED_USE_CONFIRMED"):
        load_runtime_config({"KEGG_MCP_ACCESS_MODE": "licensed"})
    config = load_runtime_config(
        {
            "KEGG_MCP_ACCESS_MODE": "licensed",
            "KEGG_MCP_LICENSED_ENDPOINT": "https://licensed.example.test/kegg",
            "KEGG_MCP_LICENSED_USE_CONFIRMED": "true",
        }
    )
    assert config.kegg.access.mode is AccessMode.LICENSED


def test_allowed_roots_are_canonicalized_and_deduplicated(tmp_path: Path) -> None:
    root = tmp_path / "shared"
    root.mkdir()
    config = load_runtime_config(
        {
            "HOME": str(tmp_path),
            "KEGG_MCP_ALLOWED_ROOTS": os.pathsep.join((str(root), str(root / "."))),
        }
    )

    assert config.allowed_roots == (str(root.resolve()),)


@pytest.mark.parametrize("value", ["relative", f"/tmp{os.pathsep}"])
def test_allowed_roots_reject_relative_or_empty_entries(value: str) -> None:
    with pytest.raises(ValueError, match="ALLOWED_ROOTS"):
        load_runtime_config({"KEGG_MCP_ALLOWED_ROOTS": value})


def test_cache_capacity_and_shared_rate_limit_root_are_deployment_configurable(
    tmp_path: Path,
) -> None:
    config = load_runtime_config(
        {
            "KEGG_MCP_CACHE_PATH": str(tmp_path / "cache.sqlite3"),
            "KEGG_MCP_CACHE_MAX_ENTRIES": "12",
            "KEGG_MCP_CACHE_MAX_PAYLOAD_BYTES": "1000000",
            "KEGG_MCP_CACHE_MAX_DATABASE_BYTES": "2000000",
            "KEGG_MCP_RATE_LIMIT_ROOT": str(tmp_path / "rate-limit"),
        }
    )

    assert config.kegg.cache.max_entries == 12
    assert config.kegg.cache.max_payload_bytes == 1_000_000
    assert config.kegg.cache.max_database_bytes == 2_000_000
    assert config.kegg.rate_limit.state_root == str(tmp_path / "rate-limit")


@pytest.mark.parametrize("value", ["0", "-1", "01", "not-a-number"])
def test_cache_capacity_environment_requires_positive_decimal_integers(value: str) -> None:
    with pytest.raises(ValueError):
        load_runtime_config({"KEGG_MCP_CACHE_MAX_ENTRIES": value})
