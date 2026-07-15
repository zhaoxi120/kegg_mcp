"""Pure, bounded, and descriptive KO coverage for KEGG pathways."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnalysisUnit,
    AnnotationDataset,
    DecisionPolicyReference,
    EvidenceMode,
    FrozenModel,
    KNumber,
    SourceProvenance,
    build_ko_evidence_view,
    select_ko_ids,
    validate_utf8_text,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import (
    GetResult,
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggFlatFileEntry,
    KeggFlatFileField,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggOperation,
    LinkResult,
    is_kegg_organism_code,
)

PATHWAY_COVERAGE_METHOD = "unique_detected_kos_over_unique_reference_kos"
PATHWAY_COVERAGE_VERSION = "2"

_MAX_PROVENANCE_BATCHES = 64
_MAX_DATASET_SOURCES = 128
_MAX_PATHWAY_CLASS_LINES = 32
_KO_LINK_TARGET = re.compile(r"^ko:(K[0-9]{5})$")
# KEGG PATHWAY CLASS wording confirmed from public flat files on 2026-07-14.
_BROAD_PATHWAY_CLASS = "Global and overview maps"

NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
TaxonId = Annotated[int, Field(strict=True, gt=0)]
CoverageRatio = Annotated[float, Field(strict=True, ge=0.0, le=1.0)]
ReferenceProvenance = Annotated[
    tuple[KeggBatchProvenance, ...],
    Field(min_length=1, max_length=_MAX_PROVENANCE_BATCHES),
]


class PathwayReferenceNamespace(StrEnum):
    """KEGG pathway namespace used to construct the KO denominator."""

    KO = "ko"
    MAP = "map"
    ORGANISM = "organism"


class PathwayReferenceScope(StrEnum):
    """Scope explicitly derived from PATHWAY metadata, never from its number."""

    STANDARD = "standard"
    GLOBAL_OR_OVERVIEW = "global_or_overview"


class PathwayCoverageStatus(StrEnum):
    """Whether the retrieved denominator supports a coverage calculation."""

    EVALUATED = "evaluated"
    NOT_EVALUABLE = "not_evaluable"


class PathwayInputKind(StrEnum):
    """Evidence context supplied to the pure KO coverage calculation."""

    KO_ONLY = "ko_only"
    ORGANISM_GENE_CONTEXT = "organism_gene_context"


class PathwayReferenceExclusionReason(StrEnum):
    """Why one retrieved relationship did not enter the KO denominator."""

    INVALID_IDENTIFIER = "invalid_identifier"
    NON_KO_IDENTIFIER = "non_ko_identifier"
    UNSUPPORTED_ENTRY = "unsupported_entry"
    SOURCE_EXCLUSION = "source_exclusion"


class PathwayCoverageWarningCode(StrEnum):
    """Stable cautions attached to descriptive pathway ratios."""

    DESCRIPTIVE_RATIO = "DESCRIPTIVE_RATIO"
    NO_REFERENCE_KOS = "NO_REFERENCE_KOS"
    GLOBAL_OR_OVERVIEW_REFERENCE = "GLOBAL_OR_OVERVIEW_REFERENCE"
    STALE_REFERENCE = "STALE_REFERENCE"
    REFERENCE_EXCLUSIONS = "REFERENCE_EXCLUSIONS"
    DUPLICATE_RELATIONSHIPS = "DUPLICATE_RELATIONSHIPS"
    ISOLATE_GENOME_CONTEXT = "ISOLATE_GENOME_CONTEXT"
    ISOLATE_PROTEOME_CONTEXT = "ISOLATE_PROTEOME_CONTEXT"
    MAG_CONTEXT = "MAG_CONTEXT"
    PANGENOME_CONTEXT = "PANGENOME_CONTEXT"
    METAGENOMIC_COMMUNITY_CONTEXT = "METAGENOMIC_COMMUNITY_CONTEXT"
    MIXED_CONTEXT = "MIXED_CONTEXT"
    UNKNOWN_CONTEXT = "UNKNOWN_CONTEXT"
    DETECTED_PREVIEW_TRUNCATED = "DETECTED_PREVIEW_TRUNCATED"
    MISSING_PREVIEW_TRUNCATED = "MISSING_PREVIEW_TRUNCATED"
    EXCLUSION_PREVIEW_TRUNCATED = "EXCLUSION_PREVIEW_TRUNCATED"


def _validate_pathway_identity(
    namespace: PathwayReferenceNamespace,
    pathway_id: str,
    organism_code: str | None,
) -> None:
    prefix, number = pathway_id[:-5], pathway_id[-5:]
    if not number.isascii() or not number.isdigit():
        raise ValueError("pathway_id must end with five ASCII digits")
    if namespace is PathwayReferenceNamespace.KO:
        if prefix != "ko" or organism_code is not None:
            raise ValueError("KO references require a koNNNNN identifier without organism code")
    elif namespace is PathwayReferenceNamespace.MAP:
        if prefix != "map" or organism_code is not None:
            raise ValueError("map references require a mapNNNNN identifier without organism code")
    elif (
        organism_code is None or not is_kegg_organism_code(organism_code) or prefix != organism_code
    ):
        raise ValueError(
            "organism references require a matching canonical organism code and prefix"
        )


class PathwayReferenceExclusion(FrozenModel):
    """One bounded source entry explicitly excluded from the denominator."""

    entry: str = Field(min_length=1, max_length=256)
    reason: PathwayReferenceExclusionReason

    @field_validator("entry")
    @classmethod
    def validate_entry(cls, value: str) -> str:
        validate_utf8_text(value, field_name="pathway reference exclusion")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("pathway reference exclusions must not contain control characters")
        return value


class OrganismGeneContext(FrozenModel):
    """Bounded provenance that KO evidence came from one KEGG organism gene context."""

    kegg_organism_code: str = Field(min_length=3, max_length=4)
    qualified_gene_count: PositiveCount

    @field_validator("kegg_organism_code")
    @classmethod
    def validate_organism_code(cls, value: str) -> str:
        if not is_kegg_organism_code(value):
            raise ValueError("kegg_organism_code must be a canonical KEGG organism code")
        return value


class PathwayInputContext(FrozenModel):
    """Serializable interpretation context without caller-supplied dataset attributes."""

    kind: PathwayInputKind = PathwayInputKind.KO_ONLY
    organism_gene_context: OrganismGeneContext | None = None

    @model_validator(mode="after")
    def validate_context(self) -> Self:
        if self.kind is PathwayInputKind.KO_ONLY and self.organism_gene_context is not None:
            raise ValueError("KO-only input cannot claim an organism gene context")
        if (
            self.kind is PathwayInputKind.ORGANISM_GENE_CONTEXT
            and self.organism_gene_context is None
        ):
            raise ValueError("organism_gene_context input requires bounded gene provenance")
        return self


class PathwayCoverageParameters(FrozenModel):
    """Serializable request parameters for one pathway coverage calculation."""

    reference_namespace: PathwayReferenceNamespace = PathwayReferenceNamespace.KO
    evidence_mode: EvidenceMode = EvidenceMode.STRICT
    input_context: PathwayInputContext = Field(default_factory=PathwayInputContext)
    allow_global_or_overview: bool = False


class PathwayCoverageLimits(FrozenModel):
    """Hard input and output bounds for pathway reference construction and coverage."""

    max_input_records: int = Field(default=500_000, strict=True, gt=0, le=1_000_000)
    max_input_kos: int = Field(default=100_000, strict=True, gt=0, le=1_000_000)
    max_reference_kos: int = Field(default=100_000, strict=True, gt=0, le=1_000_000)
    max_relationship_rows: int = Field(default=200_000, strict=True, gt=0, le=2_000_000)
    max_reference_exclusions: int = Field(default=10_000, strict=True, ge=0, le=100_000)
    max_dataset_sources: int = Field(
        default=_MAX_DATASET_SOURCES,
        strict=True,
        gt=0,
        le=_MAX_DATASET_SOURCES,
    )
    max_reference_provenance_batches: int = Field(
        default=16,
        strict=True,
        gt=0,
        le=_MAX_PROVENANCE_BATCHES,
    )
    max_pathway_class_lines: int = Field(
        default=8,
        strict=True,
        gt=0,
        le=_MAX_PATHWAY_CLASS_LINES,
    )
    max_detected_preview: int = Field(default=25, strict=True, ge=0, le=1_000)
    max_missing_preview: int = Field(default=25, strict=True, ge=0, le=1_000)
    max_exclusion_preview: int = Field(default=25, strict=True, ge=0, le=1_000)


class PathwayKoReference(FrozenModel):
    """One retrieved unique KO denominator with explicit metadata and provenance."""

    reference_namespace: PathwayReferenceNamespace
    reference_scope: PathwayReferenceScope
    pathway_id: str = Field(min_length=7, max_length=9)
    pathway_name: str = Field(min_length=1, max_length=1_000)
    pathway_class: Annotated[
        tuple[str, ...], Field(min_length=1, max_length=_MAX_PATHWAY_CLASS_LINES)
    ]
    kegg_organism_code: str | None = Field(default=None, min_length=3, max_length=4)
    reference_kos: Annotated[tuple[KNumber, ...], Field(max_length=1_000_000)]
    exclusions: Annotated[tuple[PathwayReferenceExclusion, ...], Field(max_length=100_000)] = ()
    relationship_row_count: NonNegativeCount
    duplicate_relationship_count: NonNegativeCount = 0
    link_provenance: ReferenceProvenance
    metadata_provenance: ReferenceProvenance

    @field_validator("pathway_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="pathway name")

    @field_validator("pathway_class")
    @classmethod
    def validate_class_lines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            validate_utf8_text(value, field_name="pathway class")
            if not value or len(value) > 1_000:
                raise ValueError("pathway class lines must contain 1 to 1,000 characters")
        return values

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        _validate_pathway_identity(
            self.reference_namespace,
            self.pathway_id,
            self.kegg_organism_code,
        )
        if self.reference_kos != tuple(sorted(set(self.reference_kos))):
            raise ValueError("reference_kos must be a sorted tuple of unique K numbers")
        exclusion_keys = tuple((item.entry, item.reason.value) for item in self.exclusions)
        if exclusion_keys != tuple(sorted(set(exclusion_keys))):
            raise ValueError("exclusions must be sorted and unique")
        if len({item.entry for item in self.exclusions}) != len(self.exclusions):
            raise ValueError("one excluded entry must have exactly one exclusion reason")
        included_targets = {f"ko:{ko_id}" for ko_id in self.reference_kos}
        if any(exclusion.entry in included_targets for exclusion in self.exclusions):
            raise ValueError("one LINK target cannot be both included and excluded")
        expected_rows = (
            len(self.reference_kos) + len(self.exclusions) + self.duplicate_relationship_count
        )
        if self.relationship_row_count != expected_rows:
            raise ValueError(
                "relationship row counts must account for unique targets and duplicates"
            )
        if any(item.operation is not KeggOperation.LINK for item in self.link_provenance):
            raise ValueError("pathway KO denominators require only LINK provenance")
        if any(item.operation is not KeggOperation.GET for item in self.metadata_provenance):
            raise ValueError("pathway metadata requires only GET provenance")
        if _scope_from_class(self.pathway_class) is not self.reference_scope:
            raise ValueError("reference_scope conflicts with retained PATHWAY CLASS evidence")
        return self


class PathwayCoverageWarning(FrozenModel):
    """One bounded interpretation or output warning."""

    code: PathwayCoverageWarningCode
    message: str = Field(min_length=1, max_length=1_000)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="pathway coverage warning")


class PathwayCoverageResult(FrozenModel):
    """Bounded result of a deterministic unique-KO intersection."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        allow_inf_nan=False,
        hide_input_in_errors=True,
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-coverage-result:2",
            "$schema": JSON_SCHEMA_DIALECT,
        },
    )

    pathway_id: str = Field(min_length=7, max_length=9)
    pathway_name: str = Field(min_length=1, max_length=1_000)
    pathway_class: Annotated[
        tuple[str, ...], Field(min_length=1, max_length=_MAX_PATHWAY_CLASS_LINES)
    ]
    reference_namespace: PathwayReferenceNamespace
    reference_scope: PathwayReferenceScope
    reference_kegg_organism_code: str | None = Field(default=None, min_length=3, max_length=4)
    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    decision_policy: DecisionPolicyReference
    analysis_unit: AnalysisUnit
    taxon_id: TaxonId | None
    kegg_organism_code: str | None = Field(pattern=r"^[a-z][a-z0-9]{1,7}$")
    sources: Annotated[
        tuple[SourceProvenance, ...], Field(min_length=1, max_length=_MAX_DATASET_SOURCES)
    ]
    evidence_mode: EvidenceMode
    evaluation_status: PathwayCoverageStatus
    input_record_count: NonNegativeCount
    input_unique_ko_count: NonNegativeCount
    detected_unique_ko_count: NonNegativeCount
    missing_unique_ko_count: NonNegativeCount
    reference_unique_ko_count: NonNegativeCount
    coverage_ratio: CoverageRatio | None
    detected_kos_preview: Annotated[tuple[KNumber, ...], Field(max_length=1_000)]
    missing_kos_preview: Annotated[tuple[KNumber, ...], Field(max_length=1_000)]
    detected_preview_truncated: bool
    missing_preview_truncated: bool
    excluded_entry_count: NonNegativeCount
    exclusions_preview: Annotated[tuple[PathwayReferenceExclusion, ...], Field(max_length=1_000)]
    exclusions_preview_truncated: bool
    relationship_row_count: NonNegativeCount
    duplicate_relationship_count: NonNegativeCount
    reference_link_provenance: ReferenceProvenance
    reference_metadata_provenance: ReferenceProvenance
    calculation_method: Literal["unique_detected_kos_over_unique_reference_kos"] = (
        PATHWAY_COVERAGE_METHOD
    )
    calculation_version: Literal["2"] = PATHWAY_COVERAGE_VERSION
    parameters: PathwayCoverageParameters
    limits: PathwayCoverageLimits
    warnings: Annotated[tuple[PathwayCoverageWarning, ...], Field(max_length=12)]

    @field_validator("pathway_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_utf8_text(value, field_name="pathway name")

    @field_validator("pathway_class")
    @classmethod
    def validate_class_lines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            validate_utf8_text(value, field_name="pathway class")
            if not value or len(value) > 1_000:
                raise ValueError("pathway class lines must contain 1 to 1,000 characters")
        return values

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        _validate_pathway_identity(
            self.reference_namespace,
            self.pathway_id,
            self.reference_kegg_organism_code,
        )
        if _scope_from_class(self.pathway_class) is not self.reference_scope:
            raise ValueError("reference_scope conflicts with retained PATHWAY CLASS evidence")
        if (
            self.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
            and not self.parameters.allow_global_or_overview
        ):
            raise ValueError("broad references require recorded explicit opt-in")
        if any(item.operation is not KeggOperation.LINK for item in self.reference_link_provenance):
            raise ValueError("pathway KO results require only LINK provenance")
        if any(
            item.operation is not KeggOperation.GET for item in self.reference_metadata_provenance
        ):
            raise ValueError("pathway metadata results require only GET provenance")
        if self.input_record_count > self.limits.max_input_records:
            raise ValueError("input record count exceeds the recorded limit")
        if self.input_unique_ko_count > self.limits.max_input_kos:
            raise ValueError("input KO count exceeds the recorded limit")
        if self.reference_unique_ko_count > self.limits.max_reference_kos:
            raise ValueError("reference KO count exceeds the recorded limit")
        if self.relationship_row_count > self.limits.max_relationship_rows:
            raise ValueError("relationship row count exceeds the recorded limit")
        if self.excluded_entry_count > self.limits.max_reference_exclusions:
            raise ValueError("excluded entry count exceeds the recorded limit")
        if len(self.sources) > self.limits.max_dataset_sources:
            raise ValueError("dataset source count exceeds the recorded limit")
        if len(self.pathway_class) > self.limits.max_pathway_class_lines:
            raise ValueError("pathway CLASS evidence exceeds the recorded limit")
        for provenance in (
            self.reference_link_provenance,
            self.reference_metadata_provenance,
        ):
            if len(provenance) > self.limits.max_reference_provenance_batches:
                raise ValueError("reference provenance exceeds the recorded limit")
        if self.relationship_row_count != (
            self.reference_unique_ko_count
            + self.excluded_entry_count
            + self.duplicate_relationship_count
        ):
            raise ValueError("relationship row counts are inconsistent")
        if self.detected_unique_ko_count > self.input_unique_ko_count:
            raise ValueError("detected KO count cannot exceed the selected input count")
        if self.detected_unique_ko_count + self.missing_unique_ko_count != (
            self.reference_unique_ko_count
        ):
            raise ValueError("detected and missing KO counts must partition the denominator")
        if self.reference_unique_ko_count == 0:
            if (
                self.evaluation_status is not PathwayCoverageStatus.NOT_EVALUABLE
                or self.coverage_ratio is not None
                or self.detected_unique_ko_count != 0
                or self.missing_unique_ko_count != 0
            ):
                raise ValueError("an empty denominator must be not evaluable without a ratio")
        elif (
            self.evaluation_status is not PathwayCoverageStatus.EVALUATED
            or self.coverage_ratio
            != (self.detected_unique_ko_count / self.reference_unique_ko_count)
        ):
            raise ValueError(
                "an evaluable ratio must equal detected count divided by reference count"
            )
        for preview, count, limit, truncated, label in (
            (
                self.detected_kos_preview,
                self.detected_unique_ko_count,
                self.limits.max_detected_preview,
                self.detected_preview_truncated,
                "detected",
            ),
            (
                self.missing_kos_preview,
                self.missing_unique_ko_count,
                self.limits.max_missing_preview,
                self.missing_preview_truncated,
                "missing",
            ),
        ):
            if preview != tuple(sorted(set(preview))):
                raise ValueError(f"{label} preview must be sorted and unique")
            if len(preview) != min(count, limit) or truncated != (count > limit):
                raise ValueError(f"{label} preview summary is inconsistent")
        exclusion_keys = tuple((item.entry, item.reason.value) for item in self.exclusions_preview)
        if exclusion_keys != tuple(sorted(set(exclusion_keys))):
            raise ValueError("exclusion preview must be sorted and unique")
        if len(self.exclusions_preview) != min(
            self.excluded_entry_count, self.limits.max_exclusion_preview
        ) or self.exclusions_preview_truncated != (
            self.excluded_entry_count > self.limits.max_exclusion_preview
        ):
            raise ValueError("exclusion preview summary is inconsistent")
        if self.reference_namespace is not self.parameters.reference_namespace:
            raise ValueError("result namespace must match the recorded request")
        if self.evidence_mode is not self.parameters.evidence_mode:
            raise ValueError("result evidence mode must match the recorded request")
        if self.reference_namespace is PathwayReferenceNamespace.ORGANISM:
            gene_context = self.parameters.input_context.organism_gene_context
            if (
                self.parameters.input_context.kind is not PathwayInputKind.ORGANISM_GENE_CONTEXT
                or gene_context is None
                or gene_context.kegg_organism_code != self.reference_kegg_organism_code
                or self.kegg_organism_code != self.reference_kegg_organism_code
                or self.analysis_unit not in _ORGANISM_ANALYSIS_UNITS
            ):
                raise ValueError("organism result requires an exact compatible dataset context")
        warning_codes = tuple(warning.code for warning in self.warnings)
        if len(warning_codes) != len(set(warning_codes)):
            raise ValueError("warning codes must be unique")
        if warning_codes != _expected_warning_codes(
            reference_scope=self.reference_scope,
            reference_count=self.reference_unique_ko_count,
            link_provenance=self.reference_link_provenance,
            metadata_provenance=self.reference_metadata_provenance,
            exclusion_count=self.excluded_entry_count,
            duplicate_count=self.duplicate_relationship_count,
            analysis_unit=self.analysis_unit,
            detected_truncated=self.detected_preview_truncated,
            missing_truncated=self.missing_preview_truncated,
            exclusions_truncated=self.exclusions_preview_truncated,
        ):
            raise ValueError("warnings must exactly match result conditions in canonical order")
        return self


_ANALYSIS_UNIT_WARNING_CODES = {
    AnalysisUnit.ISOLATE_GENOME: PathwayCoverageWarningCode.ISOLATE_GENOME_CONTEXT,
    AnalysisUnit.ISOLATE_PROTEOME: PathwayCoverageWarningCode.ISOLATE_PROTEOME_CONTEXT,
    AnalysisUnit.MAG: PathwayCoverageWarningCode.MAG_CONTEXT,
    AnalysisUnit.PANGENOME: PathwayCoverageWarningCode.PANGENOME_CONTEXT,
    AnalysisUnit.METAGENOMIC_COMMUNITY: (PathwayCoverageWarningCode.METAGENOMIC_COMMUNITY_CONTEXT),
    AnalysisUnit.MIXED: PathwayCoverageWarningCode.MIXED_CONTEXT,
    AnalysisUnit.UNKNOWN: PathwayCoverageWarningCode.UNKNOWN_CONTEXT,
}

_ORGANISM_ANALYSIS_UNITS = frozenset({AnalysisUnit.ISOLATE_GENOME, AnalysisUnit.ISOLATE_PROTEOME})

_WARNING_MESSAGES = {
    PathwayCoverageWarningCode.DESCRIPTIVE_RATIO: (
        "KO coverage is descriptive only and does not establish pathway presence, completeness, "
        "expression, activity, flux, or phenotype."
    ),
    PathwayCoverageWarningCode.NO_REFERENCE_KOS: (
        "No K numbers were retained in the retrieved pathway reference, so coverage is not "
        "evaluable and no ratio is reported."
    ),
    PathwayCoverageWarningCode.GLOBAL_OR_OVERVIEW_REFERENCE: (
        "A single ratio over a global or overview map aggregates a large heterogeneous KO "
        "reference and is usually not biologically specific."
    ),
    PathwayCoverageWarningCode.STALE_REFERENCE: (
        "At least one reference payload was served from an explicitly allowed stale local cache "
        "entry."
    ),
    PathwayCoverageWarningCode.REFERENCE_EXCLUSIONS: (
        "Some retrieved LINK targets were excluded from the unique K-number denominator for "
        "recorded reasons."
    ),
    PathwayCoverageWarningCode.DUPLICATE_RELATIONSHIPS: (
        "Duplicate pathway-to-KO relationship rows were deduplicated and counted explicitly."
    ),
    PathwayCoverageWarningCode.ISOLATE_GENOME_CONTEXT: (
        "For an isolate genome, this ratio summarizes annotated encoded KO evidence in the "
        "supplied genome and remains sensitive to sequence recovery and annotation quality."
    ),
    PathwayCoverageWarningCode.ISOLATE_PROTEOME_CONTEXT: (
        "For an isolate proteome, this ratio summarizes annotated KO evidence in the supplied "
        "proteome and remains sensitive to proteome and annotation coverage."
    ),
    PathwayCoverageWarningCode.MAG_CONTEXT: (
        "For a MAG, this ratio summarizes annotated encoded KO evidence in the recovered bin and "
        "is sensitive to assembly, binning, recovery, and annotation quality."
    ),
    PathwayCoverageWarningCode.PANGENOME_CONTEXT: (
        "For a pangenome, this ratio summarizes pooled encoded KO evidence across members and is "
        "not attributable to one isolate."
    ),
    PathwayCoverageWarningCode.METAGENOMIC_COMMUNITY_CONTEXT: (
        "For a metagenomic community, this ratio summarizes pooled encoded KO potential across "
        "the supplied community and is not attributable to one organism."
    ),
    PathwayCoverageWarningCode.MIXED_CONTEXT: (
        "For a mixed analysis unit, heterogeneous KO evidence limits organism- or sample-level "
        "interpretation."
    ),
    PathwayCoverageWarningCode.UNKNOWN_CONTEXT: (
        "The analysis unit is unknown, so biological interpretation of this descriptive ratio is "
        "limited."
    ),
    PathwayCoverageWarningCode.DETECTED_PREVIEW_TRUNCATED: (
        "The detected K-number preview was truncated at the configured output limit."
    ),
    PathwayCoverageWarningCode.MISSING_PREVIEW_TRUNCATED: (
        "The missing K-number preview was truncated at the configured output limit."
    ),
    PathwayCoverageWarningCode.EXCLUSION_PREVIEW_TRUNCATED: (
        "The reference-exclusion preview was truncated at the configured output limit."
    ),
}


def build_pathway_reference(
    link_result: LinkResult,
    get_result: GetResult,
    namespace: PathwayReferenceNamespace,
    *,
    limits: PathwayCoverageLimits | None = None,
) -> PathwayKoReference:
    """Build one immutable pathway KO denominator from typed LINK and GET results."""
    bounds = limits or PathwayCoverageLimits()
    pathway_id, organism_code = _validate_reference_requests(
        link_result,
        get_result,
        namespace,
        bounds,
    )
    expected_source = f"path:{pathway_id}"
    seen_targets: set[str] = set()
    ko_ids: set[str] = set()
    exclusions_by_target: dict[str, PathwayReferenceExclusion] = {}
    duplicate_count = 0

    for row in link_result.rows:
        if row.source_id != expected_source:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A PATHWAY_TO_KO row has a source that does not match the requested pathway.",
                suggested_action="Refresh the exact pathway LINK response and retry.",
                safe_details=(
                    SafeDetail(name="pathway_id", value=pathway_id),
                    SafeDetail(name="line_number", value=str(row.line_number)),
                ),
            )
        if row.target_id in seen_targets:
            duplicate_count += 1
            continue
        seen_targets.add(row.target_id)

        match = _KO_LINK_TARGET.fullmatch(row.target_id)
        if match is not None:
            ko_ids.add(match.group(1))
            continue
        reason = (
            PathwayReferenceExclusionReason.NON_KO_IDENTIFIER
            if ":" in row.target_id and not row.target_id.startswith("ko:")
            else PathwayReferenceExclusionReason.INVALID_IDENTIFIER
        )
        exclusions_by_target[row.target_id] = PathwayReferenceExclusion(
            entry=row.target_id,
            reason=reason,
        )

    pathway_name, pathway_class = _extract_pathway_metadata(
        get_result,
        pathway_id,
        bounds,
    )
    exclusions = tuple(
        sorted(
            exclusions_by_target.values(),
            key=lambda item: (item.entry, item.reason.value),
        )
    )
    if len(ko_ids) > bounds.max_reference_kos:
        _fail_limit(
            "reference_ko_count",
            len(ko_ids),
            "max_reference_kos",
            bounds.max_reference_kos,
        )
    if len(exclusions) > bounds.max_reference_exclusions:
        _fail_limit(
            "reference_exclusion_count",
            len(exclusions),
            "max_reference_exclusions",
            bounds.max_reference_exclusions,
        )
    return PathwayKoReference(
        reference_namespace=namespace,
        reference_scope=_scope_from_class(pathway_class),
        pathway_id=pathway_id,
        pathway_name=pathway_name,
        pathway_class=pathway_class,
        kegg_organism_code=organism_code,
        reference_kos=tuple(sorted(ko_ids)),
        exclusions=exclusions,
        relationship_row_count=len(link_result.rows),
        duplicate_relationship_count=duplicate_count,
        link_provenance=link_result.batches,
        metadata_provenance=get_result.batches,
    )


