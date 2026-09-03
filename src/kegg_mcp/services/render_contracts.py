"""Typed, bounded renderer handoff contracts independent of MCP transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, Self, TypeVar

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp import __version__
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageParameters,
    PathwayCoverageStatus,
    PathwayCoverageWarning,
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    evaluate_pathway_coverage,
    pathway_reference_scope_from_class,
)
from kegg_mcp.domain.analysis_view import KoAnalysisView
from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnalysisUnit,
    DecisionPolicyReference,
    FrozenModel,
    KNumber,
    NormalizedStatus,
    SourceProvenance,
    StatusCount,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.execution import AnalysisExecutionProvenance
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggOperation

RENDER_INPUT_SCHEMA_VERSION = "7"
RENDER_INPUT_MIME_TYPE = "application/vnd.kegg-mcp.render-input+json;version=7"
RENDER_INPUT_BUILDER_NAME = "kegg_render_handoff"
RENDER_INPUT_BUILDER_VERSION = "6"

PathwayId = Annotated[str, Field(pattern=r"^(?:ko|map|[a-z][a-z0-9]{1,7})[0-9]{5}$")]
MachineReason = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
_T = TypeVar("_T")


class RenderabilityStatus(StrEnum):
    """Whether the renderer can produce a complete graphic."""

    RENDERABLE = "renderable"
    SUMMARY_ONLY = "summary_only"
    NOT_RENDERABLE = "not_renderable"


class RenderInputLimits(FrozenModel):
    """Serialized renderer-handoff bounds recorded with every version 7 document."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:render-input-limits:3",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    max_evidence_ko_ids: int = Field(default=100_000, strict=True, gt=0, le=1_000_000)
    max_pathway_targets: int = Field(default=25, strict=True, ge=0, le=1_000)
    max_pathway_detected_ko_ids_per_target: int = Field(
        default=100_000, strict=True, ge=0, le=1_000_000
    )
    max_serialized_bytes: int = Field(default=50_000_000, strict=True, gt=0, le=100_000_000)


class RenderProducer(FrozenModel):
    """Core producer identity for one renderer handoff."""

    name: Literal["kegg-mcp"]
    version: str = Field(min_length=1, max_length=64)


class RenderDataset(FrozenModel):
    """Dataset identity, biological context, and annotation-source provenance."""

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    analysis_unit: AnalysisUnit
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(pattern=r"^[a-z][a-z0-9]{1,7}$")
    sources: Annotated[tuple[SourceProvenance, ...], Field(min_length=1, max_length=128)]


class VisualizationEvidence(FrozenModel):
    """Unique accepted K numbers eligible for coloring."""

    accepted_ko_ids: Annotated[tuple[KNumber, ...], Field(max_length=1_000_000)]
    status_counts: Annotated[
        tuple[StatusCount, ...],
        Field(min_length=len(NormalizedStatus), max_length=len(NormalizedStatus)),
    ]

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        if self.accepted_ko_ids != tuple(sorted(set(self.accepted_ko_ids))):
            raise ValueError("accepted_ko_ids must be sorted and unique")
        statuses = tuple(item.status for item in self.status_counts)
        if statuses != tuple(NormalizedStatus):
            raise ValueError("status_counts must use canonical normalized-status order")
        accepted_count = next(
            item.count for item in self.status_counts if item.status is NormalizedStatus.ACCEPTED
        )
        if len(self.accepted_ko_ids) > accepted_count:
            raise ValueError("unique accepted K numbers cannot exceed accepted assignments")
        return self


