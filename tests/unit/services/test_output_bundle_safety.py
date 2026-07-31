"""Security and commit-marker tests for local output bundles."""

import os
from pathlib import Path

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.services._atomic_bundle import (
    _validate_output_directory_fd,  # pyright: ignore[reportPrivateUsage]
    preflight_text_bundle_output,
)
from kegg_mcp.services.output_bundle import _write_files  # pyright: ignore[reportPrivateUsage]


def test_output_preflight_is_read_only_for_missing_and_empty_directories(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-bundle"
    preflight_text_bundle_output(missing)
    assert not missing.exists()

    empty = tmp_path / "empty-bundle"
    empty.mkdir(mode=0o700)
    preflight_text_bundle_output(empty)
    assert tuple(empty.iterdir()) == ()


def test_output_preflight_rejects_nonempty_directory_without_modification(
    tmp_path: Path,
) -> None:
    output = tmp_path / "occupied-bundle"
    output.mkdir(mode=0o700)
    sentinel = output / "caller-owned.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(KeggMcpError) as raised:
        preflight_text_bundle_output(output)

    assert raised.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert {item.name for item in output.iterdir()} == {"caller-owned.txt"}


def test_output_preflight_rejects_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(KeggMcpError) as raised:
        preflight_text_bundle_output(alias)

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert tuple(real.iterdir()) == ()


def test_output_preflight_rejects_empty_directory_without_write_permission(
    tmp_path: Path,
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root can bypass directory write permission bits")
    output = tmp_path / "read-only-bundle"
    output.mkdir(mode=0o500)
    output.chmod(0o500)
    try:
        with pytest.raises(KeggMcpError) as raised:
            preflight_text_bundle_output(output)
        assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
        assert tuple(output.iterdir()) == ()
    finally:
        output.chmod(0o700)


def test_directory_open_rejects_symlink_components(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    with pytest.raises(KeggMcpError) as raised:
        _write_files(
            alias / "bundle",
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
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


def test_large_nonempty_bundle_directory_is_rejected_without_listing_all_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir()
    for index in range(2_048):
        (output / f"caller-{index:04d}.txt").touch()

    def reject_unbounded_listing(_path: object) -> list[str]:
        raise AssertionError("output directory names were materialized")

    monkeypatch.setattr(os, "listdir", reject_unbounded_listing)

    with pytest.raises(KeggMcpError) as raised:
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    with os.scandir(output) as entries:
        assert sum(1 for _entry in entries) == 2_048


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


def test_replacement_after_manifest_publication_cannot_report_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "bundle"
    output.mkdir(mode=0o700)
    displaced = tmp_path / "displaced-bundle"
    real_unlink = os.unlink
    replaced = False

    def replace_on_temporary_cleanup(
        path: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            output.rename(displaced)
            output.mkdir(mode=0o700)
            (output / "caller-owned.txt").write_text("keep", encoding="utf-8")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", replace_on_temporary_cleanup)
    with pytest.raises(KeggMcpError) as raised:
        _write_files(
            output,
            {
                "one.txt": "one",
                "bundle_manifest.json": "new",
            },
        )

    assert replaced is True
    assert raised.value.detail.code is ErrorCode.OUTPUT_WRITE_FAILED
    assert (output / "caller-owned.txt").read_text(encoding="utf-8") == "keep"
    assert {item.name for item in output.iterdir()} == {"caller-owned.txt"}
    assert displaced.is_dir()
    assert tuple(displaced.iterdir()) == ()
