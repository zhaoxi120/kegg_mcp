"""Typed service contracts for bounded KEGG search, resolution, and relation tracing."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self, TypeAlias

from pydantic import Field, field_validator, model_validator

from kegg_mcp.domain.annotations import FrozenModel, normalize_identifier_label, validate_utf8_text
from kegg_mcp.kegg.contracts import (
    FindRequest,
    KeggBatchProvenance,
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
MAX_TRACE_SEEDS = 50
MAX_TRACE_NODES = 200
MAX_TRACE_EDGES = 500
MAX_QUERY_PROVENANCE_BATCHES = 200
MAX_MATCH_TEXT_CHARACTERS = 10_000
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
    GENOME = "genome"
    ORGANISM = "organism"


class KeggSearchMode(StrEnum):
    """Supported keyword and compound-candidate search modes."""

    KEYWORD = "keyword"
    FORMULA = "formula"
    EXACT_MASS = "exact_mass"
    MOLECULAR_WEIGHT = "molecular_weight"


class SearchKeggEntriesRequest(FrozenModel):
    """One bounded KEGG FIND request and direct-result projection."""

    database: KeggSearchDatabase
    query: str = Field(min_length=1, max_length=256)
    mode: KeggSearchMode = KeggSearchMode.KEYWORD
    max_results: int = Field(default=20, strict=True, ge=1, le=MAX_SEARCH_RESULTS)

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
        if (
            self.mode is not KeggSearchMode.KEYWORD
            and self.database is not KeggSearchDatabase.COMPOUND
        ):
            raise ValueError("formula and mass modes are supported only for compound search")
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


class KeggSearchCandidate(FrozenModel):
    """One endpoint-returned candidate without an invented relevance score."""

    entity: KeggEntityRef
    raw_match: str = Field(min_length=1, max_length=MAX_MATCH_TEXT_CHARACTERS)
    name: str | None = Field(default=None, max_length=MAX_MATCH_TEXT_CHARACTERS)

    @field_validator("raw_match", "name")
    @classmethod
    def validate_match_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_utf8_text(value, field_name="search match")


class SearchKeggEntriesResult(FrozenModel):
    """Retained complete FIND result plus a bounded candidate projection."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    database: KeggSearchDatabase
    mode: KeggSearchMode
    observed_count: int = Field(strict=True, ge=0)
    returned_count: int = Field(strict=True, ge=0, le=MAX_SEARCH_RESULTS)
    candidates: Annotated[tuple[KeggSearchCandidate, ...], Field(max_length=MAX_SEARCH_RESULTS)]
    truncated: bool
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_QUERY_PROVENANCE_BATCHES)
    ]
    interpretation_caveats: tuple[
        Literal["Candidates are endpoint matches, not relevance-ranked or selected best matches."],
        Literal["Exact-mass matches are compound candidates, not compound identifications."],
    ] = (
        "Candidates are endpoint matches, not relevance-ranked or selected best matches.",
        "Exact-mass matches are compound candidates, not compound identifications.",
    )

    @model_validator(mode="after")
    def validate_projection_counts(self) -> Self:
        if self.returned_count != len(self.candidates):
            raise ValueError("returned_count must match candidates")
        if self.observed_count < self.returned_count:
            raise ValueError("observed_count cannot be smaller than returned_count")
        if self.truncated != (self.observed_count > self.returned_count):
            raise ValueError("truncated must match the candidate projection")
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


class AmbiguityPolicy(StrEnum):
    """The only supported policy preserves every endpoint-returned candidate."""

    REPORT_ALL = "report_all"


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
    ambiguity_policy: Literal[AmbiguityPolicy.REPORT_ALL] = AmbiguityPolicy.REPORT_ALL

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
    ambiguity_policy: Literal[AmbiguityPolicy.REPORT_ALL] = AmbiguityPolicy.REPORT_ALL

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
        return self


