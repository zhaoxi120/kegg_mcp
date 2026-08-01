"""Renderer host-capability and advisory-lock portability tests."""

from __future__ import annotations

import errno

import pytest

from kegg_render_mcp import _platform as platform_module


def test_supported_posix_host_satisfies_renderer_capability_gate() -> None:
    platform_module.validate_renderer_platform()


def test_unsupported_host_has_static_native_windows_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_module, "_renderer_platform_is_supported", lambda: False)

    with pytest.raises(
        platform_module.UnsupportedRendererPlatformError,
        match=r"unsupported platform.*native Windows.*WSL",
    ):
        platform_module.validate_renderer_platform()


def test_missing_fcntl_module_fails_with_platform_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_module, "_LOCK_MODULE", None)

    with pytest.raises(
        platform_module.UnsupportedRendererPlatformError,
        match="unsupported platform",
    ):
        platform_module.acquire_exclusive_lock(3)


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EAGAIN])
def test_nonblocking_lock_treats_portable_errno_values_as_contention(
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    def contend(descriptor: int, operation_names: tuple[str, ...]) -> None:
        del descriptor, operation_names
        raise OSError(error_number, "synthetic contention")

    monkeypatch.setattr(platform_module, "_invoke_flock", contend)

    assert platform_module.acquire_exclusive_lock(3, nonblocking=True) is False


def test_nonblocking_lock_preserves_unexpected_operating_system_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(descriptor: int, operation_names: tuple[str, ...]) -> None:
        del descriptor, operation_names
        raise OSError(errno.EIO, "synthetic failure")

    monkeypatch.setattr(platform_module, "_invoke_flock", fail)

    with pytest.raises(OSError, match="synthetic failure"):
        platform_module.acquire_exclusive_lock(3, nonblocking=True)
