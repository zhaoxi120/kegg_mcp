"""Environment configuration contracts for public and licensed KEGG modes."""

import os
from pathlib import Path

import pytest

from kegg_mcp.kegg import AccessMode
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


def test_removed_offline_cache_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="public_academic or licensed"):
        load_runtime_config({"KEGG_MCP_ACCESS_MODE": "offline_cache"})


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
