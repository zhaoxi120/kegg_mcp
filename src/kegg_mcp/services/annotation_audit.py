"""Conservative annotation and KEGG relationship mapping audit."""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
from functools import partial
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    EvidenceMode,
    FrozenModel,
    NormalizedStatus,
    SourceProvenance,
    StatusCount,
    build_ko_evidence_view,
    normalize_identifier_label,
    select_ko_ids,
)
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, SafeDetail, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.kegg import KeggLinkRelationship, KeggRequestOptions
from kegg_mcp.kegg.contracts import KeggBatchProvenance
from kegg_mcp.services.kegg_relations import (
    bounded_relation_batches,
    planned_relation_request_count,
)
from kegg_mcp.services.models import DETAIL_SECTION, DatasetSource
from kegg_mcp.services.query_models import QueryRetrievalSummary
from kegg_mcp.services.query_support import (
    require_bounded_query_direct_result,
    summarize_query_retrieval,
)
from kegg_mcp.services.reference_budget import KeggRelationClient, effective_query_options
from kegg_mcp.services.result_builders import (
    _artifact_metadata,
    _json_bytes,
    _resolve_dataset,
)
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    ResultArtifactMetadata,
    ResultMetadata,
    SQLiteResultStore,
    create_retained_result,
)

MAX_AUDIT_DEGREE_BUCKETS = 512
MAX_AUDIT_UNMAPPED_PREVIEW = 50
MAX_AUDIT_RELATIONSHIP_ROWS = 50_000
MAX_AUDIT_RESPONSE_BYTES = 25_000_000
MAX_AUDIT_KEGG_REQUESTS = 100
MAX_AUDIT_ARTIFACT_BYTES = 32_000_000
MAX_AUDIT_WARNINGS = 25
MAX_AUDIT_WARNING_PREVIEW = 5
MAX_AUDIT_WARNING_PREVIEW_MESSAGE_CHARACTERS = 256


class AnnotationMappingTarget(StrEnum):
    """Allowlisted KEGG relationship classes available to the audit."""

    PATHWAY = "pathway"
    MODULE = "module"
    REACTION = "reaction"
    ENZYME = "enzyme"
    BRITE = "brite"


class AnnotationMappingExecutionStatus(StrEnum):
    """Whether the optional KEGG mapping phase ran within its request budget."""

    COMPLETED = "completed"
    NOT_REQUESTED = "not_requested"
    SKIPPED_REQUEST_LIMIT = "skipped_request_limit"
    INCOMPLETE_ROW_LIMIT = "incomplete_row_limit"
    INCOMPLETE_RESPONSE_LIMIT = "incomplete_response_limit"


class AnnotationMappingLimitKind(StrEnum):
    """Aggregate mapping limit that stopped an in-progress target."""

    ROW_COUNT = "row_count"
    RESPONSE_BYTES = "response_bytes"


class AnnotationMappingExecution(FrozenModel):
    """Explicit status for the separable remote mapping phase."""

    status: AnnotationMappingExecutionStatus
    requested_targets: Annotated[
        tuple[AnnotationMappingTarget, ...],
        Field(max_length=len(AnnotationMappingTarget)),
    ]
    completed_targets: Annotated[
        tuple[AnnotationMappingTarget, ...],
        Field(max_length=len(AnnotationMappingTarget)),
    ]
    skipped_targets: Annotated[
        tuple[AnnotationMappingTarget, ...],
        Field(max_length=len(AnnotationMappingTarget)),
    ]
    incomplete_target: AnnotationMappingTarget | None = None
    selected_unique_ko_count: int = Field(strict=True, ge=0)
    planned_request_count: int = Field(strict=True, ge=0)
    request_limit: int = Field(strict=True, ge=1)
    limit_kind: AnnotationMappingLimitKind | None = None
    limit_observed: int | None = Field(default=None, strict=True, ge=0)
    limit_value: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode="after")
    def validate_execution(self) -> AnnotationMappingExecution:
        for values in (
            self.requested_targets,
            self.completed_targets,
            self.skipped_targets,
        ):
            if len(values) != len(set(values)):
                raise ValueError("mapping execution target lists must be unique")
            if values != tuple(target for target in AnnotationMappingTarget if target in values):
                raise ValueError("mapping execution targets must use stable enum order")
        limit_values = (self.limit_kind, self.limit_observed, self.limit_value)
        if self.status is AnnotationMappingExecutionStatus.COMPLETED:
            if (
                not self.requested_targets
                or self.completed_targets != self.requested_targets
                or self.skipped_targets
                or self.incomplete_target is not None
                or self.planned_request_count > self.request_limit
                or any(value is not None for value in limit_values)
            ):
                raise ValueError("completed mapping execution must cover every requested target")
        elif self.status is AnnotationMappingExecutionStatus.NOT_REQUESTED:
            if (
                self.requested_targets
                or self.completed_targets
                or self.skipped_targets
                or self.incomplete_target is not None
                or self.planned_request_count
                or any(value is not None for value in limit_values)
            ):
                raise ValueError("not-requested mapping execution cannot contain target work")
        elif self.status is AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT:
            if (
                not self.requested_targets
                or self.completed_targets
                or self.skipped_targets != self.requested_targets
                or self.incomplete_target is not None
                or self.planned_request_count <= self.request_limit
                or any(value is not None for value in limit_values)
            ):
                raise ValueError("request-limit skips must report every requested target")
        else:
            expected_limit_kind = (
                AnnotationMappingLimitKind.ROW_COUNT
                if self.status is AnnotationMappingExecutionStatus.INCOMPLETE_ROW_LIMIT
                else AnnotationMappingLimitKind.RESPONSE_BYTES
            )
            if (
                not self.requested_targets
                or self.incomplete_target is None
                or self.planned_request_count > self.request_limit
                or self.limit_kind is not expected_limit_kind
                or self.limit_observed is None
                or self.limit_value is None
                or self.limit_observed <= self.limit_value
                or (
                    (
                        *self.completed_targets,
                        self.incomplete_target,
                        *self.skipped_targets,
                    )
                    != self.requested_targets
                )
            ):
                raise ValueError(
                    "incomplete mapping execution must partition requested targets and report "
                    "the exceeded aggregate limit"
                )
        return self