ResolveKeggEntitiesRequest: TypeAlias = Annotated[
    GeneResolutionRequest | OrganismResolutionRequest,
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
    name: str = Field(min_length=1, max_length=MAX_MATCH_TEXT_CHARACTERS)

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
        tuple[ResolvedEntityCandidate, ...], Field(max_length=MAX_RESOLUTION_ENTITIES)
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


class ResolveKeggEntitiesResult(FrozenModel):
    """Complete retained crosswalk and bounded per-input mapping statuses."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    kind: Literal["gene", "organism"]
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
        le=MAX_RESOLUTION_ENTITIES,
    )
    resolutions: Annotated[
        tuple[EntityResolution, ...], Field(min_length=1, max_length=MAX_RESOLUTION_IDENTIFIERS)
    ]
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_QUERY_PROVENANCE_BATCHES)
    ]
    interpretation_caveats: tuple[
        Literal["Unmapped identifiers are not evidence that the biological entity does not exist."],
        Literal["Ambiguous candidates are reported without automatic selection."],
        Literal[
            "Organism-specific pathway directory entries are KEGG references and do not establish "
            "pathway presence, completeness, activity, flux, or phenotype."
        ],
    ] = (
        "Unmapped identifiers are not evidence that the biological entity does not exist.",
        "Ambiguous candidates are reported without automatic selection.",
        "Organism-specific pathway directory entries are KEGG references and do not establish "
        "pathway presence, completeness, activity, flux, or phenotype.",
    )

    @model_validator(mode="after")
    def validate_resolution_counts(self) -> Self:
        if self.input_count != len(self.resolutions):
            raise ValueError("input_count must match resolutions")
        mapped = sum(
            item.status
            in {
                MappingStatus.ONE_TO_ONE,
                MappingStatus.ONE_TO_MANY,
                MappingStatus.MANY_TO_ONE,
            }
            for item in self.resolutions
        )
        ambiguous = sum(item.status is MappingStatus.ONE_TO_MANY for item in self.resolutions)
        many_to_one = sum(item.status is MappingStatus.MANY_TO_ONE for item in self.resolutions)
        mismatches = sum(item.discarded_organism_mismatch_count > 0 for item in self.resolutions)
        if (
            self.mapped_input_count != mapped
            or self.ambiguous_input_count != ambiguous
            or self.many_to_one_input_count != many_to_one
            or self.mismatch_input_count != mismatches
        ):
            raise ValueError("resolution summary counts do not match resolutions")
        if self.mapping_yield != mapped / self.input_count:
            raise ValueError("mapping_yield must match mapped_input_count / input_count")
        return self


class KeggRelationType(StrEnum):
    GENE_TO_KO = "gene_to_ko"
    GENE_TO_PATHWAY = "gene_to_pathway"
    KO_TO_PATHWAY = "ko_to_pathway"
    KO_TO_MODULE = "ko_to_module"
    KO_TO_REACTION = "ko_to_reaction"
    KO_TO_ENZYME = "ko_to_enzyme"
    KO_TO_BRITE = "ko_to_brite"
    ENZYME_TO_REACTION = "enzyme_to_reaction"
    REACTION_TO_ENZYME = "reaction_to_enzyme"
    REACTION_TO_KO = "reaction_to_ko"
    REACTION_TO_COMPOUND = "reaction_to_compound"
    REACTION_TO_PATHWAY = "reaction_to_pathway"
    COMPOUND_TO_REACTION = "compound_to_reaction"
    COMPOUND_TO_PATHWAY = "compound_to_pathway"
    PATHWAY_TO_KO = "pathway_to_ko"
    PATHWAY_TO_REACTION = "pathway_to_reaction"
    PATHWAY_TO_COMPOUND = "pathway_to_compound"
    GENOME_TO_TAXONOMY = "genome_to_taxonomy"
    TAXONOMY_TO_GENOME = "taxonomy_to_genome"


def relation_entity_kinds(
    relationship: KeggRelationType,
) -> tuple[KeggEntityKind, KeggEntityKind]:
    """Return the authoritative source and target kinds for one trace edge."""
    contract = link_relation_contract(KeggLinkRelationship(relationship.value))
    return (
        KeggEntityKind(contract.source_kind.value),
        KeggEntityKind(contract.target_database),
    )


class TraceKeggRelationsRequest(FrozenModel):
    """Bounded traversal over a fixed allowlist of KEGG relationship directions."""

    seeds: Annotated[tuple[KeggEntityRef, ...], Field(min_length=1, max_length=MAX_TRACE_SEEDS)]
    edge_types: Annotated[
        tuple[KeggRelationType, ...], Field(min_length=1, max_length=len(KeggRelationType))
    ]
    max_depth: int = Field(default=1, strict=True, ge=1, le=2)
    max_nodes: int = Field(default=MAX_TRACE_NODES, strict=True, ge=1, le=MAX_TRACE_NODES)
    max_edges: int = Field(default=MAX_TRACE_EDGES, strict=True, ge=1, le=MAX_TRACE_EDGES)

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
    """Complete retained trace and bounded typed node/edge projection."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    seed_count: int = Field(strict=True, ge=1, le=MAX_TRACE_SEEDS)
    node_count: int = Field(strict=True, ge=1, le=MAX_TRACE_NODES)
    edge_count: int = Field(strict=True, ge=0, le=MAX_TRACE_EDGES)
    nodes: Annotated[tuple[KeggEntityRef, ...], Field(min_length=1, max_length=MAX_TRACE_NODES)]
    edges: Annotated[tuple[KeggRelationEdge, ...], Field(max_length=MAX_TRACE_EDGES)]
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_QUERY_PROVENANCE_BATCHES)
    ]
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
        if self.node_count != len(self.nodes) or self.edge_count != len(self.edges):
            raise ValueError("trace counts must match the direct node and edge projections")
        if self.seed_count > self.node_count:
            raise ValueError("seed_count cannot exceed node_count")
        node_keys = tuple((node.kind, node.identifier) for node in self.nodes)
        if len(node_keys) != len(set(node_keys)):
            raise ValueError("trace nodes must be unique")
        known_nodes = set(node_keys)
        edge_keys = tuple(
            (
                edge.relationship,
                edge.source.kind,
                edge.source.identifier,
                edge.target.kind,
                edge.target.identifier,
            )
            for edge in self.edges
        )
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("trace edges must be unique")
        if any(
            (edge.source.kind, edge.source.identifier) not in known_nodes
            or (edge.target.kind, edge.target.identifier) not in known_nodes
            for edge in self.edges
        ):
            raise ValueError("trace edge endpoints must appear in nodes")
        if any(
            index >= len(self.provenance)
            for edge in self.edges
            for index in edge.provenance_batch_indexes
        ):
            raise ValueError("edge provenance_batch_indexes must reference result provenance")
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


