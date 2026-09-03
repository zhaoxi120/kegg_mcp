"""Deterministic representation helpers shared by the SVG and PNG backends."""

from __future__ import annotations

from kegg_mcp.domain import SourceProvenance

ACCEPTED_COLOR = "#FF0000"
LEGEND_FONT_SIZE = 16
LEGEND_ROW_HEIGHT = 24


def pathway_footer_height(*, has_annotation_credit: bool) -> int:
    """Return the shared compact footer height for both pathway backends."""
    return max(110, 92 + (LEGEND_ROW_HEIGHT if has_annotation_credit else 0))


def annotation_credit(sources: tuple[SourceProvenance, ...]) -> str | None:
    """Return a display-safe DeepKOALA credit when every source supports it."""
    if not sources or any(source.source_name.casefold() != "deepkoala" for source in sources):
        return None
    model_names = tuple(
        sorted(
            {
                "fragment" if source.model_name == "frag" else source.model_name
                for source in sources
                if source.model_name is not None
            }
        )
    )
    suffix = f" ({', '.join(model_names)})" if model_names else ""
    return f"Annotated by DeepKOALA{suffix}"


__all__ = [
    "ACCEPTED_COLOR",
    "LEGEND_FONT_SIZE",
    "LEGEND_ROW_HEIGHT",
    "annotation_credit",
    "pathway_footer_height",
]
