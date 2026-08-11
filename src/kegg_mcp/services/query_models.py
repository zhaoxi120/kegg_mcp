"""Typed service contracts for bounded KEGG search, resolution, and relation tracing."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, field_validator, model_validator

from kegg_mcp.domain.annotations import FrozenModel, normalize_identifier_label, validate_utf8_text
from kegg_mcp.kegg.contracts import (
    MAX_FIND_MATCH_TEXT_CHARACTERS,
    FindRequest,
    KeggFindDatabase,
    KeggFindMode,
    KeggLinkRelationship,
    is_ec_number,
    is_kegg_brite_identifier,
    is_kegg_gene_identifier,
    is_kegg_organism_code,
    is_kegg_pathway_identifier,
    link_relation_contract,
)
from kegg_mcp.services.result_store import ResultArtifactMetadata, ResultMetadata

MAX_SEARCH_RESULTS = 100
MAX_RESOLUTION_IDENTIFIERS = 50
MAX_RESOLUTION_ENTITIES = 200
MAX_ORGANISM_RESOLUTION_CANDIDATES = 5_000
MAX_RESOLUTION_UNIQUE_ENTITIES = 10_000
MAX_TRACE_SEEDS = 50
MAX_TRACE_NODES = 200
MAX_TRACE_EDGES = 500
MAX_QUERY_PROVENANCE_BATCHES = 200
MAX_QUERY_RELEASE_PREVIEW = 25
MAX_SEARCH_PREVIEW_RESULTS = 10
MAX_SEARCH_PREVIEW_MATCH_CHARACTERS = 128
MAX_RESOLUTION_INPUT_PREVIEW = 5
MAX_RESOLUTION_CANDIDATE_PREVIEW = 2
MAX_RESOLUTION_ENTITY_PREVIEW = 5
MAX_RESOLUTION_TAXONOMY_PREVIEW = 3
MAX_RESOLUTION_PATHWAY_DIRECT_PREVIEW = 2
MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS = 128
MAX_TRACE_NODE_PREVIEW = 25
MAX_TRACE_EDGE_PREVIEW = 25
MAX_ORGANISM_NAME_CHARACTERS = 2_000
MAX_TAXONOMY_LINEAGE_DEPTH = 64
MAX_TAXONOMY_LINEAGE_LABEL_CHARACTERS = 512
MAX_ORGANISM_PATHWAY_PREVIEW = 20
_MAX_GET_IDENTIFIER_CHARACTERS = 100
_MAX_RELATION_IDENTIFIER_CHARACTERS = 256


class KeggEntityKind(StrEnum):
    """Entity namespaces accepted by the bounded query services."""

    GENE = "gene"
    KO = "ko"
    PATHWAY = "pathway"
    MODULE = "module"
    REACTION = "reaction"
    ENZYME = "enzyme"
    COMPOUND = "compound"
    GLYCAN = "glycan"
    DRUG = "drug"
    RCLASS = "rclass"
    BRITE = "brite"
    GENOME = "genome"
    TAXONOMY = "taxonomy"
    ORGANISM = "organism"


class KeggEntityRef(FrozenModel):
    """One canonical typed entity used by resolver and trace services."""

    kind: KeggEntityKind
    identifier: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_identifier_for_kind(self) -> Self:
        value = self.identifier
        if self.kind is KeggEntityKind.GENE:
            valid = is_kegg_gene_identifier(value)
        elif self.kind is KeggEntityKind.KO:
            valid = _numbered_identifier(value, "K")
        elif self.kind is KeggEntityKind.PATHWAY:
            valid = is_kegg_pathway_identifier(value)
        elif self.kind is KeggEntityKind.MODULE:
            valid = _module_identifier(value)
        elif self.kind is KeggEntityKind.REACTION:
            valid = _numbered_identifier(value, "R")
        elif self.kind is KeggEntityKind.ENZYME:
            valid = is_ec_number(value)
        elif self.kind is KeggEntityKind.COMPOUND:
            valid = _numbered_identifier(value, "C")
        elif self.kind is KeggEntityKind.GLYCAN:
            valid = _numbered_identifier(value, "G")
        elif self.kind is KeggEntityKind.DRUG:
            valid = _numbered_identifier(value, "D")
        elif self.kind is KeggEntityKind.RCLASS:
            valid = (
                len(value) == 7
                and value.startswith("RC")
                and value[2:].isascii()
                and value[2:].isdigit()
            )
        elif self.kind is KeggEntityKind.BRITE:
            valid = is_kegg_brite_identifier(value)
        elif self.kind is KeggEntityKind.GENOME:
            number = value[1:]
            valid = (
                len(value) == 6 and value.startswith("T") and number.isascii() and number.isdigit()
            )
        elif self.kind is KeggEntityKind.TAXONOMY:
            prefix, separator, number = value.partition(":")
            valid = separator == ":" and prefix == "taxid" and _positive_ascii_integer(number)
        else:
            valid = is_kegg_organism_code(value)
        if not valid:
            raise ValueError("identifier is incompatible with the selected entity kind")
        return self


class KeggSearchDatabase(StrEnum):
    """Public bounded search scopes; organism is a service alias for genome."""

    KO = "ko"
    PATHWAY = "pathway"
    MODULE = "module"
    REACTION = "reaction"
    ENZYME = "enzyme"
    COMPOUND = "compound"
    GLYCAN = "glycan"
    DRUG = "drug"
    RCLASS = "rclass"
    GENOME = "genome"
    ORGANISM = "organism"


class KeggSearchMode(StrEnum):
    """Supported keyword and bounded chemical-candidate search modes."""

    KEYWORD = "keyword"
    FORMULA = "formula"
    EXACT_MASS = "exact_mass"
    MOLECULAR_WEIGHT = "molecular_weight"


class QueryRetrievalSummary(FrozenModel):
    """Compact retrieval accounting; complete batch provenance remains retained."""

    batch_count: int = Field(strict=True, ge=0, le=MAX_QUERY_PROVENANCE_BATCHES)
    network_request_count: int = Field(strict=True, ge=0)
    cache_hit_count: int = Field(strict=True, ge=0)
    stale_batch_count: int = Field(strict=True, ge=0)
    response_bytes: int = Field(strict=True, ge=0)
    database_release_count: int = Field(strict=True, ge=0, le=MAX_QUERY_PROVENANCE_BATCHES)
    database_releases: Annotated[
        tuple[str, ...],
        Field(max_length=MAX_QUERY_RELEASE_PREVIEW),
    ]
    database_releases_truncated: bool

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.cache_hit_count > self.batch_count or self.stale_batch_count > self.batch_count:
            raise ValueError("retrieval batch subsets cannot exceed the batch count")
        if self.database_release_count > self.batch_count:
            raise ValueError("distinct database releases cannot exceed the batch count")
        if self.database_release_count < len(self.database_releases):
            raise ValueError("database release count cannot be smaller than its preview")
        if self.database_releases_truncated != (
            self.database_release_count > len(self.database_releases)
        ):
            raise ValueError("database_releases_truncated must match its preview")
        return self


class SearchKeggEntriesRequest(FrozenModel):
    """One bounded KEGG FIND request and direct-result projection."""

    database: KeggSearchDatabase
    query: str = Field(min_length=1, max_length=256)
    mode: KeggSearchMode = KeggSearchMode.KEYWORD
    max_results: int = Field(
        default=20,
        strict=True,
        ge=1,
        le=MAX_SEARCH_RESULTS,
        description="Maximum endpoint candidates returned, from 1 through 100; defaults to 20.",
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("query must not contain outer whitespace")
        validate_utf8_text(value, field_name="query")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("query must not contain control characters")
        return value

    @model_validator(mode="after")
    def constrain_chemical_modes(self) -> Self:
        if self.mode is not KeggSearchMode.KEYWORD and self.database not in {
            KeggSearchDatabase.COMPOUND,
            KeggSearchDatabase.DRUG,
        }:
            raise ValueError("formula and mass modes are supported only for compound or drug")
        self.to_find_request()
        return self

    def to_find_request(self) -> FindRequest:
        """Return the typed low-level request, reusing its mode-specific query validation."""
        mode = (
            KeggFindMode.MOL_WEIGHT
            if self.mode is KeggSearchMode.MOLECULAR_WEIGHT
            else KeggFindMode(self.mode.value)
        )
        return FindRequest(
            database=KeggFindDatabase(self.database.value),
            query=self.query,
            mode=mode,
        )


class KeggSearchCandidatePreview(FrozenModel):
    """One compact endpoint-candidate preview without an invented relevance score."""

    entity: KeggEntityRef
    raw_match: str = Field(
        min_length=1,
        max_length=MAX_SEARCH_PREVIEW_MATCH_CHARACTERS,
    )
    raw_match_truncated: bool

    @field_validator("raw_match")
    @classmethod
    def validate_match_text(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="search match")

    @model_validator(mode="after")
    def validate_truncation(self) -> Self:
        if self.raw_match_truncated and len(self.raw_match) != MAX_SEARCH_PREVIEW_MATCH_CHARACTERS:
            raise ValueError("truncated search match previews must fill their fixed text bound")
        return self


class SearchKeggEntriesResult(FrozenModel):
    """Compact direct FIND projection with the complete endpoint result retained."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    database: KeggSearchDatabase
    mode: KeggSearchMode
    observed_count: int = Field(strict=True, ge=0)
    candidate_count: int = Field(strict=True, ge=0, le=MAX_SEARCH_RESULTS)
    candidate_preview: Annotated[
        tuple[KeggSearchCandidatePreview, ...],
        Field(max_length=MAX_SEARCH_PREVIEW_RESULTS),
    ]
    candidates_truncated: bool
    endpoint_candidates_truncated: bool
    retrieval: QueryRetrievalSummary
    interpretation_caveats: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(min_length=1, max_length=2),
    ]

    @model_validator(mode="after")
    def validate_projection_counts(self) -> Self:
        if self.candidate_count < len(self.candidate_preview):
            raise ValueError("candidate_count cannot be smaller than candidate_preview")
        if self.observed_count < self.candidate_count:
            raise ValueError("observed_count cannot be smaller than candidate_count")
        if self.candidates_truncated != (self.candidate_count > len(self.candidate_preview)):
            raise ValueError("candidates_truncated must match the candidate preview")
        if self.endpoint_candidates_truncated != (self.observed_count > self.candidate_count):
            raise ValueError("endpoint_candidates_truncated must match max_results")
        expected_caveat_count = 2 if self.mode is KeggSearchMode.EXACT_MASS else 1
        if len(self.interpretation_caveats) != expected_caveat_count:
            raise ValueError("search caveats must match the selected query mode")
        return self


