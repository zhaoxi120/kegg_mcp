"""Typed public contracts for bounded KEGG retrieval and local caching."""

import ipaddress
import os
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit, urlunsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from kegg_mcp.domain.annotations import JSON_SCHEMA_DIALECT, FrozenModel, validate_utf8_text

PUBLIC_KEGG_ENDPOINT = "https://rest.kegg.jp"
PUBLIC_KEGG_ENDPOINT_LABEL = "public-academic"
PARSER_VERSION = "3"
MAX_GET_ENTRIES_PER_BATCH = 10
MAX_CONFIGURED_IDENTIFIERS = 1_000

PositiveCount = Annotated[int, Field(strict=True, gt=0)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
KeggIdentifier = Annotated[str, Field(min_length=1, max_length=256)]


def default_cache_path() -> str:
    """Return the user-local default cache path without creating it."""
    cache_home = os.environ.get("XDG_CACHE_HOME")
    root = Path(cache_home).expanduser() if cache_home else Path.home() / ".cache"
    return str(root / "kegg-mcp" / "kegg.sqlite3")


class AccessMode(StrEnum):
    """Current client access mode."""

    PUBLIC_ACADEMIC = "public_academic"
    LICENSED = "licensed"
    OFFLINE_CACHE = "offline_cache"


class RetrievalEndpointClass(StrEnum):
    """Class of endpoint that originally produced a cached response."""

    PUBLIC_ACADEMIC = "public_academic"
    LICENSED = "licensed"


class PublicAcademicAccess(FrozenModel):
    """Explicit operator confirmation for the academic public API."""

    mode: Literal[AccessMode.PUBLIC_ACADEMIC] = AccessMode.PUBLIC_ACADEMIC
    academic_use_confirmed: Literal[True]
    endpoint: Literal["https://rest.kegg.jp"] = PUBLIC_KEGG_ENDPOINT


class LicensedAccess(FrozenModel):
    """Explicit operator confirmation for an authorized HTTPS endpoint."""

    mode: Literal[AccessMode.LICENSED] = AccessMode.LICENSED
    authorized_use_confirmed: Literal[True]
    endpoint: str = Field(min_length=1, max_length=2_048)
    endpoint_label: str = Field(min_length=1, max_length=100)

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        validate_utf8_text(value, field_name="endpoint")
        if (
            value != value.strip()
            or "%" in value
            or "\\" in value
            or any(ord(character) < 33 or ord(character) > 126 for character in value)
        ):
            raise ValueError("licensed endpoint must be a canonical HTTPS URL")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as error:
            raise ValueError("licensed endpoint contains an invalid network authority") from error
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("licensed endpoint must use HTTPS and include a hostname")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("licensed endpoint must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("licensed endpoint must not contain query parameters or fragments")
        hostname = parsed.hostname.lower().rstrip(".")
        if not hostname:
            raise ValueError("licensed endpoint must include a valid hostname")
        if hostname == "rest.kegg.jp":
            raise ValueError("the public KEGG endpoint requires public_academic access mode")
        if parsed.netloc.endswith(":"):
            raise ValueError("licensed endpoint contains an invalid network authority")
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("licensed endpoint contains an invalid network port")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            labels = hostname.split(".")
            if any(
                not label
                or len(label) > 63
                or re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label) is None
                for label in labels
            ):
                raise ValueError("licensed endpoint must include a canonical hostname") from None
            canonical_host = hostname
        else:
            canonical_host = address.compressed
        if ":" in canonical_host:
            canonical_host = f"[{canonical_host}]"
        authority = canonical_host
        if port is not None and port != 443:
            authority = f"{authority}:{port}"
        segments = parsed.path.replace("\\", "/").split("/")
        if any(segment in {".", ".."} for segment in segments):
            raise ValueError("licensed endpoint must not contain traversal segments")
        normalized_path = parsed.path.rstrip("/")
        return urlunsplit(("https", authority, normalized_path, "", ""))

    @field_validator("endpoint_label")
    @classmethod
    def validate_endpoint_label(cls, value: str) -> str:
        normalized = value.strip()
        validate_utf8_text(normalized, field_name="endpoint_label")
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127 for character in normalized
        ):
            raise ValueError("endpoint_label must be a safe non-empty logical label")
        return normalized


