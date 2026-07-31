"""Deterministic, bounded projections of parsed KEGG flat-file entries.

Entry cards expose typed fields that can be compared or summarized without asking
an LLM to parse KEGG flat-file text. They do not replace the retained GET detail:
unknown fields, sequence fields, and the exact source document remain available
only in that complete artifact.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from enum import StrEnum
from typing import Annotated, Literal, Self, TypedDict

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp.analysis import (
    MODULE_PARSER_NAME,
    MODULE_PARSER_VERSION,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleTokenKind,
    parse_module_definition,
)
from kegg_mcp.domain.annotations import JSON_SCHEMA_DIALECT, FrozenModel
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.kegg import GetResult, KeggGetDatabase
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION as KEGG_RESPONSE_PARSER_VERSION,
)
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggFlatFileEntry,
    KeggFlatFileField,
    KeggOperation,
)
from kegg_mcp.kegg.operations import get_entry_matches

ENTRY_CARD_SCHEMA_VERSION = "1"
ENTRY_CARD_PARSER_NAME = "kegg_flat_file_entry_card"
ENTRY_CARD_PARSER_VERSION = "1"
ENTRY_CARD_SNAPSHOT_SECTION = "entry_snapshot"
MAX_ENTRY_CARDS = 50
MAX_ENTRY_CARD_TEXT_CHARACTERS = 65_536
MAX_ENTRY_CARD_ITEMS = 2_048
MAX_ENTRY_CARD_NAMES = 256
MAX_ENTRY_CARD_DBLINK_GROUPS = 256
MAX_ENTRY_CARD_PREVIEWS = 10
MAX_ENTRY_CARD_PREVIEW_TEXT_CHARACTERS = 256
MAX_ENTRY_CARD_PREVIEW_CLASSES = 2
MAX_ENTRY_CARD_PREVIEW_FIELD_COUNTS = 16

CardText = Annotated[str, Field(min_length=1, max_length=MAX_ENTRY_CARD_TEXT_CHARACTERS)]
CardIdentifier = Annotated[str, Field(min_length=1, max_length=100)]
CardFieldName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=32)]
CardItems = Annotated[tuple[str, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)]

_EC_NUMBER = re.compile(r"^[0-9]+(?:\.(?:[0-9]+|-)){3}$")
_EC_BLOCK = re.compile(r"\[EC:([^\]]+)\]")
_PMID = re.compile(r"\bPMID:\s*([0-9]+)\b")
_REACTION_ID = re.compile(r"\bR[0-9]{5}\b")
_COMPOUND_ID = re.compile(r"\bC[0-9]{5}\b")
_GLYCAN_ID = re.compile(r"\bG[0-9]{5}\b")
_TAXONOMY_ID = re.compile(r"(?:TAX:|taxid:)?([0-9]+)")


class KeggEntryCardKind(StrEnum):
    """Flat-file entity kinds supported by deterministic card parsing."""

    KO = "ko"
    MODULE = "module"
    PATHWAY = "pathway"
    REACTION = "reaction"
    ENZYME = "enzyme"
    COMPOUND = "compound"
    GLYCAN = "glycan"
    GENE = "gene"
    GENOME = "genome"


ENTRY_CARD_DATABASES: frozenset[KeggGetDatabase] = frozenset(
    database
    for database in KeggGetDatabase
    if database.value in {kind.value for kind in KeggEntryCardKind}
)


class KeggEntryCardEntity(FrozenModel):
    """Typed identity of one requested entry, returned card, or missing entry."""

    database: KeggEntryCardKind
    identifier: CardIdentifier


class KeggEntryCardReference(FrozenModel):
    """One identifier and its optional KEGG-supplied display label."""

    identifier: CardIdentifier
    label: CardText | None = None


class KeggEntryCardDbLink(FrozenModel):
    """One external namespace and the identifiers listed by KEGG."""

    database: str = Field(min_length=1, max_length=128)
    identifiers: Annotated[
        tuple[CardText, ...], Field(min_length=1, max_length=MAX_ENTRY_CARD_ITEMS)
    ]


class KeggModuleDefinitionCard(FrozenModel):
    """Conservative structural projection of one MODULE DEFINITION field."""

    raw_definition: CardText
    parser_name: Literal["kegg_module_definition"] = MODULE_PARSER_NAME
    parser_version: Literal["1"] = MODULE_PARSER_VERSION
    is_valid: bool
    required_blocks: Annotated[tuple[CardText, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)]
    optional_components: Annotated[tuple[CardText, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)]
    referenced_modules: CardItems
    ko_components: CardItems
    diagnostic_codes: Annotated[tuple[str, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)]


class _KeggEntryCardBase(FrozenModel):
    entity: KeggEntryCardEntity
    names: Annotated[tuple[CardText, ...], Field(max_length=MAX_ENTRY_CARD_NAMES)] = ()
    definition: CardText | None = None
    classes: Annotated[tuple[CardText, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)] = ()
    dblinks: Annotated[
        tuple[KeggEntryCardDbLink, ...], Field(max_length=MAX_ENTRY_CARD_DBLINK_GROUPS)
    ] = ()
    pubmed_ids: Annotated[tuple[str, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)] = ()
    unparsed_field_names: Annotated[
        tuple[CardFieldName, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()


class KoEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.KO] = KeggEntryCardKind.KO
    ec_numbers: CardItems = ()
    modules: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    pathways: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()


class ModuleEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.MODULE] = KeggEntryCardKind.MODULE
    module_definition: KeggModuleDefinitionCard | None = None
    pathways: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    reactions: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()


class PathwayEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.PATHWAY] = KeggEntryCardKind.PATHWAY
    ko_identifiers: CardItems = ()
    modules: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    reactions: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    compounds: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    glycans: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()


class ReactionEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.REACTION] = KeggEntryCardKind.REACTION
    equation: CardText | None = None
    enzyme_ids: CardItems = ()
    ko_identifiers: CardItems = ()
    rclass_ids: CardItems = ()
    compound_ids: CardItems = ()
    glycan_ids: CardItems = ()


class EnzymeEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.ENZYME] = KeggEntryCardKind.ENZYME
    reaction_ids: CardItems = ()
    ko_identifiers: CardItems = ()


class CompoundEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.COMPOUND] = KeggEntryCardKind.COMPOUND
    formula: CardText | None = None
    exact_mass: CardText | None = None
    molecular_weight: CardText | None = None
    reactions: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    pathways: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()


class GlycanEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.GLYCAN] = KeggEntryCardKind.GLYCAN
    composition: CardText | None = None
    mass: CardText | None = None
    reactions: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    pathways: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()


class GeneEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.GENE] = KeggEntryCardKind.GENE
    organism_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")
    organism_name: CardText | None = None
    position: CardText | None = None
    orthology: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()
    pathways: Annotated[
        tuple[KeggEntryCardReference, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)
    ] = ()


class GenomeEntryCard(_KeggEntryCardBase):
    kind: Literal[KeggEntryCardKind.GENOME] = KeggEntryCardKind.GENOME
    organism_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")
    taxonomy_id: str | None = Field(default=None, pattern=r"^taxid:[0-9]+$")
    lineage: Annotated[tuple[CardText, ...], Field(max_length=MAX_ENTRY_CARD_ITEMS)] = ()


KeggEntryCard = Annotated[
    KoEntryCard
    | ModuleEntryCard
    | PathwayEntryCard
    | ReactionEntryCard
    | EnzymeEntryCard
    | CompoundEntryCard
    | GlycanEntryCard
    | GeneEntryCard
    | GenomeEntryCard,
    Field(discriminator="kind"),
]


class KeggEntryCardSnapshot(FrozenModel):
    """Snapshot-friendly cards plus the exact retrieval provenance used to build them."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:kegg-entry-card-snapshot:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    schema_version: Literal["1"] = ENTRY_CARD_SCHEMA_VERSION
    parser_name: Literal["kegg_flat_file_entry_card"] = ENTRY_CARD_PARSER_NAME
    parser_version: Literal["1"] = ENTRY_CARD_PARSER_VERSION
    response_parser_version: str = Field(
        default=KEGG_RESPONSE_PARSER_VERSION,
        pattern=r"^[0-9]+(?:\.[0-9]+)*$",
        max_length=32,
    )
    requested_entries: Annotated[
        tuple[KeggEntryCardEntity, ...],
        Field(min_length=1, max_length=MAX_ENTRY_CARDS),
    ]
    entries: Annotated[tuple[KeggEntryCard, ...], Field(max_length=MAX_ENTRY_CARDS)]
    missing_entries: Annotated[
        tuple[KeggEntryCardEntity, ...], Field(max_length=MAX_ENTRY_CARDS)
    ] = ()
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(min_length=1, max_length=MAX_ENTRY_CARDS)
    ]

    @model_validator(mode="after")
    def validate_entry_accounting(self) -> Self:
        if len(self.requested_entries) != len(set(self.requested_entries)):
            raise ValueError("requested entry-card identities must be unique")
        identities = tuple(item.entity for item in self.entries)
        if len(identities) != len(set(identities)):
            raise ValueError("entry-card identities must be unique")
        if any(item.kind is not item.entity.database for item in self.entries):
            raise ValueError("entry-card kind must match its entity database")
        if len(self.missing_entries) != len(set(self.missing_entries)):
            raise ValueError("missing entry-card identities must be unique")
        if set(identities).intersection(self.missing_entries):
            raise ValueError("one entry cannot be both returned and missing")
        accounted_count = len(identities) + len(self.missing_entries)
        if accounted_count != len(self.requested_entries):
            raise ValueError("entry-card accounting must cover every requested entry exactly once")
        requested_database_counts = Counter(item.database for item in self.requested_entries)
        accounted_database_counts = Counter(
            item.database for item in (*identities, *self.missing_entries)
        )
        if requested_database_counts != accounted_database_counts:
            raise ValueError("entry-card accounting must preserve requested database counts")
        requested = set(self.requested_entries)
        if not set(self.missing_entries).issubset(requested):
            raise ValueError("missing entry-card identities must come from the original request")
        if any(
            identity.database is not KeggEntryCardKind.GENOME and identity not in requested
            for identity in identities
        ):
            raise ValueError("returned entry-card identities must come from the original request")
        if any(item.operation is not KeggOperation.GET for item in self.provenance):
            raise ValueError("entry-card provenance must contain only KEGG GET batches")
        if any(
            item.parser_name != "flat_file" or item.parser_version != self.response_parser_version
            for item in self.provenance
        ):
            raise ValueError("entry-card provenance must match the flat-file response parser")
        return self


