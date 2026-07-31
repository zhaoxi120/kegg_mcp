"""Prepare bounded, allowlisted KEGG REST requests without performing I/O."""

import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import quote

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import (
    MAX_GET_ENTRIES_PER_BATCH,
    ConvRequest,
    FindRequest,
    GetRequest,
    InfoRequest,
    KeggClientLimits,
    KeggConvDatabase,
    KeggEntryRef,
    KeggFindDatabase,
    KeggFindMode,
    KeggFlatFileEntry,
    KeggGetDatabase,
    KeggOperation,
    LinkRequest,
    OrganismPathwayListRequest,
    is_ec_number,
    is_kegg_brite_identifier,
    is_kegg_gene_identifier,
    is_kegg_genome_identifier,
    is_kegg_pathway_identifier,
    link_relation_contract,
)


class ResponseParser(StrEnum):
    """Parser selected for one prepared KEGG response."""

    INFO = "info"
    ORGANISM_PATHWAY_LIST = "organism_pathway_list"
    FIND_TABLE = "find_table"
    FLAT_FILE = "flat_file"
    BRITE_HTEXT = "brite_htext"
    PAIR_TABLE = "pair_table"
    PATHWAY_PNG = "pathway_png"
    PATHWAY_KGML_PREFLIGHT = "pathway_kgml_preflight"


class PairTargetDatabase(StrEnum):
    """Expected target namespace for one LINK or selected CONV response."""

    PATHWAY = "pathway"
    MODULE = "module"
    REACTION = "reaction"
    ENZYME = "enzyme"
    BRITE = "brite"
    KO = "ko"
    GENES = "genes"
    NCBI_GENEID = "ncbi-geneid"
    NCBI_PROTEINID = "ncbi-proteinid"
    UNIPROT = "uniprot"
    COMPOUND = "compound"
    GLYCAN = "glycan"
    DRUG = "drug"
    CHEBI = "chebi"
    PUBCHEM = "pubchem"
    GENOME = "genome"
    TAXONOMY = "taxonomy"


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """One validated HTTP batch with no caller-controlled URL components."""

    operation: KeggOperation
    path: str
    normalized_request_key: str
    parser: ResponseParser
    requested_entries: tuple[KeggEntryRef, ...] = ()
    requested_identifiers: tuple[str, ...] = ()
    expected_pair_source_ids: frozenset[str] = frozenset()
    pair_target_database: PairTargetDatabase | None = None
    expected_pair_target_prefix: str | None = None
    find_database: KeggFindDatabase | None = None
    find_organism: str | None = None
    list_organism: str | None = None

    def __post_init__(self) -> None:
        has_pair_contract = bool(self.expected_pair_source_ids) and (
            self.pair_target_database is not None
        )
        if (self.parser is ResponseParser.PAIR_TABLE) != has_pair_contract:
            raise ValueError("pair-table requests require a complete response contract")
        if self.expected_pair_target_prefix is not None and not has_pair_contract:
            raise ValueError("pair target prefixes require a complete response contract")
        if (self.parser is ResponseParser.FIND_TABLE) != (self.find_database is not None):
            raise ValueError("find-table requests require an expected database")
        if self.find_organism is not None and self.find_database is not KeggFindDatabase.GENES:
            raise ValueError("organism-scoped find-table requests require the genes database")
        if (self.parser is ResponseParser.ORGANISM_PATHWAY_LIST) != (
            self.list_organism is not None
        ):
            raise ValueError("organism pathway list requests require a canonical organism")


_PAIR_TARGET_PATTERNS = {
    PairTargetDatabase.MODULE: re.compile(r"^(?:md|module):M[0-9]{5}$"),
    PairTargetDatabase.REACTION: re.compile(r"^(?:rn|reaction):R[0-9]{5}$"),
    PairTargetDatabase.KO: re.compile(r"^ko:K[0-9]{5}$"),
    PairTargetDatabase.NCBI_GENEID: re.compile(r"^ncbi-geneid:[1-9][0-9]*$"),
    PairTargetDatabase.NCBI_PROTEINID: re.compile(r"^ncbi-proteinid:[A-Za-z0-9._-]+$"),
    PairTargetDatabase.UNIPROT: re.compile(r"^(?:uniprot|up):[A-Za-z0-9._-]+$"),
    PairTargetDatabase.COMPOUND: re.compile(r"^(?:cpd|compound):C[0-9]{5}$"),
    PairTargetDatabase.GLYCAN: re.compile(r"^(?:gl|glycan):G[0-9]{5}$"),
    PairTargetDatabase.DRUG: re.compile(r"^(?:dr|drug):D[0-9]{5}$"),
    PairTargetDatabase.CHEBI: re.compile(r"^chebi:[1-9][0-9]*$"),
    PairTargetDatabase.PUBCHEM: re.compile(r"^pubchem:[1-9][0-9]*$"),
    PairTargetDatabase.TAXONOMY: re.compile(r"^(?:taxid|taxonomy):[1-9][0-9]*$"),
}