def _bare_gene_label(value: str) -> bool:
    return (
        value.isascii()
        and value[0].isalnum()
        and all(character.isalnum() or character in "._-" for character in value)
    )


__all__ = [
    "MAX_ORGANISM_PATHWAY_PREVIEW",
    "MAX_QUERY_PROVENANCE_BATCHES",
    "MAX_RESOLUTION_ENTITIES",
    "MAX_RESOLUTION_IDENTIFIERS",
    "MAX_SEARCH_RESULTS",
    "MAX_TRACE_EDGES",
    "MAX_TRACE_NODES",
    "MAX_TRACE_SEEDS",
    "AmbiguityPolicy",
    "EntityResolution",
    "GeneIdentifierNamespace",
    "GeneResolutionRequest",
    "GeneResolutionTarget",
    "KeggEntityKind",
    "KeggEntityRef",
    "KeggRelationEdge",
    "KeggRelationType",
    "KeggSearchCandidate",
    "KeggSearchDatabase",
    "KeggSearchMode",
    "MappingStatus",
    "OrganismIdentifierNamespace",
    "OrganismPathwayPreviewEntry",
    "OrganismPathwaySummary",
    "OrganismResolutionRequest",
    "ResolutionOperation",
    "ResolveKeggEntitiesRequest",
    "ResolveKeggEntitiesResult",
    "ResolvedEntityCandidate",
    "SearchKeggEntriesRequest",
    "SearchKeggEntriesResult",
    "TaxonomyResolutionRank",
    "TraceKeggRelationsRequest",
    "TraceKeggRelationsResult",
    "relation_entity_kinds",
]
