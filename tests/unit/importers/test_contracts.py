"""Tests for public importer configuration contracts."""

import pytest
from pydantic import ValidationError

from kegg_mcp.importers import GenericColumnMapping, ImportLimits, SourceProvenanceInput
from kegg_mcp.importers.contracts import MAX_IMPORT_FIELD_LENGTH


@pytest.mark.parametrize(
    "input_uri",
    [
        "/home/alice/result.csv",
        "C:\\Users\\alice\\result.csv",
        "~/result.csv",
        "results/../secret.csv",
        "results/result.csv",
        "file:///home/alice/result.csv",
        "inline://user:secret@example/result",
        "inline://result?token=secret",
        "inline://result#fragment",
        "",
        "result\nname.csv",
        "inline://host/%2e%2e/secret",
        "inline:///home/alice/result.csv",
        "inline://C:\\Users\\alice\\result.csv",
        "inline://C:/Users/alice/result.csv",
        "inline://\ud800",
    ],
)
def test_source_provenance_rejects_paths_and_sensitive_uri_parts(input_uri: str) -> None:
    with pytest.raises(ValidationError):
        SourceProvenanceInput(source_name="manual", input_uri=input_uri)


@pytest.mark.parametrize(
    "input_uri",
    ["result.csv", "inline://annotation-result", "mcp://client/resource", "urn:example:result"],
)
def test_source_provenance_accepts_sanitized_logical_sources(input_uri: str) -> None:
    source = SourceProvenanceInput(source_name="manual", input_uri=input_uri)

    assert source.input_uri == input_uri


def test_import_limits_reject_values_above_portable_parser_range() -> None:
    with pytest.raises(ValidationError):
        ImportLimits(
            max_bytes=10**100,
            max_rows=1,
            max_columns=1,
            max_field_length=1,
        )


def test_import_field_length_has_an_independent_retained_evidence_hard_bound() -> None:
    limits = ImportLimits(
        max_bytes=MAX_IMPORT_FIELD_LENGTH + 1,
        max_rows=1,
        max_columns=1,
        max_field_length=MAX_IMPORT_FIELD_LENGTH,
    )

    assert limits.max_bytes == MAX_IMPORT_FIELD_LENGTH + 1
    assert limits.max_field_length == MAX_IMPORT_FIELD_LENGTH
    with pytest.raises(ValidationError):
        ImportLimits(
            max_bytes=1,
            max_rows=1,
            max_columns=1,
            max_field_length=MAX_IMPORT_FIELD_LENGTH + 1,
        )


@pytest.mark.parametrize("column_name", ["", "x" * 257])
def test_generic_mapping_bounds_optional_column_names(column_name: str) -> None:
    with pytest.raises(ValidationError):
        GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            sample_id=column_name,
        )


def test_source_and_mapping_configuration_reject_non_utf8_strings() -> None:
    with pytest.raises(ValidationError):
        SourceProvenanceInput(source_name="manual", source_version="\ud800")
    with pytest.raises(ValidationError):
        GenericColumnMapping(sequence_id="sequence", ko_id="\ud800")
