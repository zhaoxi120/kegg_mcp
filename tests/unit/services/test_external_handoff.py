"""Focused contracts and serialization tests for KEGG web-tool handoffs."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.services import external_handoff
from kegg_mcp.services.external_handoff import (
    prepare_external_handoff,
    serialize_external_handoff,
)
from kegg_mcp.services.external_handoff_models import (
    ExternalHandoffBundle,
    ExternalHandoffRequest,
    ExternalHandoffTarget,
    MapperColorRequest,
    MapperColorRow,
    MapperJoinMode,
    MapperJoinRequest,
    MapperJoinRow,
    MapperMwsearchMode,
    MapperMwsearchRequest,
    MapperReconstructRequest,
    MapperReconstructRow,
    MapperSearchRequest,
    MapperSearchScope,
    SyntaxKoCompositionRequest,
    SyntaxKoSequenceRequest,
    SyntaxKoSequenceRow,
)

_FIXTURE_ROOT = Path(__file__).parents[2] / "fixtures" / "external_handoff"


def test_discriminated_request_contract_selects_one_target() -> None:
    adapter: TypeAdapter[ExternalHandoffRequest] = TypeAdapter(ExternalHandoffRequest)

    request = adapter.validate_json(
        '{"target":"mapper_search","scope":"reference","identifiers":["K00001"]}'
    )

    assert isinstance(request, MapperSearchRequest)
    assert request.target is ExternalHandoffTarget.MAPPER_SEARCH


def test_mapper_reconstruct_preserves_rows_and_caller_labels_verbatim() -> None:
    request = MapperReconstructRequest(
        target=ExternalHandoffTarget.MAPPER_RECONSTRUCT,
        rows=(
            MapperReconstructRow(user_id="@gene-1", ko_id="K00001"),
            MapperReconstructRow(ko_id="K00002"),
        ),
    )

    content = serialize_external_handoff(request)

    assert content == "@gene-1\tK00001\nK00002\n"


def test_mapper_reconstruct_rejects_exact_duplicate_rows() -> None:
    row = MapperReconstructRow(user_id="gene-1", ko_id="K00001")

    with pytest.raises(ValidationError, match="exact duplicates"):
        MapperReconstructRequest(
            target=ExternalHandoffTarget.MAPPER_RECONSTRUCT,
            rows=(row, row),
        )

    with pytest.raises(ValidationError, match="reserved comment prefix"):
        MapperReconstructRow(user_id="#sample", ko_id="K00001")


@pytest.mark.parametrize("unsupported_field", ["blocks", "annotations"])
def test_mapper_reconstruct_accepts_only_one_unannotated_data_block(
    unsupported_field: str,
) -> None:
    payload = {
        "target": ExternalHandoffTarget.MAPPER_RECONSTRUCT,
        "rows": (MapperReconstructRow(user_id="gene-1", ko_id="K00001"),),
        unsupported_field: (),
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MapperReconstructRequest.model_validate(payload)


@pytest.mark.parametrize(
    ("scope", "organism", "identifiers"),
    [
        (MapperSearchScope.REFERENCE, None, ("K00001", "R00010", "1.1.1.1", "eco")),
        (MapperSearchScope.HSA, None, ("hsa:7157", "K00001", "C00031")),
        (MapperSearchScope.ORGANISM, "eco", ("eco:b0002", "K00844", "C00031")),
    ],
)
def test_mapper_search_accepts_only_scope_compatible_identifiers(
    scope: MapperSearchScope,
    organism: str | None,
    identifiers: tuple[str, ...],
) -> None:
    request = MapperSearchRequest(
        target=ExternalHandoffTarget.MAPPER_SEARCH,
        scope=scope,
        organism=organism,
        identifiers=identifiers,
    )

    assert serialize_external_handoff(request) == "".join(
        f"{identifier}\n" for identifier in identifiers
    )


def test_mapper_search_rejects_organism_mismatch_and_duplicates() -> None:
    with pytest.raises(ValidationError, match="incompatible"):
        MapperSearchRequest(
            target=ExternalHandoffTarget.MAPPER_SEARCH,
            scope=MapperSearchScope.ORGANISM,
            organism="eco",
            identifiers=("hsa:7157",),
        )

    with pytest.raises(ValidationError, match="unique"):
        MapperSearchRequest(
            target=ExternalHandoffTarget.MAPPER_SEARCH,
            identifiers=("K00001", "K00001"),
        )


def test_mapper_search_requires_organism_only_for_organism_scope() -> None:
    with pytest.raises(ValidationError, match="requires"):
        MapperSearchRequest(
            target=ExternalHandoffTarget.MAPPER_SEARCH,
            scope=MapperSearchScope.ORGANISM,
            identifiers=("K00001",),
        )

    with pytest.raises(ValidationError, match="valid only"):
        MapperSearchRequest(
            target=ExternalHandoffTarget.MAPPER_SEARCH,
            scope=MapperSearchScope.REFERENCE,
            organism="eco",
            identifiers=("K00001",),
        )


def test_mapper_color_preserves_meaningful_repeated_identifier_colors() -> None:
    request = MapperColorRequest(
        target=ExternalHandoffTarget.MAPPER_COLOR,
        rows=(
            MapperColorRow(identifier="K00001", background_color="RED"),
            MapperColorRow(
                identifier="K00001",
                background_color="#00fF00",
                foreground_color="black",
            ),
            MapperColorRow(identifier="K00002", foreground_color="skyblue"),
        ),
    )

    assert serialize_external_handoff(request) == (
        "K00001\tRED\nK00001\t#00fF00,black\nK00002\t,skyblue\n"
    )


def test_mapper_color_matches_synthetic_official_shape_golden() -> None:
    request = MapperColorRequest(
        target=ExternalHandoffTarget.MAPPER_COLOR,
        rows=(
            MapperColorRow(
                identifier="K00001",
                background_color="skyblue",
                foreground_color="blue",
            ),
            MapperColorRow(identifier="K00002", background_color="#ff0000"),
        ),
    )

    assert serialize_external_handoff(request) == (_FIXTURE_ROOT / "mapper_color.tsv").read_text(
        encoding="utf-8"
    )


def test_mapper_color_rejects_exact_duplicate_rows_invalid_colors_and_scope_mismatch() -> None:
    row = MapperColorRow(identifier="K00001", background_color="red")
    with pytest.raises(ValidationError, match="exact duplicates"):
        MapperColorRequest(
            target=ExternalHandoffTarget.MAPPER_COLOR,
            rows=(row, row),
        )

    with pytest.raises(ValidationError, match="colors must"):
        MapperColorRow(identifier="K00001", background_color="not-a-color")

    with pytest.raises(ValidationError, match="background or foreground"):
        MapperColorRow(identifier="K00001")

    with pytest.raises(ValidationError, match="incompatible"):
        MapperColorRequest(
            target=ExternalHandoffTarget.MAPPER_COLOR,
            scope=MapperSearchScope.ORGANISM,
            organism="eco",
            rows=(MapperColorRow(identifier="hsa:7157", background_color="red"),),
        )


@pytest.mark.parametrize(
    ("mode", "rows", "expected"),
    [
        (
            MapperJoinMode.KO,
            (
                MapperJoinRow(identifier="K00001", attribute="disease association"),
                MapperJoinRow(identifier="K00001", attribute="=formula-like"),
                MapperJoinRow(identifier="K00001", attribute="  caller text  "),
            ),
            "K00001\tdisease association\nK00001\t=formula-like\nK00001\t  caller text  \n",
        ),
        (
            MapperJoinMode.BR,
            (
                MapperJoinRow(identifier="C00031", attribute="compound"),
                MapperJoinRow(identifier="D00001", attribute="drug"),
                MapperJoinRow(identifier="eco", attribute="organism"),
            ),
            "C00031\tcompound\nD00001\tdrug\neco\torganism\n",
        ),
    ],
)
def test_mapper_join_formats_allowed_binary_relations(
    mode: MapperJoinMode,
    rows: tuple[MapperJoinRow, ...],
    expected: str,
) -> None:
    request = MapperJoinRequest(
        target=ExternalHandoffTarget.MAPPER_JOIN,
        mode=mode,
        rows=rows,
    )

    assert serialize_external_handoff(request) == expected


def test_mapper_join_rejects_mode_mismatch_exact_duplicates_and_empty_attributes() -> None:
    with pytest.raises(ValidationError, match="incompatible"):
        MapperJoinRequest(
            target=ExternalHandoffTarget.MAPPER_JOIN,
            mode=MapperJoinMode.KO,
            rows=(MapperJoinRow(identifier="D00001", attribute="drug"),),
        )

    with pytest.raises(ValidationError, match="incompatible"):
        MapperJoinRequest(
            target=ExternalHandoffTarget.MAPPER_JOIN,
            mode=MapperJoinMode.BR,
            rows=(MapperJoinRow(identifier="K00001", attribute="protein"),),
        )

    row = MapperJoinRow(identifier="K00001", attribute="value")
    with pytest.raises(ValidationError, match="exact duplicates"):
        MapperJoinRequest(
            target=ExternalHandoffTarget.MAPPER_JOIN,
            mode=MapperJoinMode.KO,
            rows=(row, row),
        )

    with pytest.raises(ValidationError):
        MapperJoinRow(identifier="K00001", attribute="   ")


@pytest.mark.parametrize("value", ["=caller", "+caller", "-caller", "@caller", "'caller"])
def test_mapper_join_preserves_formula_like_attributes_verbatim(value: str) -> None:
    request = MapperJoinRequest(
        target=ExternalHandoffTarget.MAPPER_JOIN,
        mode=MapperJoinMode.KO,
        rows=(MapperJoinRow(identifier="K00001", attribute=value),),
    )

    assert serialize_external_handoff(request) == f"K00001\t{value}\n"


@pytest.mark.parametrize(
    "value",
    [
        "left\tright",
        "left\nright",
        "left\rright",
        "left\x00right",
        "left\u0085right",
        "left\u2028right",
        "left\u2029right",
    ],
)
def test_mapper_join_rejects_format_breaking_attribute_characters(value: str) -> None:
    with pytest.raises(ValidationError, match="control or line-separator characters"):
        MapperJoinRow(identifier="K00001", attribute=value)


@pytest.mark.parametrize(
    ("mode", "values"),
    [
        (MapperMwsearchMode.FORMULA, ("C6H12O6", "C5H11NO2")),
        (MapperMwsearchMode.EXACT_MASS, ("180.063388", "117.078979")),
        (MapperMwsearchMode.C_NUMBER, ("C00031", "C00148")),
    ],
)
def test_mapper_mwsearch_formats_one_homogeneous_mode(
    mode: MapperMwsearchMode,
    values: tuple[str, ...],
) -> None:
    request = MapperMwsearchRequest(
        target=ExternalHandoffTarget.MAPPER_MWSEARCH,
        mode=mode,
        values=values,
    )

    assert serialize_external_handoff(request) == "".join(f"{value}\n" for value in values)


@pytest.mark.parametrize(
    ("mode", "value"),
    [
        (MapperMwsearchMode.FORMULA, "=C6H12O6"),
        (MapperMwsearchMode.EXACT_MASS, "0"),
        (MapperMwsearchMode.EXACT_MASS, "1e3"),
        (MapperMwsearchMode.C_NUMBER, "D00001"),
    ],
)
def test_mapper_mwsearch_rejects_values_incompatible_with_mode(
    mode: MapperMwsearchMode,
    value: str,
) -> None:
    with pytest.raises(ValidationError, match="incompatible"):
        MapperMwsearchRequest(
            target=ExternalHandoffTarget.MAPPER_MWSEARCH,
            mode=mode,
            values=(value,),
        )


def test_mapper_mwsearch_rejects_duplicates() -> None:
    with pytest.raises(ValidationError, match="unique"):
        MapperMwsearchRequest(
            target=ExternalHandoffTarget.MAPPER_MWSEARCH,
            mode=MapperMwsearchMode.C_NUMBER,
            values=("C00031", "C00031"),
        )


def test_syntax_ko_composition_requires_a_unique_canonical_ko_set() -> None:
    request = SyntaxKoCompositionRequest(
        target=ExternalHandoffTarget.SYNTAX_KO_COMPOSITION,
        ko_ids=("K00002", "K00001"),
    )

    assert serialize_external_handoff(request) == "K00002\nK00001\n"

    with pytest.raises(ValidationError, match="unique"):
        SyntaxKoCompositionRequest(
            target=ExternalHandoffTarget.SYNTAX_KO_COMPOSITION,
            ko_ids=("K00001", "K00001"),
        )

    with pytest.raises(ValidationError):
        SyntaxKoCompositionRequest(
            target=ExternalHandoffTarget.SYNTAX_KO_COMPOSITION,
            ko_ids=("ko:K00001",),
        )


def test_syntax_ko_sequence_requires_explicit_caller_order_and_preserves_it() -> None:
    request = SyntaxKoSequenceRequest(
        target=ExternalHandoffTarget.SYNTAX_KO_SEQUENCE,
        order_semantics="caller_supplied_genomic_order",
        rows=(
            SyntaxKoSequenceRow(gene_id="gene-3", ko_id="K00002"),
            SyntaxKoSequenceRow(gene_id="@gene-1", ko_id="K00001"),
            SyntaxKoSequenceRow(gene_id="gene-2", ko_id="K00002"),
        ),
    )

    assert serialize_external_handoff(request) == (
        "gene-3\tK00002\n@gene-1\tK00001\ngene-2\tK00002\n"
    )


def test_syntax_ko_sequence_matches_synthetic_official_shape_golden() -> None:
    request = SyntaxKoSequenceRequest(
        target=ExternalHandoffTarget.SYNTAX_KO_SEQUENCE,
        order_semantics="caller_supplied_genomic_order",
        rows=(
            SyntaxKoSequenceRow(gene_id="gene-alpha", ko_id="K00001"),
            SyntaxKoSequenceRow(gene_id="@gene-beta", ko_id="K00002"),
        ),
    )

    assert serialize_external_handoff(request) == (
        _FIXTURE_ROOT / "syntax_ko_sequence.tsv"
    ).read_text(encoding="utf-8")


def test_syntax_ko_sequence_rejects_missing_order_confirmation_and_duplicate_genes() -> None:
    with pytest.raises(ValidationError):
        SyntaxKoSequenceRequest.model_validate(
            {
                "rows": ({"gene_id": "gene-1", "ko_id": "K00001"},),
            }
        )

    with pytest.raises(ValidationError, match="gene IDs must be unique"):
        SyntaxKoSequenceRequest(
            target=ExternalHandoffTarget.SYNTAX_KO_SEQUENCE,
            order_semantics="caller_supplied_genomic_order",
            rows=(
                SyntaxKoSequenceRow(gene_id="gene-1", ko_id="K00001"),
                SyntaxKoSequenceRow(gene_id="gene-1", ko_id="K00002"),
            ),
        )


def test_syntax_ko_sequence_does_not_accept_domain_coordinates() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SyntaxKoSequenceRow.model_validate(
            {
                "gene_id": "gene-1",
                "ko_id": "K00001",
                "domain_start": 1,
                "domain_end": 100,
            }
        )


def test_prepare_external_handoff_writes_committed_bundle_and_manifest(tmp_path: Path) -> None:
    request = MapperSearchRequest(
        target=ExternalHandoffTarget.MAPPER_SEARCH,
        scope=MapperSearchScope.ORGANISM,
        organism="eco",
        identifiers=("eco:b0002", "K00844"),
    )
    output = tmp_path / "mapper-bundle"

    result = prepare_external_handoff(request, output_directory=output)

    assert Path(result.data_file).read_text(encoding="utf-8") == "eco:b0002\nK00844\n"
    manifest = json.loads(Path(result.manifest).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "2"
    assert manifest["target"] == "mapper_search"
    assert manifest["parameters"] == {"organism": "eco", "scope": "organism"}
    assert manifest["input"] == {
        "caller_supplied": True,
        "duplicate_semantics": "duplicates_rejected",
        "item_count": 2,
        "order_preserved": True,
    }
    assert manifest["execution_boundary"] == {
        "browser_started": False,
        "external_result_parsed": False,
        "external_tool_executed": False,
        "uploaded": False,
    }
    assert manifest["format"]["official_source"] == "https://www.kegg.jp/kegg/mapper/"
    assert "spreadsheet_formula_cells_escaped" not in manifest["format"]
    assert manifest["files"][0] == {
        "byte_size": len(b"eco:b0002\nK00844\n"),
        "mime_type": "text/plain",
        "name": "mapper_search.txt",
    }
    assert "eco:b0002" not in json.dumps(manifest)
    assert result.item_count == 2
    assert result.data_byte_size == len(b"eco:b0002\nK00844\n")
    assert tuple(artifact.name for artifact in result.artifacts) == (
        "mapper_search.txt",
        "handoff_manifest.json",
    )
    assert stat.S_IMODE(Path(result.data_file).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(result.manifest).stat().st_mode) == 0o600


def test_external_handoff_bundle_requires_current_schema_version(tmp_path: Path) -> None:
    result = prepare_external_handoff(
        MapperSearchRequest(
            target=ExternalHandoffTarget.MAPPER_SEARCH,
            scope=MapperSearchScope.REFERENCE,
            identifiers=("K00001",),
        ),
        output_directory=tmp_path / "current-schema",
    )
    payload = result.model_dump(mode="json")
    del payload["schema_version"]

    with pytest.raises(ValidationError):
        ExternalHandoffBundle.model_validate(payload)


def test_prepare_external_handoff_rejects_nonempty_output_without_modification(
    tmp_path: Path,
) -> None:
    output = tmp_path / "occupied"
    output.mkdir()
    existing = output / "caller.txt"
    existing.write_text("keep", encoding="utf-8")

    with pytest.raises(KeggMcpError) as raised:
        prepare_external_handoff(
            SyntaxKoCompositionRequest(
                target=ExternalHandoffTarget.SYNTAX_KO_COMPOSITION,
                ko_ids=("K00001",),
            ),
            output_directory=output,
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    assert existing.read_text(encoding="utf-8") == "keep"
    assert tuple(output.iterdir()) == (existing,)


def test_prepare_external_handoff_enforces_byte_limit_before_filesystem_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "bounded"
    monkeypatch.setattr(external_handoff, "MAX_EXTERNAL_HANDOFF_DATA_BYTES", 5)

    with pytest.raises(KeggMcpError) as raised:
        prepare_external_handoff(
            MapperSearchRequest(
                target=ExternalHandoffTarget.MAPPER_SEARCH,
                identifiers=("K00001",),
            ),
            output_directory=output,
        )

    assert raised.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert not output.exists()
