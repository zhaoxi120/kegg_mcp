"""Core package for KEGG MCP."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kegg-mcp")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = ["__version__"]
