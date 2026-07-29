"""Unit tests for strict KEGG textual response parsers."""

from collections.abc import Callable

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg.contracts import KeggFindDatabase, KeggInfoDatabase
from kegg_mcp.kegg.parsers import (
    parse_brite_htext_response,
    parse_find_response,
    parse_flat_file_response,
    parse_info_response,
    parse_organism_pathway_list_response,
    parse_pair_response,
)


def _assert_parse_error(call: Callable[[], object], *, reason: str) -> None:
    with pytest.raises(KeggMcpError) as error:
        call()

    assert error.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert error.value.detail.recoverable is True
    assert error.value.detail.suggested_action
    details = {detail.name: detail.value for detail in error.value.detail.safe_details}
    assert details["reason"] == reason


def test_info_parser_preserves_lines_and_extracts_global_metadata() -> None:
    body = (
        b"kegg             Kyoto Encyclopedia of Genes and Genomes\n"
        b"kegg             Release 116.0+/07-14, Jul 26\n"
        b"                 pathway          583 entries\n"
        b"\n"
        b"                 module            700 entries\n"
    )

    document = parse_info_response(body, KeggInfoDatabase.KEGG)

    assert document.release == "116.0+/07-14, Jul 26"
    assert document.entry_count is None
    assert document.linked_databases == ()
    assert document.lines == (
        "kegg             Kyoto Encyclopedia of Genes and Genomes",
        "kegg             Release 116.0+/07-14, Jul 26",
        "                 pathway          583 entries",
        "                 module            700 entries",
    )


def test_info_parser_extracts_database_entry_count() -> None:
    body = (
        b"ko               KEGG Orthology Database\r\n"
        b"ko               Release 116.0+/07-14, Jul 26\r\n"
        b"                 24,321 entries\r\n"
    )

    document = parse_info_response(body, KeggInfoDatabase.KO)

    assert document.entry_count == 24_321
    assert document.linked_databases == ()


def test_info_parser_extracts_the_explicit_linked_database_block() -> None:
    body = (
        b"pathway          KEGG Pathway Database\n"
        b"pathway          Release 116.0+/07-14, Jul 26\n"
        b"linked db        module\n"
        b"                 ko\n"
        b"                 genome\n"
        b"                 <org>\n"
        b"                 compound\n"
    )

    document = parse_info_response(body, KeggInfoDatabase.PATHWAY)

    assert document.linked_databases == ("module", "ko", "genome", "<org>", "compound")


def test_info_parser_rejects_empty_response() -> None:
    _assert_parse_error(
        lambda: parse_info_response(b" \n\t", KeggInfoDatabase.KO),
        reason="empty_response",
    )


def test_info_parser_rejects_unexpected_database_header() -> None:
    _assert_parse_error(
        lambda: parse_info_response(
            b"pathway          KEGG Pathway Database\n",
            KeggInfoDatabase.KO,
        ),
        reason="unexpected_database_header",
    )


def test_info_parser_rejects_conflicting_counts() -> None:
    body = b"ko               KEGG Orthology Database\n1 entries\n2 entries\n"

    _assert_parse_error(
        lambda: parse_info_response(body, KeggInfoDatabase.KO),
        reason="conflicting_entry_counts",
    )


def test_info_parser_rejects_invalid_utf8() -> None:
    _assert_parse_error(
        lambda: parse_info_response(b"ko invalid \xff", KeggInfoDatabase.KO),
        reason="invalid_utf8",
    )


def test_organism_pathway_list_parser_preserves_the_bounded_directory() -> None:
    document = parse_organism_pathway_list_response(
        b"path:hsa00010\tGlycolysis / Gluconeogenesis - Homo sapiens (human)\n"
        b"path:hsa04012\tErbB signaling pathway - Homo sapiens (human)\n",
        "hsa",
    )

    assert document.organism == "hsa"
    assert [row.pathway_id for row in document.rows] == ["path:hsa00010", "path:hsa04012"]
    assert document.rows[0].line_number == 1
    assert document.rows[0].name.startswith("Glycolysis")


def test_organism_pathway_list_parser_accepts_an_empty_directory() -> None:
    assert parse_organism_pathway_list_response(b"", "hsa").rows == ()


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"path:mmu00010\tWrong organism\n", "unexpected_identifier"),
        (b"path:hsa00010\tName\textra\n", "expected_two_columns"),
        (b"path:hsa00010\t\n", "invalid_name"),
        (
            b"path:hsa00010\tName\npathway:hsa00010\tDuplicate alias\n",
            "duplicate_identifier",
        ),
    ],
)
def test_organism_pathway_list_parser_rejects_unexpected_rows(
    body: bytes,
    reason: str,
) -> None:
    _assert_parse_error(
        lambda: parse_organism_pathway_list_response(body, "hsa"),
        reason=reason,
    )