class PathwayRenderTarget(FrozenModel):
    """Complete-within-limit renderer target for one descriptive pathway result."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-render-target:4",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    pathway_id: PathwayId
    pathway_name: str = Field(min_length=1, max_length=1_000)
    pathway_class: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    reference_namespace: PathwayReferenceNamespace
    reference_scope: PathwayReferenceScope
    evaluation_status: PathwayCoverageStatus
    coverage_numerator: NonNegativeCount
    coverage_denominator: NonNegativeCount
    coverage_ratio: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    detected_ko_ids_complete: bool
    detected_ko_ids: Annotated[tuple[KNumber, ...], Field(max_length=1_000_000)]
    renderability: RenderabilityStatus
    not_renderable_reason: MachineReason | None = None
    reference_link_provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(min_length=1, max_length=64)
    ]
    reference_metadata_provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(min_length=1, max_length=64)
    ]
    calculation_method: Literal["unique_detected_kos_over_unique_reference_kos"]
    calculation_version: Literal["3"]
    warnings: Annotated[tuple[PathwayCoverageWarning, ...], Field(max_length=16)]

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        prefix = self.pathway_id[:-5]
        if (
            (self.reference_namespace is PathwayReferenceNamespace.KO and prefix != "ko")
            or (self.reference_namespace is PathwayReferenceNamespace.MAP and prefix != "map")
            or (
                self.reference_namespace is PathwayReferenceNamespace.ORGANISM
                and prefix in {"ko", "map"}
            )
        ):
            raise ValueError("pathway_id prefix must match reference_namespace")
        if pathway_reference_scope_from_class(self.pathway_class) is not self.reference_scope:
            raise ValueError(
                "reference_scope conflicts with retained PATHWAY classification evidence"
            )
        if any(
            batch.operation is not KeggOperation.LINK for batch in self.reference_link_provenance
        ):
            raise ValueError("pathway render targets require only LINK provenance")
        if any(
            batch.operation is not KeggOperation.GET for batch in self.reference_metadata_provenance
        ):
            raise ValueError("pathway render targets require only GET metadata provenance")
        if self.detected_ko_ids != tuple(sorted(set(self.detected_ko_ids))):
            raise ValueError("detected pathway K numbers must be sorted and unique")
        if self.coverage_numerator > self.coverage_denominator:
            raise ValueError("pathway coverage numerator cannot exceed its denominator")
        if self.detected_ko_ids_complete:
            if len(self.detected_ko_ids) != self.coverage_numerator:
                raise ValueError("complete detected evidence must match the coverage numerator")
        elif self.detected_ko_ids:
            raise ValueError("incomplete detected evidence must not contain a misleading prefix")
        if self.coverage_denominator == 0:
            if (
                self.evaluation_status is not PathwayCoverageStatus.NOT_EVALUABLE
                or self.coverage_ratio is not None
                or self.coverage_numerator != 0
            ):
                raise ValueError("zero-denominator pathway targets cannot report coverage")
        elif (
            self.evaluation_status is not PathwayCoverageStatus.EVALUATED
            or self.coverage_ratio != self.coverage_numerator / self.coverage_denominator
        ):
            raise ValueError("evaluated coverage must equal numerator divided by denominator")
        if self.renderability is RenderabilityStatus.RENDERABLE:
            if self.not_renderable_reason is not None:
                raise ValueError("renderable targets cannot have a non-renderable reason")
            if not self.detected_ko_ids_complete:
                raise ValueError("renderable pathway targets require complete detected evidence")
            if self.reference_namespace is not PathwayReferenceNamespace.KO:
                raise ValueError("version 7 renders only KO-reference pathway targets")
            if self.evaluation_status is not PathwayCoverageStatus.EVALUATED:
                raise ValueError("renderable pathway targets require an evaluated denominator")
        elif self.not_renderable_reason is None:
            raise ValueError("non-renderable pathway targets require a machine reason")
        return self


class RenderExecutionProvenance(FrozenModel):
    """Core analysis parameters and renderer-handoff builder identity."""

    analysis: AnalysisExecutionProvenance
    handoff_builder_name: Literal["kegg_render_handoff"]
    handoff_builder_version: Literal["6"]


class RenderInput(FrozenModel):
    """Complete, typed renderer input produced by the core analysis service."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:render-input:7",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    schema_version: Literal["7"]
    producer: RenderProducer
    dataset: RenderDataset
    decision_policy: DecisionPolicyReference
    evidence: VisualizationEvidence
    pathways: Annotated[tuple[PathwayRenderTarget, ...], Field(max_length=1_000)]
    execution: RenderExecutionProvenance
    limits: RenderInputLimits

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        if len(self.pathways) > self.limits.max_pathway_targets:
            raise ValueError("pathway targets exceed the recorded renderer limit")
        if len(self.evidence.accepted_ko_ids) > self.limits.max_evidence_ko_ids:
            raise ValueError("visualization evidence exceeds the recorded renderer limit")
        pathway_ids = tuple(item.pathway_id for item in self.pathways)
        if pathway_ids != tuple(sorted(set(pathway_ids))):
            raise ValueError("pathway targets must use sorted unique identifiers")
        accepted = set(self.evidence.accepted_ko_ids)
        pathway_parameters = self.execution.analysis.pathway_parameters
        for target in self.pathways:
            if len(target.detected_ko_ids) > self.limits.max_pathway_detected_ko_ids_per_target:
                raise ValueError("pathway detected evidence exceeds the recorded renderer limit")
            if not set(target.detected_ko_ids).issubset(accepted):
                raise ValueError("pathway detected evidence must use accepted K numbers")
            if (
                target.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
                and not pathway_parameters.allow_global_or_overview
            ):
                raise ValueError("global or overview targets require explicit execution opt-in")
        return self


