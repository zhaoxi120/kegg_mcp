"""Security and commit-marker tests for local output bundles."""

import os
from pathlib import Path

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.services.output_bundle import (
    _open_directory_fd_with_creation,  # pyright: ignore[reportPrivateUsage]
    _validate_output_directory_fd,  # pyright: ignore[reportPrivateUsage]
    _write_files,  # pyright: ignore[reportPrivateUsage]
)


def test_directory_open_rejects_symlink_components(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError):
        _open_directory_fd_with_creation(alias / "bundle")

    assert not (real / "bundle").exists()


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_writable_user_owned_ancestor_does_not_start_private_boundary(
    tmp_path: Path,
    mode: int,
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir(mode=mode)
    shared.chmod(mode)
    descriptor = os.open(shared, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _validate_output_directory_fd(descriptor, private_boundary=False) is False
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("mode", [0o770, 0o707])
def test_writable_user_owned_ancestor_is_rejected_below_private_boundary(
    tmp_path: Path,
    mode: int,
) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=mode)
    unsafe.chmod(mode)
    descriptor = os.open(unsafe, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError):
            _validate_output_directory_fd(descriptor, private_boundary=True)
    finally:
        os.close(descriptor)


def test_directory_validation_rejects_non_owner_below_private_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(os, "geteuid", lambda: os.fstat(descriptor).st_uid + 1)
        with pytest.raises(OSError):
            _validate_output_directory_fd(descriptor, private_boundary=True)
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


def test_failed_install_removes_service_created_empty_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "service-bundle"

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic install failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(KeggMcpError) as raised:
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
            remove_created_directory_on_failure=True,
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert not output.exists()


def test_final_open_failure_removes_just_created_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "service-bundle"
    real_open = os.open

    def fail_output_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == output.name and flags & os.O_DIRECTORY:
            raise OSError("synthetic final open failure")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", fail_output_open)
    with pytest.raises(KeggMcpError) as raised:
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
            remove_created_directory_on_failure=True,
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert not output.exists()


def test_created_directory_replacement_is_rejected_and_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "service-bundle"
    displaced = tmp_path / "displaced-service-bundle"
    real_open = os.open

    def replace_before_output_open(
        path: str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == output.name and flags & os.O_DIRECTORY:
            output.rename(displaced)
            output.mkdir(mode=0o700)
            (output / "caller-owned.txt").write_text("keep", encoding="utf-8")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_output_open)
    with pytest.raises(OSError, match="replaced"):
        _open_directory_fd_with_creation(output)

    assert (output / "caller-owned.txt").read_text(encoding="utf-8") == "keep"
    assert displaced.is_dir()
    assert tuple(displaced.iterdir()) == ()


def test_failed_install_preserves_explicit_new_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "explicit-bundle"

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic install failure")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(KeggMcpError):
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
        )

    assert output.is_dir()
    assert tuple(output.iterdir()) == ()


def test_failed_install_does_not_remove_directory_with_caller_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "service-bundle"

    def inject_file_then_fail(*_args: object, **_kwargs: object) -> None:
        (output / "caller-owned.txt").write_text("keep", encoding="utf-8")
        raise OSError("synthetic install failure")

    monkeypatch.setattr(os, "link", inject_file_then_fail)
    with pytest.raises(KeggMcpError):
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
            remove_created_directory_on_failure=True,
        )

    assert (output / "caller-owned.txt").read_text(encoding="utf-8") == "keep"


def test_failed_install_does_not_remove_replacement_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "service-bundle"
    displaced = tmp_path / "displaced-service-bundle"

    def replace_then_fail(*_args: object, **_kwargs: object) -> None:
        output.rename(displaced)
        output.mkdir(mode=0o700)
        (output / "caller-owned.txt").write_text("keep", encoding="utf-8")
        raise OSError("synthetic install failure")

    monkeypatch.setattr(os, "link", replace_then_fail)
    with pytest.raises(KeggMcpError):
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
            remove_created_directory_on_failure=True,
        )

    assert (output / "caller-owned.txt").read_text(encoding="utf-8") == "keep"
    assert displaced.is_dir()
    assert tuple(displaced.iterdir()) == ()
