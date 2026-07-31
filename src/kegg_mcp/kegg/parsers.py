"""Strict parsers for the bounded textual KEGG REST responses used by the client."""

import re
from dataclasses import dataclass
from typing import Literal, NoReturn

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import (
    MAX_FIND_MATCH_TEXT_CHARACTERS,
    KeggBriteHtextDocument,
    KeggFindDatabase,
    KeggFindDocument,
    KeggFindRow,
    KeggFlatFileDocument,
    KeggFlatFileEntry,
    KeggFlatFileField,
    KeggInfoDatabase,
    KeggInfoDocument,
    KeggOrganismPathwayDocument,
    KeggOrganismPathwayRow,
    KeggPairDocument,
    KeggPairRow,
    is_ec_number,
    is_kegg_brite_identifier,
    is_kegg_gene_identifier,
    is_kegg_organism_code,
    is_kegg_pathway_identifier,
)

_INFO_RELEASE = re.compile(r"(?:^|\s)Release\s+(?P<release>\S.*?)(?:\s*)$")
_INFO_COUNT = re.compile(
    r"^(?:(?P<database>[a-z][a-z0-9_-]{0,31})\s+)?"
    r"(?P<count>(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+))\s+entries$"
)
_INFO_LINKED_DATABASE = r"(?:[a-z][a-z0-9_-]{0,31}|<org>)"
_INFO_LINKED_START = re.compile(rf"^linked db\s+(?P<database>{_INFO_LINKED_DATABASE})\s*$")
_INFO_LINKED_CONTINUATION = re.compile(rf"^\s+(?P<database>{_INFO_LINKED_DATABASE})\s*$")
_HTEXT_METADATA_HEADER = re.compile(r"^\+[A-Z]\t")
_HTEXT_LEGACY_ROOT = re.compile(r"^A(?:\s|[0-9<])")
_HTEXT_COMPACT_ROOT = re.compile(r"^A\S")
_FIELD_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENTRY_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,99}$")
_FIND_NUMBERED_IDENTIFIERS = {
    KeggFindDatabase.KO: re.compile(r"^K[0-9]{5}$"),
    KeggFindDatabase.REACTION: re.compile(r"^R[0-9]{5}$"),
    KeggFindDatabase.COMPOUND: re.compile(r"^C[0-9]{5}$"),
    KeggFindDatabase.GLYCAN: re.compile(r"^G[0-9]{5}$"),
    KeggFindDatabase.DRUG: re.compile(r"^D[0-9]{5}$"),
    KeggFindDatabase.RCLASS: re.compile(r"^RC[0-9]{5}$"),
    KeggFindDatabase.GENOME: re.compile(r"^T[0-9]{5}$"),
    KeggFindDatabase.ORGANISM: re.compile(r"^T[0-9]{5}$"),
}
_FIND_REFERENCE_MODULE = re.compile(r"^M[0-9]{5}$")
_FIND_ORGANISM_MODULE = re.compile(r"^(?P<organism>(?:[a-z]{3,4}|T[0-9]{5}))_M[0-9]{5}$")


@dataclass
class _FieldBuilder:
    name: str
    indent_columns: Literal[0, 2]
    value_lines: list[str]
    start_line: int
    end_line: int


def _parse_error(parser: str, reason: str, *, line_number: int | None = None) -> NoReturn:
    details = [
        SafeDetail(name="parser", value=parser),
        SafeDetail(name="reason", value=reason),
    ]
    if line_number is not None:
        details.append(SafeDetail(name="line_number", value=str(line_number)))
    fail(
        ErrorCode.KEGG_PARSE_FAILED,
        f"The KEGG {parser} response could not be parsed safely.",
        suggested_action=(
            "Refresh the response from the configured KEGG endpoint or inspect endpoint "
            "compatibility."
        ),
        safe_details=tuple(details),
    )


def _decode_response(body: bytes, *, parser: str) -> str:
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _parse_error(parser, "invalid_utf8")
    if any(
        (ord(character) < 32 and character not in "\t\r\n") or ord(character) == 127
        for character in text
    ):
        _parse_error(parser, "unsupported_control_character")
    return text