class KeggEntryCardFieldCount(FrozenModel):
    field: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)
    count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARD_ITEMS)


class KeggEntryCardPreview(FrozenModel):
    kind: KeggEntryCardKind
    entity: KeggEntryCardEntity
    primary_name: str | None = Field(
        default=None, max_length=MAX_ENTRY_CARD_PREVIEW_TEXT_CHARACTERS
    )
    primary_name_truncated: bool = False
    definition_preview: str | None = Field(
        default=None, max_length=MAX_ENTRY_CARD_PREVIEW_TEXT_CHARACTERS
    )
    definition_truncated: bool = False
    class_preview: Annotated[tuple[str, ...], Field(max_length=MAX_ENTRY_CARD_PREVIEW_CLASSES)] = ()
    classes_truncated: bool = False
    class_text_truncated: bool = False
    pubmed_count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARD_ITEMS)
    selected_field_counts: Annotated[
        tuple[KeggEntryCardFieldCount, ...],
        Field(max_length=MAX_ENTRY_CARD_PREVIEW_FIELD_COUNTS),
    ] = ()
    card_fields_truncated: bool = False


class KeggEntryCardPreviewSet(FrozenModel):
    entry_count: int = Field(strict=True, ge=0, le=MAX_ENTRY_CARDS)
    previews: Annotated[tuple[KeggEntryCardPreview, ...], Field(max_length=MAX_ENTRY_CARD_PREVIEWS)]
    previews_truncated: bool

    @model_validator(mode="after")
    def validate_preview_count(self) -> Self:
        if self.entry_count < len(self.previews):
            raise ValueError("entry_count cannot be smaller than the preview count")
        if self.previews_truncated != (self.entry_count > len(self.previews)):
            raise ValueError("previews_truncated must match the preview count")
        return self


