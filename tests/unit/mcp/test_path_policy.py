"""Race-resistant direct local path policy tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import ImportLimits, SourceProvenanceInput
from kegg_mcp.mcp import path_policy
from kegg_mcp.services.models import NormalizeAnnotationsRequest


def _request(path: Path, *, max_bytes: int) -> NormalizeAnnotationsRequest:
    return NormalizeAnnotationsRequest(
        file_path=str(path),
        import_limits=ImportLimits(
            max_bytes=max_bytes,
            max_rows=100,
            max_columns=10,
            max_field_length=100,
        ),
    )


def test_explicit_download_file_at_exact_limit_is_materialized(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    source = downloads / "annotations.txt"
    source.write_bytes(b"K00001\n")

    materialized = path_policy.materialize_annotation_file(_request(source, max_bytes=7))

    assert materialized.text == "K00001\n"
    assert materialized.file_path is None
    assert materialized.source is not None
    assert materialized.source.input_path == str(source)


def test_distinct_source_provenance_path_is_not_reopened_as_annotation_input(
    tmp_path: Path,
) -> None:
    annotation_root = tmp_path / "annotations"
    annotation_root.mkdir()
    annotations = annotation_root / "deepkoala.csv"
    annotations.write_bytes(b"K00001\n")
    original_fasta = tmp_path / "deepkoala-inputs" / "proteins.faa"
    request = _request(annotations, max_bytes=7).model_copy(
        update={
            "source": SourceProvenanceInput(
                source_name="deepkoala",
                input_path=str(original_fasta),
            )
        }
    )

    materialized = path_policy.materialize_annotation_file(request)

    assert materialized.text == "K00001\n"
    assert materialized.source is not None
    assert materialized.source.input_path == str(original_fasta)
    assert not original_fasta.exists()


def test_file_one_byte_over_limit_is_rejected_without_path_disclosure(tmp_path: Path) -> None:
    private_component = "private-annotation-marker"
    source = tmp_path / private_component
    source.write_bytes(b"K00001\n")

    with pytest.raises(KeggMcpError) as caught:
        path_policy.materialize_annotation_file(
            _request(source, max_bytes=6),
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert private_component not in caught.value.detail.model_dump_json()


def test_sparse_oversized_file_is_rejected_before_read(tmp_path: Path) -> None:
    source = tmp_path / "sparse.txt"
    with source.open("wb") as handle:
        handle.truncate(1_000_000)

    with pytest.raises(KeggMcpError) as caught:
        path_policy.materialize_annotation_file(
            _request(source, max_bytes=16),
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


@pytest.mark.parametrize("link_final", [False, True])
def test_symlinked_input_is_resolved_to_canonical_target(
    tmp_path: Path,
    *,
    link_final: bool,
) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    source = real_directory / "annotations.txt"
    source.write_text("K00001\n", encoding="utf-8")
    if link_final:
        supplied = tmp_path / "annotations-link.txt"
        supplied.symlink_to(source)
    else:
        alias = tmp_path / "directory-link"
        alias.symlink_to(real_directory, target_is_directory=True)
        supplied = alias / source.name

    materialized = path_policy.materialize_annotation_file(_request(supplied, max_bytes=100))

    assert materialized.text == "K00001\n"
    assert materialized.source is not None
    assert materialized.source.input_path == str(source.resolve())


def test_relative_input_path_is_rejected_without_root_configuration_text() -> None:
    request = _request(Path("annotations.txt"), max_bytes=100)

    with pytest.raises(KeggMcpError) as caught:
        path_policy.materialize_annotation_file(request)

    assert caught.value.detail.code is ErrorCode.INVALID_ANNOTATION_TABLE
    assert "KEGG_MCP_ALLOWED_ROOTS" not in caught.value.detail.model_dump_json()


def test_in_place_mutation_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "annotations.txt"
    source.write_text("K00001\n", encoding="utf-8")
    real_read = os.read
    mutated = False

    def mutate_after_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        content = real_read(descriptor, size)
        if not mutated:
            mutated = True
            with source.open("ab") as handle:
                handle.write(b"K00002\n")
        return content

    monkeypatch.setattr(path_policy.os, "read", mutate_after_read)

    with pytest.raises(KeggMcpError) as caught:
        path_policy.materialize_annotation_file(
            _request(source, max_bytes=100),
        )

    assert caught.value.detail.code is ErrorCode.INVALID_ANNOTATION_TABLE


def test_named_file_replacement_during_read_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "annotations.txt"
    displaced = tmp_path / "displaced.txt"
    source.write_text("K00001\n", encoding="utf-8")
    real_read = os.read
    replaced = False

    def replace_after_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        content = real_read(descriptor, size)
        if not replaced:
            replaced = True
            source.rename(displaced)
            source.write_text("K00002\n", encoding="utf-8")
        return content

    monkeypatch.setattr(path_policy.os, "read", replace_after_read)

    with pytest.raises(KeggMcpError) as caught:
        path_policy.materialize_annotation_file(
            _request(source, max_bytes=100),
        )

    assert caught.value.detail.code is ErrorCode.INVALID_ANNOTATION_TABLE


def test_explicit_output_may_be_outside_configured_default_root(tmp_path: Path) -> None:
    default_root = tmp_path / "configured-output"
    default_root.mkdir(mode=0o700)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    explicit = desktop / "analysis"

    resolved = path_policy.resolve_output_directory(
        str(explicit),
        (str(default_root.resolve()),),
        default_prefix="kegg-analysis",
    )

    assert resolved == explicit


def test_explicit_output_resolves_a_symlinked_parent(tmp_path: Path) -> None:
    actual_parent = tmp_path / "Desktop"
    actual_parent.mkdir()
    selected_parent = tmp_path / "selected-output"
    selected_parent.symlink_to(actual_parent, target_is_directory=True)

    resolved = path_policy.resolve_output_directory(
        str(selected_parent / "analysis"),
        (),
        default_prefix="kegg-analysis",
    )

    assert resolved == actual_parent / "analysis"


def test_configured_root_is_used_only_for_default_output_allocation(tmp_path: Path) -> None:
    default_root = tmp_path / "configured-output"
    default_root.mkdir(mode=0o700)

    resolved = path_policy.resolve_output_directory(
        None,
        (str(default_root.resolve()),),
        default_prefix="kegg-analysis",
    )

    assert resolved is not None
    assert resolved.parent == default_root
    assert resolved.name.startswith("kegg-analysis-")


def test_relative_output_path_is_rejected_without_root_configuration_text() -> None:
    with pytest.raises(KeggMcpError) as caught:
        path_policy.resolve_output_directory(
            "analysis",
            (),
            default_prefix="kegg-analysis",
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert "KEGG_MCP_ALLOWED_ROOTS" not in caught.value.detail.model_dump_json()