class GeneIdentifierNamespace(StrEnum):
    KEGG_GENE = "kegg_gene"
    NCBI_GENEID = "ncbi_geneid"
    NCBI_PROTEINID = "ncbi_proteinid"
    UNIPROT = "uniprot"
    GENE_SYMBOL = "gene_symbol"


_EXTERNAL_GENE_PREFIXES = {
    GeneIdentifierNamespace.NCBI_GENEID: "ncbi-geneid",
    GeneIdentifierNamespace.NCBI_PROTEINID: "ncbi-proteinid",
    GeneIdentifierNamespace.UNIPROT: "uniprot",
}


class OrganismIdentifierNamespace(StrEnum):
    CODE = "code"
    GENOME = "genome"
    TAXONOMY = "taxonomy"
    NAME = "name"


class TaxonomyResolutionRank(StrEnum):
    """Approved taxonomy-to-genome resolution granularity."""

    EXACT = "exact"
    SPECIES = "species"
    GENUS = "genus"
    FAMILY = "family"
    ORDER = "order"
    CLASS = "class"
    PHYLUM = "phylum"


class OrganismCandidateMaterialization(StrEnum):
    """How candidate-only taxonomy links are expanded into GENOME records."""

    AUTO = "auto"
    IDENTITY_ONLY = "identity_only"
    FULL = "full"


class GeneResolutionTarget(StrEnum):
    """Allowlisted projections from each canonical KEGG gene candidate."""

    GENE = "gene"
    KO = "ko"
    PATHWAY = "pathway"
    MODULE = "module"
    REACTION = "reaction"
    ENZYME = "enzyme"