class OfflineCacheAccess(FrozenModel):
    """Network-disabled access to one explicitly selected cache namespace."""

    mode: Literal[AccessMode.OFFLINE_CACHE] = AccessMode.OFFLINE_CACHE
    retrieval_endpoint_class: RetrievalEndpointClass = RetrievalEndpointClass.PUBLIC_ACADEMIC
    endpoint_label: str = Field(default=PUBLIC_KEGG_ENDPOINT_LABEL, min_length=1, max_length=100)

    @field_validator("endpoint_label")
    @classmethod
    def validate_endpoint_label(cls, value: str) -> str:
        return LicensedAccess.validate_endpoint_label(value)

    @model_validator(mode="after")
    def require_public_namespace_consistency(self) -> Self:
        if (
            self.retrieval_endpoint_class is RetrievalEndpointClass.PUBLIC_ACADEMIC
            and self.endpoint_label != PUBLIC_KEGG_ENDPOINT_LABEL
        ):
            raise ValueError("public cache access requires the public-academic endpoint label")
        return self


KeggAccess = Annotated[
    PublicAcademicAccess | LicensedAccess | OfflineCacheAccess,
    Field(discriminator="mode"),
]


class KeggClientLimits(FrozenModel):
    """Hard client bounds and a safe no-burst request rate."""

    requests_per_second: float = Field(default=2.0, strict=True, gt=0.0, le=3.0)
    timeout_seconds: float = Field(default=15.0, strict=True, gt=0.0, le=120.0)
    max_response_bytes: int = Field(default=5_000_000, strict=True, gt=0, le=50_000_000)
    max_identifiers: int = Field(
        default=100,
        strict=True,
        gt=0,
        le=MAX_CONFIGURED_IDENTIFIERS,
    )
    relation_batch_size: int = Field(default=10, strict=True, gt=0, le=100)
    max_url_bytes: int = Field(default=8_192, strict=True, ge=256, le=65_536)


class RetryPolicy(FrozenModel):
    """Bounded project retry policy for transient transport failures."""

    max_retries: int = Field(default=2, strict=True, ge=0, le=5)
    initial_backoff_seconds: float = Field(default=0.5, strict=True, ge=0.0, le=30.0)
    max_backoff_seconds: float = Field(default=8.0, strict=True, ge=0.0, le=60.0)
    jitter_seconds: float = Field(default=0.25, strict=True, ge=0.0, le=5.0)

    @model_validator(mode="after")
    def validate_backoff_order(self) -> Self:
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must not be below initial_backoff_seconds")
        return self