def evaluate_pathway_coverage(
    reference: PathwayKoReference,
    dataset: AnnotationDataset,
    parameters: PathwayCoverageParameters | None = None,
    limits: PathwayCoverageLimits | None = None,
) -> PathwayCoverageResult:
    """Calculate descriptive pathway KO coverage from immutable annotation evidence."""
    request = parameters or PathwayCoverageParameters()
    bounds = limits or PathwayCoverageLimits()
    _validate_evaluation_request(reference, dataset, request, bounds)

    evidence = build_ko_evidence_view(dataset)
    selected = tuple(select_ko_ids(evidence, request.evidence_mode))
    if len(selected) > bounds.max_input_kos:
        _fail_limit("selected_ko_count", len(selected), "max_input_kos", bounds.max_input_kos)

    reference_set = frozenset(reference.reference_kos)
    detected = tuple(sorted(reference_set.intersection(selected)))
    missing = tuple(sorted(reference_set.difference(selected)))
    detected_truncated = len(detected) > bounds.max_detected_preview
    missing_truncated = len(missing) > bounds.max_missing_preview
    exclusions_truncated = len(reference.exclusions) > bounds.max_exclusion_preview
    warning_codes = _expected_warning_codes(
        reference_scope=reference.reference_scope,
        reference_count=len(reference.reference_kos),
        link_provenance=reference.link_provenance,
        metadata_provenance=reference.metadata_provenance,
        exclusion_count=len(reference.exclusions),
        duplicate_count=reference.duplicate_relationship_count,
        analysis_unit=dataset.analysis_unit,
        detected_truncated=detected_truncated,
        missing_truncated=missing_truncated,
        exclusions_truncated=exclusions_truncated,
    )
    denominator_count = len(reference.reference_kos)

    return PathwayCoverageResult(
        pathway_id=reference.pathway_id,
        pathway_name=reference.pathway_name,
        pathway_class=reference.pathway_class,
        reference_namespace=reference.reference_namespace,
        reference_scope=reference.reference_scope,
        reference_kegg_organism_code=reference.kegg_organism_code,
        dataset_id=dataset.dataset_id,
        decision_policy=evidence.policy,
        analysis_unit=dataset.analysis_unit,
        taxon_id=dataset.taxon_id,
        kegg_organism_code=dataset.kegg_organism_code,
        sources=dataset.sources,
        evidence_mode=request.evidence_mode,
        evaluation_status=(
            PathwayCoverageStatus.EVALUATED
            if denominator_count > 0
            else PathwayCoverageStatus.NOT_EVALUABLE
        ),
        input_record_count=len(dataset.records),
        input_unique_ko_count=len(selected),
        detected_unique_ko_count=len(detected),
        missing_unique_ko_count=len(missing),
        reference_unique_ko_count=denominator_count,
        coverage_ratio=(len(detected) / denominator_count if denominator_count > 0 else None),
        detected_kos_preview=detected[: bounds.max_detected_preview],
        missing_kos_preview=missing[: bounds.max_missing_preview],
        detected_preview_truncated=detected_truncated,
        missing_preview_truncated=missing_truncated,
        excluded_entry_count=len(reference.exclusions),
        exclusions_preview=reference.exclusions[: bounds.max_exclusion_preview],
        exclusions_preview_truncated=exclusions_truncated,
        relationship_row_count=reference.relationship_row_count,
        duplicate_relationship_count=reference.duplicate_relationship_count,
        reference_link_provenance=reference.link_provenance,
        reference_metadata_provenance=reference.metadata_provenance,
        parameters=request,
        limits=bounds,
        warnings=tuple(
            PathwayCoverageWarning(code=code, message=_WARNING_MESSAGES[code])
            for code in warning_codes
        ),
    )