class AnnotationAuditWarningCode(StrEnum):
    """Stable warning codes for incomplete or mixed provenance."""

    MISSING_SOURCE_VERSION = "missing_source_version"
    MISSING_MODEL_NAME = "missing_model_name"
    MISSING_MODEL_VERSION = "missing_model_version"
    MISSING_ANNOTATION_DATE = "missing_annotation_date"
    MIXED_ANNOTATION_PIPELINES = "mixed_annotation_pipelines"
    KEGG_RELEASE_UNAVAILABLE = "kegg_release_unavailable"
    STALE_KEGG_RESPONSE = "stale_kegg_response"
    INCOMPLETE_ASSEMBLY_CONTEXT = "incomplete_assembly_context"
    CONTAMINATION_CONTEXT = "contamination_context"


class GenomeType(StrEnum):
    """Optional assembly context without changing annotation evidence."""

    ISOLATE = "isolate"
    MAG = "MAG"
    SAG = "SAG"


class AnnotationQualityContext(FrozenModel):
    """Caller-supplied assembly and annotation context used only for warnings."""

    assembly_completeness: float | None = Field(default=None, ge=0.0, le=100.0)
    assembly_contamination: float | None = Field(default=None, ge=0.0, le=100.0)
    genome_type: GenomeType | None = None
    gene_caller: str | None = Field(default=None, min_length=1, max_length=256)
    annotation_tool: str | None = Field(default=None, min_length=1, max_length=256)
    annotation_database_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=256,
    )

    @field_validator("gene_caller", "annotation_tool", "annotation_database_version")
    @classmethod
    def normalize_context_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_identifier_label(value, field_name="annotation quality context")

    @model_validator(mode="after")
    def require_context_value(self) -> AnnotationQualityContext:
        if not any(
            value is not None
            for value in (
                self.assembly_completeness,
                self.assembly_contamination,
                self.genome_type,
                self.gene_caller,
                self.annotation_tool,
                self.annotation_database_version,
            )
        ):
            raise ValueError("quality_context must contain at least one supplied field")
        return self


class AnnotationAuditWarning(FrozenModel):
    """One bounded audit warning that does not alter source evidence."""

    code: AnnotationAuditWarningCode
    message: str = Field(min_length=1, max_length=1_000)
    affected_count: int = Field(default=0, strict=True, ge=0)


class AnnotationEvidenceAudit(FrozenModel):
    """Deterministic evidence and import counts from the retained dataset."""

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    analysis_unit: AnalysisUnit
    input_rows: int = Field(strict=True, ge=0)
    emitted_records: int = Field(strict=True, ge=0)
    skipped_rows: int = Field(strict=True, ge=0)
    records_with_valid_ko: int = Field(strict=True, ge=0)
    unique_valid_ko_count: int = Field(strict=True, ge=0)
    strict_unique_ko_count: int = Field(strict=True, ge=0)
    lenient_unique_ko_count: int = Field(strict=True, ge=0)
    rejected_unique_ko_count: int = Field(strict=True, ge=0)
    duplicate_assignment_count: int = Field(strict=True, ge=0)
    conflicting_assignment_count: int = Field(strict=True, ge=0)
    source_count: int = Field(strict=True, ge=1)
    status_counts: Annotated[
        tuple[StatusCount, ...],
        Field(min_length=len(NormalizedStatus), max_length=len(NormalizedStatus)),
    ]

    @model_validator(mode="after")
    def validate_counts(self) -> AnnotationEvidenceAudit:
        if sum(item.count for item in self.status_counts) != self.emitted_records:
            raise ValueError("status_counts must sum to emitted_records")
        if self.strict_unique_ko_count > self.lenient_unique_ko_count:
            raise ValueError("strict KO count cannot exceed lenient KO count")
        if self.lenient_unique_ko_count > self.unique_valid_ko_count:
            raise ValueError("lenient KO count cannot exceed all valid K numbers")
        return self


class MappingDegreeCount(FrozenModel):
    """Count of mapped K numbers with one exact target degree."""

    target_count: int = Field(strict=True, ge=1)
    ko_count: int = Field(strict=True, ge=1)