class _CommonCardValues(TypedDict):
    entity: KeggEntryCardEntity
    names: tuple[str, ...]
    definition: str | None
    classes: tuple[str, ...]
    dblinks: tuple[KeggEntryCardDbLink, ...]
    pubmed_ids: tuple[str, ...]
    unparsed_field_names: tuple[str, ...]


_COMMON_FIELDS = frozenset(
    {
        "ENTRY",
        "NAME",
        "DEFINITION",
        "CLASS",
        "DBLINKS",
        "REFERENCE",
        "AUTHORS",
        "TITLE",
        "JOURNAL",
    }
)
_KIND_FIELDS: dict[KeggEntryCardKind, frozenset[str]] = {
    KeggEntryCardKind.KO: frozenset({"MODULE", "PATHWAY", "ENZYME"}),
    KeggEntryCardKind.MODULE: frozenset(
        {"DESCRIPTION", "DEFINITION", "ORTHOLOGY", "CLASS", "PATHWAY", "REACTION", "COMPOUND"}
    ),
    KeggEntryCardKind.PATHWAY: frozenset({"MODULE", "ORTHOLOGY", "REACTION", "COMPOUND", "GLYCAN"}),
    KeggEntryCardKind.REACTION: frozenset({"EQUATION", "ENZYME", "ORTHOLOGY", "RCLASS", "COMMENT"}),
    KeggEntryCardKind.ENZYME: frozenset(
        {"SYSNAME", "REACTION", "ALL_REAC", "SUBSTRATE", "PRODUCT", "ORTHOLOGY", "COMMENT"}
    ),
    KeggEntryCardKind.COMPOUND: frozenset(
        {"FORMULA", "EXACT_MASS", "MOL_WEIGHT", "REACTION", "PATHWAY", "ENZYME"}
    ),
    KeggEntryCardKind.GLYCAN: frozenset({"COMPOSITION", "MASS", "REACTION", "PATHWAY", "ENZYME"}),
    KeggEntryCardKind.GENE: frozenset(
        {
            "SYMBOL",
            "ORTHOLOGY",
            "ORGANISM",
            "POSITION",
            "MOTIF",
            "PATHWAY",
            "MODULE",
            "NETWORK",
            "STRUCTURE",
        }
    ),
    KeggEntryCardKind.GENOME: frozenset(
        {
            "ORG_CODE",
            "CATEGORY",
            "ANNOTATION",
            "TAXONOMY",
            "LINEAGE",
            "DATA_SOURCE",
            "ORIGINAL_DB",
            "STATISTICS",
        }
    ),
}


