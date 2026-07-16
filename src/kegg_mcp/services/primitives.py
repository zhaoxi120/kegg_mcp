"""Public bounded service use cases consumed by the MCP transport layer."""

from __future__ import annotations

import csv
import io
import json
import time
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from kegg_mcp.analysis import (
    CalculationMethodReference,
    ComparisonDatasetInput,
    ComparisonLimits,
    ComparisonPreviewLimits,
    ComparisonWarning,
    ComparisonWarningCode,
    FunctionalComparisonLimits,
    KoClassComparisonSummary,
    KoSetComparisonSummary,
    ModuleAnalysisLimits,
    PairedModuleEvaluation,
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    PathwayCoverageResult,
    PathwayRankingResult,
    PathwaySelection,
    PathwaySelectionMode,
    compare_ko_datasets,
    compare_module_graphs,
    compare_pathway_references,
    evaluate_module_pair,
    evaluate_pathway_coverage,
    rank_pathways,
    summarize_ko_comparison,
)
from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    AnnotationDataset,
    AnnotationRecord,
    DecisionPolicyReference,
    EvidenceMode,
    FrozenModel,
    ImportDiagnostic,
    NormalizedStatus,
    ScoreType,
    SourceProvenance,
    ThresholdRule,
    build_ko_evidence_view,
    select_ko_ids,
    try_normalize_ko_id,
)
from kegg_mcp.domain.decisions import CANONICAL_SOURCE_STATUS_V1, USER_SUPPLIED_KO_V1
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, fail
from kegg_mcp.execution import (
    ANNOTATION_ANALYSIS_SERVICE_NAME,
    AnalysisExecutionProvenance,
    AnalysisServiceLimits,
    ExecutionStage,
    PathwayExecutionParameters,
    PathwayRankingExecution,
    StageMetric,
)
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    SourceProvenanceInput,
    TableDialect,
    import_deepkoala_detailed,
    import_generic_table,
    import_plain_ko,
)
from kegg_mcp.kegg import (
    AccessMode,
    GetRequest,
    GetResult,
    InfoRequest,
    InfoResult,
    KeggClientConfig,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    PublicAcademicAccess,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.client import KeggClient
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggPairRow,
    is_kegg_pathway_identifier,
)
from kegg_mcp.reporting import ReportInput, ReportLimits, render_report
from kegg_mcp.services.contracts import (
    ImportSummary,
    ModuleAnalysisPreview,
    PathwayAnalysisPreview,
)
from kegg_mcp.services.output_bundle import (
    ManifestPathMode,
    OutputBundle,
    write_analysis_bundle,
    write_normalization_bundle,
)
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    ReferenceLoadingLimits,
    load_module_graphs,
    load_pathway_references,
)
from kegg_mcp.services.result_store import (
    DeletedResult,
    ResultArtifactInput,
    ResultArtifactMetadata,
    ResultMetadata,
    SQLiteResultStore,
)

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
MAX_DIRECT_WARNINGS = 100
MAX_DIRECT_WARNING_CHARACTERS = 1_000
MAX_DIRECT_ANALYSIS_TARGETS = 25
MAX_DIRECT_CAVEATS = 3
MAX_DIRECT_REFERENCE_BATCHES = 100
MAX_DIRECT_SOURCE_PREVIEWS = 8
MAX_COMPARISON_INPUTS = 10
MAX_COMPARISON_WARNINGS = len(ComparisonWarningCode)
MAX_SELECTED_PATHWAY_SUMMARIES = 25

BoundedDirectText = Annotated[str, Field(min_length=1, max_length=MAX_DIRECT_WARNING_CHARACTERS)]
EntryFieldName = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=32)]
EntryIdentifier = Annotated[str, Field(min_length=1, max_length=100)]
ModuleIdentifier = Annotated[str, Field(pattern=r"^M[0-9]{5}$")]
PathwayIdentifier = Annotated[str, Field(min_length=7, max_length=9)]


class KeggPrimitiveClient(Protocol):
    """Public KEGG methods needed by primitive MCP services."""

    @property
    def config(self) -> KeggClientConfig:
        """Return the redacted-by-caller client configuration."""
        ...

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult: ...

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult: ...


class KeggConnectivityClient(Protocol):
    """Small client surface used only by the explicit connectivity probe."""

    @property
    def config(self) -> KeggClientConfig: ...

    def info(
        self,
        request: InfoRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> InfoResult: ...


class _SharedReferenceBudgetClient:
    """Enforce one aggregate request and response budget across reference loader types."""

    def __init__(self, client: KeggPrimitiveClient, limits: ReferenceLoadingLimits) -> None:
        self._client = client
        self._limits = limits
        self._request_count = 0
        self._response_bytes = 0

    @property
    def config(self) -> KeggClientConfig:
        """Expose immutable client limits for bounded service-level chunking."""
        return self._client.config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        self._reserve_request()
        result = self._client.get(request, options=options)
        self._record_batches(result.batches)
        return result

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        self._reserve_request()
        result = self._client.link(request, options=options)
        self._record_batches(result.batches)
        return result

    def _reserve_request(self) -> None:
        self._request_count += 1
        if self._request_count > self._limits.max_total_kegg_requests:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The combined reference request budget was exceeded.",
                suggested_action="Request fewer MODULE or pathway references.",
            )

    def _record_batches(self, batches: tuple[KeggBatchProvenance, ...]) -> None:
        self._response_bytes += sum(batch.response_bytes for batch in batches)
        if self._response_bytes > self._limits.max_total_response_bytes:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The combined reference response budget was exceeded.",
                suggested_action="Request fewer or smaller KEGG references.",
            )


class AnnotationInputFormat(StrEnum):
    """Inline formats accepted by the normalization service."""

    PLAIN_KO = "plain_ko"
    GENERIC_CSV = "generic_csv"
    GENERIC_TSV = "generic_tsv"
    DEEPKOALA_DETAILED = "deepkoala_detailed"


class GenericDecisionPolicy(StrEnum):
    """Named generic-table policies exposed through the MCP contract."""

    USER_SUPPLIED_KO_V1 = "user_supplied_ko_v1"
    CANONICAL_SOURCE_STATUS_V1 = "canonical_source_status_v1"


class NormalizeAnnotationsRequest(FrozenModel):
    """One bounded inline annotation payload and explicit importer configuration."""

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
    """Bounded normalized fields without retained raw row or source evidence."""

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
    """Compact source identity without caller-defined metadata or local locations."""

    source_name: str = Field(min_length=1, max_length=100)
    source_version: str | None = Field(default=None, max_length=256)
    model_name: str | None = Field(default=None, max_length=256)
    model_version: str | None = Field(default=None, max_length=256)
    annotation_date: datetime | None = None
    input_path: str | None = Field(default=None, max_length=4_096)
    importer_name: str = Field(min_length=1, max_length=100)
    importer_version: str = Field(min_length=1, max_length=32)


class AnnotationProvenanceSummary(FrozenModel):
    """Bounded dataset, policy, and annotation-source provenance."""

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
    """Bounded direct Top-N summary without the complete detected-KO set."""

    rank: int = Field(strict=True, gt=0, le=25)
    pathway_id: str = Field(pattern=r"^ko[0-9]{5}$")
    pathway_number: str = Field(pattern=r"^[0-9]{5}$")
    detected_unique_ko_count: int = Field(strict=True, gt=0)
    relationship_row_count: int = Field(strict=True, gt=0)


class NormalizeAnnotationsResult(FrozenModel):
    """Bounded normalization summary with a reusable retained dataset."""

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
    """Either a new plain-KO input or a scoped retained dataset result."""

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