class EvidenceModeMappingAudit(FrozenModel):
    """Mapping yield for one evidence view and one fixed relationship target."""

    evidence_mode: EvidenceMode
    selected_unique_ko_count: int = Field(strict=True, ge=0)
    mapped_unique_ko_count: int = Field(strict=True, ge=0)
    mapping_yield: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    raw_relationship_row_count: int = Field(strict=True, ge=0)
    unique_relationship_count: int = Field(strict=True, ge=0)
    one_to_many_ko_count: int = Field(strict=True, ge=0)
    target_degree_distribution: Annotated[
        tuple[MappingDegreeCount, ...],
        Field(max_length=MAX_AUDIT_DEGREE_BUCKETS),
    ]
    unmapped_ko_count: int = Field(strict=True, ge=0)
    unmapped_ko_preview: Annotated[tuple[str, ...], Field(max_length=MAX_AUDIT_UNMAPPED_PREVIEW)]
    unmapped_preview_truncated: bool

    @model_validator(mode="after")
    def validate_mapping_counts(self) -> EvidenceModeMappingAudit:
        if self.mapped_unique_ko_count + self.unmapped_ko_count != self.selected_unique_ko_count:
            raise ValueError("mapped and unmapped KO counts must partition the selected set")
        expected_yield = (
            None
            if self.selected_unique_ko_count == 0
            else self.mapped_unique_ko_count / self.selected_unique_ko_count
        )
        if self.mapping_yield != expected_yield:
            raise ValueError("mapping_yield must use the selected unique KO denominator")
        if self.unmapped_preview_truncated != (
            self.unmapped_ko_count > len(self.unmapped_ko_preview)
        ):
            raise ValueError("unmapped preview truncation must match its count")
        if tuple(item.target_count for item in self.target_degree_distribution) != tuple(
            sorted(item.target_count for item in self.target_degree_distribution)
        ):
            raise ValueError("target degree distribution must be sorted")
        if sum(item.ko_count for item in self.target_degree_distribution) != (
            self.mapped_unique_ko_count
        ):
            raise ValueError("target degree distribution must count every mapped KO")
        if (
            sum(item.ko_count for item in self.target_degree_distribution if item.target_count > 1)
            != self.one_to_many_ko_count
        ):
            raise ValueError("target degree distribution must agree with one-to-many count")
        return self


class AnnotationTargetMappingAudit(FrozenModel):
    """Strict and lenient yields derived from one shared KEGG retrieval."""

    target: AnnotationMappingTarget
    strict: EvidenceModeMappingAudit
    lenient: EvidenceModeMappingAudit


class EvidenceModeMappingAuditSummary(FrozenModel):
    """Compact direct mapping counts without degree distributions or KO previews."""

    evidence_mode: EvidenceMode
    selected_unique_ko_count: int = Field(strict=True, ge=0)
    mapped_unique_ko_count: int = Field(strict=True, ge=0)
    mapping_yield: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    raw_relationship_row_count: int = Field(strict=True, ge=0)
    unique_relationship_count: int = Field(strict=True, ge=0)
    one_to_many_ko_count: int = Field(strict=True, ge=0)
    unmapped_ko_count: int = Field(strict=True, ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> EvidenceModeMappingAuditSummary:
        if self.mapped_unique_ko_count + self.unmapped_ko_count != self.selected_unique_ko_count:
            raise ValueError("mapped and unmapped KO counts must partition the selected set")
        expected_yield = (
            None
            if self.selected_unique_ko_count == 0
            else self.mapped_unique_ko_count / self.selected_unique_ko_count
        )
        if self.mapping_yield != expected_yield:
            raise ValueError("mapping_yield must use the selected unique KO denominator")
        if self.one_to_many_ko_count > self.mapped_unique_ko_count:
            raise ValueError("one-to-many count cannot exceed mapped unique K numbers")
        return self


class AnnotationTargetMappingAuditSummary(FrozenModel):
    """Compact strict and lenient mapping summary for one selected relationship."""

    target: AnnotationMappingTarget
    strict: EvidenceModeMappingAuditSummary
    lenient: EvidenceModeMappingAuditSummary


class AnnotationAuditWarningPreview(FrozenModel):
    """One compact direct warning preview; the complete warning remains retained."""

    code: AnnotationAuditWarningCode
    message: str = Field(
        min_length=1,
        max_length=MAX_AUDIT_WARNING_PREVIEW_MESSAGE_CHARACTERS,
    )
    message_truncated: bool
    affected_count: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode="after")
    def validate_truncation(self) -> AnnotationAuditWarningPreview:
        if (
            self.message_truncated
            and len(self.message) != MAX_AUDIT_WARNING_PREVIEW_MESSAGE_CHARACTERS
        ):
            raise ValueError("truncated audit warning previews must fill their fixed text bound")
        return self