def build_entry_cards(result: GetResult) -> KeggEntryCardSnapshot:
    """Build complete typed cards without performing another KEGG request."""
    supported_requests = tuple((_card_kind(item.database), item) for item in result.request.entries)
    if any(kind is None for kind, _ in supported_requests):
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "The GET result contains an entry type without a typed card projection.",
            suggested_action="Request preview projection for unsupported textual entry types.",
        )

    returned: dict[tuple[KeggGetDatabase, str], KeggEntryCard] = {}
    for document in result.documents:
        if not isinstance(document, KeggFlatFileDocument):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "Typed entry cards require KEGG flat-file documents.",
                suggested_action="Use preview projection for BRITE htext documents.",
            )
        for entry in document.entries:
            matches = tuple(
                requested
                for _, requested in supported_requests
                if get_entry_matches(requested, entry)
            )
            if len(matches) != 1:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "The GET result contains an unexpected or ambiguous flat-file entry.",
                    suggested_action="Refresh the exact database-qualified entries and retry.",
                )
            requested = matches[0]
            key = (requested.database, requested.identifier)
            if key in returned:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "The GET result contains a duplicate entry for typed card parsing.",
                    suggested_action="Refresh the exact database-qualified entries and retry.",
                )
            kind = _card_kind(requested.database)
            if kind is None:  # pragma: no cover - narrowed above
                raise AssertionError("supported card request was narrowed before parsing")
            card_identifier = (
                requested.identifier if kind is KeggEntryCardKind.GENE else entry.identifier
            )
            returned[key] = _parse_entry_card(kind, entry, card_identifier)

    missing_keys = {(item.database, item.identifier) for item in result.missing_entries}
    cards: list[KeggEntryCard] = []
    missing: list[KeggEntryCardEntity] = []
    for kind, requested in supported_requests:
        if kind is None:  # pragma: no cover - narrowed above
            raise AssertionError("supported card request was narrowed before ordering")
        key = (requested.database, requested.identifier)
        card = returned.get(key)
        if card is not None:
            cards.append(card)
        elif key in missing_keys:
            missing.append(KeggEntryCardEntity(database=kind, identifier=requested.identifier))
        else:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The GET result did not account for every requested entry.",
                suggested_action="Refresh the exact database-qualified entries and retry.",
            )
    if len(missing_keys) != len(missing):
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "The GET result reported an unexpected missing entry.",
            suggested_action="Refresh the exact database-qualified entries and retry.",
        )
    return KeggEntryCardSnapshot(
        requested_entries=tuple(
            KeggEntryCardEntity(database=kind, identifier=requested.identifier)
            for kind, requested in supported_requests
            if kind is not None
        ),
        entries=tuple(cards),
        missing_entries=tuple(missing),
        provenance=result.batches,
    )


def entry_card_previews(
    snapshot: KeggEntryCardSnapshot,
    *,
    limit: int = MAX_ENTRY_CARD_PREVIEWS,
) -> KeggEntryCardPreviewSet:
    """Return a compact, list-bounded projection suitable for direct MCP output."""
    if not 0 <= limit <= MAX_ENTRY_CARD_PREVIEWS:
        raise ValueError(f"limit must be between zero and {MAX_ENTRY_CARD_PREVIEWS}")
    cards = snapshot.entries[:limit]
    previews = tuple(_entry_card_preview(card) for card in cards)
    return KeggEntryCardPreviewSet(
        entry_count=len(snapshot.entries),
        previews=previews,
        previews_truncated=len(previews) < len(snapshot.entries),
    )


def _card_kind(database: KeggGetDatabase) -> KeggEntryCardKind | None:
    try:
        return KeggEntryCardKind(database.value)
    except ValueError:
        return None