def prepare_info(request: InfoRequest, limits: KeggClientLimits) -> tuple[PreparedRequest, ...]:
    """Prepare one INFO request."""
    path = f"/info/{request.database.value}"
    return (_prepared(KeggOperation.INFO, path, ResponseParser.INFO, limits),)


def prepare_organism_pathway_list(
    request: OrganismPathwayListRequest,
    limits: KeggClientLimits,
    *,
    url_prefix_bytes: int = 0,
) -> PreparedRequest:
    """Prepare the only supported typed LIST operation."""
    path = f"/list/pathway/{request.organism}"
    return _prepared(
        KeggOperation.LIST,
        path,
        ResponseParser.ORGANISM_PATHWAY_LIST,
        limits,
        list_organism=request.organism,
        url_prefix_bytes=url_prefix_bytes,
    )


def prepare_find(
    request: FindRequest,
    limits: KeggClientLimits,
    *,
    url_prefix_bytes: int = 0,
) -> tuple[PreparedRequest, ...]:
    """Prepare one FIND request with its query encoded as one path segment."""
    encoded_query = quote(request.query, safe="", encoding="utf-8", errors="strict")
    wire_database = request.organism or request.database.wire_database
    path = f"/find/{wire_database}/{encoded_query}"
    if request.mode is not KeggFindMode.KEYWORD:
        path = f"{path}/{request.mode.value}"
    return (
        _prepared(
            KeggOperation.FIND,
            path,
            ResponseParser.FIND_TABLE,
            limits,
            url_prefix_bytes=url_prefix_bytes,
            find_database=request.database,
            find_organism=request.organism,
        ),
    )


def prepare_get(request: GetRequest, limits: KeggClientLimits) -> tuple[PreparedRequest, ...]:
    """Prepare GET batches, isolating BRITE htext and enforcing the ten-entry limit."""
    _require_identifier_limit(len(request.entries), limits)
    prepared: list[PreparedRequest] = []
    flat_batch: list[KeggEntryRef] = []

    def flush_flat_batch() -> None:
        if not flat_batch:
            return
        entries = tuple(flat_batch)
        path = f"/get/{'+'.join(entry.wire_identifier for entry in entries)}"
        prepared.append(
            _prepared(
                KeggOperation.GET,
                path,
                ResponseParser.FLAT_FILE,
                limits,
                requested_entries=entries,
            )
        )
        flat_batch.clear()

    for entry in request.entries:
        if entry.database.value == "brite":
            flush_flat_batch()
            path = f"/get/{entry.wire_identifier}"
            prepared.append(
                _prepared(
                    KeggOperation.GET,
                    path,
                    ResponseParser.BRITE_HTEXT,
                    limits,
                    requested_entries=(entry,),
                )
            )
            continue
        if any(_get_entries_may_alias(existing, entry) for existing in flat_batch):
            flush_flat_batch()
        flat_batch.append(entry)
        if len(flat_batch) == MAX_GET_ENTRIES_PER_BATCH:
            flush_flat_batch()
    flush_flat_batch()
    return tuple(prepared)


def _get_entries_may_alias(left: KeggEntryRef, right: KeggEntryRef) -> bool:
    if left.database is not right.database:
        return False
    if left.database is KeggGetDatabase.GENOME:
        return left.identifier.startswith("T") != right.identifier.startswith("T")
    if left.database is not KeggGetDatabase.GENE:
        return False
    left_prefix, _, left_suffix = left.identifier.partition(":")
    right_prefix, _, right_suffix = right.identifier.partition(":")
    return left_suffix == right_suffix and left_prefix != right_prefix