class AnnotationAuditDetail(FrozenModel):
    """Complete retained audit detail; relationship rows and provenance are stored alongside it."""

    evidence: AnnotationEvidenceAudit
    mappings: Annotated[
        tuple[AnnotationTargetMappingAudit, ...],
        Field(max_length=len(AnnotationMappingTarget)),
    ]
    mapping_execution: AnnotationMappingExecution
    lenient_only_ko_count: int = Field(strict=True, ge=0)
    lenient_only_ko_preview: Annotated[
        tuple[str, ...], Field(max_length=MAX_AUDIT_UNMAPPED_PREVIEW)
    ]
    strict_without_any_audited_relationship_count: int | None = Field(
        default=None,
        strict=True,
        ge=0,
    )
    strict_without_any_audited_relationship_preview: Annotated[
        tuple[str, ...], Field(max_length=MAX_AUDIT_UNMAPPED_PREVIEW)
    ]
    lenient_without_any_audited_relationship_count: int | None = Field(
        default=None,
        strict=True,
        ge=0,
    )
    lenient_without_any_audited_relationship_preview: Annotated[
        tuple[str, ...], Field(max_length=MAX_AUDIT_UNMAPPED_PREVIEW)
    ]
    retrieval: QueryRetrievalSummary
    quality_context: AnnotationQualityContext | None = None
    warning_count: int = Field(strict=True, ge=0, le=MAX_AUDIT_WARNINGS)
    warnings: Annotated[tuple[AnnotationAuditWarning, ...], Field(max_length=MAX_AUDIT_WARNINGS)]
    interpretation_caveats: Annotated[tuple[str, ...], Field(min_length=2, max_length=4)]

    @model_validator(mode="after")
    def validate_detail(self) -> AnnotationAuditDetail:
        if tuple(item.target for item in self.mappings) != (
            self.mapping_execution.completed_targets
        ):
            raise ValueError("mappings must match completed targets in stable order")
        if self.lenient_only_ko_count != (
            self.evidence.lenient_unique_ko_count - self.evidence.strict_unique_ko_count
        ):
            raise ValueError("lenient-only count must match strict and lenient evidence counts")
        if self.mapping_execution.selected_unique_ko_count != self.evidence.lenient_unique_ko_count:
            raise ValueError("mapping execution must use the lenient unique-KO denominator")
        if self.warning_count != len(self.warnings):
            raise ValueError("warning_count must match warnings")
        mapping_completed = (
            self.mapping_execution.status is AnnotationMappingExecutionStatus.COMPLETED
        )
        without_counts = (
            self.strict_without_any_audited_relationship_count,
            self.lenient_without_any_audited_relationship_count,
        )
        if mapping_completed != all(value is not None for value in without_counts):
            raise ValueError("no-relationship counts are available only after completed mapping")
        if not mapping_completed and (
            self.strict_without_any_audited_relationship_preview
            or self.lenient_without_any_audited_relationship_preview
        ):
            raise ValueError("skipped mapping cannot provide no-relationship previews")
        return self