def _parse_entry_card(
    kind: KeggEntryCardKind,
    entry: KeggFlatFileEntry,
    card_identifier: str,
) -> KeggEntryCard:
    common = _common_card_values(kind, entry, card_identifier)
    if kind is KeggEntryCardKind.KO:
        definition = common["definition"]
        ec_numbers = _ec_numbers(
            (definition,) if isinstance(definition, str) else (),
            _field_lines(entry.fields, "ENZYME"),
        )
        return KoEntryCard(
            **common,
            ec_numbers=ec_numbers,
            modules=_references(entry.fields, "MODULE", r"^M[0-9]{5}$"),
            pathways=_references(entry.fields, "PATHWAY", r"^[a-z][a-z0-9]{1,7}[0-9]{5}$"),
        )
    if kind is KeggEntryCardKind.MODULE:
        definition = common["definition"]
        module_definition = (
            _module_definition_card(definition) if isinstance(definition, str) else None
        )
        return ModuleEntryCard(
            **common,
            module_definition=module_definition,
            pathways=_references(entry.fields, "PATHWAY", r"^[a-z][a-z0-9]{1,7}[0-9]{5}$"),
            reactions=_references(entry.fields, "REACTION", r"^R[0-9]{5}$"),
        )
    if kind is KeggEntryCardKind.PATHWAY:
        return PathwayEntryCard(
            **common,
            ko_identifiers=_reference_identifiers(
                _references(entry.fields, "ORTHOLOGY", r"^K[0-9]{5}$")
            ),
            modules=_references(entry.fields, "MODULE", r"^M[0-9]{5}$"),
            reactions=_references(entry.fields, "REACTION", r"^R[0-9]{5}$"),
            compounds=_references(entry.fields, "COMPOUND", r"^C[0-9]{5}$"),
            glycans=_references(entry.fields, "GLYCAN", r"^G[0-9]{5}$"),
        )
    if kind is KeggEntryCardKind.REACTION:
        equation = _joined_field(entry.fields, "EQUATION")
        return ReactionEntryCard(
            **common,
            equation=equation,
            enzyme_ids=_ec_numbers(_field_lines(entry.fields, "ENZYME")),
            ko_identifiers=_reference_identifiers(
                _references(entry.fields, "ORTHOLOGY", r"^K[0-9]{5}$")
            ),
            rclass_ids=_reference_identifiers(_references(entry.fields, "RCLASS", r"^RC[0-9]{5}$")),
            compound_ids=_regex_identifiers((equation,), _COMPOUND_ID),
            glycan_ids=_regex_identifiers((equation,), _GLYCAN_ID),
        )
    if kind is KeggEntryCardKind.ENZYME:
        reaction_lines = (
            *_field_lines(entry.fields, "REACTION"),
            *_field_lines(entry.fields, "ALL_REAC"),
        )
        return EnzymeEntryCard(
            **common,
            reaction_ids=_regex_identifiers(reaction_lines, _REACTION_ID),
            ko_identifiers=_reference_identifiers(
                _references(entry.fields, "ORTHOLOGY", r"^K[0-9]{5}$")
            ),
        )
    if kind is KeggEntryCardKind.COMPOUND:
        return CompoundEntryCard(
            **common,
            formula=_joined_field(entry.fields, "FORMULA"),
            exact_mass=_joined_field(entry.fields, "EXACT_MASS"),
            molecular_weight=_joined_field(entry.fields, "MOL_WEIGHT"),
            reactions=_references(entry.fields, "REACTION", r"^R[0-9]{5}$"),
            pathways=_references(entry.fields, "PATHWAY", r"^[a-z][a-z0-9]{1,7}[0-9]{5}$"),
        )
    if kind is KeggEntryCardKind.GLYCAN:
        return GlycanEntryCard(
            **common,
            composition=_joined_field(entry.fields, "COMPOSITION"),
            mass=_joined_field(entry.fields, "MASS"),
            reactions=_references(entry.fields, "REACTION", r"^R[0-9]{5}$"),
            pathways=_references(entry.fields, "PATHWAY", r"^[a-z][a-z0-9]{1,7}[0-9]{5}$"),
        )
    if kind is KeggEntryCardKind.GENE:
        organism = _joined_field(entry.fields, "ORGANISM")
        organism_code: str | None = None
        organism_name: str | None = None
        if organism:
            organism_code, _, organism_name_value = organism.partition(" ")
            organism_name = organism_name_value.strip() or None
        return GeneEntryCard(
            **common,
            organism_code=organism_code,
            organism_name=organism_name,
            position=_joined_field(entry.fields, "POSITION"),
            orthology=_references(entry.fields, "ORTHOLOGY", r"^K[0-9]{5}$"),
            pathways=_references(entry.fields, "PATHWAY", r"^[a-z][a-z0-9]{1,7}[0-9]{5}$"),
        )
    if kind is KeggEntryCardKind.GENOME:
        taxonomy = _joined_field(entry.fields, "TAXONOMY")
        taxonomy_match = _TAXONOMY_ID.fullmatch(taxonomy or "")
        lineage_text = _joined_field(entry.fields, "LINEAGE")
        lineage = (
            _bounded_unique(
                (item.strip() for item in lineage_text.split(";") if item.strip()),
                MAX_ENTRY_CARD_ITEMS,
            )
            if lineage_text
            else ()
        )
        return GenomeEntryCard(
            **common,
            organism_code=_first_token(_joined_field(entry.fields, "ORG_CODE")),
            taxonomy_id=f"taxid:{taxonomy_match.group(1)}" if taxonomy_match else None,
            lineage=lineage,
        )
    raise AssertionError("unknown entry-card kind")  # pragma: no cover