class PrimitiveAnalysisResult(FrozenModel):
    """Reusable retained detail plus bounded module and pathway previews."""

    result: ResultMetadata
    artifacts: Annotated[tuple[ResultArtifactMetadata, ...], Field(min_length=1, max_length=5)]
    input_records: int = Field(strict=True, ge=0)
    accepted_records: int = Field(strict=True, ge=0)
    uncertain_records: int = Field(strict=True, ge=0)
    rejected_records: int = Field(strict=True, ge=0)
    selected_unique_ko_count: int = Field(strict=True, ge=0)
    pathway_selection: PathwaySelection | None = None
    candidate_pathway_count: int | None = Field(default=None, strict=True, ge=0)
    selected_pathways: Annotated[
        tuple[SelectedPathwaySummary, ...], Field(max_length=MAX_SELECTED_PATHWAY_SUMMARIES)
    ] = ()
    kegg_request_count: int = Field(default=0, strict=True, ge=0)
    network_request_count: int = Field(default=0, strict=True, ge=0)
    cache_hit_count: int = Field(default=0, strict=True, ge=0)
    kegg_response_bytes: int = Field(default=0, strict=True, ge=0)
    execution_metrics: Annotated[tuple[StageMetric, ...], Field(min_length=6, max_length=6)]
    module_target_count: int = Field(strict=True, ge=0, le=MAX_DIRECT_ANALYSIS_TARGETS)
    module_previews: Annotated[
        tuple[ModuleAnalysisPreview, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]
    pathway_target_count: int = Field(strict=True, ge=0, le=MAX_DIRECT_ANALYSIS_TARGETS)
    pathway_previews: Annotated[
        tuple[PathwayAnalysisPreview, ...], Field(max_length=MAX_DIRECT_ANALYSIS_TARGETS)
    ]
    caveats: Annotated[tuple[BoundedDirectText, ...], Field(max_length=MAX_DIRECT_CAVEATS)]
    import_summary: ImportSummary | None = None
    annotation_provenance: AnnotationProvenanceSummary
    warning_count: int = Field(default=0, strict=True, ge=0)
    warnings: Annotated[tuple[BoundedDirectText, ...], Field(max_length=MAX_DIRECT_WARNINGS)] = ()
    warnings_truncated: bool = False
    reference_provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_DIRECT_REFERENCE_BATCHES)
    ] = ()
    execution: AnalysisExecutionProvenance | None = None
    output_bundle: OutputBundle | None = None

    @model_validator(mode="after")
    def validate_compact_result(self) -> Self:
        if self.result.artifact_count != len(self.artifacts):
            raise ValueError("result artifact_count must match direct artifact metadata")
        expected_stages = tuple(ExecutionStage)
        if tuple(item.stage for item in self.execution_metrics) != expected_stages:
            raise ValueError("execution_metrics must contain every stage in canonical order")
        if self.kegg_request_count != sum(item.request_count for item in self.execution_metrics):
            raise ValueError("kegg_request_count must match stage metrics")
        if self.network_request_count != sum(
            item.network_request_count for item in self.execution_metrics
        ):
            raise ValueError("network_request_count must match stage metrics")
        if self.cache_hit_count != sum(item.cache_hit_count for item in self.execution_metrics):
            raise ValueError("cache_hit_count must match stage metrics")
        if self.kegg_response_bytes != sum(item.response_bytes for item in self.execution_metrics):
            raise ValueError("kegg_response_bytes must match stage metrics")
        if self.pathway_selection is None:
            if self.candidate_pathway_count is not None or self.selected_pathways:
                raise ValueError("automatic pathway summaries require pathway_selection")
        else:
            if self.candidate_pathway_count is None:
                raise ValueError("pathway selection requires a candidate count")
            if len(self.selected_pathways) > self.pathway_selection.top_n:
                raise ValueError("selected pathway summaries exceed top_n")
            if self.candidate_pathway_count < len(self.selected_pathways):
                raise ValueError("candidate pathway count is smaller than selected summaries")
        return self


class KeggEntryPreview(FrozenModel):
    """Small preview of one parsed KEGG GET entry."""

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
    """Bounded GET summary with the complete parsed result retained locally."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    requested_count: int = Field(strict=True, ge=1, le=MAX_GET_ENTRY_PREVIEWS)
    returned_count: int = Field(strict=True, ge=0, le=MAX_GET_ENTRY_PREVIEWS)
    missing_identifiers: Annotated[
        tuple[EntryIdentifier, ...], Field(max_length=MAX_GET_ENTRY_PREVIEWS)
    ]
    previews: Annotated[tuple[KeggEntryPreview, ...], Field(max_length=MAX_GET_ENTRY_PREVIEWS)]
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_GET_PROVENANCE_BATCHES)
    ]


class CachedKeggEntryServiceResult(FrozenModel):
    """Bounded cache-only GET summary that does not create a retained result."""

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
    """One KO-to-pathway relationship with explicit paired-view identity."""

    source_ko_id: str = Field(pattern=r"^K[0-9]{5}$")
    target_id: str = Field(pattern=r"^(?:ko|map)[0-9]{5}$")
    pathway_number: str = Field(pattern=r"^[0-9]{5}$")
    namespace: Literal["ko", "map"]
    paired_reference_id: str = Field(pattern=r"^(?:ko|map)[0-9]{5}$")


class KoMappingServiceResult(FrozenModel):
    """Bounded LINK preview with explicit ko/map pathway identity when applicable."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    relationship: KeggLinkRelationship
    source_identifier_count: int = Field(strict=True, ge=1, le=100)
    row_count: int = Field(strict=True, ge=0)
    raw_relationship_row_count: int = Field(strict=True, ge=0)
    unique_ko_pathway_count: int = Field(default=0, strict=True, ge=0)
    unique_map_pathway_count: int = Field(default=0, strict=True, ge=0)
    unique_pathway_number_count: int = Field(default=0, strict=True, ge=0)
    row_preview: Annotated[
        tuple[KeggPairRow | PathwayMappingRow, ...], Field(max_length=MAX_MAPPING_PREVIEW_ROWS)
    ]
    preview_truncated: bool
    provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_MAPPING_PROVENANCE_BATCHES)
    ]


class CompareDatasetSource(FrozenModel):
    """One labelled comparison input using inline or retained evidence."""

    label: str = Field(min_length=1, max_length=128)
    source: DatasetSource