class GeneResolutionRequest(FrozenModel):
    """Resolve selected gene identifiers without silently choosing ambiguous mappings."""

    kind: Literal["gene"]
    source_namespace: GeneIdentifierNamespace
    identifiers: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(min_length=1, max_length=MAX_RESOLUTION_IDENTIFIERS),
    ]
    organism: str | None = Field(default=None, min_length=3, max_length=4)
    targets: Annotated[
        tuple[GeneResolutionTarget, ...],
        Field(min_length=1, max_length=len(GeneResolutionTarget)),
    ] = (GeneResolutionTarget.GENE,)
    ambiguity_policy: Literal["report_all"] = "report_all"

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            normalize_identifier_label(value, field_name="gene identifier") for value in values
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("gene identifiers must be unique")
        return normalized

    @field_validator("organism")
    @classmethod
    def validate_organism(cls, value: str | None) -> str | None:
        if value is not None and not is_kegg_organism_code(value):
            raise ValueError("organism must be a supported KEGG organism-code form")
        return value

    @model_validator(mode="after")
    def require_symbol_organism(self) -> Self:
        if self.source_namespace is GeneIdentifierNamespace.GENE_SYMBOL and self.organism is None:
            raise ValueError("gene_symbol resolution requires an organism")
        if len(self.targets) != len(set(self.targets)):
            raise ValueError("gene resolution targets must be unique")
        if self.source_namespace is GeneIdentifierNamespace.KEGG_GENE:
            valid = all(
                is_kegg_gene_identifier(value) and len(value) <= _MAX_GET_IDENTIFIER_CHARACTERS
                for value in self.identifiers
            )
        elif self.source_namespace is GeneIdentifierNamespace.NCBI_GENEID:
            valid = all(
                _positive_ascii_integer(value)
                and len(self.conversion_identifier(value)) <= _MAX_RELATION_IDENTIFIER_CHARACTERS
                for value in self.identifiers
            )
        elif self.source_namespace in _EXTERNAL_GENE_PREFIXES:
            valid = all(
                _bare_gene_label(value)
                and len(self.conversion_identifier(value)) <= _MAX_RELATION_IDENTIFIER_CHARACTERS
                for value in self.identifiers
            )
        else:
            valid = all(_bare_gene_label(value) for value in self.identifiers)
        if not valid:
            raise ValueError("identifier is incompatible with the selected gene namespace")
        return self

    def conversion_identifier(self, identifier: str) -> str:
        """Return one bounded external identifier in the typed CONV wire namespace."""
        prefix = _EXTERNAL_GENE_PREFIXES.get(self.source_namespace)
        if prefix is None:
            raise ValueError("conversion identifiers require an external gene namespace")
        return f"{prefix}:{identifier}"