def _common_card_values(
    kind: KeggEntryCardKind,
    entry: KeggFlatFileEntry,
    card_identifier: str,
) -> _CommonCardValues:
    names = _names(entry.fields)
    classes = _bounded_unique(_field_lines(entry.fields, "CLASS"), MAX_ENTRY_CARD_ITEMS)
    parsed_fields = _COMMON_FIELDS | _KIND_FIELDS[kind]
    unknown_names = _bounded_unique(
        (field.name for field in entry.fields if field.name not in parsed_fields),
        MAX_ENTRY_CARD_ITEMS,
    )
    return {
        "entity": KeggEntryCardEntity(database=kind, identifier=card_identifier),
        "names": names,
        "definition": _joined_field(entry.fields, "DEFINITION"),
        "classes": classes,
        "dblinks": _dblinks(entry.fields),
        "pubmed_ids": _pubmed_ids(entry.fields),
        "unparsed_field_names": unknown_names,
    }


def _module_definition_card(definition: str) -> KeggModuleDefinitionCard:
    parsed = parse_module_definition(definition)
    required_blocks: tuple[str, ...] = ()
    optional_components: tuple[str, ...] = ()
    if parsed.ast is not None:
        _require_item_bound(len(parsed.ast.required_blocks), "MODULE required blocks")
        required_blocks = tuple(
            definition[item.span.start_offset : item.span.end_offset]
            for item in parsed.ast.required_blocks
        )
        optional_components = _module_optional_components(definition, parsed.ast.required_blocks)
    return KeggModuleDefinitionCard(
        raw_definition=definition,
        is_valid=parsed.is_valid,
        required_blocks=required_blocks,
        optional_components=optional_components,
        referenced_modules=_bounded_unique(
            (
                token.lexeme
                for token in parsed.tokens
                if token.kind is ModuleTokenKind.MODULE_REFERENCE
            ),
            MAX_ENTRY_CARD_ITEMS,
        ),
        ko_components=_bounded_unique(
            (token.lexeme for token in parsed.tokens if token.kind is ModuleTokenKind.KO),
            MAX_ENTRY_CARD_ITEMS,
        ),
        diagnostic_codes=_bounded_unique(
            (item.code.value for item in parsed.diagnostics), MAX_ENTRY_CARD_ITEMS
        ),
    )


def _module_optional_components(
    definition: str,
    roots: tuple[ModuleExpression, ...],
) -> tuple[str, ...]:
    pending = list(reversed(roots))
    components: list[str] = []
    while pending:
        expression = pending.pop()
        if expression.kind is ModuleExpressionKind.OPTIONAL:
            components.append(definition[expression.span.start_offset : expression.span.end_offset])
            _require_item_bound(len(components), "MODULE optional components")
        pending.extend(reversed(expression.children))
    return tuple(components)


def _entry_card_preview(card: KeggEntryCard) -> KeggEntryCardPreview:
    definition = card.definition
    shown_definition = definition[:MAX_ENTRY_CARD_PREVIEW_TEXT_CHARACTERS] if definition else None
    count_fields = _card_count_fields(card)
    shown_counts = count_fields[:MAX_ENTRY_CARD_PREVIEW_FIELD_COUNTS]
    full_primary_name = card.names[0] if card.names else None
    primary_name = (
        full_primary_name[:MAX_ENTRY_CARD_PREVIEW_TEXT_CHARACTERS] if full_primary_name else None
    )
    class_preview = tuple(
        item[:MAX_ENTRY_CARD_PREVIEW_TEXT_CHARACTERS]
        for item in card.classes[:MAX_ENTRY_CARD_PREVIEW_CLASSES]
    )
    return KeggEntryCardPreview(
        kind=card.kind,
        entity=card.entity,
        primary_name=primary_name,
        primary_name_truncated=bool(full_primary_name and primary_name != full_primary_name),
        definition_preview=shown_definition,
        definition_truncated=bool(definition and shown_definition != definition),
        class_preview=class_preview,
        classes_truncated=len(card.classes) > len(class_preview),
        class_text_truncated=any(
            shown != full for shown, full in zip(class_preview, card.classes, strict=False)
        ),
        pubmed_count=len(card.pubmed_ids),
        selected_field_counts=shown_counts,
        card_fields_truncated=len(count_fields) > len(shown_counts),
    )


