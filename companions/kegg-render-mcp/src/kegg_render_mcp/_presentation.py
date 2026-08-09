"""Deterministic representation helpers shared by the SVG and PNG backends."""

from __future__ import annotations

ACCEPTED_COLOR = "#FF0000"
UNSUPPORTED_COLOR = "#7F7F7F"
EMPTY_BLOCK_COLOR = "#FFFFFF"


def ratio_text(value: float | None) -> str:
    """Format a project block-coverage ratio for a static graphic."""
    return "not evaluable" if value is None else f"{value:.1%}"


def exact_completion_text(value: bool | None) -> str:
    """Format exact MODULE completion without collapsing unknown into false."""
    return "complete" if value is True else "incomplete" if value is False else "not evaluable"


def block_color(state: str) -> str:
    """Choose the shared evidence color for one authoritative MODULE block state."""
    if state == "complete":
        return ACCEPTED_COLOR
    if state == "not_evaluable":
        return UNSUPPORTED_COLOR
    return EMPTY_BLOCK_COLOR


__all__ = [
    "ACCEPTED_COLOR",
    "EMPTY_BLOCK_COLOR",
    "UNSUPPORTED_COLOR",
    "block_color",
    "exact_completion_text",
    "ratio_text",
]
