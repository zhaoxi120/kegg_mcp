"""Tests for exact K-number validation and normalization."""

import pytest

from kegg_mcp.domain import ErrorCode, KeggMcpError, normalize_ko_id, try_normalize_ko_id


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("K00001", "K00001"),
        (" ko:K12345 ", "K12345"),
        ("KO:K99999", "K99999"),
    ],
)
def test_normalize_ko_id_accepts_only_explicit_forms(raw: str, expected: str) -> None:
    assert normalize_ko_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "K1234",
        "K123456",
        "K0000A",
        "M00001",
        "abcK00001",
        "K00001 extra",
        "k00001",
        "ko:k00001",
        "",
    ],
)
def test_normalize_ko_id_rejects_invalid_or_embedded_values(raw: str) -> None:
    with pytest.raises(KeggMcpError) as error:
        normalize_ko_id(raw)

    assert error.value.detail.code is ErrorCode.INVALID_KO_IDENTIFIER
    assert error.value.detail.recoverable is True
    assert error.value.detail.suggested_action


def test_try_normalize_ko_id_returns_machine_reason() -> None:
    assert try_normalize_ko_id("not-a-ko") == (None, "invalid_ko_identifier")