def _validate_reference_requests(
    link_result: LinkResult,
    get_result: GetResult,
    namespace: PathwayReferenceNamespace,
    limits: PathwayCoverageLimits,
) -> tuple[str, str | None]:
    if link_result.request.relationship is not KeggLinkRelationship.PATHWAY_TO_KO:
        _fail_configuration(
            "The LINK result is not a PATHWAY_TO_KO response.",
            "Request the exact PATHWAY_TO_KO relationship and retry.",
        )
    if len(link_result.request.source_identifiers) != 1:
        _fail_configuration(
            "A pathway reference must be built from exactly one LINK source pathway.",
            "Issue one PATHWAY_TO_KO request per pathway.",
        )
    pathway_id = link_result.request.source_identifiers[0]
    observed_namespace, organism_code = _namespace_from_pathway_id(pathway_id)
    if observed_namespace is not namespace:
        fail(
            ErrorCode.PATHWAY_NAMESPACE_MISMATCH,
            "The requested pathway namespace does not match the LINK source identifier.",
            suggested_action="Use the namespace encoded by the pathway identifier.",
            safe_details=(
                SafeDetail(name="requested_namespace", value=namespace.value),
                SafeDetail(name="observed_namespace", value=observed_namespace.value),
            ),
        )
    expected_entries = get_result.request.entries
    if (
        len(expected_entries) != 1
        or expected_entries[0].database is not KeggGetDatabase.PATHWAY
        or expected_entries[0].identifier != pathway_id
    ):
        _fail_configuration(
            "The GET result does not request the exact LINK source pathway.",
            "Retrieve the same single PATHWAY identifier used for PATHWAY_TO_KO.",
        )
    if get_result.missing_entries:
        fail(
            ErrorCode.KEGG_ENTRY_NOT_FOUND,
            "The requested PATHWAY metadata entry is missing.",
            suggested_action="Verify the pathway identifier or refresh KEGG metadata.",
            safe_details=(SafeDetail(name="pathway_id", value=pathway_id),),
        )
    if len(link_result.rows) > limits.max_relationship_rows:
        _fail_limit(
            "relationship_row_count",
            len(link_result.rows),
            "max_relationship_rows",
            limits.max_relationship_rows,
        )
    if len(link_result.batches) > limits.max_reference_provenance_batches:
        _fail_limit(
            "link_provenance_batches",
            len(link_result.batches),
            "max_reference_provenance_batches",
            limits.max_reference_provenance_batches,
        )
    if len(get_result.batches) > limits.max_reference_provenance_batches:
        _fail_limit(
            "metadata_provenance_batches",
            len(get_result.batches),
            "max_reference_provenance_batches",
            limits.max_reference_provenance_batches,
        )
    if any(item.operation is not KeggOperation.LINK for item in link_result.batches):
        _fail_configuration(
            "The LINK result contains non-LINK provenance.",
            "Use provenance returned with the exact PATHWAY_TO_KO request.",
        )
    if any(item.operation is not KeggOperation.GET for item in get_result.batches):
        _fail_configuration(
            "The GET result contains non-GET provenance.",
            "Use provenance returned with the exact PATHWAY metadata request.",
        )
    return pathway_id, organism_code