class OrganismResolutionRequest(FrozenModel):
    """Resolve organism codes, genome identifiers, taxonomy IDs, or name candidates."""

    kind: Literal["organism"]
    source_namespace: OrganismIdentifierNamespace
    identifiers: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(min_length=1, max_length=MAX_RESOLUTION_IDENTIFIERS),
    ]
    taxonomy_rank: TaxonomyResolutionRank = TaxonomyResolutionRank.EXACT
    candidate_materialization: OrganismCandidateMaterialization = (
        OrganismCandidateMaterialization.AUTO
    )
    include_pathway_directory: bool = False
    ambiguity_policy: Literal["report_all"] = "report_all"

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            normalize_identifier_label(value, field_name="organism identifier") for value in values
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("organism identifiers must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_namespace_identifiers(self) -> Self:
        if self.source_namespace is OrganismIdentifierNamespace.CODE:
            valid = all(is_kegg_organism_code(value) for value in self.identifiers)
        elif self.source_namespace is OrganismIdentifierNamespace.GENOME:
            valid = all(_numbered_identifier(value, "T") for value in self.identifiers)
        elif self.source_namespace is OrganismIdentifierNamespace.TAXONOMY:
            valid = all(
                _positive_ascii_integer(value.removeprefix("taxid:"))
                and len(f"taxid:{value.removeprefix('taxid:')}")
                <= _MAX_RELATION_IDENTIFIER_CHARACTERS
                for value in self.identifiers
            )
            normalized_taxonomy_ids = tuple(
                value.removeprefix("taxid:") for value in self.identifiers
            )
            if len(normalized_taxonomy_ids) != len(set(normalized_taxonomy_ids)):
                raise ValueError(
                    "taxonomy identifiers must be unique after namespace normalization"
                )
        else:
            valid = True
        if not valid:
            raise ValueError("identifier is incompatible with the selected organism namespace")
        if (
            self.source_namespace is not OrganismIdentifierNamespace.TAXONOMY
            and self.taxonomy_rank is not TaxonomyResolutionRank.EXACT
        ):
            raise ValueError("taxonomy_rank is valid only for taxonomy resolution")
        if (
            self.candidate_materialization is not OrganismCandidateMaterialization.AUTO
            and self.source_namespace is not OrganismIdentifierNamespace.TAXONOMY
        ):
            raise ValueError(
                "candidate_materialization is configurable only for taxonomy resolution"
            )
        if (
            self.include_pathway_directory
            and self.effective_candidate_materialization
            is not OrganismCandidateMaterialization.FULL
        ):
            raise ValueError("include_pathway_directory requires full candidate materialization")
        return self

    @property
    def effective_candidate_materialization(self) -> OrganismCandidateMaterialization:
        """Return the deterministic projection used for this request."""
        if self.candidate_materialization is not OrganismCandidateMaterialization.AUTO:
            return self.candidate_materialization
        broad_ranks = {
            TaxonomyResolutionRank.GENUS,
            TaxonomyResolutionRank.FAMILY,
            TaxonomyResolutionRank.ORDER,
            TaxonomyResolutionRank.CLASS,
            TaxonomyResolutionRank.PHYLUM,
        }
        if (
            self.source_namespace is OrganismIdentifierNamespace.TAXONOMY
            and self.taxonomy_rank in broad_ranks
        ):
            return OrganismCandidateMaterialization.IDENTITY_ONLY
        return OrganismCandidateMaterialization.FULL


class SubstanceIdentifierNamespace(StrEnum):
    """Selected KEGG or external chemical-substance identifier namespace."""

    KEGG_COMPOUND = "kegg_compound"
    KEGG_GLYCAN = "kegg_glycan"
    KEGG_DRUG = "kegg_drug"
    CHEBI = "chebi"
    PUBCHEM_SID = "pubchem_sid"


class SubstanceResolutionTarget(StrEnum):
    """Allowlisted KEGG substance identities and one-hop projections."""

    KEGG_COMPOUND = "kegg_compound"
    KEGG_GLYCAN = "kegg_glycan"
    KEGG_DRUG = "kegg_drug"
    REACTION = "reaction"
    PATHWAY = "pathway"


_SUBSTANCE_NAMESPACE_KIND = {
    SubstanceIdentifierNamespace.KEGG_COMPOUND: KeggEntityKind.COMPOUND,
    SubstanceIdentifierNamespace.KEGG_GLYCAN: KeggEntityKind.GLYCAN,
    SubstanceIdentifierNamespace.KEGG_DRUG: KeggEntityKind.DRUG,
}
_SUBSTANCE_TARGET_KIND = {
    SubstanceResolutionTarget.KEGG_COMPOUND: KeggEntityKind.COMPOUND,
    SubstanceResolutionTarget.KEGG_GLYCAN: KeggEntityKind.GLYCAN,
    SubstanceResolutionTarget.KEGG_DRUG: KeggEntityKind.DRUG,
}


class SubstanceResolutionRequest(FrozenModel):
    """Resolve selected chemical crosswalks without claiming compound identification."""

    kind: Literal["substance"]
    source_namespace: SubstanceIdentifierNamespace
    identifiers: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(min_length=1, max_length=MAX_RESOLUTION_IDENTIFIERS),
    ]
    targets: Annotated[
        tuple[SubstanceResolutionTarget, ...],
        Field(min_length=1, max_length=len(SubstanceResolutionTarget)),
    ]
    ambiguity_policy: Literal["report_all"] = "report_all"

    @field_validator("identifiers")
    @classmethod
    def validate_identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            normalize_identifier_label(value, field_name="substance identifier") for value in values
        )
        if len(normalized) != len(set(normalized)):
            raise ValueError("substance identifiers must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_namespace_and_targets(self) -> Self:
        if len(self.targets) != len(set(self.targets)):
            raise ValueError("substance resolution targets must be unique")
        target_kinds = self.target_kinds
        if not target_kinds:
            raise ValueError("substance resolution requires at least one KEGG substance target")
        source_kind = _SUBSTANCE_NAMESPACE_KIND.get(self.source_namespace)
        if source_kind is not None:
            if target_kinds != {source_kind}:
                raise ValueError(
                    "a KEGG substance source supports only its own KEGG identity target"
                )
            valid = all(
                _substance_entity_is_valid(source_kind, identifier)
                for identifier in self.identifiers
            )
        else:
            valid = all(
                _external_substance_number(self.source_namespace, identifier) is not None
                for identifier in self.identifiers
            )
            if valid:
                conversion_identifiers = tuple(
                    self.conversion_identifier(identifier) for identifier in self.identifiers
                )
                valid = all(
                    len(identifier) <= _MAX_RELATION_IDENTIFIER_CHARACTERS
                    for identifier in conversion_identifiers
                )
                if len(conversion_identifiers) != len(set(conversion_identifiers)):
                    raise ValueError(
                        "external substance identifiers must be unique after normalization"
                    )
        if not valid:
            raise ValueError("identifier is incompatible with the selected substance namespace")
        if (
            SubstanceResolutionTarget.REACTION in self.targets
            and KeggEntityKind.DRUG in target_kinds
        ):
            raise ValueError("drug resolution does not support a reaction projection")
        return self

    @property
    def target_kinds(self) -> set[KeggEntityKind]:
        """Return requested canonical KEGG substance entity kinds."""
        return {
            _SUBSTANCE_TARGET_KIND[target]
            for target in self.targets
            if target in _SUBSTANCE_TARGET_KIND
        }

    def conversion_identifier(self, identifier: str) -> str:
        """Return one external identifier in the exact KEGG CONV wire namespace."""
        number = _external_substance_number(self.source_namespace, identifier)
        if number is None:
            raise ValueError("conversion_identifier requires an external substance namespace")
        prefix = (
            "chebi" if self.source_namespace is SubstanceIdentifierNamespace.CHEBI else "pubchem"
        )
        return f"{prefix}:{number}"


ResolveKeggEntitiesRequest: TypeAlias = Annotated[
    GeneResolutionRequest | OrganismResolutionRequest | SubstanceResolutionRequest,
    Field(discriminator="kind"),
]


class MappingStatus(StrEnum):
    UNMAPPED = "unmapped"
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    ORGANISM_MISMATCH = "organism_mismatch"


class ResolutionOperation(StrEnum):
    """Auditable operation classes used for one input resolution."""

    DIRECT = "direct"
    FIND = "find"
    GET = "get"
    CONV = "conv"
    LINK = "link"
    LIST = "list"


class OrganismPathwayPreviewEntry(FrozenModel):
    """One bounded organism-specific pathway directory preview entry."""

    pathway: KeggEntityRef
    name: str = Field(min_length=1, max_length=MAX_FIND_MATCH_TEXT_CHARACTERS)

    @model_validator(mode="after")
    def require_pathway_entity(self) -> Self:
        if self.pathway.kind is not KeggEntityKind.PATHWAY:
            raise ValueError("organism pathway previews require pathway entities")
        validate_utf8_text(self.name, field_name="organism pathway name")
        return self


class OrganismPathwaySummary(FrozenModel):
    """Complete directory count plus a bounded ordered preview."""

    total_count: int = Field(strict=True, ge=0)
    preview: Annotated[
        tuple[OrganismPathwayPreviewEntry, ...],
        Field(max_length=MAX_ORGANISM_PATHWAY_PREVIEW),
    ] = ()
    truncated: bool

    @model_validator(mode="after")
    def validate_preview(self) -> Self:
        pathway_ids = tuple(entry.pathway.identifier for entry in self.preview)
        if len(pathway_ids) != len(set(pathway_ids)):
            raise ValueError("organism pathway preview entries must be unique")
        if self.total_count < len(self.preview):
            raise ValueError("organism pathway total_count cannot be smaller than its preview")
        if self.truncated != (self.total_count > len(self.preview)):
            raise ValueError("organism pathway truncated must match the preview projection")
        return self


class ResolvedEntityCandidate(FrozenModel):
    """One canonical resolution candidate and its requested typed projections."""

    canonical_entity: KeggEntityRef
    entities: Annotated[tuple[KeggEntityRef, ...], Field(max_length=MAX_RESOLUTION_ENTITIES)] = ()
    name: str | None = Field(default=None, max_length=MAX_ORGANISM_NAME_CHARACTERS)
    taxonomy_lineage: Annotated[
        tuple[
            Annotated[
                str,
                Field(min_length=1, max_length=MAX_TAXONOMY_LINEAGE_LABEL_CHARACTERS),
            ],
            ...,
        ],
        Field(max_length=MAX_TAXONOMY_LINEAGE_DEPTH),
    ] = ()
    organism_pathways: OrganismPathwaySummary | None = None
    organism_materialization: OrganismCandidateMaterialization | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name="resolved organism name")

    @model_validator(mode="after")
    def require_unique_entities(self) -> Self:
        keys = tuple((entity.kind, entity.identifier) for entity in self.entities)
        if len(keys) != len(set(keys)):
            raise ValueError("resolved candidate entities must be unique")
        for label in self.taxonomy_lineage:
            validate_utf8_text(label, field_name="taxonomy lineage label")
        if self.canonical_entity.kind is not KeggEntityKind.ORGANISM and (
            self.name is not None or self.taxonomy_lineage or self.organism_pathways is not None
        ):
            raise ValueError("organism metadata is valid only for canonical organism candidates")
        if self.organism_materialization is OrganismCandidateMaterialization.AUTO:
            raise ValueError("resolved candidates require an effective materialization value")
        if self.organism_materialization is not None and self.canonical_entity.kind not in {
            KeggEntityKind.ORGANISM,
            KeggEntityKind.GENOME,
        }:
            raise ValueError(
                "organism materialization is valid only for organism or genome candidates"
            )
        if self.organism_pathways is not None:
            organism = self.canonical_entity.identifier
            if any(
                entry.pathway.identifier[:-5] != organism
                for entry in self.organism_pathways.preview
            ):
                raise ValueError("organism pathway previews must match the canonical organism code")
        return self


