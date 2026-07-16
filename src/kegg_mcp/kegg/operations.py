"""Prepare bounded, allowlisted KEGG REST requests without performing I/O."""

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from enum import StrEnum

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import (
    MAX_GET_ENTRIES_PER_BATCH,
    ConvRequest,
    GetRequest,
    InfoRequest,
    KeggClientLimits,
    KeggConvDatabase,
    KeggEntryRef,
    KeggLinkRelationship,
    KeggOperation,
    LinkRequest,
    is_ec_number,
    is_kegg_brite_identifier,
    is_kegg_gene_identifier,
    is_kegg_pathway_identifier,
)


class ResponseParser(StrEnum):
    """Parser selected for one prepared KEGG response."""

    INFO = "info"
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

    def __post_init__(self) -> None:
        has_pair_contract = bool(self.expected_pair_source_ids) and (
            self.pair_target_database is not None
        )
        if (self.parser is ResponseParser.PAIR_TABLE) != has_pair_contract:
            raise ValueError("pair-table requests require a complete response contract")


_LINK_TARGETS = {
    KeggLinkRelationship.KO_TO_PATHWAY: "pathway",
    KeggLinkRelationship.KO_TO_MODULE: "module",
    KeggLinkRelationship.KO_TO_REACTION: "reaction",
    KeggLinkRelationship.KO_TO_ENZYME: "enzyme",
    KeggLinkRelationship.KO_TO_BRITE: "brite",
    KeggLinkRelationship.PATHWAY_TO_KO: "ko",
}

_LINK_PAIR_TARGETS = {
    KeggLinkRelationship.KO_TO_PATHWAY: PairTargetDatabase.PATHWAY,
    KeggLinkRelationship.KO_TO_MODULE: PairTargetDatabase.MODULE,
    KeggLinkRelationship.KO_TO_REACTION: PairTargetDatabase.REACTION,
    KeggLinkRelationship.KO_TO_ENZYME: PairTargetDatabase.ENZYME,
    KeggLinkRelationship.KO_TO_BRITE: PairTargetDatabase.BRITE,
    KeggLinkRelationship.PATHWAY_TO_KO: PairTargetDatabase.KO,
}

_PAIR_TARGET_PATTERNS = {
    PairTargetDatabase.MODULE: re.compile(r"^(?:md|module):M[0-9]{5}$"),
    PairTargetDatabase.REACTION: re.compile(r"^(?:rn|reaction):R[0-9]{5}$"),
    PairTargetDatabase.KO: re.compile(r"^ko:K[0-9]{5}$"),
    PairTargetDatabase.NCBI_GENEID: re.compile(r"^ncbi-geneid:[1-9][0-9]*$"),
    PairTargetDatabase.NCBI_PROTEINID: re.compile(r"^ncbi-proteinid:[A-Za-z0-9._-]+$"),
    PairTargetDatabase.UNIPROT: re.compile(r"^(?:uniprot|up):[A-Za-z0-9._-]+$"),
}


def prepare_info(request: InfoRequest, limits: KeggClientLimits) -> tuple[PreparedRequest, ...]:
    """Prepare one INFO request."""
    path = f"/info/{request.database.value}"
    return (_prepared(KeggOperation.INFO, path, ResponseParser.INFO, limits),)


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
        flat_batch.append(entry)
        if len(flat_batch) == MAX_GET_ENTRIES_PER_BATCH:
            flush_flat_batch()
    flush_flat_batch()
    return tuple(prepared)


def prepare_link(request: LinkRequest, limits: KeggClientLimits) -> tuple[PreparedRequest, ...]:
    """Prepare selected-entry LINK batches for one approved relationship."""
    _require_identifier_limit(len(request.source_identifiers), limits)
    target = _LINK_TARGETS[request.relationship]
    prepared: list[PreparedRequest] = []
    for batch in _batches(tuple(sorted(request.source_identifiers)), limits.relation_batch_size):
        source_prefix = (
            "path" if request.relationship is KeggLinkRelationship.PATHWAY_TO_KO else "ko"
        )
        prepared.append(
            _prepared(
                KeggOperation.LINK,
                f"/link/{target}/{'+'.join(batch)}",
                ResponseParser.PAIR_TABLE,
                limits,
                requested_identifiers=batch,
                expected_pair_source_ids=frozenset(
                    f"{source_prefix}:{identifier}" for identifier in batch
                ),
                pair_target_database=_LINK_PAIR_TARGETS[request.relationship],
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
    if database is PairTargetDatabase.ENZYME:
        namespace, separator, value = identifier.partition(":")
        return separator == ":" and namespace in {"ec", "enzyme"} and is_ec_number(value)
    return _PAIR_TARGET_PATTERNS[database].fullmatch(identifier) is not None


def _conversion_source_ids(
    database: KeggConvDatabase, identifiers: tuple[str, ...]
) -> frozenset[str]:
    expected = set(identifiers)
    if database is KeggConvDatabase.UNIPROT:
        expected.update(f"up:{identifier.removeprefix('uniprot:')}" for identifier in identifiers)
    return frozenset(expected)


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
) -> PreparedRequest:
    if len(path.encode("ascii")) > limits.max_url_bytes:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The prepared KEGG request exceeds the configured URL-size limit.",
            suggested_action="Use fewer identifiers per request or a smaller relation batch size.",
            safe_details=(SafeDetail(name="operation", value=operation.value),),
        )
    return PreparedRequest(
        operation=operation,
        path=path,
        normalized_request_key=f"v1:{path}",
        parser=parser,
        requested_entries=requested_entries,
        requested_identifiers=requested_identifiers,
        expected_pair_source_ids=expected_pair_source_ids,
        pair_target_database=pair_target_database,
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