def test_find_parser_preserves_order_duplicates_text_and_source_line_numbers() -> None:
    # Two-column FIND output shape verified against the KEGG API manual on 2026-07-30.
    body = (
        "ko:K00844\thexokinase [EC:2.7.1.1]\n"
        "\n"
        "ko:K12407\tglucokinase, β-D-glucose candidate\n"
        "ko:K00844\thexokinase [EC:2.7.1.1]\n"
    ).encode()

    document = parse_find_response(body, KeggFindDatabase.KO)

    assert [row.line_number for row in document.rows] == [1, 3, 4]
    assert [(row.identifier, row.matched_text) for row in document.rows] == [
        ("ko:K00844", "hexokinase [EC:2.7.1.1]"),
        ("ko:K12407", "glucokinase, β-D-glucose candidate"),
        ("ko:K00844", "hexokinase [EC:2.7.1.1]"),
    ]


@pytest.mark.parametrize("body", [b"", b" \n\t\r\n"])
def test_find_parser_accepts_an_empty_success_response(body: bytes) -> None:
    assert parse_find_response(body, KeggFindDatabase.KO).rows == ()


@pytest.mark.parametrize(
    ("database", "identifier"),
    [
        (KeggFindDatabase.KO, "ko:K00001"),
        (KeggFindDatabase.PATHWAY, "path:map00010"),
        (KeggFindDatabase.PATHWAY, "path:hsa00010"),
        (KeggFindDatabase.MODULE, "md:M00001"),
        (KeggFindDatabase.MODULE, "md:hsa_M00001"),
        (KeggFindDatabase.MODULE, "md:T01001_M00001"),
        (KeggFindDatabase.REACTION, "rn:R00001"),
        (KeggFindDatabase.ENZYME, "ec:1.1.1.-"),
        (KeggFindDatabase.COMPOUND, "cpd:C00031"),
        (KeggFindDatabase.GENOME, "gn:T00007"),
        (KeggFindDatabase.ORGANISM, "gn:T00007"),
        (KeggFindDatabase.GENES, "hsa:10458"),
    ],
)
def test_find_parser_accepts_only_identifiers_in_the_expected_database(
    database: KeggFindDatabase,
    identifier: str,
) -> None:
    document = parse_find_response(
        f"{identifier}\tSynthetic match\n".encode(),
        database,
    )

    assert document.rows[0].identifier == identifier


def test_find_parser_requires_gene_results_to_match_the_requested_organism() -> None:
    document = parse_find_response(
        b"hsa:7157\tTP53; tumor protein p53\n",
        KeggFindDatabase.GENES,
        organism="hsa",
    )

    assert document.rows[0].identifier == "hsa:7157"
    _assert_parse_error(
        lambda: parse_find_response(
            b"mmu:22059\tTrp53; transformation related protein 53\n",
            KeggFindDatabase.GENES,
            organism="hsa",
        ),
        reason="unexpected_identifier",
    )


@pytest.mark.parametrize(
    ("database", "identifier"),
    [
        (KeggFindDatabase.KO, "path:map00010"),
        (KeggFindDatabase.PATHWAY, "path:module00010"),
        (KeggFindDatabase.MODULE, "md:abcde_M00001"),
        (KeggFindDatabase.REACTION, "rn:K00001"),
        (KeggFindDatabase.ENZYME, "ec:8.1.1.1"),
        (KeggFindDatabase.COMPOUND, "cpd:D00001"),
        (KeggFindDatabase.GENOME, "gn:K00001"),
        (KeggFindDatabase.ORGANISM, "genome:T0000"),
    ],
)
def test_find_parser_rejects_unexpected_or_malformed_identifiers(
    database: KeggFindDatabase,
    identifier: str,
) -> None:
    _assert_parse_error(
        lambda: parse_find_response(
            f"{identifier}\tSynthetic match\n".encode(),
            database,
        ),
        reason="unexpected_identifier",
    )


@pytest.mark.parametrize(
    "body",
    [
        b"ko:K00001\n",
        b"ko:K00001\tdefinition\textra\n",
    ],
)
def test_find_parser_requires_exactly_two_tab_columns(body: bytes) -> None:
    _assert_parse_error(
        lambda: parse_find_response(body, KeggFindDatabase.KO),
        reason="expected_two_columns",
    )


@pytest.mark.parametrize("matched_text", ["", " ", "a" * 10_001])
def test_find_parser_rejects_empty_or_oversized_match_text(matched_text: str) -> None:
    _assert_parse_error(
        lambda: parse_find_response(
            f"ko:K00001\t{matched_text}\n".encode(),
            KeggFindDatabase.KO,
        ),
        reason="invalid_matched_text",
    )