def get_entry_matches(requested: KeggEntryRef, returned: KeggFlatFileEntry) -> bool:
    """Return whether one parsed flat-file entry matches one typed GET reference."""
    if requested.database is KeggGetDatabase.GENE:
        prefix, _, gene_identifier = requested.identifier.partition(":")
        if returned.identifier == requested.identifier:
            return True
        if returned.identifier != gene_identifier:
            return False
        if prefix.startswith("T"):
            return prefix in _flat_field_tokens(returned, "ENTRY")
        if prefix in {"ag", "vg", "vp"}:
            return True
        return _flat_field_first_token(returned, "ORGANISM") == prefix
    if requested.database is KeggGetDatabase.GENOME:
        if returned.identifier == requested.identifier:
            return True
        return (
            is_kegg_genome_identifier(returned.identifier)
            and _flat_field_first_token(returned, "ORG_CODE") == requested.identifier
        )
    return returned.identifier == requested.identifier


def prepare_link(
    request: LinkRequest,
    limits: KeggClientLimits,
    *,
    url_prefix_bytes: int = 0,
) -> tuple[PreparedRequest, ...]:
    """Greedily prepare canonical LINK batches within identifier and URL bounds."""
    _require_identifier_limit(len(request.source_identifiers), limits)
    contract = link_relation_contract(request.relationship)
    target = request.organism_scope or contract.target_database
    path_prefix = f"/link/{target}/"
    path_suffix = request.taxonomy_rank.wire_suffix
    prepared: list[PreparedRequest] = []
    for batch in _greedy_relation_batches(
        tuple(sorted(request.source_identifiers)),
        path_prefix=path_prefix,
        path_suffix=path_suffix,
        maximum_items=limits.link_batch_size,
        maximum_url_bytes=limits.max_url_bytes,
        url_prefix_bytes=url_prefix_bytes,
        wire_value=contract.wire_source_identifier,
    ):
        prepared.append(
            _prepared(
                KeggOperation.LINK,
                (
                    f"{path_prefix}"
                    f"{'+'.join(contract.wire_source_identifier(item) for item in batch)}"
                    f"{path_suffix}"
                ),
                ResponseParser.PAIR_TABLE,
                limits,
                url_prefix_bytes=url_prefix_bytes,
                requested_identifiers=batch,
                expected_pair_source_ids=frozenset(
                    contract.response_source_identifier(identifier) for identifier in batch
                ),
                pair_target_database=PairTargetDatabase(contract.target_database),
                expected_pair_target_prefix=(
                    None if request.organism_scope is None else f"{request.organism_scope}:"
                ),
            )
        )
    return tuple(prepared)


def prepare_conv(request: ConvRequest, limits: KeggClientLimits) -> tuple[PreparedRequest, ...]:
    """Prepare selected-entry CONV batches for one approved direction."""
    _require_identifier_limit(len(request.source_identifiers), limits)
    prepared: list[PreparedRequest] = []
    for batch in _batches(tuple(sorted(request.source_identifiers)), limits.relation_batch_size):
        prepared.append(
            _prepared(
                KeggOperation.CONV,
                f"/conv/{request.target_database.value}/{'+'.join(batch)}",
                ResponseParser.PAIR_TABLE,
                limits,
                requested_identifiers=batch,
                expected_pair_source_ids=_conversion_source_ids(request.source_database, batch),
                pair_target_database=PairTargetDatabase(request.target_database.value),
            )
        )
    return tuple(prepared)


def pair_target_matches(database: PairTargetDatabase, identifier: str) -> bool:
    """Return whether a parsed target identifier matches its prepared namespace."""
    if database is PairTargetDatabase.PATHWAY:
        namespace, separator, value = identifier.partition(":")
        return (
            separator == ":"
            and namespace in {"path", "pathway"}
            and is_kegg_pathway_identifier(value)
        )
    if database is PairTargetDatabase.BRITE:
        namespace, separator, value = identifier.partition(":")
        return separator == ":" and namespace in {"br", "brite"} and is_kegg_brite_identifier(value)
    if database is PairTargetDatabase.GENES:
        return is_kegg_gene_identifier(identifier)
    if database is PairTargetDatabase.GENOME:
        namespace, separator, value = identifier.partition(":")
        return (
            separator == ":" and namespace in {"gn", "genome"} and is_kegg_genome_identifier(value)
        )
    if database is PairTargetDatabase.ENZYME:
        namespace, separator, value = identifier.partition(":")
        return separator == ":" and namespace in {"ec", "enzyme"} and is_ec_number(value)
    return _PAIR_TARGET_PATTERNS[database].fullmatch(identifier) is not None


