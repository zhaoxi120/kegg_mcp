"""Tests for bounded DeepKOALA installation inspection and selection."""

from pathlib import Path

import pytest

from deepkoala_mcp.contracts import ErrorCode
from deepkoala_mcp.installation import (
    InstallationError,
    inspect_installation,
    select_installation,
)


def test_inspection_and_latest_selection_are_deterministic(checkout: Path) -> None:
    version, resources = inspect_installation(checkout)
    selected = select_installation(checkout, "full", "latest")

    assert version == "0.1-test"
    assert [(item.model_date, item.model) for item in resources] == [
        ("202401", "full"),
        ("202401", "frag"),
        ("202502", "full"),
        ("202502", "frag"),
    ]
    assert selected.source_version == version
    assert selected.resource.model == "full"
    assert selected.resource.model_date == "202502"


def test_selection_fails_closed_for_missing_resources(checkout: Path) -> None:
    with pytest.raises(InstallationError) as captured:
        select_installation(checkout, "full", "202601")

    assert captured.value.code is ErrorCode.WEIGHTS_NOT_FOUND
