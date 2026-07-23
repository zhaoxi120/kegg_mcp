"""Typed, bounded renderer handoff contracts independent of MCP transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, Self, TypeVar

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp import __version__
from kegg_mcp.analysis.contracts import (
    CalculationMethodReference,
    ModuleAnalysisLimits,
    ModuleBlockState,
    ModuleDefinitionAst,
    ModuleEvaluationResult,
    ModuleEvaluationStatus,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleReferenceEdge,
    ModuleReferenceIssue,
    ModuleWarning,
    ModuleWarningCode,
    OptionalComponentState,
    PairedModuleEvaluation,
    ResolvedModuleDefinition,
    ResolvedModuleGraph,
    SourceSpan,
)
from kegg_mcp.analysis.module_evaluation import evaluate_module_pair
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageResult,
    PathwayCoverageStatus,
    PathwayCoverageWarning,
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    evaluate_pathway_coverage,
)
from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnalysisUnit,
    AnnotationDataset,
    DecisionPolicyReference,
    EvidenceMode,
    FrozenModel,
    KNumber,
    ModuleId,
    NormalizedStatus,
    SourceProvenance,
    StatusCount,
    build_ko_evidence_view,
)
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError, SafeDetail, fail
from kegg_mcp.execution import AnalysisExecutionProvenance
from kegg_mcp.kegg.contracts import KeggBatchProvenance

RENDER_INPUT_SCHEMA_VERSION = "3"
RENDER_INPUT_MIME_TYPE = "application/vnd.kegg-mcp.render-input+json;version=3"
RENDER_INPUT_BUILDER_NAME = "kegg_render_handoff"
RENDER_INPUT_BUILDER_VERSION = "1"
MODULE_RENDER_MAX_CANVAS_DIMENSION = 20_000
MODULE_RENDER_MAX_CANVAS_PIXELS = 20_000_000
MODULE_RENDER_MAX_SVG_NODES = 4_096

PathwayId = Annotated[str, Field(pattern=r"^(?:ko|map|[a-z][a-z0-9]{1,7})[0-9]{5}$")]
MachineReason = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
_T = TypeVar("_T")


class RenderabilityStatus(StrEnum):
    """Whether the first renderer release can produce a complete graphic."""

    RENDERABLE = "renderable"
    SUMMARY_ONLY = "summary_only"
    NOT_RENDERABLE = "not_renderable"


def module_scene_layout(
    *,
    node_count: int,
    max_depth: int,
    required_block_count: int,
    optional_component_count: int,
    reference_edge_count: int,
) -> tuple[int, int, int]:
    """Return the renderer's normative width, height, and SVG-node estimate."""
    maximum_node_x = 50 + max_depth * 220
    has_panels = bool(required_block_count or optional_component_count or reference_edge_count)
    panel_height = (
        required_block_count * 34
        + optional_component_count * 28
        + reference_edge_count * 28
        + 34 * int(bool(optional_component_count))
        + 34 * int(bool(reference_edge_count))
    )
    width = max(900, maximum_node_x + (950 if has_panels else 260))
    height = max(620, 360 + max(node_count * 58, panel_height))
    svg_nodes = (
        24
        + node_count * 4
        + required_block_count * 4
        + optional_component_count * 2
        + reference_edge_count * 2
    )
    return width, height, svg_nodes


def module_scene_fits_renderer(
    *,
    node_count: int,
    max_depth: int,
    required_block_count: int,
    optional_component_count: int,
    reference_edge_count: int,
) -> bool:
    """Return whether a complete MODULE scene fits every normative renderer bound."""
    width, height, svg_nodes = module_scene_layout(
        node_count=node_count,
        max_depth=max_depth,
        required_block_count=required_block_count,
        optional_component_count=optional_component_count,
        reference_edge_count=reference_edge_count,
    )
    return (
        width <= MODULE_RENDER_MAX_CANVAS_DIMENSION
        and height <= MODULE_RENDER_MAX_CANVAS_DIMENSION
        and width * height <= MODULE_RENDER_MAX_CANVAS_PIXELS
        and svg_nodes <= MODULE_RENDER_MAX_SVG_NODES
    )