def test_find_parser_rejects_invalid_utf8() -> None:
    _assert_parse_error(
        lambda: parse_find_response(b"ko:K00001\t\xff", KeggFindDatabase.KO),
        reason="invalid_utf8",
    )


def test_pair_parser_preserves_order_duplicates_and_source_line_numbers() -> None:
    body = b"ko:K00001\tpath:map00010\n\nko:K00002\tpath:map00020\nko:K00001\tpath:map00010\n"

    document = parse_pair_response(body)

    assert [row.line_number for row in document.rows] == [1, 3, 4]
    assert [(row.source_id, row.target_id) for row in document.rows] == [
        ("ko:K00001", "path:map00010"),
        ("ko:K00002", "path:map00020"),
        ("ko:K00001", "path:map00010"),
    ]


@pytest.mark.parametrize("body", [b"", b" \n\t\r\n"])
def test_pair_parser_accepts_empty_success_response(body: bytes) -> None:
    assert parse_pair_response(body).rows == ()


@pytest.mark.parametrize(
    "body",
    [
        b"ko:K00001\n",
        b"ko:K00001\tpath:map00010\textra\n",
    ],
)
def test_pair_parser_requires_exactly_two_tab_columns(body: bytes) -> None:
    _assert_parse_error(
        lambda: parse_pair_response(body),
        reason="expected_two_columns",
    )


@pytest.mark.parametrize(
    "body",
    [
        b"\tpath:map00010\n",
        b"ko:K00001\t\n",
        b"ko:K00001 \tpath:map00010\n",
    ],
)
def test_pair_parser_rejects_invalid_identifiers(body: bytes) -> None:
    _assert_parse_error(lambda: parse_pair_response(body), reason="invalid_identifier")


def test_pair_parser_rejects_invalid_utf8() -> None:
    _assert_parse_error(lambda: parse_pair_response(b"ko:K00001\t\xff"), reason="invalid_utf8")


def test_flat_file_parser_preserves_continuations_repeated_and_unknown_fields() -> None:
    body = (
        b"ENTRY       K00001                      KO\n"
        b"NAME        First name\n"
        b"            continued name\n"
        b"CUSTOM      first custom value\n"
        b"CUSTOM      second custom value\n"
        b"///\n"
        b"ENTRY       C00001                      Compound\n"
        b"NAME        Water\n"
        b"///\n"
    )

    document = parse_flat_file_response(body)

    assert [entry.identifier for entry in document.entries] == ["K00001", "C00001"]
    first = document.entries[0]
    assert first.start_line == 1
    assert first.end_line == 6
    assert [field.name for field in first.fields] == ["ENTRY", "NAME", "CUSTOM", "CUSTOM"]
    assert first.fields[1].value_lines == ("First name", "continued name")
    assert first.fields[1].start_line == 2
    assert first.fields[1].end_line == 3
    assert first.fields[2].value_lines == ("first custom value",)
    assert first.fields[3].value_lines == ("second custom value",)


def test_flat_file_parser_preserves_canonical_nested_field_indentation() -> None:
    body = (
        b"ENTRY       K00001                      KO\n"
        b"REFERENCE   1\n"
        b"  AUTHORS   Kanehisa M.\n"
        b"  TITLE     KEGG database entry\n"
        b"            continued title\n"
        b"  JOURNAL   Nucleic Acids Res.\n"
        b"NETWORK     nt06015  Glucose metabolism\n"
        b"  ELEMENT   K00001\n"
        b"  SEQUENCE  hsa:1\n"
        b"///\n"
    )

    document = parse_flat_file_response(body)
    fields = document.entries[0].fields

    assert [field.name for field in fields] == [
        "ENTRY",
        "REFERENCE",
        "AUTHORS",
        "TITLE",
        "JOURNAL",
        "NETWORK",
        "ELEMENT",
        "SEQUENCE",
    ]
    assert [field.indent_columns for field in fields] == [0, 0, 2, 2, 2, 0, 2, 2]
    assert fields[3].value_lines == ("KEGG database entry", "continued title")
    assert fields[3].start_line == 4
    assert fields[3].end_line == 5


def test_flat_file_parser_extracts_enzyme_identifier_after_entry_ec_marker() -> None:
    document = parse_flat_file_response(
        b"ENTRY       EC 1.1.1.1\nNAME        alcohol dehydrogenase\n///\n"
    )

    assert document.entries[0].identifier == "1.1.1.1"


@pytest.mark.parametrize("body", [b"", b" \n\t\r\n"])
def test_flat_file_parser_accepts_whitespace_only_response(body: bytes) -> None:
    assert parse_flat_file_response(body).entries == ()


