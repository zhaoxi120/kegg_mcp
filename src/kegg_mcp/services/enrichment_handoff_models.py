"""Typed contracts and fixed bounds for statistics-free enrichment handoffs."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationInfo, field_validator, model_validator

from kegg_mcp.domain.annotations import FrozenModel, normalize_identifier_label
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    is_kegg_brite_identifier,
    is_kegg_gene_identifier,
    is_kegg_organism_code,
)
from kegg_mcp.services.query_models import QueryRetrievalSummary

ENRICHMENT_HANDOFF_SCHEMA_VERSION = "1"
ENRICHMENT_HANDOFF_MANIFEST = "handoff_manifest.json"
MAX_ENRICHMENT_IDENTIFIERS = 5_000
MAX_ENRICHMENT_EXPANDED_MAPPINGS = 100_000
MAX_ENRICHMENT_GENE_SETS = 50_000
MAX_ENRICHMENT_MEMBERSHIPS = 250_000
MAX_ENRICHMENT_KEGG_REQUESTS = 100
MAX_ENRICHMENT_RELATIONSHIP_ROWS = 50_000
MAX_ENRICHMENT_RESPONSE_BYTES = 25_000_000
MAX_ENRICHMENT_ARTIFACT_BYTES = 24_000_000
MAX_ENRICHMENT_BUNDLE_BYTES = 48_000_000
MAX_ENRICHMENT_PROVENANCE_BATCHES = 100

ENRICHMENT_NO_STATISTICS_CAVEAT = (
    "This handoff contains deterministic identifier mappings and KEGG reference memberships only; "
    "it does not contain enrichment statistics, p-values, FDR, GSEA scores, pathway activity, or "
    "biological presence or absence conclusions."
)

_BARE_GENE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


class EnrichmentIdentifierNamespace(StrEnum):
    """Explicit input namespaces supported by the enrichment handoff."""

    KO = "ko"
    KEGG_GENE = "kegg_gene"
    NCBI_GENEID = "ncbi_geneid"
    NCBI_PROTEINID = "ncbi_proteinid"
    UNIPROT = "uniprot"


class EnrichmentGeneSetType(StrEnum):
    """KEGG reference classes that can become external gene-set inputs."""

    PATHWAY = "pathway"
    MODULE = "module"
    BRITE = "brite"


class EnrichmentMappingStatus(StrEnum):
    """Deterministic outcome for one supplied identifier."""

    MAPPED = "mapped"
    UNMAPPED = "unmapped"
    ORGANISM_MISMATCH = "organism_mismatch"


class EnrichmentIdentifierSet(FrozenModel):
    """One ordered, duplicate-free foreground or universe identifier set."""

    namespace: EnrichmentIdentifierNamespace
    identifiers: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=256)], ...],
        Field(min_length=1, max_length=MAX_ENRICHMENT_IDENTIFIERS),
    ]

    @field_validator("identifiers")
    @classmethod
    def normalize_identifiers(
        cls,
        values: tuple[str, ...],
        info: ValidationInfo,
    ) -> tuple[str, ...]:
        namespace = info.data.get("namespace")
        normalized: list[str] = []
        for value in values:
            if namespace is EnrichmentIdentifierNamespace.KO:
                ko_id, _ = try_normalize_ko_id(value)
                if ko_id is None:
                    raise ValueError("ko identifiers must contain exact KEGG K numbers")
                normalized.append(ko_id)
            else:
                normalized.append(
                    normalize_identifier_label(value, field_name="enrichment identifier")
                )
        if len(normalized) != len(set(normalized)):
            raise ValueError("enrichment identifiers must be unique")
        return tuple(normalized)

    @model_validator(mode="after")
    def validate_namespace_identifiers(self) -> Self:
        if self.namespace is EnrichmentIdentifierNamespace.KO:
            return self
        if self.namespace is EnrichmentIdentifierNamespace.KEGG_GENE:
            valid = all(is_kegg_gene_identifier(value) for value in self.identifiers)
        elif self.namespace is EnrichmentIdentifierNamespace.NCBI_GENEID:
            valid = all(
                value.isascii() and value.isdigit() and not value.startswith("0")
                for value in self.identifiers
            )
        else:
            valid = all(_BARE_GENE_IDENTIFIER.fullmatch(value) for value in self.identifiers)
        if not valid:
            raise ValueError("identifier is incompatible with the selected enrichment namespace")
        return self


class EnrichmentHandoffRequest(FrozenModel):
    """A foreground and explicit universe prepared without statistical testing."""

    target: Literal["enrichment"]
    foreground: EnrichmentIdentifierSet
    universe: EnrichmentIdentifierSet
    organism: str | None = Field(default=None, min_length=3, max_length=4)
    gene_sets: Annotated[
        tuple[EnrichmentGeneSetType, ...],
        Field(min_length=1, max_length=len(EnrichmentGeneSetType)),
    ] = (EnrichmentGeneSetType.PATHWAY,)
    brite_ids: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=100)], ...],
        Field(max_length=25),
    ] = ()

    @field_validator("organism")
    @classmethod
    def validate_organism(cls, value: str | None) -> str | None:
        if value is not None and not is_kegg_organism_code(value):
            raise ValueError("organism must be a canonical KEGG organism code")
        return value

    @field_validator("gene_sets")
    @classmethod
    def canonicalize_gene_sets(
        cls,
        values: tuple[EnrichmentGeneSetType, ...],
    ) -> tuple[EnrichmentGeneSetType, ...]:
        if len(values) != len(set(values)):
            raise ValueError("gene_sets must be unique")
        return tuple(target for target in EnrichmentGeneSetType if target in values)

    @model_validator(mode="after")
    def validate_handoff_scope(self) -> Self:
        if self.foreground.namespace is not self.universe.namespace:
            raise ValueError("foreground and universe must use the same identifier namespace")
        if not set(self.foreground.identifiers).issubset(self.universe.identifiers):
            raise ValueError("foreground identifiers must be an explicit subset of universe")
        if self.foreground.namespace is EnrichmentIdentifierNamespace.KO:
            if self.organism is not None:
                raise ValueError("organism is not used for direct KO enrichment input")
        elif self.organism is None:
            raise ValueError("gene identifier enrichment input requires organism context")
        if (
            self.foreground.namespace is EnrichmentIdentifierNamespace.KEGG_GENE
            and self.organism is not None
            and any(
                identifier.partition(":")[0] != self.organism
                for identifier in self.universe.identifiers
            )
        ):
            raise ValueError("KEGG gene identifiers must match the requested organism")
        if len(self.brite_ids) != len(set(self.brite_ids)):
            raise ValueError("brite_ids must be unique")
        if any(not is_kegg_brite_identifier(identifier) for identifier in self.brite_ids):
            raise ValueError("brite_ids must contain supported BRITE hierarchy identifiers")
        includes_brite = EnrichmentGeneSetType.BRITE in self.gene_sets
        if includes_brite and not self.brite_ids:
            raise ValueError(
                "BRITE enrichment handoff requires explicit brite_ids; hierarchy-file "
                "relationships are not category gene sets"
            )
        if self.brite_ids and not includes_brite:
            raise ValueError("brite_ids are valid only when BRITE gene sets are requested")
        return self


class EnrichmentInputMapping(FrozenModel):
    """Complete bounded mapping outcome for one supplied input identifier."""

    input_identifier: str = Field(min_length=1, max_length=256)
    status: EnrichmentMappingStatus
    kegg_genes: Annotated[tuple[str, ...], Field(max_length=MAX_ENRICHMENT_IDENTIFIERS)]
    organism_mismatch_genes: Annotated[
        tuple[str, ...],
        Field(max_length=MAX_ENRICHMENT_IDENTIFIERS),
    ] = ()
    ko_ids: Annotated[tuple[str, ...], Field(max_length=MAX_ENRICHMENT_IDENTIFIERS)]
    organism_mismatch_count: int = Field(strict=True, ge=0)
    expanded_mapping_count: int = Field(strict=True, ge=0)
    ambiguous: bool

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        if len(self.kegg_genes) != len(set(self.kegg_genes)):
            raise ValueError("mapped KEGG genes must be unique")
        if len(self.organism_mismatch_genes) != len(set(self.organism_mismatch_genes)):
            raise ValueError("organism-mismatch genes must be unique")
        if self.organism_mismatch_count != len(self.organism_mismatch_genes):
            raise ValueError("organism mismatch count must match retained candidates")
        if len(self.ko_ids) != len(set(self.ko_ids)):
            raise ValueError("mapped K numbers must be unique")
        expected_status = (
            EnrichmentMappingStatus.MAPPED
            if self.ko_ids
            else (
                EnrichmentMappingStatus.ORGANISM_MISMATCH
                if not self.kegg_genes and self.organism_mismatch_count
                else EnrichmentMappingStatus.UNMAPPED
            )
        )
        if self.status is not expected_status:
            raise ValueError("mapping status must match the deterministic mapping outcome")
        if self.ambiguous != (len(self.kegg_genes) > 1 or len(self.ko_ids) > 1):
            raise ValueError("ambiguous must match the retained gene and KO candidates")
        if self.status is EnrichmentMappingStatus.MAPPED and self.expanded_mapping_count < 1:
            raise ValueError("mapped identifiers require at least one expanded mapping")
        if self.status is not EnrichmentMappingStatus.MAPPED and self.expanded_mapping_count:
            raise ValueError("unmapped identifiers cannot contain expanded mappings")
        return self


class EnrichmentExpandedMapping(FrozenModel):
    """One exact retained input-to-gene-to-KO crosswalk row."""

    input_identifier: str = Field(min_length=1, max_length=256)
    kegg_gene: str | None = Field(default=None, min_length=1, max_length=256)
    ko_id: str = Field(pattern=r"^K[0-9]{5}$")

    @model_validator(mode="after")
    def validate_gene(self) -> Self:
        if self.kegg_gene is not None and not is_kegg_gene_identifier(self.kegg_gene):
            raise ValueError("kegg_gene must be a qualified KEGG gene identifier")
        return self


class EnrichmentMappingSummary(FrozenModel):
    """Mapping-yield accounting for one role in the handoff."""

    role: Literal["foreground", "universe"]
    input_count: int = Field(strict=True, ge=1, le=MAX_ENRICHMENT_IDENTIFIERS)
    mapped_input_count: int = Field(strict=True, ge=0)
    unmapped_input_count: int = Field(strict=True, ge=0)
    organism_mismatch_input_count: int = Field(strict=True, ge=0)
    ambiguous_input_count: int = Field(strict=True, ge=0)
    unique_kegg_gene_count: int = Field(strict=True, ge=0)
    unique_ko_count: int = Field(strict=True, ge=0)
    expanded_mapping_count: int = Field(strict=True, ge=0)
    mapping_yield: float = Field(strict=True, ge=0.0, le=1.0)
    organism_mismatch_candidate_count: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        if (
            self.mapped_input_count + self.unmapped_input_count + self.organism_mismatch_input_count
            != self.input_count
        ):
            raise ValueError("mapping outcome counts must partition the supplied inputs")
        if self.mapping_yield != self.mapped_input_count / self.input_count:
            raise ValueError("mapping_yield must use all supplied identifiers as denominator")
        return self


class EnrichmentGeneSet(FrozenModel):
    """One deterministic KEGG reference set expressed over universe identifiers."""

    target: EnrichmentGeneSetType
    term_id: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=4_096)
    ko_ids: Annotated[
        tuple[str, ...],
        Field(min_length=1, max_length=MAX_ENRICHMENT_IDENTIFIERS),
    ]
    universe_identifiers: Annotated[
        tuple[str, ...],
        Field(min_length=1, max_length=MAX_ENRICHMENT_IDENTIFIERS),
    ]

    @model_validator(mode="after")
    def validate_membership(self) -> Self:
        if len(self.ko_ids) != len(set(self.ko_ids)):
            raise ValueError("gene-set K numbers must be unique")
        if len(self.universe_identifiers) != len(set(self.universe_identifiers)):
            raise ValueError("gene-set universe members must be unique")
        return self


class EnrichmentGeneSetSummary(FrozenModel):
    """Term and membership counts for one requested KEGG reference class."""

    target: EnrichmentGeneSetType
    term_count: int = Field(strict=True, ge=0, le=MAX_ENRICHMENT_GENE_SETS)
    membership_count: int = Field(strict=True, ge=0, le=MAX_ENRICHMENT_MEMBERSHIPS)


class EnrichmentMappingAudit(FrozenModel):
    """Complete audit and provenance for one generated handoff."""

    schema_version: Literal["1"] = ENRICHMENT_HANDOFF_SCHEMA_VERSION
    request: EnrichmentHandoffRequest
    foreground: EnrichmentMappingSummary
    universe: EnrichmentMappingSummary
    mappings: Annotated[
        tuple[EnrichmentInputMapping, ...],
        Field(min_length=1, max_length=MAX_ENRICHMENT_IDENTIFIERS),
    ]
    expanded_mappings: Annotated[
        tuple[EnrichmentExpandedMapping, ...],
        Field(max_length=MAX_ENRICHMENT_EXPANDED_MAPPINGS),
    ]
    gene_sets: Annotated[
        tuple[EnrichmentGeneSetSummary, ...],
        Field(min_length=1, max_length=len(EnrichmentGeneSetType)),
    ]
    brite_resolved_ids: Annotated[tuple[str, ...], Field(max_length=25)] = ()
    brite_missing_ids: Annotated[tuple[str, ...], Field(max_length=25)] = ()
    brite_unmatched_ko_ids: Annotated[
        tuple[Annotated[str, Field(pattern=r"^K[0-9]{5}$")], ...],
        Field(max_length=MAX_ENRICHMENT_IDENTIFIERS),
    ] = ()
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...],
        Field(max_length=MAX_ENRICHMENT_PROVENANCE_BATCHES),
    ]
    retrieval: QueryRetrievalSummary
    ambiguity_policy: Literal["expand_all_candidates"] = "expand_all_candidates"
    statistical_tests_performed: Literal[False] = False
    interpretation_caveat: Literal[
        "This handoff contains deterministic identifier mappings and KEGG reference memberships "
        "only; it does not contain enrichment statistics, p-values, FDR, GSEA scores, pathway "
        "activity, or biological presence or absence conclusions."
    ] = ENRICHMENT_NO_STATISTICS_CAVEAT


class EnrichmentHandoffArtifact(FrozenModel):
    """One committed local handoff artifact."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    mime_type: str = Field(min_length=1, max_length=100)
    byte_size: int = Field(strict=True, ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    path: str = Field(min_length=1, max_length=4_096)


class EnrichmentHandoffBundle(FrozenModel):
    """Stable absolute paths for the committed enrichment handoff."""

    output_directory: str = Field(min_length=1, max_length=4_096)
    mapped_foreground: str = Field(min_length=1, max_length=4_096)
    mapped_universe: str = Field(min_length=1, max_length=4_096)
    unmapped: str = Field(min_length=1, max_length=4_096)
    gene_sets: str = Field(min_length=1, max_length=4_096)
    mapping_audit: str = Field(min_length=1, max_length=4_096)
    manifest: str = Field(min_length=1, max_length=4_096)
    artifacts: Annotated[
        tuple[EnrichmentHandoffArtifact, ...],
        Field(min_length=6, max_length=6),
    ]


class EnrichmentHandoffDetail(FrozenModel):
    """Complete bounded in-memory result before local serialization."""

    audit: EnrichmentMappingAudit
    gene_sets: Annotated[
        tuple[EnrichmentGeneSet, ...],
        Field(max_length=MAX_ENRICHMENT_GENE_SETS),
    ]


class EnrichmentHandoffResult(FrozenModel):
    """Compact result for one durable enrichment-input bundle."""

    target: Literal["enrichment"] = "enrichment"
    bundle: EnrichmentHandoffBundle
    foreground: EnrichmentMappingSummary
    universe: EnrichmentMappingSummary
    gene_sets: Annotated[
        tuple[EnrichmentGeneSetSummary, ...],
        Field(min_length=1, max_length=len(EnrichmentGeneSetType)),
    ]
    retrieval: QueryRetrievalSummary
    interpretation_caveat: Literal[
        "This handoff contains deterministic identifier mappings and KEGG reference memberships "
        "only; it does not contain enrichment statistics, p-values, FDR, GSEA scores, pathway "
        "activity, or biological presence or absence conclusions."
    ] = ENRICHMENT_NO_STATISTICS_CAVEAT


__all__ = [
    "ENRICHMENT_HANDOFF_MANIFEST",
    "ENRICHMENT_HANDOFF_SCHEMA_VERSION",
    "ENRICHMENT_NO_STATISTICS_CAVEAT",
    "MAX_ENRICHMENT_ARTIFACT_BYTES",
    "MAX_ENRICHMENT_BUNDLE_BYTES",
    "MAX_ENRICHMENT_EXPANDED_MAPPINGS",
    "MAX_ENRICHMENT_GENE_SETS",
    "MAX_ENRICHMENT_IDENTIFIERS",
    "MAX_ENRICHMENT_KEGG_REQUESTS",
    "MAX_ENRICHMENT_MEMBERSHIPS",
    "MAX_ENRICHMENT_PROVENANCE_BATCHES",
    "MAX_ENRICHMENT_RELATIONSHIP_ROWS",
    "MAX_ENRICHMENT_RESPONSE_BYTES",
    "EnrichmentExpandedMapping",
    "EnrichmentGeneSet",
    "EnrichmentGeneSetSummary",
    "EnrichmentGeneSetType",
    "EnrichmentHandoffArtifact",
    "EnrichmentHandoffBundle",
    "EnrichmentHandoffDetail",
    "EnrichmentHandoffRequest",
    "EnrichmentHandoffResult",
    "EnrichmentIdentifierNamespace",
    "EnrichmentIdentifierSet",
    "EnrichmentInputMapping",
    "EnrichmentMappingAudit",
    "EnrichmentMappingStatus",
    "EnrichmentMappingSummary",
]