def build_render_input(
    evidence: KoAnalysisView,
    pathway_references: tuple[PathwayKoReference, ...],
    execution: AnalysisExecutionProvenance,
    *,
    limits: RenderInputLimits | None = None,
) -> RenderInput:
    """Build one handoff from the canonical compact accepted-KO analysis view."""
    bounds = limits or RenderInputLimits()
    _validate_target_count(pathway_references, bounds)

    visualization_evidence = VisualizationEvidence(
        accepted_ko_ids=evidence.accepted_ko_ids,
        status_counts=evidence.status_counts,
    )
    evidence_ko_count = len(visualization_evidence.accepted_ko_ids)
    if evidence_ko_count > bounds.max_evidence_ko_ids:
        _fail_output_limit(
            "evidence_ko_ids",
            evidence_ko_count,
            "max_evidence_ko_ids",
            bounds.max_evidence_ko_ids,
        )

    reference_by_id = _unique_by_id(
        pathway_references, lambda item: item.pathway_id, "pathway reference"
    )
    pathway_targets = tuple(
        _pathway_target(
            evidence,
            reference_by_id[pathway_id],
            execution,
            visualization_evidence,
            bounds,
        )
        for pathway_id in sorted(reference_by_id)
    )

    sources = tuple(sorted(evidence.sources, key=lambda item: item.model_dump_json()))
    document = RenderInput(
        schema_version=RENDER_INPUT_SCHEMA_VERSION,
        producer=RenderProducer(name="kegg-mcp", version=__version__),
        dataset=RenderDataset(
            dataset_id=evidence.dataset_id,
            analysis_unit=evidence.analysis_unit,
            taxon_id=evidence.taxon_id,
            kegg_organism_code=evidence.kegg_organism_code,
            sources=sources,
        ),
        decision_policy=evidence.decision_policy,
        evidence=visualization_evidence,
        pathways=pathway_targets,
        execution=RenderExecutionProvenance(
            analysis=execution,
            handoff_builder_name=RENDER_INPUT_BUILDER_NAME,
            handoff_builder_version=RENDER_INPUT_BUILDER_VERSION,
        ),
        limits=bounds,
    )
    serialize_render_input(document)
    return document