def parse_info_response(body: bytes, database: KeggInfoDatabase) -> KeggInfoDocument:
    """Parse INFO output while retaining every non-empty source line."""
    text = _decode_response(body, parser="info")
    lines = tuple(line for line in text.splitlines() if line.strip())
    if not lines:
        _parse_error("info", "empty_response")
    if re.match(rf"^{re.escape(database.value)}(?:\s|$)", lines[0]) is None:
        _parse_error("info", "unexpected_database_header", line_number=1)

    release: str | None = None
    entry_count: int | None = None
    linked_databases: list[str] = []
    in_linked_database_block = False
    for line_number, line in enumerate(lines, start=1):
        release_match = _INFO_RELEASE.search(line)
        if release_match is not None:
            candidate = release_match.group("release")
            if len(candidate) > 256:
                _parse_error("info", "release_too_long", line_number=line_number)
            if release is not None and candidate != release:
                _parse_error("info", "conflicting_release_lines", line_number=line_number)
            release = candidate

        linked_start = _INFO_LINKED_START.fullmatch(line)
        if linked_start is not None:
            linked_database = linked_start.group("database")
            if linked_database not in linked_databases:
                linked_databases.append(linked_database)
            in_linked_database_block = True
            continue
        if in_linked_database_block:
            linked_continuation = _INFO_LINKED_CONTINUATION.fullmatch(line)
            if linked_continuation is not None:
                linked_database = linked_continuation.group("database")
                if linked_database not in linked_databases:
                    linked_databases.append(linked_database)
                continue
            in_linked_database_block = False

        count_match = _INFO_COUNT.fullmatch(line.strip())
        if count_match is None:
            continue
        count_text = count_match.group("count").replace(",", "")
        if len(count_text) > 18:
            _parse_error("info", "entry_count_too_large", line_number=line_number)
        count = int(count_text)
        count_database = count_match.group("database")
        if count_database is None or count_database == database.value:
            if entry_count is not None and count != entry_count:
                _parse_error("info", "conflicting_entry_counts", line_number=line_number)
            entry_count = count

    return KeggInfoDocument(
        database=database,
        release=release,
        entry_count=entry_count,
        linked_databases=tuple(linked_databases),
        lines=lines,
    )


def parse_organism_pathway_list_response(
    body: bytes,
    organism: str,
) -> KeggOrganismPathwayDocument:
    """Parse the pathway directory for one canonical KEGG organism code."""
    if not is_kegg_organism_code(organism):
        raise ValueError("organism must be one canonical KEGG organism code")
    text = _decode_response(body, parser="organism_pathway_list")
    expected_identifier = re.compile(rf"^(?P<identifier>{re.escape(organism)}[0-9]{{5}})$")
    rows: list[KeggOrganismPathwayRow] = []
    seen_identifiers: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        columns = line.split("\t")
        if len(columns) != 2:
            _parse_error(
                "organism_pathway_list",
                "expected_two_columns",
                line_number=line_number,
            )
        pathway_id, name = columns
        identifier_match = expected_identifier.fullmatch(pathway_id)
        if identifier_match is None:
            _parse_error(
                "organism_pathway_list",
                "unexpected_identifier",
                line_number=line_number,
            )
        canonical_pathway_id = f"path:{identifier_match.group('identifier')}"
        if canonical_pathway_id in seen_identifiers:
            _parse_error(
                "organism_pathway_list",
                "duplicate_identifier",
                line_number=line_number,
            )
        seen_identifiers.add(canonical_pathway_id)
        if not name.strip() or len(name) > MAX_FIND_MATCH_TEXT_CHARACTERS:
            _parse_error(
                "organism_pathway_list",
                "invalid_name",
                line_number=line_number,
            )
        rows.append(
            KeggOrganismPathwayRow(
                line_number=line_number,
                pathway_id=canonical_pathway_id,
                name=name,
            )
        )
    return KeggOrganismPathwayDocument(organism=organism, rows=tuple(rows))