def _card_count_fields(card: KeggEntryCard) -> tuple[KeggEntryCardFieldCount, ...]:
    pairs: tuple[tuple[str, int], ...]
    if isinstance(card, KoEntryCard):
        pairs = (
            ("ec_numbers", len(card.ec_numbers)),
            ("modules", len(card.modules)),
            ("pathways", len(card.pathways)),
        )
    elif isinstance(card, ModuleEntryCard):
        definition = card.module_definition
        pairs = (
            (
                "required_blocks",
                len(definition.required_blocks) if definition is not None else 0,
            ),
            (
                "optional_components",
                len(definition.optional_components) if definition is not None else 0,
            ),
            ("pathways", len(card.pathways)),
            ("reactions", len(card.reactions)),
        )
    elif isinstance(card, PathwayEntryCard):
        pairs = (
            ("ko_identifiers", len(card.ko_identifiers)),
            ("modules", len(card.modules)),
            ("reactions", len(card.reactions)),
            ("compounds", len(card.compounds)),
            ("glycans", len(card.glycans)),
        )
    elif isinstance(card, ReactionEntryCard):
        pairs = (
            ("enzyme_ids", len(card.enzyme_ids)),
            ("ko_identifiers", len(card.ko_identifiers)),
            ("rclass_ids", len(card.rclass_ids)),
            ("compound_ids", len(card.compound_ids)),
            ("glycan_ids", len(card.glycan_ids)),
        )
    elif isinstance(card, EnzymeEntryCard):
        pairs = (
            ("reaction_ids", len(card.reaction_ids)),
            ("ko_identifiers", len(card.ko_identifiers)),
        )
    elif isinstance(card, (CompoundEntryCard, GlycanEntryCard)):
        pairs = (
            ("reactions", len(card.reactions)),
            ("pathways", len(card.pathways)),
        )
    elif isinstance(card, GeneEntryCard):
        pairs = (
            ("orthology", len(card.orthology)),
            ("pathways", len(card.pathways)),
        )
    else:
        pairs = (("lineage", len(card.lineage)),)
    return tuple(KeggEntryCardFieldCount(field=field, count=count) for field, count in pairs)


def _names(fields: tuple[KeggFlatFileField, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for line in _field_lines(fields, "NAME"):
        values.extend(item.strip() for item in line.split(";") if item.strip())
    return _bounded_unique(values, MAX_ENTRY_CARD_NAMES)


def _joined_field(fields: tuple[KeggFlatFileField, ...], field_name: str) -> str | None:
    lines = _field_lines(fields, field_name)
    if not lines:
        return None
    value = " ".join(lines).strip()
    _require_text_bound(value, field_name)
    return value or None


def _field_lines(
    fields: tuple[KeggFlatFileField, ...],
    field_name: str,
) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for field in fields
        if field.indent_columns == 0 and field.name == field_name
        for line in field.value_lines
        if line.strip()
    )


def _references(
    fields: tuple[KeggFlatFileField, ...],
    field_name: str,
    identifier_pattern: str,
) -> tuple[KeggEntryCardReference, ...]:
    pattern = re.compile(identifier_pattern)
    references: list[KeggEntryCardReference] = []
    observed: set[str] = set()
    for line in _field_lines(fields, field_name):
        identifiers: list[str] = []
        for token in re.split(r"[\s,;+]+", line):
            normalized = token.strip()
            if not normalized:
                continue
            if pattern.fullmatch(normalized) is None:
                break
            identifiers.append(normalized)
        if not identifiers:
            continue
        normalized_label = line[len(identifiers[0]) :].strip() if len(identifiers) == 1 else ""
        _require_text_bound(normalized_label, f"{field_name} label")
        for identifier in identifiers:
            if identifier in observed:
                continue
            observed.add(identifier)
            references.append(
                KeggEntryCardReference(
                    identifier=identifier,
                    label=(normalized_label or None),
                )
            )
            _require_item_bound(len(references), f"{field_name} references")
    return tuple(references)


def _reference_identifiers(
    references: tuple[KeggEntryCardReference, ...],
) -> tuple[str, ...]:
    return tuple(item.identifier for item in references)


def _dblinks(fields: tuple[KeggFlatFileField, ...]) -> tuple[KeggEntryCardDbLink, ...]:
    grouped: dict[str, list[str]] = {}
    for line in _field_lines(fields, "DBLINKS"):
        database, separator, identifier_text = line.partition(":")
        database = database.strip()
        if not separator or not database or len(database) > 128:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A KEGG DBLINKS line cannot be represented as a typed cross-reference.",
                suggested_action=(
                    "Use preview projection and inspect the retained flat-file detail."
                ),
            )
        if database not in grouped:
            _require_item_bound(len(grouped) + 1, "DBLINK namespaces")
        target = grouped.setdefault(database, [])
        for identifier in identifier_text.split():
            normalized = identifier.strip().rstrip(",;")
            if normalized and normalized not in target:
                _require_text_bound(normalized, "DBLINK identifier")
                target.append(normalized)
                _require_item_bound(len(target), "DBLINK identifiers")
    return tuple(
        KeggEntryCardDbLink(database=database, identifiers=tuple(identifiers))
        for database, identifiers in grouped.items()
        if identifiers
    )