def serialize_render_input(value: RenderInput) -> str:
    """Return canonical UTF-8 JSON and enforce the document's serialized-byte bound."""
    content = (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    byte_count = len(content.encode("utf-8"))
    if byte_count > value.limits.max_serialized_bytes:
        _fail_output_limit(
            "render_input_bytes",
            byte_count,
            "max_serialized_bytes",
            value.limits.max_serialized_bytes,
        )
    return content


def _pathway_target(
    analysis_evidence: KoAnalysisView,
    reference: PathwayKoReference,
    execution: AnalysisExecutionProvenance,
    visualization_evidence: VisualizationEvidence,
    bounds: RenderInputLimits,
) -> PathwayRenderTarget:
    pathway_parameters = execution.pathway_parameters
    result = evaluate_pathway_coverage(
        reference,
        analysis_evidence,
        PathwayCoverageParameters(
            reference_namespace=reference.reference_namespace,
            allow_global_or_overview=pathway_parameters.allow_global_or_overview,
        ),
        execution.pathway_coverage_limits,
    )
    selected = set(visualization_evidence.accepted_ko_ids)
    detected = tuple(sorted(set(reference.reference_kos).intersection(selected)))
    if len(detected) != result.detected_unique_ko_count:
        _fail_identity("pathway reference and coverage numerator do not match")

    detected_complete = len(detected) <= bounds.max_pathway_detected_ko_ids_per_target
    renderability: RenderabilityStatus = RenderabilityStatus.RENDERABLE
    reason: str | None = None
    if not detected_complete:
        renderability = RenderabilityStatus.NOT_RENDERABLE
        reason = "pathway_detected_ko_limit_exceeded"
    elif reference.reference_namespace is not PathwayReferenceNamespace.KO:
        renderability = RenderabilityStatus.SUMMARY_ONLY
        reason = "pathway_reference_namespace_unsupported"
    elif result.evaluation_status is not PathwayCoverageStatus.EVALUATED:
        renderability = RenderabilityStatus.SUMMARY_ONLY
        reason = "pathway_not_evaluable"
    return PathwayRenderTarget(
        pathway_id=result.pathway_id,
        pathway_name=result.pathway_name,
        pathway_class=result.pathway_class,
        reference_namespace=result.reference_namespace,
        reference_scope=result.reference_scope,
        evaluation_status=result.evaluation_status,
        coverage_numerator=result.detected_unique_ko_count,
        coverage_denominator=result.reference_unique_ko_count,
        coverage_ratio=result.coverage_ratio,
        detected_ko_ids_complete=detected_complete,
        detected_ko_ids=detected if detected_complete else (),
        renderability=renderability,
        not_renderable_reason=reason,
        reference_link_provenance=result.reference_link_provenance,
        reference_metadata_provenance=result.reference_metadata_provenance,
        calculation_method=result.calculation_method,
        calculation_version=result.calculation_version,
        warnings=result.warnings,
    )


def _validate_target_count(
    references: tuple[PathwayKoReference, ...],
    bounds: RenderInputLimits,
) -> None:
    if len(references) > bounds.max_pathway_targets:
        _fail_output_limit(
            "pathway_targets",
            len(references),
            "max_pathway_targets",
            bounds.max_pathway_targets,
        )


def _unique_by_id(
    values: tuple[_T, ...],
    identity: Callable[[_T], str],
    label: str,
) -> dict[str, _T]:
    result: dict[str, _T] = {}
    for value in values:
        key = identity(value)
        if key in result:
            _fail_identity(f"{label} identifiers must be unique")
        result[key] = value
    return result


def _fail_identity(message: str) -> NoReturn:
    fail(
        ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE,
        message,
        suggested_action="Rerun the core analysis with one aligned set of references and results.",
    )


def _fail_output_limit(
    metric: str,
    observed: int,
    limit_name: str,
    maximum: int,
) -> NoReturn:
    fail(
        ErrorCode.OUTPUT_LIMIT_EXCEEDED,
        "The renderer handoff exceeds an explicit output limit.",
        suggested_action="Request fewer targets or configure a larger bounded renderer limit.",
        safe_details=(
            SafeDetail(name="metric", value=metric),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit_name", value=limit_name),
            SafeDetail(name="limit", value=str(maximum)),
        ),
    )


__all__ = [
    "RENDER_INPUT_BUILDER_NAME",
    "RENDER_INPUT_BUILDER_VERSION",
    "RENDER_INPUT_MIME_TYPE",
    "RENDER_INPUT_SCHEMA_VERSION",
    "PathwayRenderTarget",
    "RenderDataset",
    "RenderExecutionProvenance",
    "RenderInput",
    "RenderInputLimits",
    "RenderProducer",
    "RenderabilityStatus",
    "VisualizationEvidence",
    "build_render_input",
    "serialize_render_input",
]