def _extract_pathway_metadata(
    get_result: GetResult,
    pathway_id: str,
    limits: PathwayCoverageLimits,
) -> tuple[str, tuple[str, ...]]:
    documents = get_result.documents
    if len(documents) != 1:
        _fail_parse(
            "A pathway reference requires exactly one flat-file GET document.",
            pathway_id,
        )
    document = documents[0]
    if not isinstance(document, KeggFlatFileDocument):
        _fail_parse(
            "A pathway reference requires a PATHWAY flat-file GET document.",
            pathway_id,
        )
    if len(document.entries) != 1 or document.entries[0].identifier != pathway_id:
        _fail_parse(
            "The PATHWAY flat file does not contain exactly the requested entry.",
            pathway_id,
        )
    entry = document.entries[0]
    name_lines = _required_top_level_field(entry, "NAME", pathway_id)
    class_lines = _required_top_level_field(entry, "CLASS", pathway_id)
    pathway_name = " ".join(name_lines)
    if len(pathway_name) > 1_000:
        _fail_limit("pathway_name_characters", len(pathway_name), "maximum", 1_000)
    if len(class_lines) > limits.max_pathway_class_lines:
        _fail_limit(
            "pathway_class_lines",
            len(class_lines),
            "max_pathway_class_lines",
            limits.max_pathway_class_lines,
        )
    if any(len(line) > 1_000 for line in class_lines):
        _fail_limit(
            "pathway_class_line_characters",
            max(len(line) for line in class_lines),
            "maximum",
            1_000,
        )
    return pathway_name, class_lines


