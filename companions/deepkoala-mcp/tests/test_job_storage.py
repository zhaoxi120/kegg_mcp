"""Pinned output-directory publication, resource, and cleanup regression tests."""

from __future__ import annotations

import contextlib
import fcntl
import multiprocessing
import os
from multiprocessing.connection import Connection
from pathlib import Path
from typing import cast

import pytest

import deepkoala_mcp.job_storage as storage
from conftest import DETAILED_CSV
from deepkoala_mcp.contracts import ANNOTATIONS_FILENAME, RUN_REPORT_FILENAME
from deepkoala_mcp.job_storage import (
    ControlledOutputDirectory,
    OutputAlreadyExistsError,
    OutputPathError,
    OutputValidationError,
    StateSession,
    artifact_size,
    cleanup_output_directory,
    close_output_directory,
    close_state_session,
    create_output_directory,
    open_state_session,
    publish_artifacts,
    read_artifact_slice,
    release_runner_lock,
    remove_job_directory,
    try_acquire_runner_lock,
    validate_delivered_artifacts,
)


def _controlled_output(
    tmp_path: Path,
) -> tuple[Path, Path, Path, ControlledOutputDirectory]:
    root = tmp_path / "allowed"
    parent = root / "project"
    root.mkdir(mode=0o700)
    parent.mkdir(mode=0o700)
    output = parent / "run"
    return root, parent, output, create_output_directory(output, (root,))


def _raw_output(tmp_path: Path) -> Path:
    raw = tmp_path / "raw.csv"
    raw.write_bytes(DETAILED_CSV)
    return raw


