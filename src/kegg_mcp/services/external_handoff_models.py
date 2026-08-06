"""Strict request and result contracts for KEGG web-tool input handoffs."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from kegg_mcp.domain.annotations import FrozenModel, KNumber, validate_utf8_text
from kegg_mcp.kegg.contracts import (
    is_ec_number,
    is_kegg_gene_identifier,
    is_kegg_organism_code,
)
from kegg_mcp.services.output_bundle import OutputBundleArtifact

EXTERNAL_HANDOFF_SCHEMA_VERSION = "1"
MAX_EXTERNAL_HANDOFF_ITEMS = 10_000
MAX_EXTERNAL_HANDOFF_DATA_BYTES = 2_000_000

_NUMBERED_REFERENCE_ID = re.compile(r"^(?:K|R|RC|C|G|D|H)[0-9]{5}$")
_NUMBERED_COLOR_ID = re.compile(r"^(?:K|R|C|G|D)[0-9]{5}$")
_NUMBERED_BRITE_JOIN_ID = re.compile(r"^(?:C|G|R|D|H)[0-9]{5}$")
_COMPOUND_ID = re.compile(r"^C[0-9]{5}$")
_FORMULA = re.compile(r"^(?:[A-Z][a-z]?(?:[1-9][0-9]*)?)+$")
_EXACT_MASS = re.compile(r"^(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,12})?$")
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_NAMED_COLOR = re.compile(r"^[A-Za-z]{3,20}$")
_FORMAT_BREAKING_UNICODE = frozenset({"\u0085", "\u2028", "\u2029"})

HandoffItems = Annotated[
    tuple[Annotated[str, Field(min_length=1, max_length=128)], ...],
    Field(min_length=1, max_length=MAX_EXTERNAL_HANDOFF_ITEMS),
]


class ExternalHandoffTarget(StrEnum):
    """Allowlisted KEGG web-tool input formats."""

    MAPPER_RECONSTRUCT = "mapper_reconstruct"
    MAPPER_SEARCH = "mapper_search"
    MAPPER_COLOR = "mapper_color"
    MAPPER_JOIN = "mapper_join"
    MAPPER_MWSEARCH = "mapper_mwsearch"
    SYNTAX_KO_COMPOSITION = "syntax_ko_composition"
    SYNTAX_KO_SEQUENCE = "syntax_ko_sequence"


class MapperSearchScope(StrEnum):
    """KEGG Mapper Search/Color database scope selected by the user."""

    REFERENCE = "reference"
    HSA = "hsa"
    ORGANISM = "organism"


class MapperJoinMode(StrEnum):
    """KEGG Mapper Join hierarchy namespace."""

    BR = "br"
    KO = "ko"


class MapperMwsearchMode(StrEnum):
    """One mutually exclusive KEGG Mapper MWsearch input type."""

    FORMULA = "formula"
    EXACT_MASS = "exact_mass"
    C_NUMBER = "c_number"


def _validate_cell(value: str, *, field_name: str) -> str:
    validate_utf8_text(value, field_name=field_name)
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    if any(
        ord(character) < 32 or 127 <= ord(character) <= 159 or character in _FORMAT_BREAKING_UNICODE
        for character in value
    ):
        raise ValueError(f"{field_name} must not contain control or line-separator characters")
    return value


def _validate_token(value: str, *, field_name: str) -> str:
    validated = _validate_cell(value, field_name=field_name)
    if any(character.isspace() for character in validated):
        raise ValueError(f"{field_name} must not contain whitespace")
    return validated


def _is_ec_identifier(value: str) -> bool:
    return is_ec_number(value.removeprefix("ec:"))


def _validate_scope(
    scope: MapperSearchScope,
    organism: str | None,
) -> None:
    if scope is MapperSearchScope.ORGANISM:
        if organism is None or not is_kegg_organism_code(organism):
            raise ValueError("organism scope requires one canonical KEGG organism code")
    elif organism is not None:
        raise ValueError("organism is valid only with organism scope")


def _valid_search_identifier(
    value: str,
    *,
    scope: MapperSearchScope,
    organism: str | None,
) -> bool:
    if scope is MapperSearchScope.REFERENCE:
        return (
            _NUMBERED_REFERENCE_ID.fullmatch(value) is not None
            or _is_ec_identifier(value)
            or is_kegg_organism_code(value)
        )
    if re.fullmatch(r"(?:K|C|G|D)[0-9]{5}", value) is not None or _is_ec_identifier(value):
        return True
    if not is_kegg_gene_identifier(value):
        return False
    expected_prefix = "hsa" if scope is MapperSearchScope.HSA else organism
    return value.partition(":")[0] == expected_prefix


def _valid_color_identifier(
    value: str,
    *,
    scope: MapperSearchScope,
    organism: str | None,
) -> bool:
    if scope is MapperSearchScope.REFERENCE:
        return _NUMBERED_COLOR_ID.fullmatch(value) is not None or _is_ec_identifier(value)
    if re.fullmatch(r"(?:K|C|G|D)[0-9]{5}", value) is not None or _is_ec_identifier(value):
        return True
    if not is_kegg_gene_identifier(value):
        return False
    expected_prefix = "hsa" if scope is MapperSearchScope.HSA else organism
    return value.partition(":")[0] == expected_prefix


class MapperReconstructRow(FrozenModel):
    """One optional caller label and one canonical K number."""

    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    ko_id: KNumber

    @field_validator("user_id")
    @classmethod
    def validate_user_id(cls, value: str | None) -> str | None:
        validated = (
            None
            if value is None
            else _validate_token(value, field_name="Mapper Reconstruct user ID")
        )
        if validated is not None and validated.startswith("#"):
            raise ValueError("Mapper Reconstruct user IDs must not use the reserved comment prefix")
        return validated


class MapperReconstructRequest(FrozenModel):
    """Prepare KEGG Mapper Reconstruct rows in caller order."""

    # One rows collection is the complete unannotated data block; FrozenModel rejects extras.
    target: Literal[ExternalHandoffTarget.MAPPER_RECONSTRUCT]
    rows: Annotated[
        tuple[MapperReconstructRow, ...],
        Field(min_length=1, max_length=MAX_EXTERNAL_HANDOFF_ITEMS),
    ]

    @model_validator(mode="after")
    def reject_exact_duplicate_rows(self) -> Self:
        keys = tuple((row.user_id, row.ko_id) for row in self.rows)
        if len(keys) != len(set(keys)):
            raise ValueError("Mapper Reconstruct rows must not contain exact duplicates")
        return self


class MapperSearchRequest(FrozenModel):
    """Prepare bounded KEGG identifiers for Mapper Search."""

    target: Literal[ExternalHandoffTarget.MAPPER_SEARCH]
    scope: MapperSearchScope = MapperSearchScope.REFERENCE
    organism: str | None = Field(default=None, min_length=3, max_length=4)
    identifiers: HandoffItems

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(
            _validate_token(value, field_name="Mapper Search identifier") for value in values
        )

    @model_validator(mode="after")
    def validate_scope_and_identifiers(self) -> Self:
        _validate_scope(self.scope, self.organism)
        if len(self.identifiers) != len(set(self.identifiers)):
            raise ValueError("Mapper Search identifiers must be unique")
        if not all(
            _valid_search_identifier(value, scope=self.scope, organism=self.organism)
            for value in self.identifiers
        ):
            raise ValueError("identifier is incompatible with the selected Mapper Search scope")
        return self


class MapperColorRow(FrozenModel):
    """One KEGG identifier and one bounded pathway color specification."""

    identifier: str = Field(min_length=1, max_length=128)
    background_color: str | None = Field(default=None, min_length=3, max_length=20)
    foreground_color: str | None = Field(default=None, min_length=3, max_length=20)

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _validate_token(value, field_name="Mapper Color identifier")

    @field_validator("background_color", "foreground_color")
    @classmethod
    def validate_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _HEX_COLOR.fullmatch(value) is None and _NAMED_COLOR.fullmatch(value) is None:
            raise ValueError("colors must be #RRGGBB values or 3-20 letter ASCII names")
        return value

    @model_validator(mode="after")
    def require_at_least_one_color(self) -> Self:
        if self.background_color is None and self.foreground_color is None:
            raise ValueError("Mapper Color rows require a background or foreground color")
        return self


class MapperColorRequest(FrozenModel):
    """Prepare KEGG Mapper Color rows without running pathway mapping."""

    target: Literal[ExternalHandoffTarget.MAPPER_COLOR]
    scope: MapperSearchScope = MapperSearchScope.REFERENCE
    organism: str | None = Field(default=None, min_length=3, max_length=4)
    rows: Annotated[
        tuple[MapperColorRow, ...],
        Field(min_length=1, max_length=MAX_EXTERNAL_HANDOFF_ITEMS),
    ]

    @model_validator(mode="after")
    def validate_scope_and_rows(self) -> Self:
        _validate_scope(self.scope, self.organism)
        keys = tuple(
            (row.identifier, row.background_color, row.foreground_color) for row in self.rows
        )
        if len(keys) != len(set(keys)):
            raise ValueError("Mapper Color rows must not contain exact duplicates")
        if not all(
            _valid_color_identifier(row.identifier, scope=self.scope, organism=self.organism)
            for row in self.rows
        ):
            raise ValueError("identifier is incompatible with the selected Mapper Color scope")
        return self


class MapperJoinRow(FrozenModel):
    """One KEGG identifier and one user-supplied BRITE attribute."""

    identifier: str = Field(min_length=1, max_length=128)
    attribute: str = Field(min_length=1, max_length=1_000)

    @field_validator("identifier")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _validate_token(value, field_name="Mapper Join identifier")

    @field_validator("attribute")
    @classmethod
    def validate_attribute(cls, value: str) -> str:
        return _validate_cell(value, field_name="Mapper Join attribute")


class MapperJoinRequest(FrozenModel):
    """Prepare a bounded KEGG Mapper Join binary-relation file."""

    target: Literal[ExternalHandoffTarget.MAPPER_JOIN]
    mode: MapperJoinMode
    rows: Annotated[
        tuple[MapperJoinRow, ...],
        Field(min_length=1, max_length=MAX_EXTERNAL_HANDOFF_ITEMS),
    ]

    @model_validator(mode="after")
    def validate_mode_and_rows(self) -> Self:
        keys = tuple((row.identifier, row.attribute) for row in self.rows)
        if len(keys) != len(set(keys)):
            raise ValueError("Mapper Join rows must not contain exact duplicates")
        if self.mode is MapperJoinMode.KO:
            valid = all(re.fullmatch(r"K[0-9]{5}", row.identifier) for row in self.rows)
        else:
            valid = all(
                _NUMBERED_BRITE_JOIN_ID.fullmatch(row.identifier) is not None
                or is_kegg_organism_code(row.identifier)
                for row in self.rows
            )
        if not valid:
            raise ValueError("identifier is incompatible with the selected Mapper Join mode")
        return self


class MapperMwsearchRequest(FrozenModel):
    """Prepare one homogeneous KEGG Mapper MWsearch dataset."""

    target: Literal[ExternalHandoffTarget.MAPPER_MWSEARCH]
    mode: MapperMwsearchMode
    values: HandoffItems

    @field_validator("values")
    @classmethod
    def validate_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_token(value, field_name="Mapper MWsearch value") for value in values)

    @model_validator(mode="after")
    def validate_mode_and_values(self) -> Self:
        if len(self.values) != len(set(self.values)):
            raise ValueError("Mapper MWsearch values must be unique")
        if self.mode is MapperMwsearchMode.FORMULA:
            valid = all(_FORMULA.fullmatch(value) is not None for value in self.values)
        elif self.mode is MapperMwsearchMode.C_NUMBER:
            valid = all(_COMPOUND_ID.fullmatch(value) is not None for value in self.values)
        else:
            valid = all(_valid_positive_exact_mass(value) for value in self.values)
        if not valid:
            raise ValueError("value is incompatible with the selected Mapper MWsearch mode")
        return self


def _valid_positive_exact_mass(value: str) -> bool:
    if _EXACT_MASS.fullmatch(value) is None:
        return False
    try:
        return Decimal(value) > 0
    except InvalidOperation:  # pragma: no cover - regex already excludes invalid decimals
        return False


class SyntaxKoCompositionRequest(FrozenModel):
    """Prepare a unique KO set for KEGG Syntax KO composition analysis."""

    target: Literal[ExternalHandoffTarget.SYNTAX_KO_COMPOSITION]
    ko_ids: Annotated[
        tuple[KNumber, ...],
        Field(min_length=1, max_length=MAX_EXTERNAL_HANDOFF_ITEMS),
    ]

    @model_validator(mode="after")
    def require_unique_ko_ids(self) -> Self:
        if len(self.ko_ids) != len(set(self.ko_ids)):
            raise ValueError("Syntax KO composition identifiers must be unique")
        return self


class SyntaxKoSequenceRow(FrozenModel):
    """One caller-ordered genome gene identifier and its assigned K number."""

    gene_id: str = Field(min_length=1, max_length=128)
    ko_id: KNumber

    @field_validator("gene_id")
    @classmethod
    def validate_gene_id(cls, value: str) -> str:
        return _validate_token(value, field_name="Syntax KO sequence gene ID")


class SyntaxKoSequenceRequest(FrozenModel):
    """Prepare caller-confirmed genomic gene order without inferring coordinates."""

    target: Literal[ExternalHandoffTarget.SYNTAX_KO_SEQUENCE]
    order_semantics: Literal["caller_supplied_genomic_order"]
    rows: Annotated[
        tuple[SyntaxKoSequenceRow, ...],
        Field(min_length=1, max_length=MAX_EXTERNAL_HANDOFF_ITEMS),
    ]

    @model_validator(mode="after")
    def require_unique_gene_ids(self) -> Self:
        gene_ids = tuple(row.gene_id for row in self.rows)
        if len(gene_ids) != len(set(gene_ids)):
            raise ValueError("Syntax KO sequence gene IDs must be unique")
        return self


ExternalHandoffRequest = Annotated[
    MapperReconstructRequest
    | MapperSearchRequest
    | MapperColorRequest
    | MapperJoinRequest
    | MapperMwsearchRequest
    | SyntaxKoCompositionRequest
    | SyntaxKoSequenceRequest,
    Field(discriminator="target"),
]


class ExternalHandoffBundle(FrozenModel):
    """Stable paths and bounded summary for one local external-tool input bundle."""

    schema_version: Literal["1"]
    target: ExternalHandoffTarget
    output_directory: str = Field(min_length=1, max_length=4_096)
    data_file: str = Field(min_length=1, max_length=4_096)
    manifest: str = Field(min_length=1, max_length=4_096)
    item_count: int = Field(strict=True, ge=1, le=MAX_EXTERNAL_HANDOFF_ITEMS)
    data_byte_size: int = Field(strict=True, ge=1, le=MAX_EXTERNAL_HANDOFF_DATA_BYTES)
    artifacts: Annotated[
        tuple[OutputBundleArtifact, ...],
        Field(min_length=2, max_length=2),
    ]


__all__ = (
    "EXTERNAL_HANDOFF_SCHEMA_VERSION",
    "MAX_EXTERNAL_HANDOFF_DATA_BYTES",
    "MAX_EXTERNAL_HANDOFF_ITEMS",
    "ExternalHandoffBundle",
    "ExternalHandoffRequest",
    "ExternalHandoffTarget",
    "MapperColorRequest",
    "MapperColorRow",
    "MapperJoinMode",
    "MapperJoinRequest",
    "MapperJoinRow",
    "MapperMwsearchMode",
    "MapperMwsearchRequest",
    "MapperReconstructRequest",
    "MapperReconstructRow",
    "MapperSearchRequest",
    "MapperSearchScope",
    "SyntaxKoCompositionRequest",
    "SyntaxKoSequenceRequest",
    "SyntaxKoSequenceRow",
)
