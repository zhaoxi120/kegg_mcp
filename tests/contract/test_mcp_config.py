"""Environment configuration contracts for offline and authorized KEGG modes."""

import pytest

from kegg_mcp.kegg import AccessMode, RetrievalEndpointClass
from kegg_mcp.mcp.config import load_runtime_config


def test_empty_environment_is_network_disabled() -> None:
    config = load_runtime_config({"HOME": "/tmp/test-home"})
    assert config.kegg.access.mode is AccessMode.OFFLINE_CACHE


def test_public_academic_requires_literal_confirmation() -> None:
    with pytest.raises(ValueError, match="ACADEMIC_USE_CONFIRMED"):
        load_runtime_config({"KEGG_MCP_ACCESS_MODE": "public_academic"})
    config = load_runtime_config(
        {
            "KEGG_MCP_ACCESS_MODE": "public_academic",
            "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "true",
        }
    )
    assert config.kegg.access.mode is AccessMode.PUBLIC_ACADEMIC


def test_licensed_endpoint_can_be_reopened_in_offline_cache_mode() -> None:
    environment = {
        "KEGG_MCP_ACCESS_MODE": "offline_cache",
        "KEGG_MCP_LICENSED_ENDPOINT": "https://licensed.example.test/kegg",
        "KEGG_MCP_LICENSED_USE_CONFIRMED": "true",
    }
    config = load_runtime_config(environment)
    assert config.kegg.access.mode is AccessMode.OFFLINE_CACHE
    assert config.kegg.access.retrieval_endpoint_class is RetrievalEndpointClass.LICENSED
    assert config.kegg.access.endpoint_fingerprint != ""
    with pytest.raises(ValueError, match="both licensed endpoint and confirmation"):
        load_runtime_config(
            {"KEGG_MCP_LICENSED_ENDPOINT": environment["KEGG_MCP_LICENSED_ENDPOINT"]}
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