def test_flat_file_parser_allows_blank_lines_between_terminated_entries() -> None:
    body = (
        b"ENTRY       K00001                      KO\n"
        b"///\n"
        b"\n"
        b"ENTRY       K00002                      KO\n"
        b"///\n"
    )

    document = parse_flat_file_response(body)

    assert [entry.identifier for entry in document.entries] == ["K00001", "K00002"]
    assert document.entries[1].start_line == 4


def test_flat_file_parser_rejects_unterminated_entry() -> None:
    _assert_parse_error(
        lambda: parse_flat_file_response(b"ENTRY       K00001                      KO\n"),
        reason="unterminated_entry",
    )


def test_flat_file_parser_rejects_continuation_without_field() -> None:
    body = b"            orphan continuation\n///\n"

    _assert_parse_error(
        lambda: parse_flat_file_response(body),
        reason="continuation_without_field",
    )


def test_flat_file_parser_rejects_entry_without_entry_field() -> None:
    body = b"NAME        Missing entry field\n///\n"

    _assert_parse_error(
        lambda: parse_flat_file_response(body),
        reason="missing_entry_field",
    )


def test_flat_file_parser_rejects_duplicate_entry_field() -> None:
    body = b"ENTRY       K00001\nENTRY       K00002\n///\n"

    _assert_parse_error(
        lambda: parse_flat_file_response(body),
        reason="duplicate_entry_field",
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"ENTRY\n///\n", "invalid_field_columns"),
        (b" ENTRY      K00001\n///\n", "invalid_field_indent"),
        (b"   AUTHORS  Invalid indentation\n///\n", "invalid_field_indent"),
        (b"  ENTRY     K00001\n///\n", "invalid_entry_field_indent"),
        (b"///\n", "terminator_without_entry"),
    ],
)
def test_flat_file_parser_rejects_malformed_structure(body: bytes, reason: str) -> None:
    _assert_parse_error(lambda: parse_flat_file_response(body), reason=reason)


def test_flat_file_parser_rejects_invalid_utf8() -> None:
    _assert_parse_error(
        lambda: parse_flat_file_response(b"ENTRY       \xff\n"), reason="invalid_utf8"
    )


def test_brite_parser_preserves_ordered_lines() -> None:
    document = parse_brite_htext_response(
        b"+C\tKO hierarchy\n\nA09100 Metabolism\nB  09101 Carbohydrate metabolism\n",
        "br08901",
    )

    assert document.identifier == "br08901"
    assert document.lines == (
        "+C\tKO hierarchy",
        "",
        "A09100 Metabolism",
        "B  09101 Carbohydrate metabolism",
    )


def test_brite_parser_rejects_nonempty_text_that_is_not_htext() -> None:
    _assert_parse_error(
        lambda: parse_brite_htext_response(b"Not found\n", "br08901"),
        reason="invalid_htext_structure",
    )


def test_brite_parser_accepts_a_five_level_hierarchy_header() -> None:
    document = parse_brite_htext_response(
        b"+E\tEnzymes\nA Metabolism\nB  Enzymes\nE    K00001 example\n",
        "ko01000",
    )

    assert document.lines[0] == "+E\tEnzymes"


def test_brite_parser_accepts_current_compact_root_with_htext_envelope() -> None:
    # Current KEGG GET htext shape verified from br08901 on 2026-07-16.
    document = parse_brite_htext_response(
        b"+C\tMap number\n!\nAMetabolism\nB  Global and overview maps\n",
        "br08901",
    )

    assert document.lines == (
        "+C\tMap number",
        "!",
        "AMetabolism",
        "B  Global and overview maps",
    )


@pytest.mark.parametrize(
    "body",
    [
        b"AMetabolism\nB  Global and overview maps\n",
        b"+C\tMap number\nAMetabolism\nB  Global and overview maps\n",
        b"!\nAMetabolism\nB  Global and overview maps\n",
        b"AMetabolism\n+C\tMap number\n!\n",
        b"!\n+C\tMap number\nAMetabolism\n",
    ],
)
def test_brite_parser_rejects_compact_root_without_complete_htext_envelope(
    body: bytes,
) -> None:
    _assert_parse_error(
        lambda: parse_brite_htext_response(body, "br08901"),
        reason="invalid_htext_structure",
    )


def test_brite_parser_accepts_blank_response_for_client_reconciliation() -> None:
    assert parse_brite_htext_response(b" \n\t", "br08901").lines == ()


def test_brite_parser_rejects_invalid_utf8() -> None:
    _assert_parse_error(
        lambda: parse_brite_htext_response(b"A09100 \xff", "br08901"),
        reason="invalid_utf8",
    )


def test_brite_parser_rejects_invalid_requested_identifier() -> None:
    _assert_parse_error(
        lambda: parse_brite_htext_response(b"A09100 Metabolism\n", "K00001"),
        reason="invalid_requested_identifier",
    )