def _required_top_level_field(
    entry: KeggFlatFileEntry,
    field_name: str,
    pathway_id: str,
) -> tuple[str, ...]:
    fields: tuple[KeggFlatFileField, ...] = tuple(
        field for field in entry.fields if field.indent_columns == 0 and field.name == field_name
    )
    if len(fields) != 1:
        _fail_parse(
            f"The PATHWAY flat file must contain exactly one top-level {field_name} field.",
            pathway_id,
        )
    return fields[0].value_lines


def _validate_evaluation_request(
    reference: PathwayKoReference,
    dataset: AnnotationDataset,
    parameters: PathwayCoverageParameters,
    limits: PathwayCoverageLimits,
) -> None:
    if reference.reference_namespace is not parameters.reference_namespace:
        fail(
            ErrorCode.PATHWAY_NAMESPACE_MISMATCH,
            "The requested pathway namespace does not match the supplied denominator.",
            suggested_action="Select the namespace recorded on the pathway reference.",
            safe_details=(
                SafeDetail(name="requested_namespace", value=parameters.reference_namespace.value),
                SafeDetail(name="reference_namespace", value=reference.reference_namespace.value),
            ),
        )
    if len(dataset.records) > limits.max_input_records:
        _fail_limit(
            "input_record_count",
            len(dataset.records),
            "max_input_records",
            limits.max_input_records,
        )
    if len(reference.reference_kos) > limits.max_reference_kos:
        _fail_limit(
            "reference_ko_count",
            len(reference.reference_kos),
            "max_reference_kos",
            limits.max_reference_kos,
        )
    if reference.relationship_row_count > limits.max_relationship_rows:
        _fail_limit(
            "relationship_row_count",
            reference.relationship_row_count,
            "max_relationship_rows",
            limits.max_relationship_rows,
        )
    if len(reference.exclusions) > limits.max_reference_exclusions:
        _fail_limit(
            "reference_exclusion_count",
            len(reference.exclusions),
            "max_reference_exclusions",
            limits.max_reference_exclusions,
        )
    if len(dataset.sources) > limits.max_dataset_sources:
        _fail_limit(
            "dataset_source_count",
            len(dataset.sources),
            "max_dataset_sources",
            limits.max_dataset_sources,
        )
    if len(reference.pathway_class) > limits.max_pathway_class_lines:
        _fail_limit(
            "pathway_class_lines",
            len(reference.pathway_class),
            "max_pathway_class_lines",
            limits.max_pathway_class_lines,
        )
    for name, provenance in (
        ("link_provenance_batches", reference.link_provenance),
        ("metadata_provenance_batches", reference.metadata_provenance),
    ):
        if len(provenance) > limits.max_reference_provenance_batches:
            _fail_limit(
                name,
                len(provenance),
                "max_reference_provenance_batches",
                limits.max_reference_provenance_batches,
            )
    if (
        reference.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
        and not parameters.allow_global_or_overview
    ):
        _fail_configuration(
            "Global and overview pathway references require explicit opt-in.",
            "Set allow_global_or_overview only after reviewing the broad-reference warning.",
        )
    if reference.reference_namespace is PathwayReferenceNamespace.ORGANISM:
        _validate_organism_context(reference, dataset, parameters)