class EntityResolution(FrozenModel):
    """Resolution status and all candidates for one caller identifier."""

    input_identifier: str = Field(min_length=1, max_length=256)
    status: MappingStatus
    candidates: Annotated[
        tuple[ResolvedEntityCandidate, ...],
        Field(max_length=MAX_ORGANISM_RESOLUTION_CANDIDATES),
    ] = ()
    discarded_organism_mismatch_count: int = Field(default=0, strict=True, ge=0)
    operations_used: Annotated[
        tuple[ResolutionOperation, ...],
        Field(min_length=1, max_length=len(ResolutionOperation)),
    ]

    @model_validator(mode="after")
    def validate_status_cardinality(self) -> Self:
        count = len(self.candidates)
        if self.status in {MappingStatus.UNMAPPED, MappingStatus.ORGANISM_MISMATCH} and count != 0:
            raise ValueError("unmapped and mismatch resolutions cannot contain candidates")
        if self.status in {MappingStatus.ONE_TO_ONE, MappingStatus.MANY_TO_ONE} and count != 1:
            raise ValueError("one_to_one and many_to_one resolutions require exactly one candidate")
        if self.status is MappingStatus.ONE_TO_MANY and count < 2:
            raise ValueError("one_to_many resolutions require at least two candidates")
        if (
            self.status is MappingStatus.ORGANISM_MISMATCH
            and self.discarded_organism_mismatch_count == 0
        ):
            raise ValueError("organism_mismatch requires at least one discarded candidate")
        if len(self.operations_used) != len(set(self.operations_used)):
            raise ValueError("operations_used must be unique")
        return self


class OrganismPathwayDirectPreviewEntry(FrozenModel):
    """One compact pathway directory entry for the direct resolver response."""

    pathway: KeggEntityRef
    name: str = Field(
        min_length=1,
        max_length=MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS,
    )
    name_truncated: bool

    @model_validator(mode="after")
    def require_pathway_entity(self) -> Self:
        if self.pathway.kind is not KeggEntityKind.PATHWAY:
            raise ValueError("organism pathway previews require pathway entities")
        validate_utf8_text(self.name, field_name="organism pathway name")
        if self.name_truncated and len(self.name) != MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS:
            raise ValueError("truncated pathway names must fill the direct text bound")
        return self


class ResolvedEntityCandidatePreview(FrozenModel):
    """Compact direct projection of one retained resolution candidate."""

    canonical_entity: KeggEntityRef
    entity_count: int = Field(strict=True, ge=0, le=MAX_RESOLUTION_ENTITIES)
    entity_preview: Annotated[
        tuple[KeggEntityRef, ...],
        Field(max_length=MAX_RESOLUTION_ENTITY_PREVIEW),
    ] = ()
    entities_truncated: bool
    name: str | None = Field(
        default=None,
        max_length=MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS,
    )
    name_truncated: bool = False
    taxonomy_lineage_count: int = Field(
        default=0,
        strict=True,
        ge=0,
        le=MAX_TAXONOMY_LINEAGE_DEPTH,
    )
    taxonomy_lineage_preview: Annotated[
        tuple[
            Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS,
                ),
            ],
            ...,
        ],
        Field(max_length=MAX_RESOLUTION_TAXONOMY_PREVIEW),
    ] = ()
    taxonomy_lineage_truncated: bool = False
    taxonomy_lineage_text_truncated: bool = False
    organism_pathway_count: int | None = Field(default=None, strict=True, ge=0)
    organism_pathway_preview: Annotated[
        tuple[OrganismPathwayDirectPreviewEntry, ...],
        Field(max_length=MAX_RESOLUTION_PATHWAY_DIRECT_PREVIEW),
    ] = ()
    organism_pathways_truncated: bool | None = None
    organism_materialization: OrganismCandidateMaterialization | None = None

    @model_validator(mode="after")
    def validate_preview_counts(self) -> Self:
        if self.entity_count < len(self.entity_preview):
            raise ValueError("entity_count cannot be smaller than entity_preview")
        if self.entities_truncated != (self.entity_count > len(self.entity_preview)):
            raise ValueError("entities_truncated must match the entity preview")
        if self.taxonomy_lineage_count < len(self.taxonomy_lineage_preview):
            raise ValueError(
                "taxonomy_lineage_count cannot be smaller than taxonomy_lineage_preview"
            )
        if self.taxonomy_lineage_truncated != (
            self.taxonomy_lineage_count > len(self.taxonomy_lineage_preview)
        ):
            raise ValueError("taxonomy_lineage_truncated must match the lineage preview")
        if self.name is None and self.name_truncated:
            raise ValueError("a missing candidate name cannot be text-truncated")
        if self.name_truncated and len(self.name or "") != MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS:
            raise ValueError("truncated candidate names must fill the direct text bound")
        if self.taxonomy_lineage_text_truncated and not any(
            len(label) == MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS
            for label in self.taxonomy_lineage_preview
        ):
            raise ValueError("truncated lineage text must fill at least one direct text bound")
        pathway_count = self.organism_pathway_count
        pathway_truncated = self.organism_pathways_truncated
        pathway_requested = pathway_count is not None
        if pathway_requested != (pathway_truncated is not None):
            raise ValueError("organism pathway count and truncation must be present together")
        if not pathway_requested and self.organism_pathway_preview:
            raise ValueError("an unrequested organism pathway directory cannot have a preview")
        if pathway_count is not None and pathway_truncated is not None:
            if pathway_count < len(self.organism_pathway_preview):
                raise ValueError("organism_pathway_count cannot be smaller than its direct preview")
            if pathway_truncated != (pathway_count > len(self.organism_pathway_preview)):
                raise ValueError("organism_pathways_truncated must match the direct preview")
        return self