def _pubmed_ids(fields: tuple[KeggFlatFileField, ...]) -> tuple[str, ...]:
    values = (
        match.group(1)
        for field in fields
        if field.name == "REFERENCE"
        for line in field.value_lines
        for match in _PMID.finditer(line)
    )
    return _bounded_unique(values, MAX_ENTRY_CARD_ITEMS)


def _ec_numbers(*parts: Iterable[str]) -> tuple[str, ...]:
    values: list[str] = []
    for part in parts:
        for text in part:
            for block in _EC_BLOCK.findall(text):
                values.extend(block.split())
            values.extend(text.replace(";", " ").split())
    return _bounded_unique(
        (value.strip("[],;") for value in values if _EC_NUMBER.fullmatch(value.strip("[],;"))),
        MAX_ENTRY_CARD_ITEMS,
    )


def _regex_identifiers(
    values: Iterable[str | None],
    pattern: re.Pattern[str],
) -> tuple[str, ...]:
    return _bounded_unique(
        (match.group(0) for value in values if value for match in pattern.finditer(value)),
        MAX_ENTRY_CARD_ITEMS,
    )


def _bounded_unique(values: Iterable[str], limit: int) -> tuple[str, ...]:
    output: list[str] = []
    observed: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in observed:
            continue
        _require_text_bound(normalized, "entry-card field")
        observed.add(normalized)
        output.append(normalized)
        if len(output) > limit:
            fail(
                ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                "A typed KEGG entry-card field exceeds its item bound.",
                suggested_action=(
                    "Use preview projection and inspect the retained flat-file detail."
                ),
            )
    return tuple(output)


def _require_text_bound(value: str, field_name: str) -> None:
    if len(value) > MAX_ENTRY_CARD_TEXT_CHARACTERS:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            f"The typed KEGG {field_name} field exceeds the entry-card text bound.",
            suggested_action="Use preview projection and inspect the retained flat-file detail.",
        )


def _require_item_bound(count: int, field_name: str) -> None:
    limit = (
        MAX_ENTRY_CARD_DBLINK_GROUPS if field_name == "DBLINK namespaces" else MAX_ENTRY_CARD_ITEMS
    )
    if count > limit:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            f"The typed KEGG {field_name} field exceeds the entry-card item bound.",
            suggested_action="Use preview projection and inspect the retained flat-file detail.",
        )


def _first_token(value: str | None) -> str | None:
    return value.split(maxsplit=1)[0] if value else None


__all__ = [
    "ENTRY_CARD_DATABASES",
    "ENTRY_CARD_PARSER_NAME",
    "ENTRY_CARD_PARSER_VERSION",
    "ENTRY_CARD_SCHEMA_VERSION",
    "ENTRY_CARD_SNAPSHOT_SECTION",
    "CompoundEntryCard",
    "EnzymeEntryCard",
    "GeneEntryCard",
    "GenomeEntryCard",
    "GlycanEntryCard",
    "KeggEntryCard",
    "KeggEntryCardDbLink",
    "KeggEntryCardEntity",
    "KeggEntryCardFieldCount",
    "KeggEntryCardKind",
    "KeggEntryCardPreview",
    "KeggEntryCardPreviewSet",
    "KeggEntryCardReference",
    "KeggEntryCardSnapshot",
    "KeggModuleDefinitionCard",
    "KoEntryCard",
    "ModuleEntryCard",
    "PathwayEntryCard",
    "ReactionEntryCard",
    "build_entry_cards",
    "entry_card_previews",
]