def _validate_organism_context(
    reference: PathwayKoReference,
    dataset: AnnotationDataset,
    parameters: PathwayCoverageParameters,
) -> None:
    context = parameters.input_context
    gene_context = context.organism_gene_context
    if context.kind is not PathwayInputKind.ORGANISM_GENE_CONTEXT or gene_context is None:
        _fail_configuration(
            "Organism pathway coverage requires bounded organism gene context.",
            "Provide an OrganismGeneContext for the exact KEGG organism code.",
        )
    if dataset.analysis_unit not in _ORGANISM_ANALYSIS_UNITS:
        _fail_configuration(
            "Organism pathway coverage is limited to isolate genome or isolate proteome data.",
            "Use a KO or map reference for pooled, MAG, mixed, or unknown analysis units.",
            details=(SafeDetail(name="analysis_unit", value=dataset.analysis_unit.value),),
        )
    expected_code = reference.kegg_organism_code
    if (
        expected_code is None
        or dataset.kegg_organism_code != expected_code
        or gene_context.kegg_organism_code != expected_code
    ):
        _fail_configuration(
            "The pathway, dataset, and gene context must use the exact same KEGG organism code.",
            "Correct the dataset metadata or select a matching organism pathway.",
        )


def _namespace_from_pathway_id(
    pathway_id: str,
) -> tuple[PathwayReferenceNamespace, str | None]:
    prefix = pathway_id[:-5]
    if prefix == "ko":
        return PathwayReferenceNamespace.KO, None
    if prefix == "map":
        return PathwayReferenceNamespace.MAP, None
    if is_kegg_organism_code(prefix):
        return PathwayReferenceNamespace.ORGANISM, prefix
    fail(
        ErrorCode.PATHWAY_NAMESPACE_MISMATCH,
        "The pathway identifier is outside the supported KO, map, and organism namespaces.",
        suggested_action="Use a koNNNNN, mapNNNNN, or canonical organism pathway identifier.",
    )