class EntityResolutionPreview(FrozenModel):
    """Compact direct status and bounded candidates for one caller identifier."""

    input_identifier: str = Field(min_length=1, max_length=256)
    status: MappingStatus
    candidate_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_ORGANISM_RESOLUTION_CANDIDATES,
    )
    candidate_preview: Annotated[
        tuple[ResolvedEntityCandidatePreview, ...],
        Field(max_length=MAX_RESOLUTION_CANDIDATE_PREVIEW),
    ] = ()
    candidates_truncated: bool
    discarded_organism_mismatch_count: int = Field(default=0, strict=True, ge=0)
    operations_used: Annotated[
        tuple[ResolutionOperation, ...],
        Field(min_length=1, max_length=len(ResolutionOperation)),
    ]

    @model_validator(mode="after")
    def validate_preview(self) -> Self:
        if self.candidate_count < len(self.candidate_preview):
            raise ValueError("candidate_count cannot be smaller than candidate_preview")
        if self.candidates_truncated != (self.candidate_count > len(self.candidate_preview)):
            raise ValueError("candidates_truncated must match the candidate preview")
        if self.status in {MappingStatus.UNMAPPED, MappingStatus.ORGANISM_MISMATCH}:
            if self.candidate_count != 0:
                raise ValueError("unmapped and mismatch previews cannot contain candidates")
        elif self.status in {MappingStatus.ONE_TO_ONE, MappingStatus.MANY_TO_ONE}:
            if self.candidate_count != 1:
                raise ValueError("one-to-one and many-to-one previews require one candidate")
        elif self.candidate_count < 2:
            raise ValueError("one-to-many previews require at least two candidates")
        return self