class AnnotationMappingAuditResult(FrozenModel):
    """Compact direct annotation audit with complete detail retained."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    evidence: AnnotationEvidenceAudit
    mapping_execution: AnnotationMappingExecution
    mappings: Annotated[
        tuple[AnnotationTargetMappingAuditSummary, ...],
        Field(max_length=len(AnnotationMappingTarget)),
    ]
    lenient_only_ko_count: int = Field(strict=True, ge=0)
    strict_without_any_audited_relationship_count: int | None = Field(
        default=None,
        strict=True,
        ge=0,
    )
    lenient_without_any_audited_relationship_count: int | None = Field(
        default=None,
        strict=True,
        ge=0,
    )
    retrieval: QueryRetrievalSummary
    warning_count: int = Field(strict=True, ge=0, le=MAX_AUDIT_WARNINGS)
    warning_preview: Annotated[
        tuple[AnnotationAuditWarningPreview, ...],
        Field(max_length=MAX_AUDIT_WARNING_PREVIEW),
    ]
    warnings_truncated: bool
    interpretation_caveats: Annotated[tuple[str, ...], Field(min_length=2, max_length=4)]

    @model_validator(mode="after")
    def validate_summary(self) -> AnnotationMappingAuditResult:
        if tuple(item.target for item in self.mappings) != self.mapping_execution.completed_targets:
            raise ValueError("mapping summaries must match completed targets in stable order")
        if self.lenient_only_ko_count != (
            self.evidence.lenient_unique_ko_count - self.evidence.strict_unique_ko_count
        ):
            raise ValueError("lenient-only count must match strict and lenient evidence counts")
        if self.mapping_execution.selected_unique_ko_count != self.evidence.lenient_unique_ko_count:
            raise ValueError("mapping execution must use the lenient unique-KO denominator")
        mapping_completed = (
            self.mapping_execution.status is AnnotationMappingExecutionStatus.COMPLETED
        )
        without_counts = (
            self.strict_without_any_audited_relationship_count,
            self.lenient_without_any_audited_relationship_count,
        )
        if mapping_completed != all(value is not None for value in without_counts):
            raise ValueError("no-relationship counts are available only after completed mapping")
        if self.warning_count < len(self.warning_preview):
            raise ValueError("warning_count cannot be smaller than warning_preview")
        if self.warnings_truncated != (self.warning_count > len(self.warning_preview)):
            raise ValueError("warnings_truncated must match warning_preview")
        return self


_RELATIONSHIPS = {
    AnnotationMappingTarget.PATHWAY: KeggLinkRelationship.KO_TO_PATHWAY,
    AnnotationMappingTarget.MODULE: KeggLinkRelationship.KO_TO_MODULE,
    AnnotationMappingTarget.REACTION: KeggLinkRelationship.KO_TO_REACTION,
    AnnotationMappingTarget.ENZYME: KeggLinkRelationship.KO_TO_ENZYME,
    AnnotationMappingTarget.BRITE: KeggLinkRelationship.KO_TO_BRITE,
}


def audit_annotation_mapping(
    source: DatasetSource,
    *,
    client: KeggRelationClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    quality_context: AnnotationQualityContext | None = None,
    mapping_targets: tuple[AnnotationMappingTarget, ...] = tuple(AnnotationMappingTarget),
    options: KeggRequestOptions | None = None,
) -> AnnotationMappingAuditResult:
    """Audit normalized evidence and selected KEGG mapping yields without inference."""
    options = effective_query_options(options)
    mapping_targets = _canonical_mapping_targets(mapping_targets)
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    evidence_view = build_ko_evidence_view(dataset)
    strict_kos = select_ko_ids(evidence_view, EvidenceMode.STRICT)
    lenient_kos = select_ko_ids(evidence_view, EvidenceMode.LENIENT)
    planned_request_count = sum(
        planned_relation_request_count(
            lenient_kos,
            relationship=_RELATIONSHIPS[target],
            client=client,
        )
        for target in mapping_targets
    )
    if not mapping_targets:
        mapping_status = AnnotationMappingExecutionStatus.NOT_REQUESTED
    elif planned_request_count > MAX_AUDIT_KEGG_REQUESTS:
        mapping_status = AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT
    else:
        mapping_status = AnnotationMappingExecutionStatus.COMPLETED
    skipped_targets: tuple[AnnotationMappingTarget, ...] = (
        mapping_targets
        if mapping_status is AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT
        else ()
    )

    mappings: list[AnnotationTargetMappingAudit] = []
    completed_targets: list[AnnotationMappingTarget] = []
    all_batches: list[KeggBatchProvenance] = []
    mapped_strict_any: set[str] = set()
    mapped_lenient_any: set[str] = set()
    complete_rows: dict[str, list[dict[str, object]]] = {}
    remaining_rows = MAX_AUDIT_RELATIONSHIP_ROWS
    remaining_bytes = MAX_AUDIT_RESPONSE_BYTES
    remaining_requests = MAX_AUDIT_KEGG_REQUESTS
    incomplete_target: AnnotationMappingTarget | None = None
    limit_kind: AnnotationMappingLimitKind | None = None
    limit_observed: int | None = None
    limit_value: int | None = None

    targets_to_map = (
        mapping_targets if mapping_status is AnnotationMappingExecutionStatus.COMPLETED else ()
    )
    for target_index, target in enumerate(targets_to_map):
        if lenient_kos:
            accepted_partial_batches: list[KeggBatchProvenance] = []
            consumed_rows = MAX_AUDIT_RELATIONSHIP_ROWS - remaining_rows
            consumed_bytes = MAX_AUDIT_RESPONSE_BYTES - remaining_bytes
            try:
                mapped = bounded_relation_batches(
                    lenient_kos,
                    relationship=_RELATIONSHIPS[target],
                    client=client,
                    options=options,
                    max_total_requests=remaining_requests,
                    max_total_rows=remaining_rows,
                    max_total_response_bytes=remaining_bytes,
                    record_batch=partial(
                        _record_audit_batches,
                        accepted_partial_batches,
                    ),
                )
            except KeggMcpError as error:
                aggregate_limit = _audit_mapping_limit(
                    error,
                    consumed_rows=consumed_rows,
                    consumed_bytes=consumed_bytes,
                )
                if aggregate_limit is None:
                    raise
                (
                    mapping_status,
                    limit_kind,
                    limit_observed,
                    limit_value,
                ) = aggregate_limit
                incomplete_target = target
                skipped_targets = targets_to_map[target_index + 1 :]
                all_batches.extend(accepted_partial_batches)
                break
            remaining_requests -= len(mapped.batches)
            remaining_rows -= len(mapped.rows)
            response_bytes = sum(batch.response_bytes for batch in mapped.batches)
            remaining_bytes -= response_bytes
            batch_offset = len(all_batches)
            rows = tuple(
                row.model_copy(update={"batch_index": row.batch_index + batch_offset})
                for row in mapped.rows
            )
            all_batches.extend(mapped.batches)
        else:
            rows = ()
        targets_by_ko: dict[str, set[str]] = defaultdict(set)
        raw_rows_by_ko: dict[str, int] = defaultdict(int)
        serialized_rows: list[dict[str, object]] = []
        for row in rows:
            ko_id, _ = try_normalize_ko_id(row.source_id.rsplit(":", 1)[-1])
            if ko_id is None or ko_id not in lenient_kos:
                fail(
                    ErrorCode.KEGG_PARSE_FAILED,
                    "A KEGG audit relationship row has an unexpected source identifier.",
                    suggested_action="Refresh the typed KEGG relationship response and retry.",
                )
            targets_by_ko[ko_id].add(row.target_id)
            raw_rows_by_ko[ko_id] += 1
            serialized_rows.append(row.model_dump(mode="json"))
        complete_rows[target.value] = serialized_rows
        mapped_lenient_any.update(targets_by_ko)
        mapped_strict_any.update(set(strict_kos) & set(targets_by_ko))
        mappings.append(
            AnnotationTargetMappingAudit(
                target=target,
                strict=_mode_mapping_audit(
                    EvidenceMode.STRICT,
                    strict_kos,
                    targets_by_ko,
                    raw_rows_by_ko,
                ),
                lenient=_mode_mapping_audit(
                    EvidenceMode.LENIENT,
                    lenient_kos,
                    targets_by_ko,
                    raw_rows_by_ko,
                ),
            )
        )
        completed_targets.append(target)

    mapping_execution = AnnotationMappingExecution(
        status=mapping_status,
        requested_targets=mapping_targets,
        completed_targets=tuple(completed_targets),
        skipped_targets=skipped_targets,
        incomplete_target=incomplete_target,
        selected_unique_ko_count=len(lenient_kos),
        planned_request_count=(
            0
            if mapping_status is AnnotationMappingExecutionStatus.NOT_REQUESTED
            else planned_request_count
        ),
        request_limit=MAX_AUDIT_KEGG_REQUESTS,
        limit_kind=limit_kind,
        limit_observed=limit_observed,
        limit_value=limit_value,
    )

    warnings = _audit_warnings(
        dataset.sources,
        tuple(all_batches),
        quality_context=quality_context,
    )
    strict_without_any = (
        tuple(sorted(set(strict_kos) - mapped_strict_any))
        if mapping_status is AnnotationMappingExecutionStatus.COMPLETED
        else ()
    )
    lenient_without_any = (
        tuple(sorted(set(lenient_kos) - mapped_lenient_any))
        if mapping_status is AnnotationMappingExecutionStatus.COMPLETED
        else ()
    )
    lenient_only = tuple(sorted(set(lenient_kos) - set(strict_kos)))
    evidence = AnnotationEvidenceAudit(
        dataset_id=dataset.dataset_id,
        analysis_unit=dataset.analysis_unit,
        input_rows=dataset.import_report.input_rows,
        emitted_records=dataset.import_report.emitted_records,
        skipped_rows=dataset.import_report.skipped_rows,
        records_with_valid_ko=sum(record.ko_id is not None for record in dataset.records),
        unique_valid_ko_count=len(evidence_view.records_by_ko),
        strict_unique_ko_count=len(strict_kos),
        lenient_unique_ko_count=len(lenient_kos),
        rejected_unique_ko_count=len(evidence_view.rejected_kos),
        duplicate_assignment_count=dataset.import_report.duplicate_count,
        conflicting_assignment_count=dataset.import_report.conflict_count,
        source_count=len(dataset.sources),
        status_counts=evidence_view.status_counts,
    )
    batches = tuple(all_batches)
    retrieval = summarize_query_retrieval(batches)
    detail = AnnotationAuditDetail(
        evidence=evidence,
        mappings=tuple(mappings),
        mapping_execution=mapping_execution,
        lenient_only_ko_count=len(lenient_only),
        lenient_only_ko_preview=lenient_only[:MAX_AUDIT_UNMAPPED_PREVIEW],
        strict_without_any_audited_relationship_count=(
            len(strict_without_any)
            if mapping_status is AnnotationMappingExecutionStatus.COMPLETED
            else None
        ),
        strict_without_any_audited_relationship_preview=strict_without_any[
            :MAX_AUDIT_UNMAPPED_PREVIEW
        ],
        lenient_without_any_audited_relationship_count=(
            len(lenient_without_any)
            if mapping_status is AnnotationMappingExecutionStatus.COMPLETED
            else None
        ),
        lenient_without_any_audited_relationship_preview=lenient_without_any[
            :MAX_AUDIT_UNMAPPED_PREVIEW
        ],
        retrieval=retrieval,
        quality_context=quality_context,
        warning_count=len(warnings),
        warnings=warnings,
        interpretation_caveats=_audit_caveats(mapping_execution),
    )
    payload = _json_bytes(
        {
            "detail": detail.model_dump(mode="json"),
            "complete_relationship_rows": complete_rows,
            "strict_ko_ids": list(strict_kos),
            "lenient_only_ko_ids": list(lenient_only),
            "strict_without_any_audited_relationship": list(strict_without_any),
            "lenient_without_any_audited_relationship": list(lenient_without_any),
            "provenance": [batch.model_dump(mode="json") for batch in batches],
        }
    )
    if len(payload) > MAX_AUDIT_ARTIFACT_BYTES:
        fail(
            ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            "The annotation mapping audit artifact exceeded its output bound.",
            suggested_action="Audit a smaller annotation dataset.",
            safe_details=(
                SafeDetail(name="observed_bytes", value=str(len(payload))),
                SafeDetail(name="limit_bytes", value=str(MAX_AUDIT_ARTIFACT_BYTES)),
            ),
        )
    with create_retained_result(
        result_store,
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION,
                mime_type="application/json",
                content=payload,
            ),
        ),
    ) as stored:
        result = AnnotationMappingAuditResult(
            result=stored,
            artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
            evidence=evidence,
            mapping_execution=mapping_execution,
            mappings=tuple(_mapping_summary(mapping) for mapping in mappings),
            lenient_only_ko_count=len(lenient_only),
            strict_without_any_audited_relationship_count=(
                len(strict_without_any)
                if mapping_status is AnnotationMappingExecutionStatus.COMPLETED
                else None
            ),
            lenient_without_any_audited_relationship_count=(
                len(lenient_without_any)
                if mapping_status is AnnotationMappingExecutionStatus.COMPLETED
                else None
            ),
            retrieval=retrieval,
            warning_count=len(warnings),
            warning_preview=tuple(
                _warning_preview(warning) for warning in warnings[:MAX_AUDIT_WARNING_PREVIEW]
            ),
            warnings_truncated=len(warnings) > MAX_AUDIT_WARNING_PREVIEW,
            interpretation_caveats=detail.interpretation_caveats,
        )
        require_bounded_query_direct_result(result)
        return result


def _mapping_summary(
    mapping: AnnotationTargetMappingAudit,
) -> AnnotationTargetMappingAuditSummary:
    return AnnotationTargetMappingAuditSummary(
        target=mapping.target,
        strict=_mode_mapping_summary(mapping.strict),
        lenient=_mode_mapping_summary(mapping.lenient),
    )


def _record_audit_batches(
    destination: list[KeggBatchProvenance],
    _row_count: int,
    batches: tuple[KeggBatchProvenance, ...],
) -> None:
    destination.extend(batches)


def _mode_mapping_summary(
    mapping: EvidenceModeMappingAudit,
) -> EvidenceModeMappingAuditSummary:
    return EvidenceModeMappingAuditSummary(
        evidence_mode=mapping.evidence_mode,
        selected_unique_ko_count=mapping.selected_unique_ko_count,
        mapped_unique_ko_count=mapping.mapped_unique_ko_count,
        mapping_yield=mapping.mapping_yield,
        raw_relationship_row_count=mapping.raw_relationship_row_count,
        unique_relationship_count=mapping.unique_relationship_count,
        one_to_many_ko_count=mapping.one_to_many_ko_count,
        unmapped_ko_count=mapping.unmapped_ko_count,
    )


def _warning_preview(
    warning: AnnotationAuditWarning,
) -> AnnotationAuditWarningPreview:
    message = warning.message[:MAX_AUDIT_WARNING_PREVIEW_MESSAGE_CHARACTERS]
    return AnnotationAuditWarningPreview(
        code=warning.code,
        message=message,
        message_truncated=len(warning.message) > len(message),
        affected_count=warning.affected_count,
    )


def _mode_mapping_audit(
    mode: EvidenceMode,
    selected_kos: tuple[str, ...],
    targets_by_ko: dict[str, set[str]],
    raw_rows_by_ko: dict[str, int],
) -> EvidenceModeMappingAudit:
    selected = set(selected_kos)
    mapped = selected & set(targets_by_ko)
    unmapped = tuple(sorted(selected - mapped))
    unique_edges = sum(len(targets_by_ko[ko_id]) for ko_id in mapped)
    degree_counts: dict[int, int] = defaultdict(int)
    for ko_id in mapped:
        degree_counts[len(targets_by_ko[ko_id])] += 1
    return EvidenceModeMappingAudit(
        evidence_mode=mode,
        selected_unique_ko_count=len(selected),
        mapped_unique_ko_count=len(mapped),
        mapping_yield=(None if not selected else len(mapped) / len(selected)),
        raw_relationship_row_count=sum(raw_rows_by_ko[ko_id] for ko_id in mapped),
        unique_relationship_count=unique_edges,
        one_to_many_ko_count=sum(len(targets_by_ko[ko_id]) > 1 for ko_id in mapped),
        target_degree_distribution=tuple(
            MappingDegreeCount(target_count=degree, ko_count=count)
            for degree, count in sorted(degree_counts.items())
        ),
        unmapped_ko_count=len(unmapped),
        unmapped_ko_preview=unmapped[:MAX_AUDIT_UNMAPPED_PREVIEW],
        unmapped_preview_truncated=len(unmapped) > MAX_AUDIT_UNMAPPED_PREVIEW,
    )


def _canonical_mapping_targets(
    targets: tuple[AnnotationMappingTarget, ...],
) -> tuple[AnnotationMappingTarget, ...]:
    if len(targets) != len(set(targets)):
        raise ValueError("annotation mapping targets must be unique")
    return tuple(target for target in AnnotationMappingTarget if target in targets)


def _audit_mapping_limit(
    error: KeggMcpError,
    *,
    consumed_rows: int,
    consumed_bytes: int,
) -> (
    tuple[
        AnnotationMappingExecutionStatus,
        AnnotationMappingLimitKind,
        int,
        int,
    ]
    | None
):
    if error.detail.code is not ErrorCode.INPUT_LIMIT_EXCEEDED:
        return None
    details = {item.name: item.value for item in error.detail.safe_details}
    try:
        observed = int(details["observed"])
    except (KeyError, ValueError):
        return None
    limit_name = details.get("limit_name")
    if limit_name == "relationship_row_count":
        return (
            AnnotationMappingExecutionStatus.INCOMPLETE_ROW_LIMIT,
            AnnotationMappingLimitKind.ROW_COUNT,
            consumed_rows + observed,
            MAX_AUDIT_RELATIONSHIP_ROWS,
        )
    if limit_name == "relationship_response_bytes":
        return (
            AnnotationMappingExecutionStatus.INCOMPLETE_RESPONSE_LIMIT,
            AnnotationMappingLimitKind.RESPONSE_BYTES,
            consumed_bytes + observed,
            MAX_AUDIT_RESPONSE_BYTES,
        )
    return None


def _audit_caveats(
    execution: AnnotationMappingExecution,
) -> tuple[str, ...]:
    if execution.status is AnnotationMappingExecutionStatus.COMPLETED:
        mapping_scope = (
            "The no-relationship counts cover only the selected audited relationships; "
            "missing links are not evidence of biological absence."
        )
    elif execution.status is AnnotationMappingExecutionStatus.NOT_REQUESTED:
        mapping_scope = (
            "KEGG relationship mapping was not requested; the evidence audit remains complete "
            "and no relationship absence was assessed."
        )
    elif execution.status is AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT:
        mapping_scope = (
            "KEGG relationship mapping was skipped before network access because its planned "
            "request count exceeded the audit limit; the evidence audit remains complete."
        )
    else:
        mapping_scope = (
            "KEGG relationship mapping stopped at an aggregate row or response-byte limit; "
            "only completed targets have mapping summaries, partial rows for the incomplete "
            "target were discarded, and the evidence audit remains complete."
        )
    return (
        mapping_scope,
        "Mapping yield is descriptive and is not enrichment, validation, activity, or phenotype.",
    )


def _audit_warnings(
    sources: tuple[SourceProvenance, ...],
    batches: tuple[KeggBatchProvenance, ...],
    *,
    quality_context: AnnotationQualityContext | None,
) -> tuple[AnnotationAuditWarning, ...]:
    warnings: list[AnnotationAuditWarning] = []
    missing_fields = (
        (
            AnnotationAuditWarningCode.MISSING_SOURCE_VERSION,
            "source_version",
            "One or more annotation sources do not identify their source/database version.",
        ),
        (
            AnnotationAuditWarningCode.MISSING_MODEL_NAME,
            "model_name",
            "One or more annotation sources do not identify an annotation model.",
        ),
        (
            AnnotationAuditWarningCode.MISSING_MODEL_VERSION,
            "model_version",
            "One or more annotation sources do not identify an annotation model version.",
        ),
        (
            AnnotationAuditWarningCode.MISSING_ANNOTATION_DATE,
            "annotation_date",
            "One or more annotation sources do not record an annotation date.",
        ),
    )
    for code, field_name, message in missing_fields:
        applicable_sources = (
            tuple(source for source in sources if source.importer_name != "plain_ko")
            if field_name in {"model_name", "model_version"}
            else sources
        )
        count = sum(getattr(source, field_name) is None for source in applicable_sources)
        if count:
            warnings.append(
                AnnotationAuditWarning(
                    code=code,
                    message=message,
                    affected_count=count,
                )
            )
    identities = {
        (
            source.source_name,
            source.source_version,
            source.model_name,
            source.model_version,
        )
        for source in sources
    }
    if len(identities) > 1:
        warnings.append(
            AnnotationAuditWarning(
                code=AnnotationAuditWarningCode.MIXED_ANNOTATION_PIPELINES,
                message=(
                    "The dataset combines annotation pipeline or version identities; scores and "
                    "thresholds must not be compared unless their semantics are known to match."
                ),
                affected_count=len(sources),
            )
        )
    missing_release_count = sum(batch.database_release is None for batch in batches)
    if missing_release_count:
        warnings.append(
            AnnotationAuditWarning(
                code=AnnotationAuditWarningCode.KEGG_RELEASE_UNAVAILABLE,
                message="The KEGG endpoint did not report a database release for these mappings.",
                affected_count=missing_release_count,
            )
        )
    stale_count = sum(batch.is_stale for batch in batches)
    if stale_count:
        warnings.append(
            AnnotationAuditWarning(
                code=AnnotationAuditWarningCode.STALE_KEGG_RESPONSE,
                message=(
                    "One or more mapping batches were served from explicitly allowed stale cache."
                ),
                affected_count=stale_count,
            )
        )
    if (
        quality_context is not None
        and quality_context.assembly_completeness is not None
        and quality_context.assembly_completeness < 100.0
    ):
        warnings.append(
            AnnotationAuditWarning(
                code=AnnotationAuditWarningCode.INCOMPLETE_ASSEMBLY_CONTEXT,
                message=(
                    "Reported assembly completeness below 100% may lower observed MODULE "
                    "required-block and KEGG relationship coverage; this audit does not correct "
                    "scores, infer missing K numbers, or perform gap filling."
                ),
                affected_count=1,
            )
        )
    if (
        quality_context is not None
        and quality_context.assembly_contamination is not None
        and quality_context.assembly_contamination > 0.0
    ):
        warnings.append(
            AnnotationAuditWarning(
                code=AnnotationAuditWarningCode.CONTAMINATION_CONTEXT,
                message=(
                    "Reported assembly contamination may contribute extra annotation evidence; "
                    "this audit does not remove or reweight supplied K-number assignments."
                ),
                affected_count=1,
            )
        )
    return tuple(warnings[:MAX_AUDIT_WARNINGS])


__all__ = [
    "MAX_AUDIT_ARTIFACT_BYTES",
    "AnnotationAuditDetail",
    "AnnotationAuditWarning",
    "AnnotationAuditWarningCode",
    "AnnotationAuditWarningPreview",
    "AnnotationEvidenceAudit",
    "AnnotationMappingAuditResult",
    "AnnotationMappingExecution",
    "AnnotationMappingExecutionStatus",
    "AnnotationMappingLimitKind",
    "AnnotationMappingTarget",
    "AnnotationQualityContext",
    "AnnotationTargetMappingAudit",
    "AnnotationTargetMappingAuditSummary",
    "EvidenceModeMappingAudit",
    "EvidenceModeMappingAuditSummary",
    "GenomeType",
    "MappingDegreeCount",
    "audit_annotation_mapping",
]
