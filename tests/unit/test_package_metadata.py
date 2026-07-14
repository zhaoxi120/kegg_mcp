from importlib.metadata import version

import kegg_mcp


def test_package_version_matches_installed_metadata() -> None:
    assert kegg_mcp.__version__ == version("kegg-mcp")