def _conversion_source_ids(
    database: KeggConvDatabase, identifiers: tuple[str, ...]
) -> frozenset[str]:
    if database is KeggConvDatabase.COMPOUND:
        return frozenset(f"cpd:{identifier}" for identifier in identifiers)
    if database is KeggConvDatabase.GLYCAN:
        return frozenset(f"gl:{identifier}" for identifier in identifiers)
    if database is KeggConvDatabase.DRUG:
        return frozenset(f"dr:{identifier}" for identifier in identifiers)
    expected = set(identifiers)
    if database is KeggConvDatabase.UNIPROT:
        expected.update(f"up:{identifier.removeprefix('uniprot:')}" for identifier in identifiers)
    return frozenset(expected)


def _flat_field_tokens(entry: KeggFlatFileEntry, field_name: str) -> tuple[str, ...]:
    for field in entry.fields:
        if field.name == field_name and field.indent_columns == 0:
            return tuple(field.value_lines[0].strip().split())
    return ()


def _flat_field_first_token(entry: KeggFlatFileEntry, field_name: str) -> str | None:
    tokens = _flat_field_tokens(entry, field_name)
    return tokens[0] if tokens else None


def _prepared(
    operation: KeggOperation,
    path: str,
    parser: ResponseParser,
    limits: KeggClientLimits,
    *,
    requested_entries: tuple[KeggEntryRef, ...] = (),
    requested_identifiers: tuple[str, ...] = (),
    expected_pair_source_ids: frozenset[str] = frozenset(),
    pair_target_database: PairTargetDatabase | None = None,
    expected_pair_target_prefix: str | None = None,
    find_database: KeggFindDatabase | None = None,
    find_organism: str | None = None,
    list_organism: str | None = None,
    url_prefix_bytes: int = 0,
) -> PreparedRequest:
    if url_prefix_bytes < 0:
        raise ValueError("url_prefix_bytes must be non-negative")
    if url_prefix_bytes + len(path.encode("ascii")) > limits.max_url_bytes:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The prepared KEGG request exceeds the configured URL-size limit.",
            suggested_action="Use fewer identifiers per request or a smaller relation batch size.",
            safe_details=(SafeDetail(name="operation", value=operation.value),),
        )
    return PreparedRequest(
        operation=operation,
        path=path,
        normalized_request_key=path,
        parser=parser,
        requested_entries=requested_entries,
        requested_identifiers=requested_identifiers,
        expected_pair_source_ids=expected_pair_source_ids,
        pair_target_database=pair_target_database,
        expected_pair_target_prefix=expected_pair_target_prefix,
        find_database=find_database,
        find_organism=find_organism,
        list_organism=list_organism,
    )


def _require_identifier_limit(count: int, limits: KeggClientLimits) -> None:
    if count > limits.max_identifiers:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The KEGG request contains too many identifiers.",
            suggested_action="Split the identifiers into smaller service calls.",
            safe_details=(
                SafeDetail(name="identifier_count", value=str(count)),
                SafeDetail(name="configured_limit", value=str(limits.max_identifiers)),
            ),
        )


def _batches(values: Sequence[str], size: int) -> Iterator[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield tuple(values[start : start + size])


def _greedy_relation_batches(
    values: Sequence[str],
    *,
    path_prefix: str,
    path_suffix: str = "",
    maximum_items: int,
    maximum_url_bytes: int,
    url_prefix_bytes: int,
    wire_value: Callable[[str], str] = lambda value: value,
) -> Iterator[tuple[str, ...]]:
    """Pack sorted identifiers into the fewest bounded request paths."""
    current: list[str] = []
    for value in values:
        candidate = (*current, value)
        candidate_path = (
            f"{path_prefix}{'+'.join(wire_value(item) for item in candidate)}{path_suffix}"
        )
        exceeds_items = len(candidate) > maximum_items
        exceeds_url = url_prefix_bytes + len(candidate_path.encode("ascii")) > maximum_url_bytes
        if current and (exceeds_items or exceeds_url):
            yield tuple(current)
            current = []
            candidate = (value,)
            candidate_path = f"{path_prefix}{wire_value(value)}{path_suffix}"
            exceeds_items = False
            exceeds_url = url_prefix_bytes + len(candidate_path.encode("ascii")) > maximum_url_bytes
        if exceeds_items or exceeds_url:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "One KEGG relationship identifier cannot fit the configured URL-size limit.",
                suggested_action=(
                    "Raise the bounded URL limit or use a shorter supported identifier."
                ),
                safe_details=(SafeDetail(name="operation", value=KeggOperation.LINK.value),),
            )
        current.append(value)
    if current:
        yield tuple(current)