class CachePolicy(FrozenModel):
    """Local SQLite cache location and freshness policy."""

    path: str = Field(default_factory=default_cache_path, min_length=1, max_length=4_096)
    ttl_seconds: int = Field(default=604_800, strict=True, gt=0, le=31_536_000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        validate_utf8_text(value, field_name="cache path")
        if "\x00" in value:
            raise ValueError("cache path must not contain NUL characters")
        return value


class KeggClientConfig(FrozenModel):
    """Serializable configuration for the KEGG client layer."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:kegg-client-config:1",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    access: KeggAccess = Field(default_factory=OfflineCacheAccess)
    limits: KeggClientLimits = Field(default_factory=KeggClientLimits)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    cache: CachePolicy = Field(default_factory=CachePolicy)


class KeggOperation(StrEnum):
    """Typed operations implemented in Milestone 2."""

    INFO = "info"
    GET = "get"
    LINK = "link"
    CONV = "conv"


class KeggInfoDatabase(StrEnum):
    """Bounded INFO database allowlist."""

    KEGG = "kegg"
    PATHWAY = "pathway"
    BRITE = "brite"
    MODULE = "module"
    KO = "ko"
    GENES = "genes"
    GENOME = "genome"
    COMPOUND = "compound"
    REACTION = "reaction"
    ENZYME = "enzyme"


class KeggGetDatabase(StrEnum):
    """Textual GET databases supported by the MVP."""

    KO = "ko"
    MODULE = "module"
    PATHWAY = "pathway"
    REACTION = "reaction"
    ENZYME = "enzyme"
    COMPOUND = "compound"
    BRITE = "brite"


class KeggBriteEntryKind(StrEnum):
    """BRITE content kinds explicitly supported by typed GET parsing."""

    HIERARCHY = "hierarchy"


_KEGG_ORGANISM_CODE = re.compile(r"^[a-z]{3,4}$")
_KEGG_T_NUMBER = re.compile(r"^T[0-9]{5}$")
_KEGG_GENE_ENTRY = re.compile(r"^[A-Za-z0-9._-]+$")
# Fixed non-GENES prefixes cross-checked against the KEGG organism catalog on 2026-07-14.
_NON_ORGANISM_CODE_PREFIXES = frozenset(
    {
        "atc",
        "cpd",
        "drug",
        "jtc",
        "kegg",
        "map",
        "ndc",
        "path",
        "pmid",
        "vtax",
    }
)
_PATHWAY_REFERENCE_PREFIXES = frozenset({"map", "ko", "ec", "rn", "vg", "vx"})
_BRITE_REFERENCE_PREFIXES = frozenset({"br", "jp", "ko"})
_GENES_COLLECTION_PREFIXES = frozenset({"ag", "vg", "vp"})
_EC_CLASSES = frozenset(str(number) for number in range(1, 8))


def is_kegg_organism_code(value: str) -> bool:
    """Return whether a value has organism-code syntax and is not a fixed KEGG prefix."""
    return (
        _KEGG_ORGANISM_CODE.fullmatch(value) is not None
        and value not in _NON_ORGANISM_CODE_PREFIXES
    )


def is_kegg_gene_identifier(value: str) -> bool:
    """Return whether a qualified identifier has bounded KEGG GENES syntax."""
    prefix, separator, entry = value.partition(":")
    if separator != ":" or _KEGG_GENE_ENTRY.fullmatch(entry) is None:
        return False
    return (
        prefix in _GENES_COLLECTION_PREFIXES
        or _KEGG_T_NUMBER.fullmatch(prefix) is not None
        or is_kegg_organism_code(prefix)
    )


def _split_numbered_identifier(value: str) -> tuple[str, str] | None:
    if len(value) <= 5:
        return None
    prefix, number = value[:-5], value[-5:]
    if not number.isascii() or not number.isdigit():
        return None
    return prefix, number


def is_kegg_pathway_identifier(value: str) -> bool:
    """Return whether an identifier has supported KEGG pathway syntax."""
    parts = _split_numbered_identifier(value)
    if parts is None:
        return False
    prefix, _ = parts
    return prefix in _PATHWAY_REFERENCE_PREFIXES or is_kegg_organism_code(prefix)


def is_kegg_brite_identifier(value: str) -> bool:
    """Return whether an identifier has supported KEGG BRITE hierarchy syntax."""
    parts = _split_numbered_identifier(value)
    if parts is None:
        return False
    prefix, _ = parts
    return prefix in _BRITE_REFERENCE_PREFIXES or is_kegg_organism_code(prefix)


def is_ec_number(value: str) -> bool:
    """Return whether a value has complete or trailing-partial IUBMB EC number syntax."""
    parts = value.split(".")
    if len(parts) != 4 or parts[0] not in _EC_CLASSES:
        return False
    missing_level_seen = False
    for part in parts[1:]:
        if part == "-":
            missing_level_seen = True
        elif missing_level_seen or not part.isascii() or not part.isdigit():
            return False
    return True


_ENTRY_PATTERNS = {
    KeggGetDatabase.KO: re.compile(r"^K[0-9]{5}$"),
    KeggGetDatabase.MODULE: re.compile(r"^M[0-9]{5}$"),
    KeggGetDatabase.REACTION: re.compile(r"^R[0-9]{5}$"),
    KeggGetDatabase.COMPOUND: re.compile(r"^C[0-9]{5}$"),
}


class KeggEntryRef(FrozenModel):
    """One database-qualified, syntax-checked KEGG entry."""

    database: KeggGetDatabase
    identifier: str = Field(min_length=1, max_length=100)
    brite_kind: KeggBriteEntryKind | None = None

    @model_validator(mode="after")
    def validate_identifier(self) -> Self:
        if self.database is KeggGetDatabase.PATHWAY:
            valid_identifier = is_kegg_pathway_identifier(self.identifier)
        elif self.database is KeggGetDatabase.BRITE:
            valid_identifier = is_kegg_brite_identifier(self.identifier)
        elif self.database is KeggGetDatabase.ENZYME:
            valid_identifier = is_ec_number(self.identifier)
        else:
            valid_identifier = _ENTRY_PATTERNS[self.database].fullmatch(self.identifier) is not None
        if not valid_identifier:
            raise ValueError("identifier is incompatible with the selected KEGG database")
        if self.database is KeggGetDatabase.BRITE and self.brite_kind is None:
            raise ValueError("BRITE GET entries require an explicit supported content kind")
        if self.database is not KeggGetDatabase.BRITE and self.brite_kind is not None:
            raise ValueError("brite_kind is valid only for BRITE GET entries")
        return self

    @property
    def wire_identifier(self) -> str:
        """Return the validated identifier form used in KEGG URLs."""
        if self.database is KeggGetDatabase.ENZYME:
            return f"ec:{self.identifier}"
        if self.database is KeggGetDatabase.BRITE:
            return f"br:{self.identifier}"
        return self.identifier


class InfoRequest(FrozenModel):
    """Retrieve release/statistics text for one approved database."""

    database: KeggInfoDatabase


class GetRequest(FrozenModel):
    """Retrieve one ordered, duplicate-free set of approved KEGG entries."""

    entries: Annotated[
        tuple[KeggEntryRef, ...],
        Field(min_length=1, max_length=MAX_CONFIGURED_IDENTIFIERS),
    ]

    @model_validator(mode="after")
    def require_unique_entries(self) -> Self:
        keys = tuple((entry.database, entry.identifier) for entry in self.entries)
        if len(keys) != len(set(keys)):
            raise ValueError("GET entries must be unique and retain caller order")
        return self


class KeggLinkRelationship(StrEnum):
    """Approved relationship directions for LINK requests."""

    KO_TO_PATHWAY = "ko_to_pathway"
    KO_TO_MODULE = "ko_to_module"
    KO_TO_REACTION = "ko_to_reaction"
    KO_TO_ENZYME = "ko_to_enzyme"
    KO_TO_BRITE = "ko_to_brite"
    PATHWAY_TO_KO = "pathway_to_ko"


class LinkRequest(FrozenModel):
    """Retrieve one bounded, explicitly approved KEGG relationship."""

    relationship: KeggLinkRelationship
    source_identifiers: Annotated[
        tuple[KeggIdentifier, ...],
        Field(min_length=1, max_length=MAX_CONFIGURED_IDENTIFIERS),
    ]

    @model_validator(mode="after")
    def validate_source_identifiers(self) -> Self:
        if self.relationship is KeggLinkRelationship.PATHWAY_TO_KO:
            identifiers_are_valid = all(
                is_kegg_pathway_identifier(identifier) for identifier in self.source_identifiers
            )
        else:
            pattern = _ENTRY_PATTERNS[KeggGetDatabase.KO]
            identifiers_are_valid = all(
                pattern.fullmatch(identifier) is not None for identifier in self.source_identifiers
            )
        if not identifiers_are_valid:
            raise ValueError("LINK source identifier is incompatible with the relationship")
        if len(self.source_identifiers) != len(set(self.source_identifiers)):
            raise ValueError("LINK source identifiers must be unique")
        return self


class KeggConvDatabase(StrEnum):
    """Approved selected-entry gene conversion databases."""

    GENES = "genes"
    NCBI_GENEID = "ncbi-geneid"
    NCBI_PROTEINID = "ncbi-proteinid"
    UNIPROT = "uniprot"


class ConvRequest(FrozenModel):
    """Convert a bounded selected set of gene identifiers, never a whole database."""

    target_database: KeggConvDatabase
    source_database: KeggConvDatabase
    source_identifiers: Annotated[
        tuple[KeggIdentifier, ...],
        Field(min_length=1, max_length=MAX_CONFIGURED_IDENTIFIERS),
    ]

    @model_validator(mode="after")
    def validate_conversion_direction(self) -> Self:
        outside = {
            KeggConvDatabase.NCBI_GENEID,
            KeggConvDatabase.NCBI_PROTEINID,
            KeggConvDatabase.UNIPROT,
        }
        valid_pair = (
            self.target_database is KeggConvDatabase.GENES and self.source_database in outside
        ) or (self.source_database is KeggConvDatabase.GENES and self.target_database in outside)
        if not valid_pair:
            raise ValueError("CONV supports only selected external-gene identifier conversions")
        if self.source_database is KeggConvDatabase.GENES:
            identifiers_are_valid = all(
                is_kegg_gene_identifier(identifier) for identifier in self.source_identifiers
            )
        elif self.source_database is KeggConvDatabase.NCBI_GENEID:
            pattern = re.compile(r"^ncbi-geneid:[1-9][0-9]*$")
            identifiers_are_valid = all(
                pattern.fullmatch(identifier) is not None for identifier in self.source_identifiers
            )
        else:
            prefix = re.escape(self.source_database.value)
            pattern = re.compile(rf"^{prefix}:[A-Za-z0-9._-]+$")
            identifiers_are_valid = all(
                pattern.fullmatch(identifier) is not None for identifier in self.source_identifiers
            )
        if not identifiers_are_valid:
            raise ValueError("CONV identifier is incompatible with source_database")
        if len(self.source_identifiers) != len(set(self.source_identifiers)):
            raise ValueError("CONV source identifiers must be unique")
        return self


class KeggRequestOptions(FrozenModel):
    """Per-call cache behavior."""

    refresh: bool = False
    allow_stale: bool = False


MAX_HTTP_METADATA_ITEMS = 16


class HttpMetadata(FrozenModel):
    """One allowlisted non-sensitive HTTP response header."""

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=100)
    value: str = Field(max_length=1_000)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        validate_utf8_text(value, field_name="HTTP metadata value")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("HTTP metadata value must not contain control characters")
        return value


class ResponseOrigin(StrEnum):
    """Where a response body was served from."""

    NETWORK = "network"
    CACHE = "cache"


class CacheLookupState(StrEnum):
    """Cache decision for one HTTP batch."""

    NOT_CHECKED = "not_checked"
    MISS = "miss"
    FRESH_HIT = "fresh_hit"
    STALE_HIT = "stale_hit"
    STALE_DISALLOWED = "stale_disallowed"
    REFRESH_BYPASS = "refresh_bypass"


class KeggBatchProvenance(FrozenModel):
    """Serializable provenance for one network/cache batch."""

    operation: KeggOperation
    request_key: str = Field(default="unavailable", min_length=1, max_length=4_096)
    access_mode: AccessMode
    retrieval_endpoint_class: RetrievalEndpointClass
    endpoint_label: str = Field(min_length=1, max_length=100)
    origin: ResponseOrigin
    cache_lookup_state: CacheLookupState
    retrieved_at: datetime
    served_at: datetime
    expires_at: datetime
    response_bytes: NonNegativeCount
    parser_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    parser_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)
    database_release: str | None = Field(max_length=256)
    http_metadata: Annotated[
        tuple[HttpMetadata, ...], Field(max_length=MAX_HTTP_METADATA_ITEMS)
    ] = ()
    attempt_count: NonNegativeCount
    is_stale: bool

    @model_validator(mode="after")
    def validate_timestamps_and_origin(self) -> Self:
        for name, value in (
            ("retrieved_at", self.retrieved_at),
            ("served_at", self.served_at),
            ("expires_at", self.expires_at),
        ):
            if value.utcoffset() is None:
                raise ValueError(f"{name} must include a timezone")
        if self.expires_at <= self.retrieved_at:
            raise ValueError("expires_at must follow retrieved_at")
        if self.origin is ResponseOrigin.NETWORK and self.is_stale:
            raise ValueError("network responses cannot be stale")
        if self.origin is ResponseOrigin.NETWORK and self.attempt_count == 0:
            raise ValueError("network responses require at least one request attempt")
        if self.origin is ResponseOrigin.CACHE and self.attempt_count != 0:
            raise ValueError("cache responses must not report network attempts")
        cache_hit_states = {CacheLookupState.FRESH_HIT, CacheLookupState.STALE_HIT}
        if self.origin is ResponseOrigin.CACHE and self.cache_lookup_state not in cache_hit_states:
            raise ValueError("cache responses require a cache-hit state")
        if self.origin is ResponseOrigin.NETWORK and self.cache_lookup_state in cache_hit_states:
            raise ValueError("network responses cannot report a cache-hit state")
        if self.is_stale != (self.cache_lookup_state is CacheLookupState.STALE_HIT):
            raise ValueError("is_stale must match the stale-hit cache state")
        if (
            self.cache_lookup_state is CacheLookupState.FRESH_HIT
            and self.served_at >= self.expires_at
        ):
            raise ValueError("fresh cache responses must be served before expiry")
        if (
            self.cache_lookup_state is CacheLookupState.STALE_HIT
            and self.served_at < self.expires_at
        ):
            raise ValueError("stale cache responses must be served at or after expiry")
        if self.access_mode is AccessMode.OFFLINE_CACHE and self.origin is ResponseOrigin.NETWORK:
            raise ValueError("offline access cannot produce a network response")
        if (
            self.access_mode is AccessMode.PUBLIC_ACADEMIC
            and self.retrieval_endpoint_class is not RetrievalEndpointClass.PUBLIC_ACADEMIC
        ):
            raise ValueError("public access requires public endpoint provenance")
        if (
            self.access_mode is AccessMode.LICENSED
            and self.retrieval_endpoint_class is not RetrievalEndpointClass.LICENSED
        ):
            raise ValueError("licensed access requires licensed endpoint provenance")
        return self


class KeggInfoDocument(FrozenModel):
    """Conservative parsed INFO text with all non-empty lines retained."""

    database: KeggInfoDatabase
    release: str | None = Field(max_length=256)
    entry_count: NonNegativeCount | None
    linked_databases: tuple[str, ...]
    lines: tuple[str, ...]


class KeggPairRow(FrozenModel):
    """One source-to-target row from LINK or CONV output."""

    batch_index: NonNegativeCount = 0
    line_number: PositiveCount
    source_id: str = Field(min_length=1, max_length=256)
    target_id: str = Field(min_length=1, max_length=256)


class KeggPairDocument(FrozenModel):
    """Ordered LINK/CONV rows; an empty 200 response is a valid empty document."""

    rows: tuple[KeggPairRow, ...]


class KeggFlatFileField(FrozenModel):
    """One field occurrence with canonical nesting and continuation lines preserved."""

    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=32)
    indent_columns: Literal[0, 2] = 0
    value_lines: Annotated[tuple[str, ...], Field(min_length=1)]
    start_line: PositiveCount
    end_line: PositiveCount

    @model_validator(mode="after")
    def validate_line_span(self) -> Self:
        if self.end_line < self.start_line:
            raise ValueError("field end_line must not precede start_line")
        return self


class KeggFlatFileEntry(FrozenModel):
    """One terminated KEGG flat-file entry."""

    identifier: str = Field(min_length=1, max_length=100)
    fields: Annotated[tuple[KeggFlatFileField, ...], Field(min_length=1)]
    start_line: PositiveCount
    end_line: PositiveCount


class KeggDocumentFormat(StrEnum):
    """Supported textual GET formats."""

    FLAT_FILE = "flat_file"
    BRITE_HTEXT = "brite_htext"


class KeggFlatFileDocument(FrozenModel):
    """One parsed GET flat-file batch."""

    format: Literal[KeggDocumentFormat.FLAT_FILE] = KeggDocumentFormat.FLAT_FILE
    entries: tuple[KeggFlatFileEntry, ...]


class KeggBriteHtextDocument(FrozenModel):
    """One bounded BRITE htext response with source lines preserved."""

    format: Literal[KeggDocumentFormat.BRITE_HTEXT] = KeggDocumentFormat.BRITE_HTEXT
    identifier: str = Field(min_length=1, max_length=100)
    lines: tuple[str, ...]


KeggGetDocument = Annotated[
    KeggFlatFileDocument | KeggBriteHtextDocument,
    Field(discriminator="format"),
]


class InfoResult(FrozenModel):
    """Parsed INFO document and batch provenance."""

    request: InfoRequest
    document: KeggInfoDocument
    batch: KeggBatchProvenance


class GetResult(FrozenModel):
    """Ordered GET documents with explicit partial/missing identifiers."""

    request: GetRequest
    documents: tuple[KeggGetDocument, ...]
    missing_entries: tuple[KeggEntryRef, ...]
    batches: Annotated[tuple[KeggBatchProvenance, ...], Field(min_length=1)]


class LinkResult(FrozenModel):
    """Parsed LINK rows with per-batch provenance."""

    request: LinkRequest
    rows: tuple[KeggPairRow, ...]
    batches: Annotated[tuple[KeggBatchProvenance, ...], Field(min_length=1)]


class ConvResult(FrozenModel):
    """Parsed CONV rows with per-batch provenance."""

    request: ConvRequest
    rows: tuple[KeggPairRow, ...]
    batches: Annotated[tuple[KeggBatchProvenance, ...], Field(min_length=1)]
