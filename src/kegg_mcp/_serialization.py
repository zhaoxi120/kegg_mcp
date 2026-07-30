"""Private serialization safety helpers shared across bounded artifacts."""

from __future__ import annotations

_SPREADSHEET_FORMULA_PREFIXES = frozenset({"=", "+", "-", "@", "\t", "\r", "\n", "'"})


def escape_spreadsheet_formula(value: str) -> str:
    """Prefix formula-like cells so spreadsheet applications treat them as text."""
    # Apostrophes are escaped as well, making one-prefix removal reversible for consumers.
    if value and value[0] in _SPREADSHEET_FORMULA_PREFIXES:
        return f"'{value}"
    return value


__all__ = ["escape_spreadsheet_formula"]