class ResolveKeggEntitiesResult(FrozenModel):
    """Compact direct resolution preview with a complete retained crosswalk."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    kind: Literal["gene", "organism", "substance"]
    input_count: int = Field(strict=True, ge=1, le=MAX_RESOLUTION_IDENTIFIERS)
    mapped_input_count: int = Field(strict=True, ge=0, le=MAX_RESOLUTION_IDENTIFIERS)
    ambiguous_input_count: int = Field(strict=True, ge=0, le=MAX_RESOLUTION_IDENTIFIERS)
    many_to_one_input_count: int = Field(strict=True, ge=0, le=MAX_RESOLUTION_IDENTIFIERS)
    mismatch_input_count: int = Field(strict=True, ge=0, le=MAX_RESOLUTION_IDENTIFIERS)
    mapping_yield: float = Field(ge=0.0, le=1.0)
    mapped_entity_count_before_deduplication: int = Field(strict=True, ge=0)
    unique_mapped_entity_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_RESOLUTION_UNIQUE_ENTITIES,
    )
    resolution_previews: Annotated[
        tuple[EntityResolutionPreview, ...],
        Field(min_length=1, max_length=MAX_RESOLUTION_INPUT_PREVIEW),
    ]
    resolutions_truncated: bool
    retrieval: QueryRetrievalSummary
    interpretation_caveats: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(min_length=2, max_length=3),
    ]

    @model_validator(mode="after")
    def validate_resolution_counts(self) -> Self:
        if self.input_count < len(self.resolution_previews):
            raise ValueError("input_count cannot be smaller than resolution_previews")
        if self.resolutions_truncated != (self.input_count > len(self.resolution_previews)):
            raise ValueError("resolutions_truncated must match the input preview")
        if self.mapping_yield != self.mapped_input_count / self.input_count:
            raise ValueError("mapping_yield must match mapped_input_count / input_count")
        if any(
            count > self.input_count
            for count in (
                self.mapped_input_count,
                self.ambiguous_input_count,
                self.many_to_one_input_count,
                self.mismatch_input_count,
            )
        ):
            raise ValueError("resolution summary subsets cannot exceed input_count")
        return self


class KeggRelationType(StrEnum):
    GENE_TO_KO = "gene_to_ko"
    GENE_TO_PATHWAY = "gene_to_pathway"
    KO_TO_GENE = "ko_to_gene"
    KO_TO_PATHWAY = "ko_to_pathway"
    KO_TO_MODULE = "ko_to_module"
    KO_TO_REACTION = "ko_to_reaction"
    KO_TO_ENZYME = "ko_to_enzyme"
    KO_TO_BRITE = "ko_to_brite"
    ENZYME_TO_REACTION = "enzyme_to_reaction"
    REACTION_TO_ENZYME = "reaction_to_enzyme"
    REACTION_TO_KO = "reaction_to_ko"
    REACTION_TO_COMPOUND = "reaction_to_compound"
    REACTION_TO_GLYCAN = "reaction_to_glycan"
    REACTION_TO_PATHWAY = "reaction_to_pathway"
    COMPOUND_TO_REACTION = "compound_to_reaction"
    COMPOUND_TO_PATHWAY = "compound_to_pathway"
    GLYCAN_TO_REACTION = "glycan_to_reaction"
    GLYCAN_TO_PATHWAY = "glycan_to_pathway"
    DRUG_TO_PATHWAY = "drug_to_pathway"
    MODULE_TO_KO = "module_to_ko"
    MODULE_TO_PATHWAY = "module_to_pathway"
    MODULE_TO_REACTION = "module_to_reaction"
    PATHWAY_TO_KO = "pathway_to_ko"
    PATHWAY_TO_GENE = "pathway_to_gene"
    PATHWAY_TO_MODULE = "pathway_to_module"
    PATHWAY_TO_REACTION = "pathway_to_reaction"
    PATHWAY_TO_COMPOUND = "pathway_to_compound"
    PATHWAY_TO_GLYCAN = "pathway_to_glycan"
    GENOME_TO_TAXONOMY = "genome_to_taxonomy"
    TAXONOMY_TO_GENOME = "taxonomy_to_genome"


def relation_entity_kinds(
    relationship: KeggRelationType,
) -> tuple[KeggEntityKind, KeggEntityKind]:
    """Return the authoritative source and target kinds for one trace edge."""
    contract = link_relation_contract(KeggLinkRelationship(relationship.value))
    target_kind = (
        KeggEntityKind.GENE
        if relationship in {KeggRelationType.KO_TO_GENE, KeggRelationType.PATHWAY_TO_GENE}
        else KeggEntityKind(contract.target_database)
    )
    return (
        KeggEntityKind(contract.source_kind.value),
        target_kind,
    )


class TraceKeggRelationsRequest(FrozenModel):
    """Bounded traversal over a fixed allowlist of KEGG relationship directions."""

    seeds: Annotated[tuple[KeggEntityRef, ...], Field(min_length=1, max_length=MAX_TRACE_SEEDS)]
    edge_types: Annotated[
        tuple[KeggRelationType, ...], Field(min_length=1, max_length=len(KeggRelationType))
    ]
    organism_scope: str | None = Field(default=None, min_length=3, max_length=4)
    max_depth: int = Field(
        default=1,
        strict=True,
        ge=1,
        le=2,
        description="Maximum traversal depth, from 1 through 2; defaults to 1.",
    )
    max_nodes: int = Field(
        default=MAX_TRACE_NODES,
        strict=True,
        ge=1,
        le=MAX_TRACE_NODES,
        description=(
            "Maximum graph nodes retained, including seeds, from 1 through 200; defaults to 200."
        ),
    )
    max_edges: int = Field(
        default=MAX_TRACE_EDGES,
        strict=True,
        ge=1,
        le=MAX_TRACE_EDGES,
        description="Maximum relationship edges retained, from 1 through 500; defaults to 500.",
    )

    @model_validator(mode="after")
    def require_unique_inputs(self) -> Self:
        seed_keys = tuple((seed.kind, seed.identifier) for seed in self.seeds)
        if len(seed_keys) != len(set(seed_keys)):
            raise ValueError("trace seeds must be unique")
        if len(self.edge_types) != len(set(self.edge_types)):
            raise ValueError("trace edge_types must be unique")
        if len(self.seeds) > self.max_nodes:
            raise ValueError("max_nodes cannot be smaller than the seed count")
        seed_kinds = {seed.kind for seed in self.seeds}
        if not any(
            relation_entity_kinds(relationship)[0] in seed_kinds for relationship in self.edge_types
        ):
            raise ValueError("at least one edge type must be traversable from the supplied seeds")
        scoped_gene_edges = {
            KeggRelationType.KO_TO_GENE,
            KeggRelationType.PATHWAY_TO_GENE,
        }
        requested_scoped_edges = set(self.edge_types) & scoped_gene_edges
        if requested_scoped_edges:
            if self.organism_scope is None or not is_kegg_organism_code(self.organism_scope):
                raise ValueError("organism-scoped gene edges require one canonical organism_scope")
            if KeggRelationType.PATHWAY_TO_GENE in requested_scoped_edges and any(
                seed.kind is KeggEntityKind.PATHWAY and seed.identifier[:-5] != self.organism_scope
                for seed in self.seeds
            ):
                raise ValueError("organism-specific pathway seeds must match organism_scope")
        elif self.organism_scope is not None:
            raise ValueError("organism_scope is valid only for KO-to-gene or pathway-to-gene edges")
        module_source_edges = {
            relationship
            for relationship in self.edge_types
            if relation_entity_kinds(relationship)[0] is KeggEntityKind.MODULE
        }
        if module_source_edges and any(
            seed.kind is KeggEntityKind.MODULE and not _numbered_identifier(seed.identifier, "M")
            for seed in self.seeds
        ):
            raise ValueError("MODULE-source relation tracing supports only reference M identifiers")
        return self


class KeggRelationEdge(FrozenModel):
    """One typed endpoint-returned edge at its first traversal depth."""

    relationship: KeggRelationType
    source: KeggEntityRef
    target: KeggEntityRef
    depth: int = Field(strict=True, ge=1, le=2)
    provenance_batch_indexes: Annotated[
        tuple[Annotated[int, Field(strict=True, ge=0)], ...],
        Field(min_length=1, max_length=MAX_QUERY_PROVENANCE_BATCHES),
    ]

    @field_validator("provenance_batch_indexes")
    @classmethod
    def require_sorted_unique_provenance_indexes(
        cls,
        value: tuple[int, ...],
    ) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("provenance_batch_indexes must be sorted and unique")
        return value

    @model_validator(mode="after")
    def require_relationship_endpoint_kinds(self) -> Self:
        source_kind, target_kind = relation_entity_kinds(self.relationship)
        if self.source.kind is not source_kind or self.target.kind is not target_kind:
            raise ValueError("edge endpoint kinds must match the selected relationship")
        return self


class TraceKeggRelationsResult(FrozenModel):
    """Compact direct trace preview with complete retained nodes and edges."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    seed_count: int = Field(strict=True, ge=1, le=MAX_TRACE_SEEDS)
    node_count: int = Field(strict=True, ge=1, le=MAX_TRACE_NODES)
    edge_count: int = Field(strict=True, ge=0, le=MAX_TRACE_EDGES)
    node_preview: Annotated[
        tuple[KeggEntityRef, ...],
        Field(min_length=1, max_length=MAX_TRACE_NODE_PREVIEW),
    ]
    nodes_truncated: bool
    edge_preview: Annotated[
        tuple[KeggRelationEdge, ...],
        Field(max_length=MAX_TRACE_EDGE_PREVIEW),
    ]
    edges_truncated: bool
    retrieval: QueryRetrievalSummary
    interpretation_caveats: tuple[
        Literal[
            "KEGG relationships are database cross-references, not evidence of regulation, "
            "causality, or mechanism."
        ],
        Literal["A traced reference does not establish activity or phenotype."],
        Literal[
            "No returned edge means no matching relationship was retrieved within this bounded "
            "trace; it is not evidence of biological absence."
        ],
    ] = (
        "KEGG relationships are database cross-references, not evidence of regulation, "
        "causality, or mechanism.",
        "A traced reference does not establish activity or phenotype.",
        "No returned edge means no matching relationship was retrieved within this bounded "
        "trace; it is not evidence of biological absence.",
    )

    @model_validator(mode="after")
    def validate_trace_counts(self) -> Self:
        if self.node_count < len(self.node_preview) or self.edge_count < len(self.edge_preview):
            raise ValueError("trace counts cannot be smaller than direct previews")
        if self.nodes_truncated != (self.node_count > len(self.node_preview)):
            raise ValueError("nodes_truncated must match the node preview")
        if self.edges_truncated != (self.edge_count > len(self.edge_preview)):
            raise ValueError("edges_truncated must match the edge preview")
        if self.seed_count > self.node_count:
            raise ValueError("seed_count cannot exceed node_count")
        node_keys = tuple((node.kind, node.identifier) for node in self.node_preview)
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("trace node previews must be unique")
        edge_keys = tuple(
            (
                edge.relationship,
                edge.source.kind,
                edge.source.identifier,
                edge.target.kind,
                edge.target.identifier,
            )
            for edge in self.edge_preview
        )
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("trace edge previews must be unique")
        if any(
            index >= self.retrieval.batch_count
            for edge in self.edge_preview
            for index in edge.provenance_batch_indexes
        ):
            raise ValueError(
                "edge provenance_batch_indexes must reference retained full provenance"
            )
        return self