def _scope_from_class(pathway_class: tuple[str, ...]) -> PathwayReferenceScope:
    if any(_BROAD_PATHWAY_CLASS in line for line in pathway_class):
        return PathwayReferenceScope.GLOBAL_OR_OVERVIEW
    return PathwayReferenceScope.STANDARD


def _expected_warning_codes(
    *,
    reference_scope: PathwayReferenceScope,
    reference_count: int,
    link_provenance: tuple[KeggBatchProvenance, ...],
    metadata_provenance: tuple[KeggBatchProvenance, ...],
    exclusion_count: int,
    duplicate_count: int,
    analysis_unit: AnalysisUnit,
    detected_truncated: bool,
    missing_truncated: bool,
    exclusions_truncated: bool,
) -> tuple[PathwayCoverageWarningCode, ...]:
    codes = [PathwayCoverageWarningCode.DESCRIPTIVE_RATIO]
    if reference_count == 0:
        codes.append(PathwayCoverageWarningCode.NO_REFERENCE_KOS)
    if reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW:
        codes.append(PathwayCoverageWarningCode.GLOBAL_OR_OVERVIEW_REFERENCE)
    if any(item.is_stale for item in (*link_provenance, *metadata_provenance)):
        codes.append(PathwayCoverageWarningCode.STALE_REFERENCE)
    if exclusion_count > 0:
        codes.append(PathwayCoverageWarningCode.REFERENCE_EXCLUSIONS)
    if duplicate_count > 0:
        codes.append(PathwayCoverageWarningCode.DUPLICATE_RELATIONSHIPS)
    codes.append(_ANALYSIS_UNIT_WARNING_CODES[analysis_unit])
    if detected_truncated:
        codes.append(PathwayCoverageWarningCode.DETECTED_PREVIEW_TRUNCATED)
    if missing_truncated:
        codes.append(PathwayCoverageWarningCode.MISSING_PREVIEW_TRUNCATED)
    if exclusions_truncated:
        codes.append(PathwayCoverageWarningCode.EXCLUSION_PREVIEW_TRUNCATED)
    return tuple(codes)


