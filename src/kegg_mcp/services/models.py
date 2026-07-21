"""Public service-layer request, result, preview, and status models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kegg_mcp.analysis import (
    CalculationMethodReference,
    ComparisonLimits,
    ComparisonPreviewLimits,
    ComparisonWarning,
    ComparisonWarningCode,
    KoClassComparisonSummary,
    ModuleSelection,
    PathwaySelection,
)
from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    DecisionPolicyReference,
    EvidenceMode,
    FrozenModel,
    ImportDiagnostic,
    NormalizedStatus,
    ScoreType,
    ThresholdRule,
)
from kegg_mcp.domain.errors import ErrorCode
from kegg_mcp.importers import GenericColumnMapping, ImportLimits, SourceProvenanceInput
from kegg_mcp.kegg import AccessMode, KeggGetDatabase, KeggLinkRelationship
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggPairRow, RetrievalEndpointClass
from kegg_mcp.services.contracts import ImportSummary, ModuleAnalysisPreview, PathwayAnalysisPreview
from kegg_mcp.services.output_bundle import ManifestPathMode, OutputBundle
from kegg_mcp.services.result_store import ResultArtifactMetadata, ResultMetadata

DEFAULT_IMPORT_LIMITS = ImportLimits(
    max_bytes=5_000_000,
    max_rows=100_000,
    max_columns=64,
    max_field_length=16_384,
)
DATASET_SECTION = "dataset"
DETAIL_SECTION = "detail"
MAX_NORMALIZATION_PREVIEW = 100
MAX_ENTRY_PREVIEW_CHARACTERS = 2_000
MAX_ENTRY_PREVIEW_FIELDS = 64
MAX_GET_ENTRY_PREVIEWS = 50
MAX_GET_PROVENANCE_BATCHES = 5
MAX_MAPPING_PREVIEW_ROWS = 200
MAX_MAPPING_PROVENANCE_BATCHES = 10
MAX_DIRECT_WARNINGS = 25
MAX_DIRECT_WARNING_CHARACTERS = 1_000
MAX_DIRECT_ANALYSIS_TARGETS = 25
MAX_DIRECT_CAVEATS = 3
MAX_DIRECT_REFERENCE_BATCHES = 100
MAX_DIRECT_SOURCE_PREVIEWS = 8
MAX_COMPARISON_INPUTS = 10
MAX_COMPARISON_WARNINGS = len(ComparisonWarningCode)
MAX_SELECTED_MODULE_SUMMARIES = 25
MAX_SELECTED_PATHWAY_SUMMARIES = 25

BoundedDirectText = Annotated[str, Field(min_length=1, max_length=MAX_DIRECT_WARNING_CHARACTERS)]
EntryFieldName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=32)]
EntryIdentifier = Annotated[str, Field(min_length=1, max_length=100)]
ModuleIdentifier = Annotated[str, Field(pattern=r"^M[0-9]{5}$")]
PathwayIdentifier = Annotated[str, Field(min_length=7, max_length=9)]


class AnnotationInputFormat(StrEnum):
    PLAIN_KO = "plain_ko"
    GENERIC_CSV = "generic_csv"
    GENERIC_TSV = "generic_tsv"
    DEEPKOALA_DETAILED = "deepkoala_detailed"


class GenericDecisionPolicy(StrEnum):
    USER_SUPPLIED_KO = "user_supplied_ko"
    CANONICAL_SOURCE_STATUS = "canonical_source_status"


class NormalizeAnnotationsRequest(FrozenModel):
    text: str | None = Field(default=None, min_length=1, max_length=5_000_000)
    file_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    output_directory: str | None = Field(default=None, min_length=1, max_length=4_096)
    manifest_path_mode: ManifestPathMode = ManifestPathMode.REDACTED
    input_format: AnnotationInputFormat = AnnotationInputFormat.PLAIN_KO
    import_limits: ImportLimits = DEFAULT_IMPORT_LIMITS
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN
    sample_id: str = Field(default="sample-1", min_length=1, max_length=256)
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")
    source: SourceProvenanceInput | None = None
    column_mapping: GenericColumnMapping | None = None
    decision_policy: GenericDecisionPolicy | None = None
    preview_limit: int = Field(default=20, strict=True, ge=0, le=MAX_NORMALIZATION_PREVIEW)
    diagnostic_preview_limit: int = Field(
        default=20,
        strict=True,
        ge=0,
        le=MAX_NORMALIZATION_PREVIEW,
    )

    @model_validator(mode="after")
    def validate_format_configuration(self) -> Self:
        if (self.text is None) == (self.file_path is None):
            raise ValueError("provide exactly one of text or file_path")
        is_generic = self.input_format in {
            AnnotationInputFormat.GENERIC_CSV,
            AnnotationInputFormat.GENERIC_TSV,
        }
        if not is_generic and (self.column_mapping is not None or self.decision_policy is not None):
            raise ValueError("column_mapping and decision_policy are valid only for generic tables")
        for name in ("max_bytes", "max_rows", "max_columns", "max_field_length"):
            if getattr(self.import_limits, name) > getattr(DEFAULT_IMPORT_LIMITS, name):
                raise ValueError(f"import_limits.{name} exceeds the MCP service hard bound")
        if self.source is not None:
            for field in self.source.source_metadata:
                if (
                    isinstance(field.value, str)
                    and len(field.value) > self.import_limits.max_field_length
                ):
                    raise ValueError(
                        "source.source_metadata string value exceeds import_limits.max_field_length"
                    )
        return self


class AnnotationRecordPreview(FrozenModel):
    record_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    sample_id: str = Field(min_length=1, max_length=256)
    sequence_id: str | None = Field(default=None, max_length=256)
    ko_id: str | None = Field(default=None, pattern=r"^K[0-9]{5}$")
    normalized_status: NormalizedStatus
    status_reason: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    score: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    score_type: ScoreType | None = None
    threshold: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    threshold_rule: ThresholdRule | None = None
    rank: int | None = Field(default=None, strict=True, gt=0)
    domain_start: int | None = Field(default=None, strict=True, gt=0)
    domain_end: int | None = Field(default=None, strict=True, gt=0)


class AnnotationSourceSummary(FrozenModel):
    source_name: str = Field(min_length=1, max_length=100)
    source_version: str | None = Field(default=None, max_length=256)
    model_name: str | None = Field(default=None, max_length=256)
    model_version: str | None = Field(default=None, max_length=256)
    annotation_date: datetime | None = None
    input_path: str | None = Field(default=None, max_length=4_096)
    importer_name: str = Field(min_length=1, max_length=100)
    importer_version: str = Field(min_length=1, max_length=32)


class AnnotationProvenanceSummary(FrozenModel):
    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    decision_policy: DecisionPolicyReference
    analysis_unit: AnalysisUnit
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")
    source_count: int = Field(strict=True, ge=1)
    source_preview: Annotated[
        tuple[AnnotationSourceSummary, ...], Field(max_length=MAX_DIRECT_SOURCE_PREVIEWS)
    ]
    sources_truncated: bool


class SelectedPathwaySummary(FrozenModel):
    rank: int = Field(strict=True, gt=0, le=25)
    pathway_id: str = Field(pattern=r"^ko[0-9]{5}$")
    pathway_number: str = Field(pattern=r"^[0-9]{5}$")
    detected_unique_ko_count: int = Field(strict=True, gt=0)
    relationship_row_count: int = Field(strict=True, gt=0)


class SelectedModuleSummary(FrozenModel):
    rank: int = Field(strict=True, gt=0, le=25)
    module_id: ModuleIdentifier
    detected_unique_ko_count: int = Field(strict=True, gt=0)
    relationship_row_count: int = Field(strict=True, gt=0)


class NormalizeAnnotationsResult(FrozenModel):
    result: ResultMetadata
    artifact: ResultArtifactMetadata
    import_summary: ImportSummary
    provenance: AnnotationProvenanceSummary
    record_preview: Annotated[
        tuple[AnnotationRecordPreview, ...], Field(max_length=MAX_NORMALIZATION_PREVIEW)
    ]
    preview_truncated: bool
    diagnostic_count: int = Field(strict=True, ge=0)
    diagnostic_preview: Annotated[
        tuple[ImportDiagnostic, ...], Field(max_length=MAX_NORMALIZATION_PREVIEW)
    ]
    diagnostics_truncated: bool
    column_mapping_inferred: bool = False
    output_bundle: OutputBundle | None = None


class DatasetSource(FrozenModel):
    ko_text: str | None = Field(default=None, min_length=1, max_length=5_000_000)
    result_id: str | None = Field(default=None, pattern=r"^res_[A-Za-z0-9_-]{32}$")
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN
    sample_id: str = Field(default="sample-1", min_length=1, max_length=256)

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> Self:
        if (self.ko_text is None) == (self.result_id is None):
            raise ValueError("provide exactly one of ko_text or result_id")
        if self.result_id is not None and (
            self.analysis_unit is not AnalysisUnit.UNKNOWN or self.sample_id != "sample-1"
        ):
            raise ValueError(
                "analysis_unit and sample_id cannot override context in a retained dataset"
            )
        return self


class AnalysisResultSummary(FrozenModel):
    """Small shared counts and messages returned by every analysis tool."""

    input_records: int = Field(strict=True, ge=0)
    accepted_records: int = Field(strict=True, ge=0)
    uncertain_records: int = Field(strict=True, ge=0)
    rejected_records: int = Field(strict=True, ge=0)
    selected_unique_ko_count: int = Field(strict=True, ge=0)
    kegg_request_count: int = Field(default=0, strict=True, ge=0)
    network_request_count: int = Field(default=0, strict=True, ge=0)
    cache_hit_count: int = Field(default=0, strict=True, ge=0)
    kegg_response_bytes: int = Field(default=0, strict=True, ge=0)
    caveats: Annotated[tuple[BoundedDirectText, ...], Field(max_length=MAX_DIRECT_CAVEATS)]
    warning_count: int = Field(default=0, strict=True, ge=0)
    warnings: Annotated[tuple[BoundedDirectText, ...], Field(max_length=MAX_DIRECT_WARNINGS)] = ()
    warnings_truncated: bool = False

    @model_validator(mode="after")
    def validate_warning_summary(self) -> Self:
        if self.warning_count < len(self.warnings):
            raise ValueError("warning_count cannot be smaller than the direct warning preview")
        if self.warnings_truncated != (self.warning_count > len(self.warnings)):
            raise ValueError("warnings_truncated must match the warning preview count")
        return self


class AutomaticPathwaySelectionSummary(FrozenModel):
    """Bounded direct summary of server-side Top-N pathway selection."""

    parameters: PathwaySelection
    candidate_pathway_count: int = Field(strict=True, ge=0)
    selected_pathways: Annotated[
        tuple[SelectedPathwaySummary, ...], Field(max_length=MAX_SELECTED_PATHWAY_SUMMARIES)
    ]

    @model_validator(mode="after")
    def validate_selection_summary(self) -> Self:
        if len(self.selected_pathways) > self.parameters.top_n:
            raise ValueError("selected pathway summaries exceed top_n")
        if self.candidate_pathway_count < len(self.selected_pathways):
            raise ValueError("candidate pathway count is smaller than selected summaries")
        return self


class AutomaticModuleSelectionSummary(FrozenModel):
    """Bounded direct summary of server-side Top-N MODULE selection."""

    parameters: ModuleSelection
    evidence_mode: EvidenceMode
    candidate_module_count: int = Field(strict=True, ge=0)
    selected_modules: Annotated[
        tuple[SelectedModuleSummary, ...], Field(max_length=MAX_SELECTED_MODULE_SUMMARIES)
    ]

    @model_validator(mode="after")
    def validate_selection_summary(self) -> Self:
        if len(self.selected_modules) > self.parameters.top_n:
            raise ValueError("selected MODULE summaries exceed top_n")
        if self.candidate_module_count < len(self.selected_modules):
            raise ValueError("candidate MODULE count is smaller than selected summaries")
        return self


class AnalyzeKoAnnotationsResult(FrozenModel):
    """Concise one-call result with both relevant target preview types."""

    result: ResultMetadata
    artifacts: Annotated[tuple[ResultArtifactMetadata, ...], Field(min_length=1, max_length=7)]
    summary: AnalysisResultSummary
    module_target_count: int = Field(strict=True, ge=0, le=MAX_DIRECT_ANALYSIS_TARGETS)
    module_previews: Annotated[
        tuple[ModuleAnalysisPreview, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]
    automatic_module_selection: AutomaticModuleSelectionSummary | None = None
    pathway_target_count: int = Field(strict=True, ge=0, le=MAX_DIRECT_ANALYSIS_TARGETS)
    pathway_previews: Annotated[
        tuple[PathwayAnalysisPreview, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]
    automatic_pathway_selection: AutomaticPathwaySelectionSummary | None = None
    output_bundle: OutputBundle | None = None

    @model_validator(mode="after")
    def validate_direct_metadata(self) -> Self:
        if self.result.artifact_count != len(self.artifacts):
            raise ValueError("result artifact_count must match direct artifact metadata")
        if self.module_target_count != len(self.module_previews):
            raise ValueError("module_target_count must match module_previews")
        if self.pathway_target_count != len(self.pathway_previews):
            raise ValueError("pathway_target_count must match pathway_previews")
        return self


class AnalyzeModulesResult(FrozenModel):
    """Concise MODULE-only result."""

    result: ResultMetadata
    artifacts: Annotated[tuple[ResultArtifactMetadata, ...], Field(min_length=1, max_length=5)]
    summary: AnalysisResultSummary
    module_target_count: int = Field(strict=True, ge=0, le=MAX_DIRECT_ANALYSIS_TARGETS)
    module_previews: Annotated[
        tuple[ModuleAnalysisPreview, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]

    @model_validator(mode="after")
    def validate_direct_metadata(self) -> Self:
        if self.result.artifact_count != len(self.artifacts):
            raise ValueError("result artifact_count must match direct artifact metadata")
        if self.module_target_count != len(self.module_previews):
            raise ValueError("module_target_count must match module_previews")
        return self


class AnalyzePathwaysResult(FrozenModel):
    """Concise pathway-only result."""

    result: ResultMetadata
    artifacts: Annotated[tuple[ResultArtifactMetadata, ...], Field(min_length=1, max_length=5)]
    summary: AnalysisResultSummary
    pathway_target_count: int = Field(strict=True, ge=0, le=MAX_DIRECT_ANALYSIS_TARGETS)
    pathway_previews: Annotated[
        tuple[PathwayAnalysisPreview, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]

    @model_validator(mode="after")
    def validate_direct_metadata(self) -> Self:
        if self.result.artifact_count != len(self.artifacts):
            raise ValueError("result artifact_count must match direct artifact metadata")
        if self.pathway_target_count != len(self.pathway_previews):
            raise ValueError("pathway_target_count must match pathway_previews")
        return self


class KeggEntryPreview(FrozenModel):
    database: KeggGetDatabase
    identifier: str = Field(min_length=1, max_length=100)
    format: str = Field(min_length=1, max_length=32)
    field_names: Annotated[
        tuple[EntryFieldName, ...], Field(max_length=MAX_ENTRY_PREVIEW_FIELDS)
    ] = ()
    field_names_truncated: bool = False
    text_preview: str = Field(max_length=MAX_ENTRY_PREVIEW_CHARACTERS)
    preview_truncated: bool


class KeggEntriesServiceResult(FrozenModel):
    result: ResultMetadata
    artifact: ResultArtifactMetadata
    requested_count: int = Field(strict=True, ge=1, le=MAX_GET_ENTRY_PREVIEWS)
    returned_count: int = Field(strict=True, ge=0, le=MAX_GET_ENTRY_PREVIEWS)
    missing_identifiers: Annotated[
        tuple[EntryIdentifier, ...], Field(max_length=MAX_GET_ENTRY_PREVIEWS)
    ]
    previews: Annotated[tuple[KeggEntryPreview, ...], Field(max_length=MAX_GET_ENTRY_PREVIEWS)]
    provenance_batch_count: int = Field(strict=True, ge=0, le=MAX_GET_ENTRY_PREVIEWS)
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_GET_PROVENANCE_BATCHES)
    ]
    provenance_truncated: bool

    @model_validator(mode="after")
    def validate_direct_previews(self) -> Self:
        if self.returned_count != len(self.previews):
            raise ValueError("returned_count must match the entry preview count")
        if self.provenance_batch_count < len(self.provenance):
            raise ValueError("provenance_batch_count cannot be smaller than its preview")
        if self.provenance_truncated != (self.provenance_batch_count > len(self.provenance)):
            raise ValueError("provenance_truncated must match the provenance preview count")
        return self


class CachedKeggEntryServiceResult(FrozenModel):
    requested_count: int = Field(strict=True, ge=1, le=MAX_GET_ENTRY_PREVIEWS)
    returned_count: int = Field(strict=True, ge=0, le=MAX_GET_ENTRY_PREVIEWS)
    missing_identifiers: Annotated[
        tuple[EntryIdentifier, ...], Field(max_length=MAX_GET_ENTRY_PREVIEWS)
    ]
    previews: Annotated[tuple[KeggEntryPreview, ...], Field(max_length=MAX_GET_ENTRY_PREVIEWS)]
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_GET_PROVENANCE_BATCHES)
    ]


class PathwayMappingRow(FrozenModel):
    source_ko_id: str = Field(pattern=r"^K[0-9]{5}$")
    target_id: str = Field(pattern=r"^(?:ko|map)[0-9]{5}$")
    pathway_number: str = Field(pattern=r"^[0-9]{5}$")
    namespace: Literal["ko", "map"]
    paired_reference_id: str = Field(pattern=r"^(?:ko|map)[0-9]{5}$")


class KoMappingServiceResult(FrozenModel):
    result: ResultMetadata
    artifact: ResultArtifactMetadata
    relationship: KeggLinkRelationship
    source_identifier_count: int = Field(strict=True, ge=1, le=100)
    row_count: int = Field(strict=True, ge=0)
    raw_relationship_row_count: int = Field(strict=True, ge=0)
    unique_reference_pathway_number_count: int = Field(default=0, strict=True, ge=0)
    available_ko_reference_view_count: int = Field(default=0, strict=True, ge=0)
    available_map_reference_view_count: int = Field(default=0, strict=True, ge=0)
    row_preview: Annotated[
        tuple[KeggPairRow | PathwayMappingRow, ...], Field(max_length=MAX_MAPPING_PREVIEW_ROWS)
    ]
    preview_truncated: bool
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_MAPPING_PROVENANCE_BATCHES)
    ]


class CompareDatasetSource(FrozenModel):
    label: str = Field(min_length=1, max_length=128)
    source: DatasetSource


class ComparisonDatasetSummary(FrozenModel):
    input_index: int = Field(strict=True, ge=0, lt=MAX_COMPARISON_INPUTS)
    label: str = Field(min_length=1, max_length=128)
    annotation: AnnotationProvenanceSummary
    sample_label_count: int = Field(strict=True, ge=0)
    record_count: int = Field(strict=True, ge=0)
    accepted_ko_count: int = Field(strict=True, ge=0)
    uncertain_record_ko_count: int = Field(strict=True, ge=0)
    lenient_additional_ko_count: int = Field(strict=True, ge=0)
    lenient_ko_count: int = Field(strict=True, ge=0)


class KoSetComparisonPreview(FrozenModel):
    datasets: Annotated[
        tuple[ComparisonDatasetSummary, ...],
        Field(min_length=2, max_length=MAX_COMPARISON_INPUTS),
    ]
    partitions: Annotated[tuple[KoClassComparisonSummary, ...], Field(min_length=4, max_length=4)]
    calculation_method: CalculationMethodReference
    warnings: Annotated[tuple[ComparisonWarning, ...], Field(max_length=MAX_COMPARISON_WARNINGS)]
    detail_limits: ComparisonLimits
    preview_limits: ComparisonPreviewLimits


class FunctionalComparisonSummary(FrozenModel):
    module_target_count: int = Field(strict=True, ge=0, le=25)
    strict_module_differences: Annotated[
        tuple[ModuleIdentifier, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]
    lenient_module_differences: Annotated[
        tuple[ModuleIdentifier, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]
    pathway_target_count: int = Field(strict=True, ge=0, le=25)
    strict_pathway_differences: Annotated[
        tuple[PathwayIdentifier, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]
    lenient_pathway_differences: Annotated[
        tuple[PathwayIdentifier, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]


class CompareKoSetsResult(FrozenModel):
    result: ResultMetadata
    artifact: ResultArtifactMetadata
    summary: KoSetComparisonPreview
    functional_summary: FunctionalComparisonSummary


class ServerStatusResult(FrozenModel):
    server_version: str = Field(min_length=1, max_length=100)
    transport: Literal["stdio"] = "stdio"
    access_mode: AccessMode
    cache_endpoint_class: RetrievalEndpointClass
    network_enabled: bool
    connectivity: Literal["not_probed"] = "not_probed"
    academic_use_confirmed: bool
    licensed_use_confirmed: bool
    cache_configured: bool = True
    inspection_status: Literal["not_probed"] = "not_probed"
    entry_count: None = None
    stored_payload_bytes: None = None
    newest_entry_age_seconds: None = None
    result_store_configured: bool = True
    file_handoff_enabled: bool
    allowed_root_count: int = Field(strict=True, ge=0)
    supported_input_formats: Annotated[
        tuple[AnnotationInputFormat, ...], Field(max_length=len(AnnotationInputFormat))
    ] = tuple(AnnotationInputFormat)
    supported_tools: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=100)], ...], Field(max_length=16)
    ]
    result_scope: Literal["stdio_session"] = "stdio_session"
    result_active_ttl_seconds: int = Field(strict=True, gt=0)
    orphan_cleanup_after_seconds: int = Field(strict=True, gt=0)
    normal_exit_scope_cleanup: bool = True
    durable_output: Literal["output_bundle"] = "output_bundle"
    result_quota_bytes: int = Field(strict=True, gt=0)


class ConnectivityState(StrEnum):
    REACHABLE = "reachable"
    NETWORK_DISABLED = "network_disabled"
    DNS_FAILURE = "dns_failure"
    CONNECTION_FAILURE = "connection_failure"
    AUTHORIZATION_CONFIGURATION_FAILURE = "authorization_configuration_failure"


class ConnectivityProbeResult(FrozenModel):
    state: ConnectivityState
    access_mode: AccessMode
    endpoint_class: RetrievalEndpointClass
    probed_at: datetime
    error_code: ErrorCode | None = None
    suggested_action: str | None = Field(default=None, max_length=1_000)


__all__ = [
    "DATASET_SECTION",
    "DEFAULT_IMPORT_LIMITS",
    "DETAIL_SECTION",
    "MAX_COMPARISON_INPUTS",
    "MAX_DIRECT_ANALYSIS_TARGETS",
    "MAX_DIRECT_CAVEATS",
    "MAX_DIRECT_REFERENCE_BATCHES",
    "MAX_DIRECT_SOURCE_PREVIEWS",
    "MAX_DIRECT_WARNINGS",
    "MAX_ENTRY_PREVIEW_CHARACTERS",
    "MAX_ENTRY_PREVIEW_FIELDS",
    "MAX_GET_ENTRY_PREVIEWS",
    "MAX_GET_PROVENANCE_BATCHES",
    "MAX_MAPPING_PREVIEW_ROWS",
    "MAX_MAPPING_PROVENANCE_BATCHES",
    "MAX_NORMALIZATION_PREVIEW",
    "MAX_SELECTED_MODULE_SUMMARIES",
    "MAX_SELECTED_PATHWAY_SUMMARIES",
    "AnalysisResultSummary",
    "AnalyzeKoAnnotationsResult",
    "AnalyzeModulesResult",
    "AnalyzePathwaysResult",
    "AnnotationInputFormat",
    "AnnotationProvenanceSummary",
    "AnnotationRecordPreview",
    "AnnotationSourceSummary",
    "AutomaticModuleSelectionSummary",
    "AutomaticPathwaySelectionSummary",
    "CachedKeggEntryServiceResult",
    "CompareDatasetSource",
    "CompareKoSetsResult",
    "ComparisonDatasetSummary",
    "ConnectivityProbeResult",
    "ConnectivityState",
    "DatasetSource",
    "FunctionalComparisonSummary",
    "GenericDecisionPolicy",
    "KeggEntriesServiceResult",
    "KeggEntryPreview",
    "KoMappingServiceResult",
    "KoSetComparisonPreview",
    "NormalizeAnnotationsRequest",
    "NormalizeAnnotationsResult",
    "PathwayMappingRow",
    "SelectedModuleSummary",
    "SelectedPathwaySummary",
    "ServerStatusResult",
]
