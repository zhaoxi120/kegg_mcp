"""Conservative annotation and KEGG relationship mapping audit."""

from __future__ import annotations

from collections import defaultdict
from enum import StrEnum
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
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.kegg import KeggLinkRelationship, KeggRequestOptions, ResponseOrigin
from kegg_mcp.kegg.contracts import KeggBatchProvenance
from kegg_mcp.services.kegg_relations import bounded_relation_batches
from kegg_mcp.services.models import DETAIL_SECTION, DatasetSource
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
    compensate_created_result,
)

MAX_AUDIT_KOS = 500
MAX_AUDIT_UNMAPPED_PREVIEW = 50
MAX_AUDIT_PROVENANCE_PREVIEW = 25
MAX_AUDIT_RELATIONSHIP_ROWS = 50_000
MAX_AUDIT_RESPONSE_BYTES = 25_000_000
MAX_AUDIT_KEGG_REQUESTS = 100
MAX_AUDIT_ARTIFACT_BYTES = 32_000_000
MAX_AUDIT_WARNINGS = 25


class AnnotationMappingTarget(StrEnum):
    """KEGG relationship classes included in the fixed audit."""

    PATHWAY = "pathway"
    MODULE = "module"
    REACTION = "reaction"
    ENZYME = "enzyme"
    BRITE = "brite"


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
        Field(max_length=MAX_AUDIT_KOS),
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


class KeggAuditRetrievalSummary(FrozenModel):
    """Bounded retrieval provenance summary for the complete mapping audit."""

    batch_count: int = Field(strict=True, ge=0)
    network_request_count: int = Field(strict=True, ge=0)
    cache_hit_count: int = Field(strict=True, ge=0)
    stale_batch_count: int = Field(strict=True, ge=0)
    response_bytes: int = Field(strict=True, ge=0)
    database_releases: Annotated[tuple[str, ...], Field(max_length=25)]
    provenance_preview: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=MAX_AUDIT_PROVENANCE_PREVIEW)
    ]
    provenance_truncated: bool

    @model_validator(mode="after")
    def validate_provenance_preview(self) -> KeggAuditRetrievalSummary:
        if self.provenance_truncated != (self.batch_count > len(self.provenance_preview)):
            raise ValueError("provenance truncation must match the batch count")
        return self


class AnnotationAuditDetail(FrozenModel):
    """Direct bounded audit summary; complete relationship rows remain retained."""

    evidence: AnnotationEvidenceAudit
    mappings: Annotated[
        tuple[AnnotationTargetMappingAudit, ...],
        Field(
            min_length=len(AnnotationMappingTarget),
            max_length=len(AnnotationMappingTarget),
        ),
    ]
    lenient_only_ko_count: int = Field(strict=True, ge=0)
    lenient_only_ko_preview: Annotated[
        tuple[str, ...], Field(max_length=MAX_AUDIT_UNMAPPED_PREVIEW)
    ]
    strict_without_any_audited_relationship_count: int = Field(strict=True, ge=0)
    strict_without_any_audited_relationship_preview: Annotated[
        tuple[str, ...], Field(max_length=MAX_AUDIT_UNMAPPED_PREVIEW)
    ]
    lenient_without_any_audited_relationship_count: int = Field(strict=True, ge=0)
    lenient_without_any_audited_relationship_preview: Annotated[
        tuple[str, ...], Field(max_length=MAX_AUDIT_UNMAPPED_PREVIEW)
    ]
    retrieval: KeggAuditRetrievalSummary
    quality_context: AnnotationQualityContext | None = None
    warning_count: int = Field(strict=True, ge=0, le=MAX_AUDIT_WARNINGS)
    warnings: Annotated[tuple[AnnotationAuditWarning, ...], Field(max_length=MAX_AUDIT_WARNINGS)]
    interpretation_caveats: Annotated[tuple[str, ...], Field(min_length=2, max_length=4)] = (
        (
            "The no-relationship counts cover only pathway, MODULE, reaction, enzyme, and BRITE "
            "relationships audited here; missing links are not evidence of biological absence."
        ),
        "Mapping yield is descriptive and is not enrichment, validation, activity, or phenotype.",
    )

    @model_validator(mode="after")
    def validate_detail(self) -> AnnotationAuditDetail:
        if tuple(item.target for item in self.mappings) != tuple(AnnotationMappingTarget):
            raise ValueError("mappings must contain every fixed audit target in stable order")
        if self.lenient_only_ko_count != (
            self.evidence.lenient_unique_ko_count - self.evidence.strict_unique_ko_count
        ):
            raise ValueError("lenient-only count must match strict and lenient evidence counts")
        if self.warning_count != len(self.warnings):
            raise ValueError("warning_count must match warnings")
        return self


