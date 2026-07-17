"""Security and commit-marker tests for local output bundles."""

import os
from pathlib import Path

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.services.output_bundle import (
    _open_directory_fd,  # pyright: ignore[reportPrivateUsage]
    _validate_output_directory_fd,  # pyright: ignore[reportPrivateUsage]
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


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_directory_open_rejects_writable_user_owned_ancestors(
    tmp_path: Path,
    mode: int,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=mode)
    unsafe.chmod(mode)

    with pytest.raises(OSError):
        _open_directory_fd(unsafe / "bundle")

    assert not (unsafe / "bundle").exists()


def test_directory_validation_rejects_non_owner_below_owner_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(os, "geteuid", lambda: os.fstat(descriptor).st_uid + 1)
        with pytest.raises(OSError):
            _validate_output_directory_fd(descriptor, owner_boundary=True)
    finally:
        os.close(descriptor)


def test_existing_bundle_is_rejected_without_modification(tmp_path: Path) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    manifest = output / "bundle_manifest.json"
    manifest.write_text("old", encoding="utf-8")

    with pytest.raises(KeggMcpError) as raised:
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    assert manifest.read_text(encoding="utf-8") == "old"
    assert not (output / "one.txt").exists()


def test_failed_install_leaves_no_commit_manifest_or_partial_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    real_link = os.link
    calls = 0

    def fail_second_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic install failure")
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(os, "link", fail_second_link)
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
    assert not (output / "one.txt").exists()
    assert not (output / "two.txt").exists()
    assert not tuple(output.glob(".*.tmp"))
