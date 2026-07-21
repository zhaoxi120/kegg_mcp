"""Pinned output-directory publication, resource, and cleanup regression tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import deepkoala_mcp.job_storage as storage
from conftest import DETAILED_CSV
from deepkoala_mcp.contracts import ANNOTATIONS_FILENAME, RUN_REPORT_FILENAME
from deepkoala_mcp.job_storage import (
    ControlledOutputDirectory,
    OutputValidationError,
    artifact_size,
    cleanup_output_directory,
    close_output_directory,
    create_output_directory,
    publish_artifacts,
    read_artifact_slice,
    remove_job_directory,
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


def test_private_job_cleanup_accepts_regular_files_created_under_public_umask(
    tmp_path: Path,
) -> None:
    session = tmp_path / f"session_{'a' * 32}"
    job = session / f"job_{'b' * 32}"
    session.mkdir(mode=0o700)
    job.mkdir(mode=0o700)
    session.chmod(0o700)
    job.chmod(0o700)
    output = job / "output.csv"
    output.write_bytes(DETAILED_CSV)
    output.chmod(0o644)

    remove_job_directory(job, session)

    assert not job.exists()


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

    def _write(directory_fd: int, name: str, content: bytes) -> None:
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
        original_write(directory_fd, name, content)

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

    def _write(directory_fd: int, name: str, content: bytes) -> None:
        nonlocal calls
        original_write(directory_fd, name, content)
        calls += 1
        if calls == 1:
            (output / "unexpected").write_text("do not remove\n", encoding="ascii")

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