class RenderInputLimits(FrozenModel):
    """Serialized renderer-handoff bounds recorded with every version 3 document."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:render-input-limits:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    max_evidence_ko_ids: int = Field(default=100_000, strict=True, gt=0, le=1_000_000)
    max_module_targets: int = Field(default=100, strict=True, ge=0, le=1_000)
    max_pathway_targets: int = Field(default=25, strict=True, ge=0, le=1_000)
    max_total_targets: int = Field(default=125, strict=True, ge=0, le=2_000)
    max_module_definitions_per_target: int = Field(default=256, strict=True, gt=0, le=10_000)
    max_module_ast_nodes_per_target: int = Field(default=50_000, strict=True, gt=0, le=1_000_000)
    max_module_required_blocks_per_target: int = Field(default=1_000, strict=True, gt=0, le=10_000)
    max_module_optional_components_per_target: int = Field(
        default=1_000, strict=True, gt=0, le=10_000
    )
    max_module_uncertain_support_per_target: int = Field(
        default=10_000, strict=True, gt=0, le=10_000
    )
    max_pathway_detected_ko_ids_per_target: int = Field(
        default=100_000, strict=True, ge=0, le=1_000_000
    )
    max_serialized_bytes: int = Field(default=50_000_000, strict=True, gt=0, le=100_000_000)

    @model_validator(mode="after")
    def validate_target_capacity(self) -> Self:
        if self.max_module_targets + self.max_pathway_targets > self.max_total_targets:
            raise ValueError("per-type target limits must fit within max_total_targets")
        return self


class RenderProducer(FrozenModel):
    """Core producer identity for one renderer handoff."""

    name: Literal["kegg-mcp"] = "kegg-mcp"
    version: str = Field(min_length=1, max_length=64)


class RenderDataset(FrozenModel):
    """Dataset identity, biological context, and annotation-source provenance."""

    dataset_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    analysis_unit: AnalysisUnit
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(pattern=r"^[a-z][a-z0-9]{1,7}$")
    sources: Annotated[tuple[SourceProvenance, ...], Field(min_length=1, max_length=128)]


class VisualizationEvidence(FrozenModel):
    """Only accepted and policy-defined uncertain K numbers eligible for coloring."""

    accepted_ko_ids: Annotated[tuple[KNumber, ...], Field(max_length=1_000_000)]
    uncertain_ko_ids: Annotated[tuple[KNumber, ...], Field(max_length=1_000_000)]
    accepted_count: NonNegativeCount
    uncertain_count: NonNegativeCount
    status_counts: Annotated[
        tuple[StatusCount, ...],
        Field(min_length=len(NormalizedStatus), max_length=len(NormalizedStatus)),
    ]

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        for name, values, count in (
            ("accepted_ko_ids", self.accepted_ko_ids, self.accepted_count),
            ("uncertain_ko_ids", self.uncertain_ko_ids, self.uncertain_count),
        ):
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
            if count != len(values):
                raise ValueError(f"{name} count is inconsistent")
        statuses = tuple(item.status for item in self.status_counts)
        if statuses != tuple(NormalizedStatus):
            raise ValueError("status_counts must use canonical normalized-status order")
        if not set(self.accepted_ko_ids).isdisjoint(self.uncertain_ko_ids):
            raise ValueError("accepted and uncertain visualization evidence must be disjoint")
        return self


class ModuleCompletionRenderResult(FrozenModel):
    """Exact completion and project block coverage without preview fields."""

    evidence_mode: EvidenceMode
    evidence_ko_count: NonNegativeCount
    evaluation_status: ModuleEvaluationStatus
    is_complete: bool | None
    block_coverage: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    completed_required_blocks: NonNegativeCount
    evaluable_required_blocks: NonNegativeCount
    required_block_count: NonNegativeCount
    calculation_method: CalculationMethodReference
    warnings: Annotated[tuple[ModuleWarning, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        if not (
            0
            <= self.completed_required_blocks
            <= self.evaluable_required_blocks
            <= self.required_block_count
        ):
            raise ValueError("required-block counts are inconsistent")
        expected_complete = {
            ModuleEvaluationStatus.COMPLETE: True,
            ModuleEvaluationStatus.INCOMPLETE: False,
            ModuleEvaluationStatus.PARTIALLY_EVALUABLE: None,
            ModuleEvaluationStatus.NOT_EVALUABLE: None,
        }[self.evaluation_status]
        if self.is_complete is not expected_complete:
            raise ValueError("is_complete is inconsistent with evaluation_status")
        fully_evaluable = (
            self.required_block_count > 0
            and self.evaluable_required_blocks == self.required_block_count
        )
        if self.evaluation_status in {
            ModuleEvaluationStatus.COMPLETE,
            ModuleEvaluationStatus.INCOMPLETE,
        }:
            if not fully_evaluable:
                raise ValueError(
                    "complete or incomplete status requires every block to be evaluable"
                )
        elif self.evaluation_status is ModuleEvaluationStatus.PARTIALLY_EVALUABLE:
            if not 0 < self.evaluable_required_blocks < self.required_block_count:
                raise ValueError("partial status requires evaluable and not-evaluable blocks")
        elif self.evaluable_required_blocks != 0:
            raise ValueError("not-evaluable status cannot contain evaluable required blocks")
        if fully_evaluable:
            if self.block_coverage != self.completed_required_blocks / self.required_block_count:
                raise ValueError("block_coverage must use every required top-level block")
        elif self.block_coverage is not None:
            raise ValueError("partial or zero-block results cannot report block coverage")
        return self


class ModuleRequiredBlockRenderState(FrozenModel):
    """Complete strict and lenient state for one required root top-level block."""

    block_index: int = Field(strict=True, gt=0)
    source_span: SourceSpan
    strict_state: ModuleBlockState
    lenient_state: ModuleBlockState
    uncertain_support_ko_ids: Annotated[tuple[KNumber, ...], Field(max_length=10_000)] = ()

    @model_validator(mode="after")
    def validate_support(self) -> Self:
        if self.uncertain_support_ko_ids != tuple(sorted(set(self.uncertain_support_ko_ids))):
            raise ValueError("uncertain block support must be sorted and unique")
        if self.uncertain_support_ko_ids and not (
            self.strict_state is not ModuleBlockState.COMPLETE
            and self.lenient_state is ModuleBlockState.COMPLETE
        ):
            raise ValueError("uncertain block support must explain a lenient completion change")
        return self


class ModuleOptionalComponentRenderState(FrozenModel):
    """Complete strict and lenient presence state for one optional expression."""

    component_index: int = Field(strict=True, gt=0)
    source_module_id: ModuleId
    source_span: SourceSpan
    strict_state: OptionalComponentState
    lenient_state: OptionalComponentState


class ModuleUncertainRenderSupport(FrozenModel):
    """Policy-defined uncertain K-number support that changes required blocks."""

    ko_id: KNumber
    required_block_indexes: Annotated[
        tuple[Annotated[int, Field(strict=True, gt=0)], ...], Field(min_length=1, max_length=10_000)
    ]

    @model_validator(mode="after")
    def validate_indexes(self) -> Self:
        if self.required_block_indexes != tuple(sorted(set(self.required_block_indexes))):
            raise ValueError("uncertain-support block indexes must be sorted and unique")
        return self


class ModuleRenderTarget(FrozenModel):
    """Complete-within-limit renderer target for one resolved KEGG MODULE graph."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:module-render-target:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    module_id: ModuleId
    module_name: str | None = Field(default=None, max_length=1_000)
    renderability: RenderabilityStatus
    not_renderable_reason: MachineReason | None = None
    definition_graph_complete: bool
    definitions: Annotated[tuple[ResolvedModuleDefinition, ...], Field(max_length=10_000)]
    reference_edges: Annotated[tuple[ModuleReferenceEdge, ...], Field(max_length=100_000)]
    reference_issues: Annotated[tuple[ModuleReferenceIssue, ...], Field(max_length=100_000)]
    strict: ModuleCompletionRenderResult
    lenient: ModuleCompletionRenderResult
    required_block_states_complete: bool
    required_block_states: Annotated[
        tuple[ModuleRequiredBlockRenderState, ...], Field(max_length=10_000)
    ]
    optional_component_states_complete: bool
    optional_component_states: Annotated[
        tuple[ModuleOptionalComponentRenderState, ...], Field(max_length=10_000)
    ]
    uncertain_support: Annotated[tuple[ModuleUncertainRenderSupport, ...], Field(max_length=10_000)]
    parser_name: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    parser_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)
    resolver_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)
    reference_retrieval_provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=5_000)
    ]
    warnings: Annotated[tuple[ModuleWarning, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.strict.evidence_mode is not EvidenceMode.STRICT:
            raise ValueError("strict summary must use strict evidence")
        if self.lenient.evidence_mode is not EvidenceMode.LENIENT:
            raise ValueError("lenient summary must use lenient evidence")
        if self.strict.required_block_count != self.lenient.required_block_count:
            raise ValueError("strict and lenient summaries must share the block denominator")
        definition_ids = tuple(item.definition.module_id for item in self.definitions)
        if definition_ids != tuple(sorted(set(definition_ids))):
            raise ValueError("MODULE definitions must use sorted unique identifiers")
        if self.definition_graph_complete and self.module_id not in definition_ids:
            raise ValueError("a complete definition graph must include the root MODULE")
        if not self.definition_graph_complete and self.definitions:
            raise ValueError("an incomplete definition graph must not contain a partial graph")
        edge_keys = tuple(_edge_sort_key(item) for item in self.reference_edges)
        if edge_keys != tuple(sorted(set(edge_keys))):
            raise ValueError("MODULE reference edges must use deterministic unique order")
        issue_keys = tuple(_issue_sort_key(item) for item in self.reference_issues)
        if issue_keys != tuple(sorted(set(issue_keys))):
            raise ValueError("MODULE reference issues must use deterministic unique order")
        if not self.definition_graph_complete and (self.reference_edges or self.reference_issues):
            raise ValueError("an incomplete definition graph must not contain partial graph detail")

        block_indexes = tuple(item.block_index for item in self.required_block_states)
        expected_indexes = tuple(range(1, self.strict.required_block_count + 1))
        if self.required_block_states_complete:
            if block_indexes != expected_indexes:
                raise ValueError("complete block states must cover every block in order")
            for summary, field_name in (
                (self.strict, "strict_state"),
                (self.lenient, "lenient_state"),
            ):
                states = tuple(getattr(item, field_name) for item in self.required_block_states)
                completed = sum(state is ModuleBlockState.COMPLETE for state in states)
                evaluable = sum(state is not ModuleBlockState.NOT_EVALUABLE for state in states)
                if (
                    completed != summary.completed_required_blocks
                    or evaluable != summary.evaluable_required_blocks
                ):
                    raise ValueError("complete block states must match completion summaries")
        elif self.required_block_states:
            raise ValueError("incomplete block states must not contain a misleading prefix")
        optional_indexes = tuple(item.component_index for item in self.optional_component_states)
        if self.optional_component_states_complete:
            if optional_indexes != tuple(range(1, len(optional_indexes) + 1)):
                raise ValueError("optional component states must use contiguous output order")
        elif self.optional_component_states:
            raise ValueError("incomplete optional states must not contain a misleading prefix")

        support_ids = tuple(item.ko_id for item in self.uncertain_support)
        if support_ids != tuple(sorted(set(support_ids))):
            raise ValueError("uncertain support must use sorted unique K numbers")
        if not self.required_block_states_complete and self.uncertain_support:
            raise ValueError("uncertain support requires a complete required-block vector")
        support_by_block: dict[int, set[str]] = {}
        for item in self.uncertain_support:
            for index in item.required_block_indexes:
                support_by_block.setdefault(index, set()).add(item.ko_id)
        for block in self.required_block_states:
            if block.uncertain_support_ko_ids != tuple(
                sorted(support_by_block.get(block.block_index, set()))
            ):
                raise ValueError("block support and target uncertain support are inconsistent")

        if self.renderability is RenderabilityStatus.RENDERABLE:
            if self.not_renderable_reason is not None:
                raise ValueError("renderable targets cannot have a non-renderable reason")
            if not (
                self.definition_graph_complete
                and self.required_block_states_complete
                and self.optional_component_states_complete
            ):
                raise ValueError("renderable MODULE targets require complete renderer data")
            if not _module_render_target_fits(self):
                raise ValueError("renderable MODULE target exceeds the normative renderer layout")
        elif self.not_renderable_reason is None:
            raise ValueError("non-renderable MODULE targets require a machine reason")
        return self


class PathwayRenderTarget(FrozenModel):
    """Complete-within-limit renderer target for one descriptive pathway result."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-render-target:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    pathway_id: PathwayId
    pathway_number: str = Field(pattern=r"^[0-9]{5}$")
    pathway_name: str = Field(min_length=1, max_length=1_000)
    pathway_class: Annotated[tuple[str, ...], Field(min_length=1, max_length=32)]
    reference_namespace: PathwayReferenceNamespace
    reference_scope: PathwayReferenceScope
    evidence_mode: EvidenceMode
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
    calculation_method: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)
    calculation_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)*$", max_length=32)
    warnings: Annotated[tuple[PathwayCoverageWarning, ...], Field(max_length=16)]

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.pathway_id[-5:] != self.pathway_number:
            raise ValueError("pathway_number must match pathway_id")
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
            if self.reference_scope is not PathwayReferenceScope.STANDARD:
                raise ValueError("global or overview pathways are not renderable in version 3")
            if self.reference_namespace is not PathwayReferenceNamespace.KO:
                raise ValueError("version 3 renders only KO-reference pathway targets")
            if self.evaluation_status is not PathwayCoverageStatus.EVALUATED:
                raise ValueError("renderable pathway targets require an evaluated denominator")
        elif self.not_renderable_reason is None:
            raise ValueError("non-renderable pathway targets require a machine reason")
        return self


class RenderExecutionProvenance(FrozenModel):
    """Core analysis parameters and renderer-handoff builder identity."""

    analysis: AnalysisExecutionProvenance
    handoff_builder_name: Literal["kegg_render_handoff"] = RENDER_INPUT_BUILDER_NAME
    handoff_builder_version: Literal["1"] = RENDER_INPUT_BUILDER_VERSION


class RenderInput(FrozenModel):
    """Complete, typed renderer input produced by the core analysis service."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:render-input:3",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    schema_version: Literal["3"] = RENDER_INPUT_SCHEMA_VERSION
    producer: RenderProducer
    dataset: RenderDataset
    decision_policy: DecisionPolicyReference
    evidence: VisualizationEvidence
    modules: Annotated[tuple[ModuleRenderTarget, ...], Field(max_length=1_000)]
    pathways: Annotated[tuple[PathwayRenderTarget, ...], Field(max_length=1_000)]
    execution: RenderExecutionProvenance
    limits: RenderInputLimits

    @model_validator(mode="after")
    def validate_document(self) -> Self:
        if len(self.modules) > self.limits.max_module_targets:
            raise ValueError("MODULE targets exceed the recorded renderer limit")
        if len(self.pathways) > self.limits.max_pathway_targets:
            raise ValueError("pathway targets exceed the recorded renderer limit")
        if len(self.modules) + len(self.pathways) > self.limits.max_total_targets:
            raise ValueError("renderer targets exceed the recorded total limit")
        if (
            self.evidence.accepted_count + self.evidence.uncertain_count
            > self.limits.max_evidence_ko_ids
        ):
            raise ValueError("visualization evidence exceeds the recorded renderer limit")
        module_ids = tuple(item.module_id for item in self.modules)
        pathway_ids = tuple(item.pathway_id for item in self.pathways)
        if module_ids != tuple(sorted(set(module_ids))):
            raise ValueError("MODULE targets must use sorted unique identifiers")
        if pathway_ids != tuple(sorted(set(pathway_ids))):
            raise ValueError("pathway targets must use sorted unique identifiers")
        accepted = set(self.evidence.accepted_ko_ids)
        uncertain = set(self.evidence.uncertain_ko_ids)
        for target in self.modules:
            if len(target.definitions) > self.limits.max_module_definitions_per_target:
                raise ValueError("MODULE definitions exceed the recorded renderer limit")
            if (
                sum(item.parse_result.ast_node_count for item in target.definitions)
                > self.limits.max_module_ast_nodes_per_target
            ):
                raise ValueError("MODULE AST nodes exceed the recorded renderer limit")
            if (
                len(target.required_block_states)
                > self.limits.max_module_required_blocks_per_target
            ):
                raise ValueError("MODULE required blocks exceed the recorded renderer limit")
            if (
                len(target.optional_component_states)
                > self.limits.max_module_optional_components_per_target
            ):
                raise ValueError("MODULE optional components exceed the recorded renderer limit")
            if len(target.uncertain_support) > self.limits.max_module_uncertain_support_per_target:
                raise ValueError("MODULE uncertain support exceeds the recorded renderer limit")
            if any(item.ko_id not in uncertain for item in target.uncertain_support):
                raise ValueError(
                    "MODULE uncertain support must use uncertain visualization evidence"
                )
        for target in self.pathways:
            if len(target.detected_ko_ids) > self.limits.max_pathway_detected_ko_ids_per_target:
                raise ValueError("pathway detected evidence exceeds the recorded renderer limit")
            selected = (
                accepted if target.evidence_mode is EvidenceMode.STRICT else accepted | uncertain
            )
            if not set(target.detected_ko_ids).issubset(selected):
                raise ValueError("pathway detected evidence must use the selected evidence mode")
        return self


def build_render_input(
    dataset: AnnotationDataset,
    module_graphs: tuple[ResolvedModuleGraph, ...],
    module_results: tuple[PairedModuleEvaluation, ...],
    pathway_references: tuple[PathwayKoReference, ...],
    pathway_results: tuple[PathwayCoverageResult, ...],
    execution: AnalysisExecutionProvenance,
    *,
    limits: RenderInputLimits | None = None,
) -> RenderInput:
    """Build and identity-check one complete renderer handoff from core analysis values."""
    bounds = limits or RenderInputLimits()
    pairs = module_results
    _validate_target_counts(module_graphs, pairs, pathway_references, pathway_results, bounds)
    _validate_execution_identity(pairs, pathway_results, execution)

    evidence_view = build_ko_evidence_view(dataset)
    evidence = VisualizationEvidence(
        accepted_ko_ids=evidence_view.accepted_kos,
        uncertain_ko_ids=evidence_view.uncertain_kos,
        accepted_count=len(evidence_view.accepted_kos),
        uncertain_count=len(evidence_view.uncertain_kos),
        status_counts=evidence_view.status_counts,
    )
    if evidence.accepted_count + evidence.uncertain_count > bounds.max_evidence_ko_ids:
        _fail_output_limit(
            "evidence_ko_ids",
            evidence.accepted_count + evidence.uncertain_count,
            "max_evidence_ko_ids",
            bounds.max_evidence_ko_ids,
        )

    graph_by_id = _unique_by_id(module_graphs, lambda item: item.root_module_id, "MODULE graph")
    pair_by_id = _unique_by_id(pairs, lambda item: item.strict.module_id, "MODULE result")
    if set(graph_by_id) != set(pair_by_id):
        _fail_identity("MODULE graph and result target identifiers do not match")
    module_targets = tuple(
        _module_target(dataset, graph_by_id[module_id], pair_by_id[module_id], bounds)
        for module_id in sorted(graph_by_id)
    )

    reference_by_id = _unique_by_id(
        pathway_references, lambda item: item.pathway_id, "pathway reference"
    )
    result_by_id = _unique_by_id(pathway_results, lambda item: item.pathway_id, "pathway result")
    if set(reference_by_id) != set(result_by_id):
        _fail_identity("pathway reference and result target identifiers do not match")
    pathway_targets = tuple(
        _pathway_target(
            dataset,
            reference_by_id[pathway_id],
            result_by_id[pathway_id],
            evidence,
            bounds,
        )
        for pathway_id in sorted(reference_by_id)
    )

    sources = tuple(sorted(dataset.sources, key=lambda item: item.model_dump_json()))
    document = RenderInput(
        producer=RenderProducer(version=__version__),
        dataset=RenderDataset(
            dataset_id=dataset.dataset_id,
            analysis_unit=dataset.analysis_unit,
            taxon_id=dataset.taxon_id,
            kegg_organism_code=dataset.kegg_organism_code,
            sources=sources,
        ),
        decision_policy=dataset.import_report.decision_policy,
        evidence=evidence,
        modules=module_targets,
        pathways=pathway_targets,
        execution=RenderExecutionProvenance(analysis=execution),
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


def parse_render_input_json(payload: str | bytes) -> RenderInput:
    """Strictly validate a version 3 handoff with pre- and post-parse byte bounds."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    if len(raw) > 100_000_000:
        _fail_output_limit("render_input_bytes", len(raw), "hard_max_serialized_bytes", 100_000_000)
    value = RenderInput.model_validate_json(raw, strict=True)
    if len(raw) > value.limits.max_serialized_bytes:
        _fail_output_limit(
            "render_input_bytes",
            len(raw),
            "max_serialized_bytes",
            value.limits.max_serialized_bytes,
        )
    serialize_render_input(value)
    return value


def _module_render_target_fits(target: ModuleRenderTarget) -> bool:
    return _module_render_payload_fits(
        target.definitions,
        required_block_count=len(target.required_block_states),
        optional_component_count=len(target.optional_component_states),
        reference_edge_count=len(target.reference_edges),
    )


def _module_render_payload_fits(
    definitions: tuple[ResolvedModuleDefinition, ...],
    *,
    required_block_count: int,
    optional_component_count: int,
    reference_edge_count: int,
) -> bool:
    node_count, max_depth = _module_definition_scene_metrics(definitions)
    return module_scene_fits_renderer(
        node_count=node_count,
        max_depth=max_depth,
        required_block_count=required_block_count,
        optional_component_count=optional_component_count,
        reference_edge_count=reference_edge_count,
    )


def _module_definition_scene_metrics(
    definitions: tuple[ResolvedModuleDefinition, ...],
) -> tuple[int, int]:
    node_count = 0
    max_depth = 0
    for definition in definitions:
        node_count += 1
        parse_result = definition.parse_result
        ast = parse_result.ast
        if ast is None:
            node_count += 1
            max_depth = max(max_depth, 1)
            continue
        node_count += parse_result.ast_node_count
        pending: list[tuple[ModuleExpression, int]] = [(block, 1) for block in ast.required_blocks]
        while pending:
            expression, depth = pending.pop()
            max_depth = max(max_depth, depth)
            pending.extend((child, depth + 1) for child in expression.children)
    return node_count, max_depth


def _module_target(
    dataset: AnnotationDataset,
    graph: ResolvedModuleGraph,
    pair: PairedModuleEvaluation,
    bounds: RenderInputLimits,
) -> ModuleRenderTarget:
    _validate_module_identity(dataset, graph, pair)
    _validate_original_module_evaluation(dataset, graph, pair)
    root = next(item for item in graph.modules if item.definition.module_id == graph.root_module_id)
    definitions_fit = (
        len(graph.modules) <= bounds.max_module_definitions_per_target
        and graph.total_ast_nodes <= bounds.max_module_ast_nodes_per_target
    )
    definitions = (
        tuple(sorted(graph.modules, key=lambda item: item.definition.module_id))
        if definitions_fit
        else ()
    )
    edges = tuple(sorted(graph.edges, key=_edge_sort_key)) if definitions_fit else ()
    issues = tuple(sorted(graph.issues, key=_issue_sort_key)) if definitions_fit else ()
    strict_summary = _completion_summary(pair.strict)
    lenient_summary = _completion_summary(pair.lenient)

    renderability: RenderabilityStatus = RenderabilityStatus.RENDERABLE
    reason: str | None = None
    blocks_complete = False
    optional_complete = False
    block_states: tuple[ModuleRequiredBlockRenderState, ...] = ()
    optional_states: tuple[ModuleOptionalComponentRenderState, ...] = ()
    support: tuple[ModuleUncertainRenderSupport, ...] = ()
    ast = root.parse_result.ast
    optional_count = _optional_expression_count(graph)
    if not definitions_fit:
        renderability = RenderabilityStatus.NOT_RENDERABLE
        reason = (
            "module_definition_limit_exceeded"
            if len(graph.modules) > bounds.max_module_definitions_per_target
            else "module_ast_node_limit_exceeded"
        )
    elif ast is None or not root.parse_result.is_valid:
        renderability = RenderabilityStatus.SUMMARY_ONLY
        reason = "module_definition_not_evaluable"
    elif len(ast.required_blocks) > bounds.max_module_required_blocks_per_target:
        renderability = RenderabilityStatus.NOT_RENDERABLE
        reason = "module_required_block_limit_exceeded"
    elif optional_count > bounds.max_module_optional_components_per_target:
        renderability = RenderabilityStatus.NOT_RENDERABLE
        reason = "module_optional_component_limit_exceeded"
    else:
        complete_pair = _complete_render_pair(graph, dataset, pair.strict.limits, bounds)
        _validate_module_semantics(pair, complete_pair)
        block_states, support, support_complete = _required_block_states(ast, complete_pair)
        optional_states, optional_complete = _optional_states(complete_pair, optional_count)
        blocks_complete = len(block_states) == pair.strict.required_block_count
        if not support_complete:
            renderability = RenderabilityStatus.NOT_RENDERABLE
            reason = "module_uncertain_support_limit_exceeded"
            block_states = ()
            blocks_complete = False
            support = ()
        elif not optional_complete:
            renderability = RenderabilityStatus.NOT_RENDERABLE
            reason = "module_optional_component_limit_exceeded"
            optional_states = ()
        elif not _module_render_payload_fits(
            definitions,
            required_block_count=len(block_states),
            optional_component_count=len(optional_states),
            reference_edge_count=len(edges),
        ):
            renderability = RenderabilityStatus.NOT_RENDERABLE
            reason = "module_renderer_layout_limit_exceeded"
            block_states = ()
            blocks_complete = False
            optional_states = ()
            optional_complete = False
            support = ()

    warnings_by_code: dict[ModuleWarningCode, ModuleWarning] = {
        warning.code: warning for warning in (*pair.strict.warnings, *pair.lenient.warnings)
    }
    warnings = tuple(
        warnings_by_code[code] for code in sorted(warnings_by_code, key=lambda item: item.value)
    )
    return ModuleRenderTarget(
        module_id=graph.root_module_id,
        module_name=pair.strict.module_name,
        renderability=renderability,
        not_renderable_reason=reason,
        definition_graph_complete=definitions_fit,
        definitions=definitions,
        reference_edges=edges,
        reference_issues=issues,
        strict=strict_summary,
        lenient=lenient_summary,
        required_block_states_complete=blocks_complete,
        required_block_states=block_states,
        optional_component_states_complete=optional_complete,
        optional_component_states=optional_states,
        uncertain_support=support,
        parser_name=root.parse_result.parser_name,
        parser_version=root.parse_result.parser_version,
        resolver_version=graph.resolver_version,
        reference_retrieval_provenance=graph.retrieval_provenance,
        warnings=warnings,
    )


def _complete_render_pair(
    graph: ResolvedModuleGraph,
    dataset: AnnotationDataset,
    original_limits: ModuleAnalysisLimits,
    bounds: RenderInputLimits,
) -> PairedModuleEvaluation:
    evaluation = original_limits.evaluation.model_copy(
        update={
            "max_block_previews": bounds.max_module_required_blocks_per_target,
            "max_optional_components": bounds.max_module_optional_components_per_target,
            "max_uncertain_support_items": bounds.max_module_uncertain_support_per_target,
        }
    )
    limits = ModuleAnalysisLimits(
        parsing=original_limits.parsing,
        resolution=original_limits.resolution,
        evaluation=evaluation,
    )
    return evaluate_module_pair(graph, dataset, limits)


def _required_block_states(
    ast: ModuleDefinitionAst,
    pair: PairedModuleEvaluation,
) -> tuple[
    tuple[ModuleRequiredBlockRenderState, ...],
    tuple[ModuleUncertainRenderSupport, ...],
    bool,
]:
    strict_states = _block_state_map(pair.strict)
    lenient_states = _block_state_map(pair.lenient)
    support_by_ko: dict[str, tuple[int, ...]] = {}
    complete = True
    for item in pair.lenient.uncertain_support:
        if item.required_block_indexes_truncated:
            complete = False
            continue
        support_by_ko[item.ko_id] = item.required_block_indexes
    changed = {
        index
        for index in range(1, pair.strict.required_block_count + 1)
        if strict_states[index] is not ModuleBlockState.COMPLETE
        and lenient_states[index] is ModuleBlockState.COMPLETE
    }
    support_by_block: dict[int, set[str]] = {index: set() for index in changed}
    for ko_id, indexes in support_by_ko.items():
        for index in indexes:
            if index in support_by_block:
                support_by_block[index].add(ko_id)
    if any(not values for values in support_by_block.values()):
        complete = False
    support = tuple(
        ModuleUncertainRenderSupport(ko_id=ko_id, required_block_indexes=indexes)
        for ko_id, indexes in sorted(support_by_ko.items())
        if any(index in changed for index in indexes)
    )
    states = tuple(
        ModuleRequiredBlockRenderState(
            block_index=index,
            source_span=ast.required_blocks[index - 1].span,
            strict_state=strict_states[index],
            lenient_state=lenient_states[index],
            uncertain_support_ko_ids=tuple(sorted(support_by_block.get(index, set()))),
        )
        for index in range(1, pair.strict.required_block_count + 1)
    )
    return states, support, complete


def _block_state_map(result: ModuleEvaluationResult) -> dict[int, ModuleBlockState]:
    states = {index: ModuleBlockState.COMPLETE for index in result.present_blocks_preview}
    states.update({item.block_index: item.state for item in result.missing_blocks_preview})
    states.update({item.block_index: item.state for item in result.not_evaluable_blocks_preview})
    expected = set(range(1, result.required_block_count + 1))
    if set(states) != expected:
        _fail_identity("complete renderer block evaluation did not retain every required block")
    return states


def _optional_states(
    pair: PairedModuleEvaluation,
    expected_count: int,
) -> tuple[tuple[ModuleOptionalComponentRenderState, ...], bool]:
    strict = pair.strict.optional_components
    lenient = pair.lenient.optional_components
    if len(strict) != expected_count or len(lenient) != expected_count:
        return (), False
    values: list[ModuleOptionalComponentRenderState] = []
    for strict_item, lenient_item in zip(strict, lenient, strict=True):
        identity = ("component_index", "source_module_id", "source_span")
        if any(getattr(strict_item, name) != getattr(lenient_item, name) for name in identity):
            _fail_identity("strict and lenient optional-component identities do not match")
        values.append(
            ModuleOptionalComponentRenderState(
                component_index=strict_item.component_index,
                source_module_id=strict_item.source_module_id,
                source_span=strict_item.source_span,
                strict_state=strict_item.state,
                lenient_state=lenient_item.state,
            )
        )
    return tuple(values), True


def _pathway_target(
    dataset: AnnotationDataset,
    reference: PathwayKoReference,
    result: PathwayCoverageResult,
    evidence: VisualizationEvidence,
    bounds: RenderInputLimits,
) -> PathwayRenderTarget:
    _validate_pathway_identity(dataset, reference, result)
    try:
        recomputed = evaluate_pathway_coverage(
            reference,
            dataset,
            result.parameters,
            result.limits,
        )
    except KeggMcpError:
        _fail_identity(
            "The pathway result cannot be recalculated from its reference, dataset, parameters, "
            "and limits"
        )
    if recomputed != result:
        _fail_identity(
            "The pathway result does not match a deterministic recalculation from its reference, "
            "dataset, parameters, and limits"
        )
    selected = set(evidence.accepted_ko_ids)
    if result.evidence_mode is EvidenceMode.LENIENT:
        selected.update(evidence.uncertain_ko_ids)
    detected = tuple(sorted(set(reference.reference_kos).intersection(selected)))
    if len(detected) != result.detected_unique_ko_count:
        _fail_identity("pathway reference and coverage numerator do not match")

    detected_complete = len(detected) <= bounds.max_pathway_detected_ko_ids_per_target
    renderability: RenderabilityStatus = RenderabilityStatus.RENDERABLE
    reason: str | None = None
    if not detected_complete:
        renderability = RenderabilityStatus.NOT_RENDERABLE
        reason = "pathway_detected_ko_limit_exceeded"
    elif reference.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW:
        renderability = RenderabilityStatus.SUMMARY_ONLY
        reason = "global_or_overview_pathway_unsupported"
    elif reference.reference_namespace is not PathwayReferenceNamespace.KO:
        renderability = RenderabilityStatus.SUMMARY_ONLY
        reason = "pathway_reference_namespace_unsupported"
    elif result.evaluation_status is not PathwayCoverageStatus.EVALUATED:
        renderability = RenderabilityStatus.SUMMARY_ONLY
        reason = "pathway_not_evaluable"
    return PathwayRenderTarget(
        pathway_id=result.pathway_id,
        pathway_number=result.pathway_id[-5:],
        pathway_name=result.pathway_name,
        pathway_class=result.pathway_class,
        reference_namespace=result.reference_namespace,
        reference_scope=result.reference_scope,
        evidence_mode=result.evidence_mode,
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


def _completion_summary(result: ModuleEvaluationResult) -> ModuleCompletionRenderResult:
    return ModuleCompletionRenderResult(
        evidence_mode=result.evidence_mode,
        evidence_ko_count=result.evidence_ko_count,
        evaluation_status=result.evaluation_status,
        is_complete=result.is_complete,
        block_coverage=result.block_coverage,
        completed_required_blocks=result.completed_required_blocks,
        evaluable_required_blocks=result.evaluable_required_blocks,
        required_block_count=result.required_block_count,
        calculation_method=result.calculation_method,
        warnings=result.warnings,
    )


def _validate_module_identity(
    dataset: AnnotationDataset,
    graph: ResolvedModuleGraph,
    pair: PairedModuleEvaluation,
) -> None:
    if graph.root_module_id != pair.strict.module_id:
        _fail_identity("MODULE graph root does not match its evaluation result")
    root = next(item for item in graph.modules if item.definition.module_id == graph.root_module_id)
    if root.definition.module_name != pair.strict.module_name:
        _fail_identity("MODULE graph metadata does not match its evaluation result")
    if (
        pair.strict.dataset_id != dataset.dataset_id
        or pair.lenient.dataset_id != dataset.dataset_id
    ):
        _fail_identity("MODULE result dataset does not match the renderer dataset")
    if pair.strict.decision_policy != dataset.import_report.decision_policy:
        _fail_identity("MODULE decision policy does not match the renderer dataset")
    if pair.strict.reference_retrieval_provenance != graph.retrieval_provenance:
        _fail_identity("MODULE retrieval provenance does not match the resolved graph")
    if graph.limits != pair.strict.limits.resolution:
        _fail_identity("MODULE resolver limits do not match the evaluation limits")
    if any(item.parse_result.limits != pair.strict.limits.parsing for item in graph.modules):
        _fail_identity("MODULE parser limits do not match the evaluation limits")
    graph_provenance = tuple(
        (item.definition.module_id, item.definition.provenance) for item in graph.modules
    )
    result_provenance = tuple((item.module_id, item.provenance) for item in pair.strict.provenance)
    if graph_provenance != result_provenance:
        _fail_identity("MODULE definition provenance does not match the resolved graph")
    expected_block_count = (
        len(root.parse_result.ast.required_blocks) if root.parse_result.ast else 0
    )
    if pair.strict.required_block_count != expected_block_count:
        _fail_identity("MODULE block denominator does not match the resolved root AST")


def _validate_original_module_evaluation(
    dataset: AnnotationDataset,
    graph: ResolvedModuleGraph,
    original: PairedModuleEvaluation,
) -> None:
    try:
        recomputed = evaluate_module_pair(graph, dataset, original.strict.limits)
    except KeggMcpError:
        _fail_identity(
            "The MODULE result cannot be recalculated from its graph, dataset, and recorded limits"
        )
    if recomputed != original:
        _fail_identity(
            "The MODULE result does not match a deterministic recalculation from its graph, "
            "dataset, and recorded limits"
        )


def _validate_module_semantics(
    original: PairedModuleEvaluation,
    complete: PairedModuleEvaluation,
) -> None:
    for original_result, complete_result in (
        (original.strict, complete.strict),
        (original.lenient, complete.lenient),
    ):
        if _module_semantic_identity(original_result) != _module_semantic_identity(complete_result):
            _fail_identity("complete renderer evaluation changed the MODULE analysis result")


def _module_semantic_identity(result: ModuleEvaluationResult) -> tuple[object, ...]:
    return (
        result.module_id,
        result.module_name,
        result.dataset_id,
        result.decision_policy,
        result.evidence_mode,
        result.evidence_ko_count,
        result.evaluation_status,
        result.is_complete,
        result.block_coverage,
        result.completed_required_blocks,
        result.evaluable_required_blocks,
        result.required_block_count,
        result.calculation_method,
        result.reference_retrieval_provenance,
        result.provenance,
    )


def _validate_pathway_identity(
    dataset: AnnotationDataset,
    reference: PathwayKoReference,
    result: PathwayCoverageResult,
) -> None:
    fields: tuple[tuple[object, object], ...] = (
        (reference.pathway_id, result.pathway_id),
        (reference.pathway_name, result.pathway_name),
        (reference.pathway_class, result.pathway_class),
        (reference.reference_namespace, result.reference_namespace),
        (reference.reference_scope, result.reference_scope),
        (reference.kegg_organism_code, result.reference_kegg_organism_code),
        (len(reference.reference_kos), result.reference_unique_ko_count),
        (len(reference.exclusions), result.excluded_entry_count),
        (reference.relationship_row_count, result.relationship_row_count),
        (reference.duplicate_relationship_count, result.duplicate_relationship_count),
        (reference.link_provenance, result.reference_link_provenance),
        (reference.metadata_provenance, result.reference_metadata_provenance),
        (dataset.dataset_id, result.dataset_id),
        (dataset.import_report.decision_policy, result.decision_policy),
        (dataset.analysis_unit, result.analysis_unit),
        (dataset.taxon_id, result.taxon_id),
        (dataset.kegg_organism_code, result.kegg_organism_code),
        (dataset.sources, result.sources),
    )
    if any(left != right for left, right in fields):
        _fail_identity("pathway reference, result, and dataset identities do not align")


def _validate_target_counts(
    graphs: tuple[ResolvedModuleGraph, ...],
    pairs: tuple[PairedModuleEvaluation, ...],
    references: tuple[PathwayKoReference, ...],
    results: tuple[PathwayCoverageResult, ...],
    bounds: RenderInputLimits,
) -> None:
    if len(graphs) != len(pairs):
        _fail_identity("MODULE graph and result counts do not match")
    if len(references) != len(results):
        _fail_identity("pathway reference and result counts do not match")
    for metric, observed, limit_name, maximum in (
        ("module_targets", len(graphs), "max_module_targets", bounds.max_module_targets),
        ("pathway_targets", len(references), "max_pathway_targets", bounds.max_pathway_targets),
        (
            "total_targets",
            len(graphs) + len(references),
            "max_total_targets",
            bounds.max_total_targets,
        ),
    ):
        if observed > maximum:
            _fail_output_limit(metric, observed, limit_name, maximum)


def _validate_execution_identity(
    module_results: tuple[PairedModuleEvaluation, ...],
    pathway_results: tuple[PathwayCoverageResult, ...],
    execution: AnalysisExecutionProvenance,
) -> None:
    if any(
        pair.strict.limits != execution.module_analysis_limits
        or pair.lenient.limits != execution.module_analysis_limits
        for pair in module_results
    ):
        _fail_identity("MODULE result limits do not match analysis execution provenance")
    parameters = execution.pathway_parameters
    if any(
        result.limits != execution.pathway_coverage_limits
        or result.parameters.evidence_mode is not parameters.evidence_mode
        or result.parameters.allow_global_or_overview != parameters.allow_global_or_overview
        for result in pathway_results
    ):
        _fail_identity("pathway result parameters or limits do not match execution provenance")


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


def _optional_expression_count(graph: ResolvedModuleGraph) -> int:
    count = 0
    for resolved in graph.modules:
        ast = resolved.parse_result.ast
        if ast is None:
            continue
        pending: list[ModuleExpression] = list(ast.required_blocks)
        while pending:
            expression = pending.pop()
            if expression.kind is ModuleExpressionKind.OPTIONAL:
                count += 1
            pending.extend(expression.children)
    return count


def _edge_sort_key(value: ModuleReferenceEdge) -> tuple[str, str, int, int]:
    return (
        value.source_module_id,
        value.target_module_id,
        value.source_span.start_offset,
        value.source_span.end_offset,
    )


def _issue_sort_key(
    value: ModuleReferenceIssue,
) -> tuple[str, str, int, int, str, tuple[str, ...]]:
    return (
        value.source_module_id,
        value.target_module_id,
        value.source_span.start_offset,
        value.source_span.end_offset,
        value.kind.value,
        value.path,
    )


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
    "ModuleCompletionRenderResult",
    "ModuleOptionalComponentRenderState",
    "ModuleRenderTarget",
    "ModuleRequiredBlockRenderState",
    "ModuleUncertainRenderSupport",
    "PathwayRenderTarget",
    "RenderDataset",
    "RenderExecutionProvenance",
    "RenderInput",
    "RenderInputLimits",
    "RenderProducer",
    "RenderabilityStatus",
    "VisualizationEvidence",
    "build_render_input",
    "parse_render_input_json",
    "serialize_render_input",
]