def parse_find_response(
    body: bytes,
    database: KeggFindDatabase,
    *,
    organism: str | None = None,
) -> KeggFindDocument:
    """Parse ordered two-column FIND candidates for one expected database."""
    text = _decode_response(body, parser="find_table")
    rows: list[KeggFindRow] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        columns = line.split("\t")
        if len(columns) != 2:
            _parse_error("find_table", "expected_two_columns", line_number=line_number)
        identifier, matched_text = columns
        if not _find_identifier_matches(database, identifier, organism=organism):
            _parse_error("find_table", "unexpected_identifier", line_number=line_number)
        if not matched_text.strip() or len(matched_text) > MAX_FIND_MATCH_TEXT_CHARACTERS:
            _parse_error("find_table", "invalid_matched_text", line_number=line_number)
        rows.append(
            KeggFindRow(
                line_number=line_number,
                identifier=identifier,
                matched_text=matched_text,
            )
        )
    return KeggFindDocument(rows=tuple(rows))


def _find_identifier_matches(
    database: KeggFindDatabase,
    identifier: str,
    *,
    organism: str | None,
) -> bool:
    if database is KeggFindDatabase.GENES:
        if not is_kegg_gene_identifier(identifier):
            return False
        return organism is None or identifier.partition(":")[0] == organism
    if database is KeggFindDatabase.PATHWAY:
        return is_kegg_pathway_identifier(identifier)
    if database is KeggFindDatabase.MODULE:
        if _FIND_REFERENCE_MODULE.fullmatch(identifier) is not None:
            return True
        match = _FIND_ORGANISM_MODULE.fullmatch(identifier)
        if match is None:
            return False
        module_organism = match.group("organism")
        return module_organism is not None and (
            module_organism.startswith("T") or is_kegg_organism_code(module_organism)
        )
    if database is KeggFindDatabase.ENZYME:
        return is_ec_number(identifier)
    return _FIND_NUMBERED_IDENTIFIERS[database].fullmatch(identifier) is not None


def parse_pair_response(body: bytes) -> KeggPairDocument:
    """Parse ordered source-to-target LINK or CONV rows, including duplicates."""
    text = _decode_response(body, parser="pair_table")
    rows: list[KeggPairRow] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        columns = line.split("\t")
        if len(columns) != 2:
            _parse_error("pair_table", "expected_two_columns", line_number=line_number)
        source_id, target_id = columns
        if not _valid_pair_identifier(source_id) or not _valid_pair_identifier(target_id):
            _parse_error("pair_table", "invalid_identifier", line_number=line_number)
        rows.append(
            KeggPairRow(
                line_number=line_number,
                source_id=source_id,
                target_id=target_id,
            )
        )
    return KeggPairDocument(rows=tuple(rows))


def _valid_pair_identifier(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 256
        and all(33 <= ord(character) <= 126 for character in value)
    )


def parse_flat_file_response(body: bytes) -> KeggFlatFileDocument:
    """Parse standard 12-column KEGG flat files without discarding unknown fields."""
    text = _decode_response(body, parser="flat_file")
    if not text.strip():
        return KeggFlatFileDocument(entries=())

    entries: list[KeggFlatFileEntry] = []
    fields: list[_FieldBuilder] = []
    current_field: _FieldBuilder | None = None
    entry_start_line: int | None = None

    for line_number, line in enumerate(text.splitlines(), start=1):
        if _is_terminator(line):
            if not fields or entry_start_line is None:
                _parse_error("flat_file", "terminator_without_entry", line_number=line_number)
            entries.append(
                _finish_flat_entry(
                    fields,
                    start_line=entry_start_line,
                    end_line=line_number,
                )
            )
            fields = []
            current_field = None
            entry_start_line = None
            continue

        if not fields and not line.strip():
            continue
        if len(line) < 12:
            _parse_error("flat_file", "invalid_field_columns", line_number=line_number)

        label_columns = line[:12]
        value = line[12:]
        if label_columns == " " * 12:
            if current_field is None:
                _parse_error("flat_file", "continuation_without_field", line_number=line_number)
            current_field.value_lines.append(value)
            current_field.end_line = line_number
            continue

        label_without_indent = label_columns.lstrip(" ")
        indent_columns = len(label_columns) - len(label_without_indent)
        if indent_columns not in {0, 2}:
            _parse_error("flat_file", "invalid_field_indent", line_number=line_number)
        canonical_indent: Literal[0, 2] = 0 if indent_columns == 0 else 2
        field_name = label_without_indent.rstrip(" ")
        expected_label = (" " * canonical_indent) + field_name.ljust(12 - canonical_indent)
        if label_columns != expected_label or _FIELD_NAME.fullmatch(field_name) is None:
            _parse_error("flat_file", "invalid_field_name", line_number=line_number)
        if field_name == "ENTRY" and canonical_indent != 0:
            _parse_error("flat_file", "invalid_entry_field_indent", line_number=line_number)
        current_field = _FieldBuilder(
            name=field_name,
            indent_columns=canonical_indent,
            value_lines=[value],
            start_line=line_number,
            end_line=line_number,
        )
        fields.append(current_field)
        if entry_start_line is None:
            entry_start_line = line_number

    if fields:
        _parse_error("flat_file", "unterminated_entry", line_number=len(text.splitlines()))
    if not entries:
        _parse_error("flat_file", "no_entries")
    return KeggFlatFileDocument(entries=tuple(entries))