def _fail_configuration(
    message: str,
    suggested_action: str,
    *,
    details: tuple[SafeDetail, ...] = (),
) -> NoReturn:
    fail(
        ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
        message,
        suggested_action=suggested_action,
        safe_details=details,
    )


def _fail_limit(
    observed_name: str,
    observed_value: int,
    limit_name: str,
    limit_value: int,
) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "Pathway analysis input exceeds a configured bound.",
        suggested_action="Reduce the input or raise the corresponding bounded limit.",
        safe_details=(
            SafeDetail(name=observed_name, value=str(observed_value)),
            SafeDetail(name=limit_name, value=str(limit_value)),
        ),
    )


def _fail_parse(message: str, pathway_id: str) -> NoReturn:
    fail(
        ErrorCode.KEGG_PARSE_FAILED,
        message,
        suggested_action="Refresh the exact PATHWAY flat-file response and retry.",
        safe_details=(SafeDetail(name="pathway_id", value=pathway_id),),
    )


__all__ = [
    "PATHWAY_COVERAGE_METHOD",
    "PATHWAY_COVERAGE_VERSION",
    "OrganismGeneContext",
    "PathwayCoverageLimits",
    "PathwayCoverageParameters",
    "PathwayCoverageResult",
    "PathwayCoverageStatus",
    "PathwayCoverageWarning",
    "PathwayCoverageWarningCode",
    "PathwayInputContext",
    "PathwayInputKind",
    "PathwayKoReference",
    "PathwayReferenceExclusion",
    "PathwayReferenceExclusionReason",
    "PathwayReferenceNamespace",
    "PathwayReferenceScope",
    "build_pathway_reference",
    "evaluate_pathway_coverage",
]
