"""Security and commit-marker tests for local output bundles."""

import os
from pathlib import Path

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.services.output_bundle import (
    _open_directory_fd,  # pyright: ignore[reportPrivateUsage]
    _write_files,  # pyright: ignore[reportPrivateUsage]
)


def test_directory_open_rejects_symlink_components(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError):
        _open_directory_fd(alias / "bundle")

    assert not (real / "bundle").exists()


def test_failed_install_leaves_no_commit_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "bundle_manifest.json").write_text("old", encoding="utf-8")
    real_replace = os.replace
    calls = 0

    def fail_second_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic install failure")
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(os, "replace", fail_second_replace)
    with pytest.raises(KeggMcpError) as raised:
        _write_files(
            output,
            {
                "one.txt": "one",
                "two.txt": "two",
                "bundle_manifest.json": "new",
            },
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert not (output / "bundle_manifest.json").exists()
    assert not tuple(output.glob(".*.tmp"))
