"""Detailed CSV structure and importer-handoff validation tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import deepkoala_mcp.output as output_module
from deepkoala_mcp.output import OutputValidationError, validate_detailed_csv

HEADER = "name,predict_label,probability,threshold,annotate"


def _write_output(tmp_path: Path, payload: str) -> Path:
    output = tmp_path / "detailed.csv"
    output.write_text(payload, encoding="utf-8", newline="")
    return output


def test_detailed_csv_supports_quoted_fields_and_empty_annotate(tmp_path: Path) -> None:
    output = _write_output(
        tmp_path,
        (
            f"{HEADER},note\r\n"
            '"seq,1",K00001,0.9,0.5,,"quoted, note"\r\n'
            'seq2,K00002,0.8,0.5,*,"two\nlines"\r\n'
        ),
    )

    summary = validate_detailed_csv(output, max_rows=2)

    assert summary.row_count == 2
    assert summary.column_count == 6


@pytest.mark.parametrize(
    "row",
    [
        "",
        "   ",
        "seq1,K00001,0.9,0.5",
        "seq1,K00001,0.9,0.5,*,unexpected",
    ],
)
def test_detailed_csv_rejects_blank_or_mismatched_rows(
    tmp_path: Path,
    row: str,
) -> None:
    output = _write_output(tmp_path, f"{HEADER}\n{row}\nseq2,K00002,0.8,0.5,*\n")

    with pytest.raises(OutputValidationError) as captured:
        validate_detailed_csv(output, max_rows=3)

    assert captured.value.code == "OUTPUT_INVALID"


@pytest.mark.parametrize(
    "row",
    [
        ",K00001,0.9,0.5,*",
        "   ,K00001,0.9,0.5,*",
        "seq1,,0.9,0.5,*",
        "seq1,   ,0.9,0.5,*",
        "seq1,K00001,,0.5,*",
        "seq1,K00001,   ,0.5,*",
        "seq1,K00001,0.9,,*",
        "seq1,K00001,0.9,   ,*",
    ],
)
def test_detailed_csv_rejects_missing_required_values(
    tmp_path: Path,
    row: str,
) -> None:
    output = _write_output(tmp_path, f"{HEADER}\n{row}\n")

    with pytest.raises(OutputValidationError) as captured:
        validate_detailed_csv(output, max_rows=1)

    assert captured.value.code == "OUTPUT_INVALID"


@pytest.mark.parametrize(
    "header",
    [
        f"{HEADER},",
        f"{HEADER},note,note",
    ],
)
def test_detailed_csv_rejects_empty_or_duplicate_header_fields(
    tmp_path: Path,
    header: str,
) -> None:
    output = _write_output(tmp_path, f"{header}\nseq1,K00001,0.9,0.5,*\n")

    with pytest.raises(OutputValidationError) as captured:
        validate_detailed_csv(output, max_rows=1)

    assert captured.value.code == "OUTPUT_INVALID"


def test_detailed_csv_rejects_malformed_quoted_content(tmp_path: Path) -> None:
    output = _write_output(tmp_path, f'{HEADER}\n"seq1,K00001,0.9,0.5,*\n')

    with pytest.raises(OutputValidationError) as captured:
        validate_detailed_csv(output, max_rows=1)

    assert captured.value.code == "OUTPUT_INVALID"


def test_detailed_csv_detects_ctime_change_when_size_and_mtime_are_restored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _write_output(tmp_path, f"{HEADER}\nseq1,K00001,0.9,0.5,*\n")
    original = output.stat()
    original_read = output_module.os.read
    changed = False

    def racing_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        content = original_read(descriptor, count)
        opened = os.fstat(descriptor)
        if content and opened.st_ino == original.st_ino and not changed:
            changed = True
            output.write_text(
                f"{HEADER}\nseq1,K99999,0.9,0.5,*\n",
                encoding="utf-8",
                newline="",
            )
            os.utime(output, ns=(original.st_atime_ns, original.st_mtime_ns))
        return content

    monkeypatch.setattr(output_module.os, "read", racing_read)

    with pytest.raises(OutputValidationError, match="changed during read"):
        validate_detailed_csv(output, max_rows=1)