def test_existing_empty_output_is_pinned_and_not_removed_by_cleanup(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "existing"
    output.mkdir(mode=0o700)

    controlled = create_output_directory(output, (root,))
    try:
        assert controlled.created_by_service is False
        cleanup_output_directory(controlled)
        assert output.is_dir()
        assert tuple(output.iterdir()) == ()
    finally:
        close_output_directory(controlled)


def test_existing_nonempty_output_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "existing"
    output.mkdir(mode=0o700)
    (output / "occupied").write_text("x", encoding="ascii")

    with pytest.raises(OutputAlreadyExistsError, match="not empty"):
        create_output_directory(output, (root,))


def test_final_output_open_failure_removes_just_created_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "run"
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

    monkeypatch.setattr(storage.os, "open", fail_output_open)
    with pytest.raises(OutputPathError, match="opened safely"):
        create_output_directory(output, (root,))

    assert not output.exists()


def test_created_output_replacement_is_rejected_and_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "run"
    displaced = root / "displaced-run"
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

    monkeypatch.setattr(storage.os, "open", replace_before_output_open)
    with pytest.raises(OutputPathError, match="replaced"):
        create_output_directory(output, (root,))

    assert (output / "caller-owned.txt").read_text(encoding="utf-8") == "keep"
    assert displaced.is_dir()
    assert tuple(displaced.iterdir()) == ()


def test_cleanup_preserves_caller_file_in_an_adopted_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "existing"
    output.mkdir(mode=0o700)
    controlled = create_output_directory(output, (root,))
    injected = output / ANNOTATIONS_FILENAME
    injected.write_text("caller-owned\n", encoding="ascii")

    try:
        with pytest.raises(ValueError, match="changed before publication"):
            cleanup_output_directory(controlled)
        assert injected.read_text(encoding="ascii") == "caller-owned\n"
    finally:
        close_output_directory(controlled)


def test_cleanup_preserves_replaced_delivered_file_in_adopted_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    output = root / "existing"
    output.mkdir(mode=0o700)
    controlled = create_output_directory(output, (root,))
    publish_artifacts(
        raw_output=_raw_output(tmp_path),
        output_directory=controlled,
        report="report\n",
        max_output_bytes=5_000_000,
    )
    replaced = output / ANNOTATIONS_FILENAME
    replaced.unlink()
    replaced.write_text("caller replacement\n", encoding="ascii")

    try:
        with pytest.raises(OutputValidationError, match="replaced or changed"):
            cleanup_output_directory(controlled)
        assert replaced.read_text(encoding="ascii") == "caller replacement\n"
        assert (output / RUN_REPORT_FILENAME).is_file()
    finally:
        close_output_directory(controlled)


def _hold_runner_lock_in_spawned_process(state_root: str, control: Connection) -> None:
    state: StateSession | None = None
    runner_lock: int | None = None
    try:
        state = open_state_session(Path(state_root))
        runner_lock = try_acquire_runner_lock(state)
        if runner_lock is None:
            raise RuntimeError("spawned process could not acquire the runner lock")
        control.send(("ready", state.session.name))
        if not control.poll(10.0) or control.recv() != "release":
            raise TimeoutError("spawned process did not receive the release command")
        acquired_lock = runner_lock
        runner_lock = None
        release_runner_lock(acquired_lock)
        acquired_state = state
        state = None
        close_state_session(acquired_state)
        control.send(("closed", ""))
    except BaseException as error:
        with contextlib.suppress(BrokenPipeError, EOFError, OSError):
            control.send(("error", f"{type(error).__name__}: {error}"))
        raise
    finally:
        if runner_lock is not None:
            release_runner_lock(runner_lock)
        if state is not None:
            close_state_session(state)
        control.close()


def _receive_spawned_message(control: Connection, expected: str) -> str:
    if not control.poll(10.0):
        pytest.fail(f"spawned process did not report {expected!r} within the timeout")
    message = cast(tuple[str, str], control.recv())
    if len(message) != 2 or message[0] != expected:
        pytest.fail(f"spawned process reported {message!r}, expected {expected!r}")
    return message[1]


def test_shared_state_root_preserves_live_sessions_and_reaps_only_orphans(
    tmp_path: Path,
) -> None:
    state_root = (tmp_path / "state").resolve()
    first = open_state_session(state_root)
    orphan = state_root / f"session_{'a' * 32}"
    orphan.mkdir(mode=0o700)
    orphan.chmod(0o700)
    orphan_lock = orphan / ".session.lock"
    orphan_lock.touch(mode=0o600)
    orphan_lock.chmod(0o600)
    orphan_job = orphan / f"job_{'b' * 32}"
    orphan_job.mkdir(mode=0o700)
    orphan_job.chmod(0o700)
    (orphan_job / "output.csv").write_bytes(DETAILED_CSV)
    second = open_state_session(state_root)
    first_closed = False
    try:
        assert first.session != second.session
        assert first.session.is_dir()
        assert second.session.is_dir()
        assert not orphan.exists()

        runner_lock = try_acquire_runner_lock(first)
        assert runner_lock is not None
        assert fcntl.fcntl(runner_lock, fcntl.F_GETFD) & fcntl.FD_CLOEXEC
        assert try_acquire_runner_lock(second) is None
        release_runner_lock(runner_lock)

        close_state_session(first)
        first_closed = True
        assert second.session.is_dir()
    finally:
        if not first_closed:
            close_state_session(first)
        close_state_session(second)


def test_shared_state_root_coordinates_runner_lock_across_spawned_processes(
    tmp_path: Path,
) -> None:
    state_root = (tmp_path / "state").resolve()
    context = multiprocessing.get_context("spawn")
    parent_control, child_control = context.Pipe(duplex=True)
    process = context.Process(
        target=_hold_runner_lock_in_spawned_process,
        args=(str(state_root), child_control),
    )
    parent_state: StateSession | None = None
    parent_runner_lock: int | None = None
    release_sent = False
    try:
        process.start()
        child_control.close()
        child_session_name = _receive_spawned_message(parent_control, "ready")
        child_session = state_root / child_session_name
        assert child_session.is_dir()

        parent_state = open_state_session(state_root)
        assert parent_state.session != child_session
        assert try_acquire_runner_lock(parent_state) is None

        closing_state = parent_state
        parent_state = None
        close_state_session(closing_state)
        assert child_session.is_dir()

        parent_state = open_state_session(state_root)
        parent_control.send("release")
        release_sent = True
        _receive_spawned_message(parent_control, "closed")
        process.join(timeout=10.0)
        assert process.exitcode == 0
        assert not child_session.exists()
        assert parent_state.session.is_dir()

        parent_runner_lock = try_acquire_runner_lock(parent_state)
        assert parent_runner_lock is not None
    finally:
        if parent_runner_lock is not None:
            release_runner_lock(parent_runner_lock)
        if process.is_alive():
            if not release_sent:
                with contextlib.suppress(BrokenPipeError, EOFError, OSError):
                    parent_control.send("release")
            process.join(timeout=5.0)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5.0)
        if parent_state is not None:
            close_state_session(parent_state)
        parent_control.close()
        child_control.close()