class AnnotationMappingAuditResult(FrozenModel):
    """Retained annotation mapping audit returned by the MCP service."""

    result: ResultMetadata
    artifact: ResultArtifactMetadata
    detail: AnnotationAuditDetail


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
    options: KeggRequestOptions | None = None,
) -> AnnotationMappingAuditResult:
    """Audit normalized evidence and five fixed KEGG mapping yields without inference."""
    options = effective_query_options(options)
    dataset = _resolve_dataset(source, result_store=result_store, scope_id=scope_id)
    evidence_view = build_ko_evidence_view(dataset)
    strict_kos = select_ko_ids(evidence_view, EvidenceMode.STRICT)
    lenient_kos = select_ko_ids(evidence_view, EvidenceMode.LENIENT)
    if len(lenient_kos) > MAX_AUDIT_KOS:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The annotation audit KO set exceeds the fixed unbiased mapping bound.",
            suggested_action=(
                f"Audit a dataset with no more than {MAX_AUDIT_KOS} lenient unique K numbers."
            ),
            safe_details=(
                SafeDetail(name="selected_unique_ko_count", value=str(len(lenient_kos))),
                SafeDetail(name="maximum_unique_ko_count", value=str(MAX_AUDIT_KOS)),
            ),
        )

    mappings: list[AnnotationTargetMappingAudit] = []
    all_batches: list[KeggBatchProvenance] = []
    mapped_strict_any: set[str] = set()
    mapped_lenient_any: set[str] = set()
    complete_rows: dict[str, list[dict[str, object]]] = {}
    remaining_rows = MAX_AUDIT_RELATIONSHIP_ROWS
    remaining_bytes = MAX_AUDIT_RESPONSE_BYTES
    remaining_requests = MAX_AUDIT_KEGG_REQUESTS

    for target in AnnotationMappingTarget:
        if lenient_kos:
            mapped = bounded_relation_batches(
                lenient_kos,
                relationship=_RELATIONSHIPS[target],
                client=client,
                options=options,
                max_total_requests=remaining_requests,
                max_total_rows=remaining_rows,
                max_total_response_bytes=remaining_bytes,
            )
            remaining_requests -= len(mapped.batches)
            remaining_rows -= len(mapped.rows)
            response_bytes = sum(batch.response_bytes for batch in mapped.batches)
            remaining_bytes -= response_bytes
            all_batches.extend(mapped.batches)
            rows = mapped.rows
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

    warnings = _audit_warnings(
        dataset.sources,
        tuple(all_batches),
        quality_context=quality_context,
    )
    strict_without_any = tuple(sorted(set(strict_kos) - mapped_strict_any))
    lenient_without_any = tuple(sorted(set(lenient_kos) - mapped_lenient_any))
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
    retrieval = KeggAuditRetrievalSummary(
        batch_count=len(batches),
        network_request_count=sum(
            batch.attempt_count for batch in batches if batch.origin is ResponseOrigin.NETWORK
        ),
        cache_hit_count=sum(batch.origin is ResponseOrigin.CACHE for batch in batches),
        stale_batch_count=sum(batch.is_stale for batch in batches),
        response_bytes=sum(batch.response_bytes for batch in batches),
        database_releases=tuple(
            sorted({batch.database_release for batch in batches if batch.database_release})
        ),
        provenance_preview=batches[:MAX_AUDIT_PROVENANCE_PREVIEW],
        provenance_truncated=len(batches) > MAX_AUDIT_PROVENANCE_PREVIEW,
    )
    detail = AnnotationAuditDetail(
        evidence=evidence,
        mappings=tuple(mappings),
        lenient_only_ko_count=len(lenient_only),
        lenient_only_ko_preview=lenient_only[:MAX_AUDIT_UNMAPPED_PREVIEW],
        strict_without_any_audited_relationship_count=len(strict_without_any),
        strict_without_any_audited_relationship_preview=strict_without_any[
            :MAX_AUDIT_UNMAPPED_PREVIEW
        ],
        lenient_without_any_audited_relationship_count=len(lenient_without_any),
        lenient_without_any_audited_relationship_preview=lenient_without_any[
            :MAX_AUDIT_UNMAPPED_PREVIEW
        ],
        retrieval=retrieval,
        quality_context=quality_context,
        warning_count=len(warnings),
        warnings=warnings,
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
    stored = result_store.create(
        scope_id,
        (
            ResultArtifactInput(
                section=DETAIL_SECTION,
                mime_type="application/json",
                content=payload,
            ),
        ),
    )
    try:
        return AnnotationMappingAuditResult(
            result=stored,
            artifact=_artifact_metadata(DETAIL_SECTION, "application/json", payload),
            detail=detail,
        )
    except BaseException:
        compensate_created_result(
            result_store,
            scope_id,
            stored.result_id,
            stored.created_at,
        )
        raise


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
        count = sum(getattr(source, field_name) is None for source in sources)
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
    "AnnotationEvidenceAudit",
    "AnnotationMappingAuditResult",
    "AnnotationMappingTarget",
    "AnnotationQualityContext",
    "AnnotationTargetMappingAudit",
    "EvidenceModeMappingAudit",
    "GenomeType",
    "KeggAuditRetrievalSummary",
    "MappingDegreeCount",
    "audit_annotation_mapping",
]