def _is_terminator(line: str) -> bool:
    return line.startswith("///") and not line[3:].strip()


def _finish_flat_entry(
    fields: list[_FieldBuilder],
    *,
    start_line: int,
    end_line: int,
) -> KeggFlatFileEntry:
    entry_fields = [
        field for field in fields if field.name == "ENTRY" and field.indent_columns == 0
    ]
    if not entry_fields:
        _parse_error("flat_file", "missing_entry_field", line_number=start_line)
    if len(entry_fields) != 1:
        _parse_error("flat_file", "duplicate_entry_field", line_number=entry_fields[1].start_line)
    entry_value = entry_fields[0].value_lines[0].strip()
    if not entry_value:
        _parse_error("flat_file", "empty_entry_identifier", line_number=entry_fields[0].start_line)
    entry_tokens = entry_value.split()
    identifier = (
        entry_tokens[1] if len(entry_tokens) >= 2 and entry_tokens[0] == "EC" else entry_tokens[0]
    )
    if _ENTRY_IDENTIFIER.fullmatch(identifier) is None:
        _parse_error(
            "flat_file", "invalid_entry_identifier", line_number=entry_fields[0].start_line
        )

    parsed_fields = tuple(
        KeggFlatFileField(
            name=field.name,
            indent_columns=field.indent_columns,
            value_lines=tuple(field.value_lines),
            start_line=field.start_line,
            end_line=field.end_line,
        )
        for field in fields
    )
    return KeggFlatFileEntry(
        identifier=identifier,
        fields=parsed_fields,
        start_line=start_line,
        end_line=end_line,
    )


def parse_brite_htext_response(body: bytes, identifier: str) -> KeggBriteHtextDocument:
    """Parse BRITE htext by retaining its ordered source lines verbatim."""
    if not is_kegg_brite_identifier(identifier):
        _parse_error("brite_htext", "invalid_requested_identifier")
    text = _decode_response(body, parser="brite_htext")
    lines = () if not text.strip() else tuple(text.splitlines())
    if lines:
        nonempty_lines = tuple(line for line in lines if line.strip())
        has_legacy_root = any(_HTEXT_LEGACY_ROOT.match(line) for line in nonempty_lines)
        metadata_index = next(
            (
                index
                for index, line in enumerate(nonempty_lines)
                if _HTEXT_METADATA_HEADER.match(line)
            ),
            None,
        )
        delimiter_index = next(
            (
                index
                for index, line in enumerate(nonempty_lines)
                if line == "!" and metadata_index is not None and index > metadata_index
            ),
            None,
        )
        has_compact_root = delimiter_index is not None and any(
            _HTEXT_COMPACT_ROOT.match(line) for line in nonempty_lines[delimiter_index + 1 :]
        )
        has_root_data_line = has_legacy_root or has_compact_root
        if not has_root_data_line:
            _parse_error("brite_htext", "invalid_htext_structure")
    return KeggBriteHtextDocument(identifier=identifier, lines=lines)