def test_session_without_owner_lease_fails_closed(tmp_path: Path) -> None:
    state_root = (tmp_path / "state").resolve()
    state_root.mkdir(mode=0o700)
    unsafe_session = state_root / f"session_{'a' * 32}"
    unsafe_session.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="could not be opened safely"):
        open_state_session(state_root)

    assert unsafe_session.is_dir()
    assert os.listdir(unsafe_session) == []


def test_close_state_session_rejects_named_directory_replacement(tmp_path: Path) -> None:
    state = open_state_session((tmp_path / "state").resolve())
    moved = state.root / "moved-session"
    state.session.rename(moved)
    state.session.mkdir(mode=0o700)
    state.session.chmod(0o700)

    with pytest.raises(ValueError, match="cleanup failed"):
        close_state_session(state)

    assert state.session.is_dir()
    assert moved.is_dir()


def test_private_job_cleanup_accepts_regular_files_created_under_public_umask(
    tmp_path: Path,
) -> None:
    state = open_state_session((tmp_path / "state").resolve())
    job = state.session / f"job_{'b' * 32}"
    job.mkdir(mode=0o700)
    job.chmod(0o700)
    output = job / "output.csv"
    output.write_bytes(DETAILED_CSV)
    output.chmod(0o644)

    try:
        remove_job_directory(job, state)
        assert not job.exists()
    finally:
        close_state_session(state)


def test_private_job_cleanup_rejects_named_directory_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = open_state_session((tmp_path / "state").resolve())
    job = state.session / f"job_{'b' * 32}"
    moved = state.session / "moved-job"
    job.mkdir(mode=0o700)
    output = job / "output.csv"
    output.write_bytes(DETAILED_CSV)
    replacement_marker = job / "replacement"
    real_bounded_names = storage._bounded_names  # pyright: ignore[reportPrivateUsage]
    replaced = False

    def replace_after_scan(
        descriptor: int,
        *,
        maximum: int,
        message: str,
    ) -> tuple[str, ...]:
        nonlocal replaced
        names = real_bounded_names(descriptor, maximum=maximum, message=message)
        if message == "controlled job directory exceeds the file bound" and not replaced:
            replaced = True
            job.rename(moved)
            job.mkdir(mode=0o700)
            replacement_marker.touch(mode=0o600)
            os.chmod(replacement_marker, 0o600)
        return names

    monkeypatch.setattr(storage, "_bounded_names", replace_after_scan)
    try:
        with pytest.raises(ValueError, match="job directory was replaced"):
            remove_job_directory(job, state)
        assert replacement_marker.is_file()
        assert moved.is_dir()
    finally:
        monkeypatch.setattr(storage, "_bounded_names", real_bounded_names)
        replacement_marker.unlink(missing_ok=True)
        job.rmdir()
        moved.rename(job)
        close_state_session(state)