class ComparisonDatasetSummary(FrozenModel):
    """Compact comparison provenance without raw source metadata or sample labels."""

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
    """Bounded direct comparison summary; complete provenance remains retained."""

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
    """Bounded target-level differences; lossless outcomes remain in the detail artifact."""

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
    """Bounded deterministic comparison plus retained lossless detail."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    summary: KoSetComparisonPreview
    functional_summary: FunctionalComparisonSummary


class ServerStatusResult(FrozenModel):
    """Redacted operational status safe for MCP clients."""

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
    """Operator-facing result of one explicit low-cost KEGG preflight."""

    REACHABLE = "reachable"
    DNS_FAILURE = "dns_failure"
    CONNECTION_FAILURE = "connection_failure"
    AUTHORIZATION_CONFIGURATION_FAILURE = "authorization_configuration_failure"


class ConnectivityProbeResult(FrozenModel):
    """Redacted connectivity outcome without endpoint URLs or environment values."""

    state: ConnectivityState
    access_mode: AccessMode
    endpoint_class: RetrievalEndpointClass
    probed_at: datetime
    error_code: ErrorCode | None = None
    suggested_action: str | None = Field(default=None, max_length=1_000)


def normalize_annotations(
    request: NormalizeAnnotationsRequest,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
    output_directory: Path | None = None,
) -> NormalizeAnnotationsResult:
    """Normalize one inline payload and retain its complete typed dataset."""
    dataset = _import_dataset(request)
    content = dataset.model_dump_json().encode("utf-8")
    output_bundle = (
        write_normalization_bundle(
            dataset,
            output_directory,
            manifest_path_mode=request.manifest_path_mode,
        )
        if output_directory is not None
        else None
    )
    metadata = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DATASET_SECTION, mime_type="application/json", content=content
            ),
        ),
    )
    artifact = _artifact_metadata(DATASET_SECTION, "application/json", content)
    preview = tuple(
        _annotation_record_preview(record) for record in dataset.records[: request.preview_limit]
    )
    diagnostics = dataset.import_report.diagnostics[: request.diagnostic_preview_limit]
    return NormalizeAnnotationsResult(
        result=metadata,
        artifact=artifact,
        import_summary=_import_summary(dataset),
        provenance=_annotation_provenance(dataset),
        record_preview=preview,
        preview_truncated=len(preview) < len(dataset.records),
        diagnostic_count=len(dataset.import_report.diagnostics),
        diagnostic_preview=diagnostics,
        diagnostics_truncated=len(diagnostics) < len(dataset.import_report.diagnostics),
        column_mapping_inferred=(
            request.input_format
            in {AnnotationInputFormat.GENERIC_CSV, AnnotationInputFormat.GENERIC_TSV}
            and request.column_mapping is None
        ),
        output_bundle=output_bundle,
    )


def analyze_annotation_targets(
    request: NormalizeAnnotationsRequest,
    *,
    module_ids: tuple[str, ...],
    pathways: tuple[PathwaySpec, ...],
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    pathway_evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    pathway_selection: PathwaySelection | None = None,
    allow_global_or_overview: bool = False,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    module_limits: ModuleAnalysisLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
    report_limits: ReportLimits | None = None,
    output_directory: Path | None = None,
) -> PrimitiveAnalysisResult:
    """Normalize any supported inline format and analyze all selected targets in one call."""
    effective_report_limits = report_limits or ReportLimits()
    effective_module_limits = module_limits or ModuleAnalysisLimits()
    effective_pathway_limits = pathway_limits or PathwayCoverageLimits()
    _validate_report_capacity(effective_report_limits, result_store)
    result_store.list_results(scope_id, limit=1)
    stage_elapsed = {stage: 0 for stage in ExecutionStage}
    started = time.perf_counter_ns()
    dataset = _import_dataset(request)
    stage_elapsed[ExecutionStage.ANNOTATION_IMPORT] = _elapsed_ms(started)
    effective_options = options or KeggRequestOptions(refresh=False)
    effective_reference_limits = reference_limits or ReferenceLoadingLimits()
    budgeted_client = _SharedReferenceBudgetClient(client, effective_reference_limits)
    discovery_provenance: tuple[KeggBatchProvenance, ...] = ()
    ranking: PathwayRankingResult | None = None
    ranking_execution: PathwayRankingExecution | None = None
    if (
        pathway_selection is not None
        and pathway_selection.mode is PathwaySelectionMode.TOP_DETECTED
    ):
        if pathway_selection.top_n > effective_reference_limits.max_pathway_specs:
            fail(
                ErrorCode.INPUT_LIMIT_EXCEEDED,
                "The requested Top-N pathway count exceeds the deployment pathway bound.",
                suggested_action="Lower top_n or raise the deployment-owned pathway limit.",
            )
        started = time.perf_counter_ns()
        relationship_rows, discovery_provenance = _map_selected_ko_relationships(
            dataset,
            evidence_mode=pathway_evidence_mode,
            client=budgeted_client,
            options=effective_options,
        )
        stage_elapsed[ExecutionStage.KO_PATHWAY_MAPPING] = _elapsed_ms(started)
        started = time.perf_counter_ns()
        ranking = rank_pathways(dataset, relationship_rows, pathway_evidence_mode)
        stage_elapsed[ExecutionStage.PATHWAY_RANKING] = _elapsed_ms(started)
        if not ranking.rows:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "No reference pathway could be ranked from the selected K numbers.",
                suggested_action="Supply explicit pathway targets or different KO evidence.",
            )
        selected_rows = ranking.rows[: pathway_selection.top_n]
        pathways = tuple(PathwaySpec(pathway_id=row.pathway_id) for row in selected_rows)
        ranking_execution = _pathway_ranking_execution(
            ranking,
            pathway_selection,
            dataset=dataset,
            mapping_provenance=discovery_provenance,
        )
    elif pathway_selection is not None:
        if pathway_selection.mode is not PathwaySelectionMode.EXPLICIT:
            raise AssertionError("validated pathway selection uses an unsupported mode")
        if not pathways or pathway_selection.top_n != len(pathways):
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "Explicit pathway selection must match the supplied pathway target count.",
                suggested_action="Set top_n to the number of explicit pathways or omit selection.",
            )
    elif not module_ids and not pathways:
        started = time.perf_counter_ns()
        pathways, discovery_provenance = _discover_reference_pathways(
            dataset,
            client=budgeted_client,
            options=effective_options,
            limits=effective_reference_limits,
        )
        stage_elapsed[ExecutionStage.KO_PATHWAY_MAPPING] = _elapsed_ms(started)

    started = time.perf_counter_ns()
    graphs = (
        load_module_graphs(
            budgeted_client,
            module_ids,
            options=effective_options,
            limits=effective_reference_limits,
            analysis_limits=effective_module_limits,
        )
        if module_ids
        else ()
    )
    references = load_pathway_references(
        budgeted_client,
        pathways,
        options=effective_options,
        limits=effective_reference_limits,
        pathway_limits=effective_pathway_limits,
    )
    stage_elapsed[ExecutionStage.REFERENCE_LOADING] = _elapsed_ms(started)

    started = time.perf_counter_ns()
    modules = tuple(
        evaluate_module_pair(graph, dataset, effective_module_limits) for graph in graphs
    )
    coverages = tuple(
        evaluate_pathway_coverage(
            reference,
            dataset,
            PathwayCoverageParameters(
                reference_namespace=reference.reference_namespace,
                evidence_mode=pathway_evidence_mode,
                allow_global_or_overview=allow_global_or_overview,
            ),
            effective_pathway_limits,
        )
        for reference in references
    )
    execution = AnalysisExecutionProvenance(
        service_name=ANNOTATION_ANALYSIS_SERVICE_NAME,
        import_limits=request.import_limits,
        kegg_request_options=effective_options,
        reference_loading_limits=effective_reference_limits,
        module_analysis_limits=effective_module_limits,
        pathway_parameters=PathwayExecutionParameters(
            evidence_mode=pathway_evidence_mode,
            allow_global_or_overview=allow_global_or_overview,
            ranking=ranking_execution,
        ),
        pathway_coverage_limits=effective_pathway_limits,
        report_limits=effective_report_limits,
        direct_result_limits=AnalysisServiceLimits(
            max_module_previews=MAX_DIRECT_ANALYSIS_TARGETS,
            max_pathway_previews=MAX_DIRECT_ANALYSIS_TARGETS,
        ),
    )
    rendered = render_report(
        ReportInput(
            dataset=dataset,
            execution=execution,
            module_evaluations=modules,
            pathway_coverages=coverages,
            pathway_selection=pathway_selection if ranking is not None else None,
            pathway_ranking=ranking.rows if ranking is not None else (),
        ),
        limits=effective_report_limits,
    )
    stage_elapsed[ExecutionStage.ANALYSIS] = _elapsed_ms(started)
    output_bundle = None
    started = time.perf_counter_ns()
    if output_directory is not None:
        summary_artifact = next(
            artifact for artifact in rendered.artifacts if artifact.section.value == "summary"
        )
        output_bundle = write_analysis_bundle(
            dataset,
            graphs,
            modules,
            references,
            coverages,
            execution=execution,
            analysis_report=summary_artifact.content,
            output_directory=output_directory,
            pathway_ranking=ranking,
            manifest_path_mode=request.manifest_path_mode,
        )
    stage_elapsed[ExecutionStage.BUNDLE_WRITE] = _elapsed_ms(started)

    stored_inputs = [
        ResultArtifactInput(
            section=artifact.section.value,
            mime_type=artifact.mime_type,
            content=artifact.content.encode("utf-8"),
        )
        for artifact in rendered.artifacts
    ]
    artifact_metadata = [
        ResultArtifactMetadata(
            section=artifact.section.value,
            mime_type=artifact.mime_type,
            byte_size=artifact.utf8_byte_size,
        )
        for artifact in rendered.artifacts
    ]
    if ranking is not None:
        ranking_content = _json_bytes(
            {
                "decision_policy": dataset.import_report.decision_policy.model_dump(mode="json"),
                "mapping_provenance": [
                    batch.model_dump(mode="json") for batch in discovery_provenance
                ],
                "ranking": ranking.model_dump(mode="json", exclude={"relationships"}),
            }
        )
        relationship_content = _json_bytes(
            {"relationships": [item.model_dump(mode="json") for item in ranking.relationships]}
        )
        for section, content in (
            ("pathway_ranking", ranking_content),
            ("ko_pathway_relationships", relationship_content),
        ):
            stored_inputs.append(
                ResultArtifactInput(
                    section=section,
                    mime_type="application/json",
                    content=content,
                )
            )
            artifact_metadata.append(_artifact_metadata(section, "application/json", content))
    result = result_store.create(scope_id, tuple(stored_inputs))
    artifacts = tuple(artifact_metadata)
    caveats = ["K-number assignments are annotation evidence, not experimental validation."]
    if modules:
        caveats.append(
            "Exact MODULE completion and project-defined required-block coverage are separate."
        )
    if coverages:
        caveats.append(
            "Pathway KO coverage is descriptive and does not establish presence, activity, or flux."
        )
    warnings = _analysis_warnings(dataset, modules, coverages)
    warning_preview = warnings[:MAX_DIRECT_WARNINGS]
    reference_provenance = _reference_provenance(modules, coverages)
    metrics = _execution_metrics(
        stage_elapsed,
        mapping_provenance=discovery_provenance,
        reference_provenance=reference_provenance,
    )
    selected_ko_ids = select_ko_ids(build_ko_evidence_view(dataset), pathway_evidence_mode)
    selected_pathways = (
        tuple(
            SelectedPathwaySummary(
                rank=row.rank,
                pathway_id=row.pathway_id,
                pathway_number=row.pathway_number,
                detected_unique_ko_count=row.detected_unique_ko_count,
                relationship_row_count=row.relationship_row_count,
            )
            for row in ranking.rows[: pathway_selection.top_n]
        )
        if ranking is not None and pathway_selection is not None
        else ()
    )
    status_counts = _status_record_counts(dataset)
    return PrimitiveAnalysisResult(
        result=result,
        artifacts=artifacts,
        input_records=dataset.import_report.input_rows,
        accepted_records=status_counts[NormalizedStatus.ACCEPTED],
        uncertain_records=status_counts[NormalizedStatus.UNCERTAIN],
        rejected_records=status_counts[NormalizedStatus.REJECTED],
        selected_unique_ko_count=len(selected_ko_ids),
        pathway_selection=pathway_selection if ranking is not None else None,
        candidate_pathway_count=len(ranking.rows) if ranking is not None else None,
        selected_pathways=selected_pathways,
        kegg_request_count=sum(item.request_count for item in metrics),
        network_request_count=sum(item.network_request_count for item in metrics),
        cache_hit_count=sum(item.cache_hit_count for item in metrics),
        kegg_response_bytes=sum(item.response_bytes for item in metrics),
        execution_metrics=metrics,
        module_target_count=len(modules),
        module_previews=tuple(_module_preview(item) for item in modules),
        pathway_target_count=len(coverages),
        pathway_previews=tuple(_pathway_preview(item) for item in coverages),
        caveats=tuple(caveats),
        import_summary=_import_summary(dataset),
        annotation_provenance=_annotation_provenance(dataset),
        warning_count=len(warnings),
        warnings=warning_preview,
        warnings_truncated=len(warning_preview) < len(warnings),
        reference_provenance=(
            ()
            if ranking is not None
            else _reference_provenance(
                modules,
                coverages,
                additional=discovery_provenance,
            )
        ),
        execution=execution,
        output_bundle=output_bundle,
    )


def analyze_module_targets(
    source: DatasetSource,
    module_ids: tuple[str, ...],
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    analysis_limits: ModuleAnalysisLimits | None = None,
) -> PrimitiveAnalysisResult:
    """Evaluate bounded MODULE targets using inline or retained annotation evidence."""
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    refs = load_module_graphs(
        client,
        module_ids,
        options=options or KeggRequestOptions(),
        limits=reference_limits,
        analysis_limits=analysis_limits,
    )
    pairs = tuple(evaluate_module_pair(graph, dataset, analysis_limits) for graph in refs)
    payload = _json_bytes({"module_evaluations": [item.model_dump(mode="json") for item in pairs]})
    result = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    reference_provenance = _reference_provenance(pairs, ())
    metrics = _execution_metrics(
        {stage: 0 for stage in ExecutionStage},
        reference_provenance=reference_provenance,
    )
    status_counts = _status_record_counts(dataset)
    selected_ko_ids = select_ko_ids(build_ko_evidence_view(dataset), EvidenceMode.STRICT)
    return PrimitiveAnalysisResult(
        result=result,
        artifacts=(_artifact_metadata(DETAIL_SECTION, "application/json", payload),),
        input_records=dataset.import_report.input_rows,
        accepted_records=status_counts[NormalizedStatus.ACCEPTED],
        uncertain_records=status_counts[NormalizedStatus.UNCERTAIN],
        rejected_records=status_counts[NormalizedStatus.REJECTED],
        selected_unique_ko_count=len(selected_ko_ids),
        kegg_request_count=sum(item.request_count for item in metrics),
        network_request_count=sum(item.network_request_count for item in metrics),
        cache_hit_count=sum(item.cache_hit_count for item in metrics),
        kegg_response_bytes=sum(item.response_bytes for item in metrics),
        execution_metrics=metrics,
        module_target_count=len(pairs),
        module_previews=tuple(_module_preview(item) for item in pairs),
        pathway_target_count=0,
        pathway_previews=(),
        caveats=(
            (
                "Exact MODULE completion and project-defined required-block coverage are "
                "separate results."
            ),
            "K-number assignments are annotation evidence, not experimental validation.",
        ),
        annotation_provenance=_annotation_provenance(dataset),
        reference_provenance=reference_provenance,
    )


def analyze_pathway_targets(
    source: DatasetSource,
    pathways: tuple[PathwaySpec, ...],
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    evidence_mode: EvidenceMode = EvidenceMode.STRICT,
    allow_global_or_overview: bool = False,
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
) -> PrimitiveAnalysisResult:
    """Evaluate bounded descriptive pathway coverage from retained or inline evidence."""
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    refs = load_pathway_references(
        client,
        pathways,
        options=options or KeggRequestOptions(),
        limits=reference_limits,
        pathway_limits=pathway_limits,
    )
    coverages = tuple(
        evaluate_pathway_coverage(
            reference,
            dataset,
            PathwayCoverageParameters(
                reference_namespace=reference.reference_namespace,
                evidence_mode=evidence_mode,
                allow_global_or_overview=allow_global_or_overview,
            ),
            pathway_limits,
        )
        for reference in refs
    )
    payload = _json_bytes(
        {"pathway_coverages": [item.model_dump(mode="json") for item in coverages]}
    )
    result = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    reference_provenance = _reference_provenance((), coverages)
    metrics = _execution_metrics(
        {stage: 0 for stage in ExecutionStage},
        reference_provenance=reference_provenance,
    )
    status_counts = _status_record_counts(dataset)
    selected_ko_ids = select_ko_ids(build_ko_evidence_view(dataset), evidence_mode)
    return PrimitiveAnalysisResult(
        result=result,
        artifacts=(_artifact_metadata(DETAIL_SECTION, "application/json", payload),),
        input_records=dataset.import_report.input_rows,
        accepted_records=status_counts[NormalizedStatus.ACCEPTED],
        uncertain_records=status_counts[NormalizedStatus.UNCERTAIN],
        rejected_records=status_counts[NormalizedStatus.REJECTED],
        selected_unique_ko_count=len(selected_ko_ids),
        kegg_request_count=sum(item.request_count for item in metrics),
        network_request_count=sum(item.network_request_count for item in metrics),
        cache_hit_count=sum(item.cache_hit_count for item in metrics),
        kegg_response_bytes=sum(item.response_bytes for item in metrics),
        execution_metrics=metrics,
        module_target_count=0,
        module_previews=(),
        pathway_target_count=len(coverages),
        pathway_previews=tuple(_pathway_preview(item) for item in coverages),
        caveats=(
            (
                "Pathway KO coverage is descriptive and does not establish pathway presence, "
                "activity, or flux."
            ),
            "The reference namespace and unique-KO denominator are explicit in every result.",
        ),
        annotation_provenance=_annotation_provenance(dataset),
        reference_provenance=reference_provenance,
    )


def retrieve_kegg_entries(
    request: GetRequest,
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
) -> KeggEntriesServiceResult:
    """Retrieve approved entries and retain the complete parsed response locally."""
    fetched = client.get(request, options=options)
    payload = fetched.model_dump_json().encode("utf-8")
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    previews = _entry_previews(fetched, request)
    returned = len(previews)
    return KeggEntriesServiceResult(
        result=stored,
        artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
        requested_count=len(request.entries),
        returned_count=returned,
        missing_identifiers=tuple(item.identifier for item in fetched.missing_entries),
        previews=previews,
        provenance=tuple(fetched.batches),
    )


def _entry_previews(fetched: GetResult, request: GetRequest) -> tuple[KeggEntryPreview, ...]:
    database_by_identifier = {item.identifier: item.database for item in request.entries}
    previews: list[KeggEntryPreview] = []
    for document in fetched.documents:
        if isinstance(document, KeggFlatFileDocument):
            for entry in document.entries:
                text = "\n".join(
                    f"{field.name}: {' '.join(field.value_lines)}" for field in entry.fields
                )
                shown = text[:MAX_ENTRY_PREVIEW_CHARACTERS]
                field_names = tuple(dict.fromkeys(field.name for field in entry.fields))
                shown_field_names = field_names[:MAX_ENTRY_PREVIEW_FIELDS]
                previews.append(
                    KeggEntryPreview(
                        database=database_by_identifier[entry.identifier],
                        identifier=entry.identifier,
                        format=document.format.value,
                        field_names=shown_field_names,
                        field_names_truncated=len(shown_field_names) < len(field_names),
                        text_preview=shown,
                        preview_truncated=len(shown) < len(text),
                    )
                )
        else:
            text = "\n".join(document.lines)
            shown = text[:MAX_ENTRY_PREVIEW_CHARACTERS]
            previews.append(
                KeggEntryPreview(
                    database=KeggGetDatabase.BRITE,
                    identifier=document.identifier,
                    format=document.format.value,
                    text_preview=shown,
                    preview_truncated=len(shown) < len(text),
                )
            )
    return tuple(previews)


def read_cached_kegg_entry(
    request: GetRequest,
    *,
    client: KeggPrimitiveClient,
) -> CachedKeggEntryServiceResult:
    """Read one GET entry from the configured cache without network fallback."""
    cache_client = KeggClient(client.config)
    fetched = cache_client.get(
        request,
        options=KeggRequestOptions(refresh=False, allow_stale=True, cache_only=True),
    )
    previews = _entry_previews(fetched, request)
    return CachedKeggEntryServiceResult(
        requested_count=len(request.entries),
        returned_count=len(previews),
        missing_identifiers=tuple(item.identifier for item in fetched.missing_entries),
        previews=previews,
        provenance=fetched.batches,
    )


def map_ko_identifiers(
    request: LinkRequest,
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
    preview_limit: int = 100,
) -> KoMappingServiceResult:
    """Map selected K numbers to one explicitly approved KEGG relationship."""
    if request.relationship is KeggLinkRelationship.PATHWAY_TO_KO:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "map_ko_ids accepts K numbers as sources and does not expose pathway-to-KO expansion.",
            suggested_action="Choose a KO-to-pathway, module, reaction, enzyme, or BRITE target.",
        )
    if not 0 <= preview_limit <= MAX_MAPPING_PREVIEW_ROWS:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The mapping preview limit is outside the MCP service bound.",
            suggested_action=f"Choose preview_limit between 0 and {MAX_MAPPING_PREVIEW_ROWS}.",
        )
    mapped = client.link(request, options=options)
    pathway_rows = (
        tuple(_pathway_mapping_row(row) for row in mapped.rows)
        if request.relationship is KeggLinkRelationship.KO_TO_PATHWAY
        else ()
    )
    payload = (
        _json_bytes(
            {
                "relationship": request.relationship.value,
                "rows": [row.model_dump(mode="json") for row in pathway_rows],
                "provenance": [batch.model_dump(mode="json") for batch in mapped.batches],
            }
        )
        if pathway_rows
        else mapped.model_dump_json().encode("utf-8")
    )
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    rows = (pathway_rows or mapped.rows)[:preview_limit]
    pathway_numbers = {row.pathway_number for row in pathway_rows}
    return KoMappingServiceResult(
        result=stored,
        artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
        relationship=request.relationship,
        source_identifier_count=len(request.source_identifiers),
        row_count=len(mapped.rows),
        raw_relationship_row_count=len(mapped.rows),
        unique_ko_pathway_count=len(pathway_numbers),
        unique_map_pathway_count=len(pathway_numbers),
        unique_pathway_number_count=len(pathway_numbers),
        row_preview=tuple(rows),
        preview_truncated=len(rows) < len(mapped.rows),
        provenance=tuple(mapped.batches),
    )


def _pathway_mapping_row(row: KeggPairRow) -> PathwayMappingRow:
    source_value = row.source_id.rsplit(":", 1)[-1]
    target_value = row.target_id.rsplit(":", 1)[-1]
    ko_id, _ = try_normalize_ko_id(source_value)
    if ko_id is None or not is_kegg_pathway_identifier(target_value):
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "A KO-to-pathway relationship row has incompatible identifiers.",
            suggested_action="Refresh the typed KEGG LINK response and retry.",
        )
    prefix = target_value[:-5]
    if prefix == "ko":
        namespace: Literal["ko", "map"] = "ko"
    elif prefix == "map":
        namespace = "map"
    else:
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "A KO-to-pathway relationship row uses an unsupported namespace.",
            suggested_action="Use reference ko/map pathway relationships for KO evidence.",
        )
    paired_prefix = "map" if namespace == "ko" else "ko"
    return PathwayMappingRow(
        source_ko_id=ko_id,
        target_id=target_value,
        pathway_number=target_value[-5:],
        namespace=namespace,
        paired_reference_id=f"{paired_prefix}{target_value[-5:]}",
    )


def compare_annotation_sets(
    inputs: tuple[CompareDatasetSource, ...],
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
    client: KeggPrimitiveClient | None = None,
    module_ids: tuple[str, ...] = (),
    pathways: tuple[PathwaySpec, ...] = (),
    options: KeggRequestOptions | None = None,
    reference_limits: ReferenceLoadingLimits | None = None,
    module_limits: ModuleAnalysisLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
    functional_limits: FunctionalComparisonLimits | None = None,
    allow_global_or_overview: bool = False,
    limits: ComparisonLimits | None = None,
    preview_limits: ComparisonPreviewLimits | None = None,
) -> CompareKoSetsResult:
    """Compare inline or scoped retained datasets with deterministic set semantics."""
    datasets = tuple(
        ComparisonDatasetInput(
            label=item.label,
            dataset=_resolve_dataset(item.source, result_store=result_store, scope_id=scope_id),
        )
        for item in inputs
    )
    detail = compare_ko_datasets(datasets, limits=limits)
    summary = summarize_ko_comparison(detail, limits=preview_limits)
    if (module_ids or pathways) and client is None:
        raise AssertionError("functional comparison targets require a KEGG reference client")
    effective_options = options or KeggRequestOptions()
    effective_reference_limits = reference_limits or ReferenceLoadingLimits()
    reference_client = (
        None if client is None else _SharedReferenceBudgetClient(client, effective_reference_limits)
    )
    module_comparison = None
    if module_ids:
        if reference_client is None:
            raise AssertionError("MODULE comparison targets require a KEGG reference client")
        graphs = load_module_graphs(
            reference_client,
            module_ids,
            options=effective_options,
            limits=effective_reference_limits,
            analysis_limits=module_limits,
        )
        module_comparison = compare_module_graphs(
            datasets,
            graphs,
            comparison_limits=limits,
            functional_limits=functional_limits,
        )
    pathway_comparison = None
    if pathways:
        if reference_client is None:
            raise AssertionError("pathway comparison targets require a KEGG reference client")
        references = load_pathway_references(
            reference_client,
            pathways,
            options=effective_options,
            limits=effective_reference_limits,
            pathway_limits=pathway_limits,
        )
        pathway_comparison = compare_pathway_references(
            datasets,
            references,
            comparison_limits=limits,
            functional_limits=functional_limits,
            coverage_limits=pathway_limits,
            allow_global_or_overview=allow_global_or_overview,
        )
    payload = _json_bytes(
        {
            "ko_comparison": detail.model_dump(mode="json"),
            "module_comparison": (
                None if module_comparison is None else module_comparison.model_dump(mode="json")
            ),
            "pathway_comparison": (
                None if pathway_comparison is None else pathway_comparison.model_dump(mode="json")
            ),
        }
    )
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION, mime_type="application/json", content=payload
            ),
        ),
    )
    return CompareKoSetsResult(
        result=stored,
        artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
        summary=_comparison_preview(summary),
        functional_summary=FunctionalComparisonSummary(
            module_target_count=(
                0 if module_comparison is None else len(module_comparison.targets)
            ),
            strict_module_differences=(
                ()
                if module_comparison is None
                else tuple(
                    target.module_id
                    for target in module_comparison.targets
                    if target.strict.outcomes_differ
                )
            ),
            lenient_module_differences=(
                ()
                if module_comparison is None
                else tuple(
                    target.module_id
                    for target in module_comparison.targets
                    if target.lenient.outcomes_differ
                )
            ),
            pathway_target_count=(
                0 if pathway_comparison is None else len(pathway_comparison.targets)
            ),
            strict_pathway_differences=(
                ()
                if pathway_comparison is None
                else tuple(
                    target.reference.pathway_id
                    for target in pathway_comparison.targets
                    if target.strict.outcomes_differ
                )
            ),
            lenient_pathway_differences=(
                ()
                if pathway_comparison is None
                else tuple(
                    target.reference.pathway_id
                    for target in pathway_comparison.targets
                    if target.lenient.outcomes_differ
                )
            ),
        ),
    )


def get_server_status_service(
    *,
    server_version: str,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    supported_tools: tuple[str, ...],
    allowed_root_count: int,
) -> ServerStatusResult:
    """Return redacted configuration facts without probing or revealing paths."""
    access = client.config.access
    cache_endpoint_class = (
        RetrievalEndpointClass.PUBLIC_ACADEMIC
        if isinstance(access, PublicAcademicAccess)
        else RetrievalEndpointClass.LICENSED
    )
    return ServerStatusResult(
        server_version=server_version,
        access_mode=access.mode,
        cache_endpoint_class=cache_endpoint_class,
        network_enabled=True,
        academic_use_confirmed=access.mode is AccessMode.PUBLIC_ACADEMIC,
        licensed_use_confirmed=cache_endpoint_class is RetrievalEndpointClass.LICENSED,
        file_handoff_enabled=allowed_root_count > 0,
        allowed_root_count=allowed_root_count,
        supported_tools=supported_tools,
        result_active_ttl_seconds=result_store.limits.retention_seconds,
        orphan_cleanup_after_seconds=result_store.limits.retention_seconds,
        result_quota_bytes=result_store.limits.quota_bytes,
    )


def delete_analysis_result(
    result_id: str,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> DeletedResult:
    """Delete one current-session retained result without exposing other scopes."""
    return result_store.delete(scope_id, result_id)


def probe_kegg_connectivity_service(
    client: KeggConnectivityClient,
    *,
    now: datetime | None = None,
) -> ConnectivityProbeResult:
    """Probe a typed INFO endpoint once and classify failures before biological analysis."""
    checked_now = (now or datetime.now(UTC)).astimezone(UTC)
    access = client.config.access
    endpoint_class = (
        RetrievalEndpointClass.PUBLIC_ACADEMIC
        if isinstance(access, PublicAcademicAccess)
        else RetrievalEndpointClass.LICENSED
    )
    try:
        result = client.info(
            InfoRequest(database=KeggInfoDatabase.KEGG),
            options=KeggRequestOptions(refresh=True),
        )
    except KeggMcpError as error:
        details = {item.name: item.value for item in error.detail.safe_details}
        transport_kind = details.get("transport_kind")
        if transport_kind == "dns":
            state = ConnectivityState.DNS_FAILURE
        elif transport_kind == "connection":
            state = ConnectivityState.CONNECTION_FAILURE
        else:
            state = ConnectivityState.AUTHORIZATION_CONFIGURATION_FAILURE
        return ConnectivityProbeResult(
            state=state,
            access_mode=access.mode,
            endpoint_class=endpoint_class,
            probed_at=checked_now,
            error_code=error.detail.code,
            suggested_action=error.detail.suggested_action,
        )
    return ConnectivityProbeResult(
        state=ConnectivityState.REACHABLE,
        access_mode=access.mode,
        endpoint_class=endpoint_class,
        probed_at=result.batch.served_at,
    )


def _import_dataset(request: NormalizeAnnotationsRequest) -> AnnotationDataset:
    if request.text is None:
        raise AssertionError("file-backed requests must be materialized before service execution")
    if request.input_format is AnnotationInputFormat.PLAIN_KO:
        return import_plain_ko(
            request.text,
            limits=request.import_limits,
            analysis_unit=request.analysis_unit,
            sample_id=request.sample_id,
            taxon_id=request.taxon_id,
            kegg_organism_code=request.kegg_organism_code,
            source=request.source,
        )
    if request.input_format is AnnotationInputFormat.DEEPKOALA_DETAILED:
        return import_deepkoala_detailed(
            request.text,
            limits=request.import_limits,
            analysis_unit=request.analysis_unit,
            sample_id=request.sample_id,
            taxon_id=request.taxon_id,
            kegg_organism_code=request.kegg_organism_code,
            source=request.source,
        )
    mapping = request.column_mapping or _infer_generic_column_mapping(
        request.text,
        delimiter=("," if request.input_format is AnnotationInputFormat.GENERIC_CSV else "\t"),
    )
    selected_policy = request.decision_policy
    if selected_policy is None:
        selected_policy = (
            GenericDecisionPolicy.CANONICAL_SOURCE_STATUS_V1
            if mapping.raw_decision is not None
            else GenericDecisionPolicy.USER_SUPPLIED_KO_V1
        )
    policy = (
        USER_SUPPLIED_KO_V1
        if selected_policy is GenericDecisionPolicy.USER_SUPPLIED_KO_V1
        else CANONICAL_SOURCE_STATUS_V1
    )
    dialect = (
        TableDialect.CSV
        if request.input_format is AnnotationInputFormat.GENERIC_CSV
        else TableDialect.TSV
    )
    return import_generic_table(
        request.text,
        dialect=dialect,
        mapping=mapping,
        policy=policy,
        limits=request.import_limits,
        analysis_unit=request.analysis_unit,
        default_sample_id=request.sample_id,
        taxon_id=request.taxon_id,
        kegg_organism_code=request.kegg_organism_code,
        source=request.source,
    )


def _infer_generic_column_mapping(text: str, *, delimiter: str) -> GenericColumnMapping:
    """Infer only unambiguous common generic-table column names and echo them in the dataset."""
    try:
        header = next(csv.reader(io.StringIO(text), delimiter=delimiter, strict=True))
    except (StopIteration, csv.Error):
        fail(
            ErrorCode.INVALID_ANNOTATION_TABLE,
            "The generic annotation table does not contain a readable header.",
            suggested_action="Provide a UTF-8 CSV or TSV file with one header row.",
        )
    normalized: dict[str, list[str]] = {}
    for column in header:
        normalized.setdefault(column.strip().casefold(), []).append(column)

    def select(logical_name: str, aliases: tuple[str, ...], *, required: bool) -> str | None:
        matches = [value for alias in aliases for value in normalized.get(alias, ())]
        if len(matches) > 1:
            fail(
                ErrorCode.AMBIGUOUS_COLUMN_MAPPING,
                f"More than one common column matches {logical_name}.",
                suggested_action=f"Supply column_mapping.{logical_name} explicitly.",
            )
        if not matches and required:
            fail(
                ErrorCode.MISSING_REQUIRED_COLUMN,
                f"No common column name matches {logical_name}.",
                suggested_action=f"Supply column_mapping.{logical_name} explicitly.",
            )
        return matches[0] if matches else None

    sequence_id = select(
        "sequence_id",
        ("sequence_id", "protein_id", "seq_id", "query_id", "gene_id"),
        required=True,
    )
    ko_id = select("ko_id", ("ko_id", "ko", "k_number", "kegg_orthology"), required=True)
    if sequence_id is None or ko_id is None:
        raise AssertionError("required generic-column inference returned no column")
    return GenericColumnMapping(
        sequence_id=sequence_id,
        ko_id=ko_id,
        protein_name=select(
            "protein_name", ("protein_name", "protein", "description"), required=False
        ),
        sample_id=select("sample_id", ("sample_id", "sample"), required=False),
        raw_decision=select(
            "raw_decision", ("raw_decision", "decision", "status", "annotate"), required=False
        ),
    )


def _resolve_dataset(
    source: DatasetSource,
    *,
    result_store: SQLiteResultStore,
    scope_id: str,
) -> AnnotationDataset:
    if source.ko_text is not None:
        return import_plain_ko(
            source.ko_text,
            limits=DEFAULT_IMPORT_LIMITS,
            analysis_unit=source.analysis_unit,
            sample_id=source.sample_id,
        )
    if source.result_id is None:
        raise AssertionError("dataset source validation omitted both source variants")
    chunks: list[bytes] = []
    offset = 0
    while True:
        page = result_store.read_artifact(
            scope_id,
            source.result_id,
            DATASET_SECTION,
            offset=offset,
            limit=result_store.limits.max_range_bytes,
        )
        chunks.append(page.content)
        if page.next_offset is None:
            break
        offset = page.next_offset
    try:
        return AnnotationDataset.model_validate_json(b"".join(chunks))
    except ValueError:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The retained result does not contain a valid annotation dataset.",
            suggested_action="Use the result_id returned by normalize_ko_annotations.",
        )


def _import_summary(dataset: AnnotationDataset) -> ImportSummary:
    report = dataset.import_report
    return ImportSummary(
        dataset_id=dataset.dataset_id,
        analysis_unit=dataset.analysis_unit,
        input_rows=report.input_rows,
        emitted_records=report.emitted_records,
        skipped_rows=report.skipped_rows,
        duplicate_count=report.duplicate_count,
        conflict_count=report.conflict_count,
        status_counts=report.status_counts,
    )


def _module_preview(item: PairedModuleEvaluation) -> ModuleAnalysisPreview:
    return ModuleAnalysisPreview(
        module_id=item.strict.module_id,
        module_name=item.strict.module_name,
        strict_status=item.strict.evaluation_status,
        strict_is_complete=item.strict.is_complete,
        strict_block_coverage=item.strict.block_coverage,
        lenient_status=item.lenient.evaluation_status,
        lenient_is_complete=item.lenient.is_complete,
        lenient_block_coverage=item.lenient.block_coverage,
        strict_to_lenient_changed=item.strict_to_lenient_changed,
    )


def _pathway_preview(item: PathwayCoverageResult) -> PathwayAnalysisPreview:
    return PathwayAnalysisPreview(
        pathway_id=item.pathway_id,
        pathway_name=item.pathway_name,
        reference_namespace=item.reference_namespace,
        reference_scope=item.reference_scope,
        evidence_mode=item.evidence_mode,
        evaluation_status=item.evaluation_status,
        detected_unique_ko_count=item.detected_unique_ko_count,
        reference_unique_ko_count=item.reference_unique_ko_count,
        coverage_ratio=item.coverage_ratio,
        warning_codes=tuple(warning.code.value for warning in item.warnings),
    )


def _elapsed_ms(started_ns: int) -> int:
    """Return a non-negative integral elapsed time without exposing wall-clock data."""
    return max(0, (time.perf_counter_ns() - started_ns) // 1_000_000)


def _status_record_counts(dataset: AnnotationDataset) -> dict[NormalizedStatus, int]:
    return {
        status: sum(record.normalized_status is status for record in dataset.records)
        for status in NormalizedStatus
    }


def _execution_metrics(
    elapsed: dict[ExecutionStage, int],
    *,
    mapping_provenance: tuple[KeggBatchProvenance, ...] = (),
    reference_provenance: tuple[KeggBatchProvenance, ...] = (),
) -> tuple[StageMetric, ...]:
    """Build the fixed six-row direct metric summary."""
    values: list[StageMetric] = []
    for stage in ExecutionStage:
        if stage is ExecutionStage.KO_PATHWAY_MAPPING:
            batches = mapping_provenance
        elif stage is ExecutionStage.REFERENCE_LOADING:
            batches = reference_provenance
        else:
            batches = ()
        values.append(
            StageMetric(
                stage=stage,
                elapsed_ms=elapsed.get(stage, 0),
                request_count=len(batches),
                network_request_count=sum(
                    batch.attempt_count
                    for batch in batches
                    if batch.origin is ResponseOrigin.NETWORK
                ),
                cache_hit_count=sum(batch.origin is ResponseOrigin.CACHE for batch in batches),
                response_bytes=sum(batch.response_bytes for batch in batches),
            )
        )
    return tuple(values)


def _map_selected_ko_relationships(
    dataset: AnnotationDataset,
    *,
    evidence_mode: EvidenceMode,
    client: _SharedReferenceBudgetClient,
    options: KeggRequestOptions,
) -> tuple[tuple[KeggPairRow, ...], tuple[KeggBatchProvenance, ...]]:
    """Issue bounded KO-to-pathway calls and merge rows without changing their semantics."""
    selected_ko_ids = select_ko_ids(build_ko_evidence_view(dataset), evidence_mode)
    if not selected_ko_ids:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Automatic pathway ranking requires at least one selected K number.",
            suggested_action="Provide selected KO evidence or explicit MODULE/pathway targets.",
        )
    maximum_per_call = min(100, client.config.limits.max_identifiers)
    rows: list[KeggPairRow] = []
    provenance: list[KeggBatchProvenance] = []
    for start in range(0, len(selected_ko_ids), maximum_per_call):
        result = client.link(
            LinkRequest(
                relationship=KeggLinkRelationship.KO_TO_PATHWAY,
                source_identifiers=selected_ko_ids[start : start + maximum_per_call],
            ),
            options=options,
        )
        batch_offset = len(provenance)
        rows.extend(
            KeggPairRow(
                batch_index=row.batch_index + batch_offset,
                line_number=row.line_number,
                source_id=row.source_id,
                target_id=row.target_id,
            )
            for row in result.rows
        )
        provenance.extend(result.batches)
    return tuple(rows), tuple(provenance)


def _pathway_ranking_execution(
    ranking: PathwayRankingResult,
    selection: PathwaySelection,
    *,
    dataset: AnnotationDataset,
    mapping_provenance: tuple[KeggBatchProvenance, ...],
) -> PathwayRankingExecution:
    selected_rows = ranking.rows[: selection.top_n]
    return PathwayRankingExecution(
        selection=selection,
        evidence_mode=ranking.evidence_mode,
        decision_policy=dataset.import_report.decision_policy,
        selected_unique_ko_count=len(ranking.selected_ko_ids),
        candidate_pathway_count=len(ranking.rows),
        selected_pathway_ids=tuple(row.pathway_id for row in selected_rows),
        mapping_request_count=len(mapping_provenance),
        mapping_network_request_count=sum(
            batch.attempt_count
            for batch in mapping_provenance
            if batch.origin is ResponseOrigin.NETWORK
        ),
        mapping_cache_hit_count=sum(
            batch.origin is ResponseOrigin.CACHE for batch in mapping_provenance
        ),
        mapping_response_bytes=sum(batch.response_bytes for batch in mapping_provenance),
    )


def _reference_provenance(
    modules: tuple[PairedModuleEvaluation, ...],
    pathways: tuple[PathwayCoverageResult, ...],
    *,
    additional: tuple[KeggBatchProvenance, ...] = (),
) -> tuple[KeggBatchProvenance, ...]:
    batches = list(additional)
    batches.extend(
        batch for module in modules for batch in module.strict.reference_retrieval_provenance
    )
    batches.extend(
        batch
        for pathway in pathways
        for batch in (
            *pathway.reference_link_provenance,
            *pathway.reference_metadata_provenance,
        )
    )
    unique: list[KeggBatchProvenance] = []
    seen: set[str] = set()
    for batch in batches:
        key = batch.model_dump_json()
        if key in seen:
            continue
        seen.add(key)
        unique.append(batch)
    if len(unique) > MAX_DIRECT_REFERENCE_BATCHES:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Reference provenance exceeds the direct-result batch bound.",
            suggested_action="Request fewer MODULE or pathway references.",
        )
    return tuple(unique)


def _discover_reference_pathways(
    dataset: AnnotationDataset,
    *,
    client: _SharedReferenceBudgetClient,
    options: KeggRequestOptions,
    limits: ReferenceLoadingLimits,
) -> tuple[tuple[PathwaySpec, ...], tuple[KeggBatchProvenance, ...]]:
    """Discover canonical KO-reference pathways once from accepted annotation evidence."""
    ko_ids = tuple(
        sorted(
            {
                record.ko_id
                for record in dataset.records
                if record.ko_id is not None
                and record.normalized_status is NormalizedStatus.ACCEPTED
            }
        )
    )
    if not ko_ids:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Automatic pathway discovery requires at least one accepted K number.",
            suggested_action="Provide accepted KO evidence or explicit MODULE/pathway targets.",
        )
    numbers: set[str] = set()
    provenance: list[KeggBatchProvenance] = []
    for start in range(0, len(ko_ids), 100):
        result = client.link(
            LinkRequest(
                relationship=KeggLinkRelationship.KO_TO_PATHWAY,
                source_identifiers=ko_ids[start : start + 100],
            ),
            options=options,
        )
        provenance.extend(result.batches)
        for row in result.rows:
            target = row.target_id.rsplit(":", 1)[-1]
            if not is_kegg_pathway_identifier(target) or target[:-5] not in {"ko", "map"}:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "Automatic pathway discovery returned an unsupported pathway namespace.",
                    suggested_action="Refresh the typed KO-to-pathway relationship and retry.",
                )
            numbers.add(target[-5:])
    if not numbers:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "No reference pathway could be discovered from the accepted K numbers.",
            suggested_action="Supply explicit pathway or MODULE targets for this KO set.",
        )
    if len(numbers) > limits.max_pathway_specs:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "Automatic pathway discovery exceeded the deployment pathway bound.",
            suggested_action=(
                "Supply an explicit subset of pathway identifiers or raise the deployment-owned "
                "reference limit after capacity review."
            ),
        )
    return (
        tuple(PathwaySpec(pathway_id=f"ko{number}") for number in sorted(numbers)),
        tuple(provenance),
    )


def _analysis_warnings(
    dataset: AnnotationDataset,
    modules: tuple[PairedModuleEvaluation, ...],
    pathways: tuple[PathwayCoverageResult, ...],
) -> tuple[str, ...]:
    values = [diagnostic.message for diagnostic in dataset.import_report.diagnostics]
    values.extend(warning.message for module in modules for warning in module.strict.warnings)
    values.extend(warning.message for pathway in pathways for warning in pathway.warnings)
    return tuple(dict.fromkeys(values))


def _annotation_record_preview(record: AnnotationRecord) -> AnnotationRecordPreview:
    return AnnotationRecordPreview(
        record_id=record.record_id,
        sample_id=record.sample_id,
        sequence_id=record.sequence_id,
        ko_id=record.ko_id,
        normalized_status=record.normalized_status,
        status_reason=record.status_reason,
        score=record.score,
        score_type=record.score_type,
        threshold=record.threshold,
        threshold_rule=record.threshold_rule,
        rank=record.rank,
        domain_start=record.domain_start,
        domain_end=record.domain_end,
    )


def _annotation_source_summary(source: SourceProvenance) -> AnnotationSourceSummary:
    return AnnotationSourceSummary(
        source_name=source.source_name,
        source_version=source.source_version,
        model_name=source.model_name,
        model_version=source.model_version,
        annotation_date=source.annotation_date,
        input_path=source.input_path,
        importer_name=source.importer_name,
        importer_version=source.importer_version,
    )


def _annotation_provenance(dataset: AnnotationDataset) -> AnnotationProvenanceSummary:
    source_preview = tuple(
        _annotation_source_summary(source)
        for source in dataset.sources[:MAX_DIRECT_SOURCE_PREVIEWS]
    )
    return AnnotationProvenanceSummary(
        dataset_id=dataset.dataset_id,
        decision_policy=dataset.import_report.decision_policy,
        analysis_unit=dataset.analysis_unit,
        taxon_id=dataset.taxon_id,
        kegg_organism_code=dataset.kegg_organism_code,
        source_count=len(dataset.sources),
        source_preview=source_preview,
        sources_truncated=len(source_preview) < len(dataset.sources),
    )


def _comparison_preview(summary: KoSetComparisonSummary) -> KoSetComparisonPreview:
    datasets: list[ComparisonDatasetSummary] = []
    for item in summary.datasets:
        source_preview = tuple(
            _annotation_source_summary(source)
            for source in item.sources[:MAX_DIRECT_SOURCE_PREVIEWS]
        )
        datasets.append(
            ComparisonDatasetSummary(
                input_index=item.input_index,
                label=item.label,
                annotation=AnnotationProvenanceSummary(
                    dataset_id=item.dataset_id,
                    decision_policy=item.decision_policy,
                    analysis_unit=item.analysis_unit,
                    taxon_id=item.taxon_id,
                    kegg_organism_code=item.kegg_organism_code,
                    source_count=len(item.sources),
                    source_preview=source_preview,
                    sources_truncated=len(source_preview) < len(item.sources),
                ),
                sample_label_count=len(item.sample_labels),
                record_count=item.record_count,
                accepted_ko_count=item.accepted_ko_count,
                uncertain_record_ko_count=item.uncertain_record_ko_count,
                lenient_additional_ko_count=item.lenient_additional_ko_count,
                lenient_ko_count=item.lenient_ko_count,
            )
        )
    return KoSetComparisonPreview(
        datasets=tuple(datasets),
        partitions=summary.partitions,
        calculation_method=summary.calculation_method,
        warnings=summary.warnings,
        detail_limits=summary.detail_limits,
        preview_limits=summary.preview_limits,
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _artifact_metadata(section: str, mime_type: str, content: bytes) -> ResultArtifactMetadata:
    return ResultArtifactMetadata(
        section=section,
        mime_type=mime_type,
        byte_size=len(content),
    )


def _validate_report_capacity(limits: ReportLimits, store: SQLiteResultStore) -> None:
    maxima = (
        limits.max_structured_json_bytes,
        limits.max_markdown_bytes,
        limits.max_annotation_csv_bytes,
    )
    if any(maximum > store.limits.max_artifact_bytes for maximum in maxima):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "A report artifact limit exceeds the retained-result artifact limit.",
            suggested_action="Use compatible report and result-store byte limits.",
        )
    if sum(maxima) > min(store.limits.max_result_bytes, store.limits.quota_bytes):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "The report bundle limits exceed retained-result capacity.",
            suggested_action="Use compatible report and result-store bundle limits.",
        )


__all__ = [
    "DATASET_SECTION",
    "DEFAULT_IMPORT_LIMITS",
    "DETAIL_SECTION",
    "AnnotationInputFormat",
    "AnnotationProvenanceSummary",
    "AnnotationRecordPreview",
    "AnnotationSourceSummary",
    "CachedKeggEntryServiceResult",
    "CompareDatasetSource",
    "CompareKoSetsResult",
    "DatasetSource",
    "FunctionalComparisonSummary",
    "GenericDecisionPolicy",
    "KeggEntriesServiceResult",
    "KeggEntryPreview",
    "KeggPrimitiveClient",
    "KoMappingServiceResult",
    "KoSetComparisonPreview",
    "NormalizeAnnotationsRequest",
    "NormalizeAnnotationsResult",
    "PrimitiveAnalysisResult",
    "SelectedPathwaySummary",
    "ServerStatusResult",
    "analyze_annotation_targets",
    "analyze_module_targets",
    "analyze_pathway_targets",
    "compare_annotation_sets",
    "delete_analysis_result",
    "get_server_status_service",
    "map_ko_identifiers",
    "normalize_annotations",
    "read_cached_kegg_entry",
    "retrieve_kegg_entries",
]