def _numbered_identifier(value: str, prefix: str) -> bool:
    number = value[1:]
    return len(value) == 6 and value.startswith(prefix) and number.isascii() and number.isdigit()


def _module_identifier(value: str) -> bool:
    if _numbered_identifier(value, "M"):
        return True
    prefix, separator, module = value.partition("_")
    return (
        separator == "_"
        and (is_kegg_organism_code(prefix) or _numbered_identifier(prefix, "T"))
        and _numbered_identifier(module, "M")
    )


def _positive_ascii_integer(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit() and value[0] in "123456789"


def _substance_entity_is_valid(kind: KeggEntityKind, identifier: str) -> bool:
    try:
        KeggEntityRef(kind=kind, identifier=identifier)
    except ValueError:
        return False
    return True


def _external_substance_number(
    namespace: SubstanceIdentifierNamespace,
    identifier: str,
) -> str | None:
    if namespace is SubstanceIdentifierNamespace.CHEBI:
        prefix_pattern = r"(?i:chebi):"
    elif namespace is SubstanceIdentifierNamespace.PUBCHEM_SID:
        prefix_pattern = r"(?i:(?:pubchem|sid)):"
    else:
        return None
    match = re.fullmatch(
        rf"(?:{prefix_pattern})?(?P<number>[1-9][0-9]*)",
        identifier,
    )
    return None if match is None else match.group("number")


def _bare_gene_label(value: str) -> bool:
    return (
        value.isascii()
        and value[0].isalnum()
        and all(character.isalnum() or character in "._-" for character in value)
    )


__all__ = [
    "MAX_ORGANISM_PATHWAY_PREVIEW",
    "MAX_ORGANISM_RESOLUTION_CANDIDATES",
    "MAX_QUERY_PROVENANCE_BATCHES",
    "MAX_QUERY_RELEASE_PREVIEW",
    "MAX_RESOLUTION_CANDIDATE_PREVIEW",
    "MAX_RESOLUTION_DIRECT_TEXT_CHARACTERS",
    "MAX_RESOLUTION_ENTITIES",
    "MAX_RESOLUTION_ENTITY_PREVIEW",
    "MAX_RESOLUTION_IDENTIFIERS",
    "MAX_RESOLUTION_INPUT_PREVIEW",
    "MAX_RESOLUTION_PATHWAY_DIRECT_PREVIEW",
    "MAX_RESOLUTION_TAXONOMY_PREVIEW",
    "MAX_RESOLUTION_UNIQUE_ENTITIES",
    "MAX_SEARCH_PREVIEW_MATCH_CHARACTERS",
    "MAX_SEARCH_PREVIEW_RESULTS",
    "MAX_SEARCH_RESULTS",
    "MAX_TRACE_EDGES",
    "MAX_TRACE_EDGE_PREVIEW",
    "MAX_TRACE_NODES",
    "MAX_TRACE_NODE_PREVIEW",
    "MAX_TRACE_SEEDS",
    "EntityResolution",
    "EntityResolutionPreview",
    "GeneIdentifierNamespace",
    "GeneResolutionRequest",
    "GeneResolutionTarget",
    "KeggEntityKind",
    "KeggEntityRef",
    "KeggRelationEdge",
    "KeggRelationType",
    "KeggSearchCandidatePreview",
    "KeggSearchDatabase",
    "KeggSearchMode",
    "MappingStatus",
    "OrganismCandidateMaterialization",
    "OrganismIdentifierNamespace",
    "OrganismPathwayDirectPreviewEntry",
    "OrganismPathwayPreviewEntry",
    "OrganismPathwaySummary",
    "OrganismResolutionRequest",
    "QueryRetrievalSummary",
    "ResolutionOperation",
    "ResolveKeggEntitiesRequest",
    "ResolveKeggEntitiesResult",
    "ResolvedEntityCandidate",
    "ResolvedEntityCandidatePreview",
    "SearchKeggEntriesRequest",
    "SearchKeggEntriesResult",
    "SubstanceIdentifierNamespace",
    "SubstanceResolutionRequest",
    "SubstanceResolutionTarget",
    "TaxonomyResolutionRank",
    "TraceKeggRelationsRequest",
    "TraceKeggRelationsResult",
    "relation_entity_kinds",
]
