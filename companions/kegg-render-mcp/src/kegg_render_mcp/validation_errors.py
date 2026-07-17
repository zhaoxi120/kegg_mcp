"""Bounded, input-free summaries of Pydantic validation failures."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import ValidationError

_UNSAFE_PATH_CHARACTER = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class ValidationIssueSummary:
    issue_count: int
    field_path: str


def summarize_validation_error(error: ValidationError) -> ValidationIssueSummary:
    """Return only schema-owned location data, never the rejected value or context."""
    issues = error.errors(include_url=False, include_context=False, include_input=False)
    location = issues[0].get("loc", ()) if issues else ()
    parts: list[str] = []
    for segment in location:
        if isinstance(segment, int):
            parts.append(f"[{segment}]")
            continue
        safe = _UNSAFE_PATH_CHARACTER.sub("_", str(segment)).strip("_")[:48]
        if not safe:
            safe = "field"
        parts.append(("." if parts else "") + safe)
    field_path = "".join(parts)[:160] or "root"
    return ValidationIssueSummary(issue_count=error.error_count(), field_path=field_path)


__all__ = ["ValidationIssueSummary", "summarize_validation_error"]