def test_publish_accepts_fully_empty_multi_domain_unclassified_row(tmp_path: Path) -> None:
    _, _, _, controlled = _controlled_output(tmp_path)
    raw = tmp_path / "multi.csv"
    raw.write_bytes(b"name,predict_label,probability,threshold,start,end,annotate\nshort,,,,,,\n")
    try:
        annotations, _, _ = publish_artifacts(
            raw_output=raw,
            output_directory=controlled,
            report="report\n",
            max_output_bytes=5_000_000,
        )
        assert annotations.read_bytes() == raw.read_bytes()
    finally:
        close_output_directory(controlled)


@pytest.mark.parametrize(
    "row",
    [
        b"short,,0.5,,,,\n",
        b"protein,K00001,0.9,0.5,1,,*\n",
        b"protein,K00001,0.9,0.5,start,10,*\n",
    ],
)
def test_publish_rejects_partial_empty_or_malformed_multi_domain_rows(
    tmp_path: Path,
    row: bytes,
) -> None:
    _, _, _, controlled = _controlled_output(tmp_path)
    raw = tmp_path / "multi.csv"
    raw.write_bytes(b"name,predict_label,probability,threshold,start,end,annotate\n" + row)
    try:
        with pytest.raises(OutputValidationError):
            publish_artifacts(
                raw_output=raw,
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
    finally:
        close_output_directory(controlled)


def test_publish_rejects_ancestor_symlink_substitution(tmp_path: Path) -> None:
    root, parent, _, controlled = _controlled_output(tmp_path)
    original_parent = root / "project-original"
    escape = tmp_path / "escape"
    escape.mkdir(mode=0o700)
    (escape / "run").mkdir(mode=0o700)
    parent.rename(original_parent)
    parent.symlink_to(escape, target_is_directory=True)
    try:
        with pytest.raises(OutputValidationError, match="changed or became unsafe"):
            publish_artifacts(
                raw_output=_raw_output(tmp_path),
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
        assert not any((escape / "run").iterdir())
        assert not any((original_parent / "run").iterdir())
        with pytest.raises(OutputValidationError):
            cleanup_output_directory(controlled)
        assert not any((escape / "run").iterdir())
    finally:
        close_output_directory(controlled)


def test_publish_rejects_same_name_directory_replacement(tmp_path: Path) -> None:
    root, _, output, controlled = _controlled_output(tmp_path)
    original = root / "original-run"
    output.rename(original)
    output.mkdir(mode=0o700)
    try:
        with pytest.raises(OutputValidationError, match="changed or became unsafe"):
            publish_artifacts(
                raw_output=_raw_output(tmp_path),
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
        assert not any(output.iterdir())
        assert not any(original.iterdir())
        with pytest.raises(OutputValidationError):
            cleanup_output_directory(controlled)
        assert output.is_dir()
    finally:
        close_output_directory(controlled)


def test_publish_rejects_configured_root_replacement(tmp_path: Path) -> None:
    root, _, _, controlled = _controlled_output(tmp_path)
    original_root = tmp_path / "allowed-original"
    root.rename(original_root)
    replacement = root / "project" / "run"
    replacement.mkdir(parents=True, mode=0o700)
    try:
        with pytest.raises(OutputValidationError, match="changed or became unsafe"):
            publish_artifacts(
                raw_output=_raw_output(tmp_path),
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
        assert not any(replacement.iterdir())
        assert not any((original_root / "project" / "run").iterdir())
        with pytest.raises(OutputValidationError):
            cleanup_output_directory(controlled)
        assert replacement.is_dir()
    finally:
        close_output_directory(controlled)


def test_resources_and_cleanup_reject_post_publish_ancestor_substitution(
    tmp_path: Path,
) -> None:
    root, parent, _, controlled = _controlled_output(tmp_path)
    publish_artifacts(
        raw_output=_raw_output(tmp_path),
        output_directory=controlled,
        report="report\n",
        max_output_bytes=5_000_000,
    )
    original_parent = root / "project-original"
    escape = tmp_path / "escape"
    malicious = escape / "run"
    malicious.mkdir(parents=True, mode=0o700)
    (malicious / ANNOTATIONS_FILENAME).write_text("not the delivered CSV\n", encoding="utf-8")
    (malicious / RUN_REPORT_FILENAME).write_text("not the delivered report\n", encoding="utf-8")
    parent.rename(original_parent)
    parent.symlink_to(escape, target_is_directory=True)
    try:
        with pytest.raises(OutputValidationError):
            validate_delivered_artifacts(controlled, max_output_bytes=5_000_000)
        with pytest.raises(OutputValidationError):
            artifact_size(controlled, ANNOTATIONS_FILENAME, max_bytes=5_000_000)
        with pytest.raises(OutputValidationError):
            read_artifact_slice(
                controlled,
                ANNOTATIONS_FILENAME,
                max_bytes=5_000_000,
                offset=0,
                limit=64,
            )
        with pytest.raises(OutputValidationError):
            cleanup_output_directory(controlled)
        assert (malicious / ANNOTATIONS_FILENAME).read_text(encoding="utf-8") == (
            "not the delivered CSV\n"
        )
        assert (malicious / RUN_REPORT_FILENAME).is_file()
    finally:
        close_output_directory(controlled)


def test_publish_checks_only_the_first_unexpected_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, _, controlled = _controlled_output(tmp_path)

    class _OneThenFail:
        calls = 0

        def __enter__(self):  # pyright: ignore[reportUnknownParameterType,reportMissingParameterType]
            return self

        def __exit__(self, *args: object) -> None:
            del args

        def __iter__(self):  # pyright: ignore[reportUnknownParameterType,reportMissingParameterType]
            return self

        def __next__(self) -> object:
            self.calls += 1
            if self.calls == 1:
                return object()
            raise AssertionError("publish scanned beyond the first entry")

    entries = _OneThenFail()

    def _scandir(_descriptor: int) -> _OneThenFail:
        return entries

    monkeypatch.setattr(storage.os, "scandir", _scandir)
    try:
        with pytest.raises(OutputValidationError, match="no longer empty"):
            publish_artifacts(
                raw_output=_raw_output(tmp_path),
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
        assert entries.calls == 1
    finally:
        close_output_directory(controlled)


@pytest.mark.parametrize("changed_component", ["root", "ancestor", "output"])
def test_publish_rolls_back_if_the_stable_path_changes_during_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_component: str,
) -> None:
    root, parent, output, controlled = _controlled_output(tmp_path)
    original_write = storage._write_noreplace  # pyright: ignore[reportPrivateUsage]
    changed = False
    moved_output: Path | None = None

    def _write(
        directory_fd: int,
        name: str,
        content: bytes,
    ) -> tuple[int, int, int, int, int]:
        nonlocal changed, moved_output
        if not changed:
            changed = True
            if changed_component == "root":
                moved_root = tmp_path / "allowed-moved"
                root.rename(moved_root)
                moved_output = moved_root / "project" / "run"
                output.mkdir(parents=True, mode=0o700)
            elif changed_component == "ancestor":
                moved_parent = root / "project-moved"
                parent.rename(moved_parent)
                moved_output = moved_parent / "run"
                output.mkdir(parents=True, mode=0o700)
            else:
                moved_output = root / "project" / "run-moved"
                output.rename(moved_output)
                output.mkdir(mode=0o700)
        return original_write(directory_fd, name, content)

    monkeypatch.setattr(storage, "_write_noreplace", _write)
    try:
        with pytest.raises(OutputValidationError, match="changed or became unsafe"):
            publish_artifacts(
                raw_output=_raw_output(tmp_path),
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
        assert moved_output is not None
        assert not any(moved_output.iterdir())
        assert not any(output.iterdir())
    finally:
        close_output_directory(controlled)


def test_publish_rejects_and_rolls_back_an_entry_added_during_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, output, controlled = _controlled_output(tmp_path)
    original_write = storage._write_noreplace  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def _write(
        directory_fd: int,
        name: str,
        content: bytes,
    ) -> tuple[int, int, int, int, int]:
        nonlocal calls
        identity = original_write(directory_fd, name, content)
        calls += 1
        if calls == 1:
            (output / "unexpected").write_text("do not remove\n", encoding="ascii")
        return identity

    monkeypatch.setattr(storage, "_write_noreplace", _write)
    try:
        with pytest.raises(OutputValidationError, match="exactly two artifacts"):
            publish_artifacts(
                raw_output=_raw_output(tmp_path),
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
        assert tuple(path.name for path in output.iterdir()) == ("unexpected",)
    finally:
        close_output_directory(controlled)


def test_publish_rechecks_the_named_path_after_artifact_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, parent, output, controlled = _controlled_output(tmp_path)
    moved_parent = root / "project-moved"
    original_capture = storage._capture_delivered_identities  # pyright: ignore[reportPrivateUsage]
    calls = 0

    def _capture(
        directory_fd: int,
        max_output_bytes: int,
    ) -> tuple[tuple[str, tuple[int, int, int, int, int]], ...]:
        nonlocal calls
        identities = original_capture(directory_fd, max_output_bytes)
        calls += 1
        if calls == 2:
            parent.rename(moved_parent)
            output.mkdir(parents=True, mode=0o700)
        return identities

    monkeypatch.setattr(storage, "_capture_delivered_identities", _capture)
    try:
        with pytest.raises(OutputValidationError, match="changed or became unsafe"):
            publish_artifacts(
                raw_output=_raw_output(tmp_path),
                output_directory=controlled,
                report="report\n",
                max_output_bytes=5_000_000,
            )
        assert not any((moved_parent / "run").iterdir())
        assert not any(output.iterdir())
    finally:
        close_output_directory(controlled)


@pytest.mark.parametrize("artifact_name", [ANNOTATIONS_FILENAME, RUN_REPORT_FILENAME])
def test_delivered_artifact_replacement_fails_closed(
    tmp_path: Path,
    artifact_name: str,
) -> None:
    _, _, output, controlled = _controlled_output(tmp_path)
    publish_artifacts(
        raw_output=_raw_output(tmp_path),
        output_directory=controlled,
        report="report\n",
        max_output_bytes=5_000_000,
    )
    artifact = output / artifact_name
    artifact.unlink()
    artifact.write_bytes(b"replacement\n")
    try:
        with pytest.raises(OutputValidationError):
            validate_delivered_artifacts(controlled, max_output_bytes=5_000_000)
        with pytest.raises(OutputValidationError):
            artifact_size(controlled, artifact_name, max_bytes=5_000_000)
        with pytest.raises(OutputValidationError):
            read_artifact_slice(
                controlled,
                artifact_name,
                max_bytes=5_000_000,
                offset=0,
                limit=64,
            )
    finally:
        close_output_directory(controlled)


def test_delivered_artifact_in_place_mutation_fails_closed(tmp_path: Path) -> None:
    _, _, output, controlled = _controlled_output(tmp_path)
    publish_artifacts(
        raw_output=_raw_output(tmp_path),
        output_directory=controlled,
        report="report\n",
        max_output_bytes=5_000_000,
    )
    annotations = output / ANNOTATIONS_FILENAME
    original = annotations.read_bytes()
    annotations.write_bytes(b"X" + original[1:])
    try:
        with pytest.raises(OutputValidationError, match="replaced or changed"):
            validate_delivered_artifacts(controlled, max_output_bytes=5_000_000)
    finally:
        close_output_directory(controlled)


def test_cleanup_rejects_more_than_the_fixed_entry_bound(tmp_path: Path) -> None:
    _, _, output, controlled = _controlled_output(tmp_path)
    for index in range(10):
        (output / f"unexpected-{index}").write_text("x", encoding="ascii")
    try:
        with pytest.raises(ValueError, match="exceeds the entry bound"):
            cleanup_output_directory(controlled)
        assert len(tuple(output.iterdir())) == 10
    finally:
        close_output_directory(controlled)
