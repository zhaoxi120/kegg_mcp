"""Validation and normalization for KEGG identifiers."""

import re

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, SafeDetail, fail

_KO_PATTERN = re.compile(r"K[0-9]{5}\Z")


def normalize_ko_id(raw: str) -> str:
    """Normalize one exact K number or ``ko:``-prefixed K number.

    The function trims surrounding whitespace and accepts a case-insensitive
    ``ko:`` namespace prefix. It intentionally does not extract identifiers
    from free text or change the case of the K number itself.
    """
    candidate = raw.strip()
    if candidate[:3].lower() == "ko:":
        candidate = candidate[3:]
    if not _KO_PATTERN.fullmatch(candidate):
        fail(
            ErrorCode.INVALID_KO_IDENTIFIER,
            "The value is not a valid KEGG K number.",
            suggested_action="Provide an identifier such as K00001 or ko:K00001.",
            safe_details=(SafeDetail(name="value_length", value=str(len(raw))),),
        )
    return candidate


def try_normalize_ko_id(raw: str) -> tuple[str | None, str | None]:
    """Return a normalized identifier and reason without raising."""
    try:
        return normalize_ko_id(raw), None
    except KeggMcpError:
        return None, "invalid_ko_identifier"
