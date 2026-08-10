"""Typed, bounded renderer handoff contracts independent of MCP transport."""

from __future__ import annotations

import json
from collections.abc import Callable
from enum import StrEnum
from typing import Annotated, Literal, NoReturn, Self, TypeVar

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp import __version__
from kegg_mcp.analysis.contracts import (
    MODULE_CALCULATION_METHOD,
    MODULE_CALCULATION_VERSION,
    MODULE_PARSER_NAME,
    MODULE_PARSER_VERSION,
    MODULE_RESOLVER_VERSION,
    CalculationMethodReference,
    ModuleAnalysisLimits,
    ModuleBlockState,
    ModuleEvaluationResult,
    ModuleEvaluationStatus,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleReferenceEdge,
    ModuleReferenceIssue,
    ModuleWarning,
    OptionalComponentState,
    ResolvedModuleDefinition,
    ResolvedModuleGraph,
)
from kegg_mcp.analysis.module_evaluation import evaluate_module
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
    ModuleId,
    NormalizedStatus,
    SourceProvenance,
    StatusCount,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.execution import AnalysisExecutionProvenance
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggOperation

RENDER_INPUT_SCHEMA_VERSION = "6"
RENDER_INPUT_MIME_TYPE = "application/vnd.kegg-mcp.render-input+json;version=6"
RENDER_INPUT_BUILDER_NAME = "kegg_render_handoff"
RENDER_INPUT_BUILDER_VERSION = "5"
MODULE_RENDER_MAX_CANVAS_DIMENSION = 20_000
MODULE_RENDER_MAX_CANVAS_PIXELS = 20_000_000
MODULE_RENDER_MAX_SVG_NODES = 4_096

PathwayId = Annotated[str, Field(pattern=r"^(?:ko|map|[a-z][a-z0-9]{1,7})[0-9]{5}$")]
MachineReason = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=100)]
NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]
_T = TypeVar("_T")


class RenderabilityStatus(StrEnum):
    """Whether the renderer can produce a complete graphic."""

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
    """Serialized renderer-handoff bounds recorded with every version 6 document."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:render-input-limits:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    max_evidence_ko_ids: int = Field(default=100_000, strict=True, gt=0, le=1_000_000)
    max_module_targets: int = Field(default=100, strict=True, ge=0, le=1_000)
    max_pathway_targets: int = Field(default=25, strict=True, ge=0, le=1_000)
    max_module_definitions_per_target: int = Field(default=256, strict=True, gt=0, le=10_000)
    max_module_ast_nodes_per_target: int = Field(default=50_000, strict=True, gt=0, le=1_000_000)
    max_module_required_blocks_per_target: int = Field(default=1_000, strict=True, gt=0, le=10_000)
    max_module_optional_components_per_target: int = Field(
        default=1_000, strict=True, gt=0, le=10_000
    )
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


class ModuleCompletionRenderResult(FrozenModel):
    """Exact completion and project block coverage without preview fields."""

    evaluation_status: ModuleEvaluationStatus
    is_complete: bool | None
    block_coverage: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    completed_required_blocks: NonNegativeCount
    evaluable_required_blocks: NonNegativeCount
    required_block_count: NonNegativeCount
    calculation_method: CalculationMethodReference

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
    """Complete state for one required root top-level block."""

    block_index: int = Field(strict=True, gt=0)
    state: ModuleBlockState


class ModuleOptionalComponentRenderState(FrozenModel):
    """Complete presence state for one optional expression."""

    component_index: int = Field(strict=True, gt=0)
    source_module_id: ModuleId
    state: OptionalComponentState


class ModuleRenderTarget(FrozenModel):
    """Complete-within-limit renderer target for one resolved KEGG MODULE graph."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:module-render-target:3",
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
    completion: ModuleCompletionRenderResult
    required_block_states_complete: bool
    required_block_states: Annotated[
        tuple[ModuleRequiredBlockRenderState, ...], Field(max_length=10_000)
    ]
    optional_component_states_complete: bool
    optional_component_states: Annotated[
        tuple[ModuleOptionalComponentRenderState, ...], Field(max_length=10_000)
    ]
    parser_name: Literal["kegg_module_definition"]
    parser_version: Literal["1"]
    resolver_version: Literal["1"]
    reference_retrieval_provenance: Annotated[
        tuple[KeggBatchProvenance, ...], Field(max_length=5_000)
    ]
    warnings: Annotated[tuple[ModuleWarning, ...], Field(max_length=32)]

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        current_calculation = CalculationMethodReference(
            name=MODULE_CALCULATION_METHOD,
            version=MODULE_CALCULATION_VERSION,
        )
        if self.completion.calculation_method != current_calculation:
            raise ValueError("MODULE calculation identity must match the current contract")
        if any(
            item.parse_result.parser_name != self.parser_name
            or item.parse_result.parser_version != self.parser_version
            for item in self.definitions
        ):
            raise ValueError("MODULE definition parser identity must match the target contract")
        if any(
            batch.operation is not KeggOperation.GET
            for batch in self.reference_retrieval_provenance
        ):
            raise ValueError("MODULE render targets require only GET retrieval provenance")
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
        expected_indexes = tuple(range(1, self.completion.required_block_count + 1))
        if self.required_block_states_complete:
            if block_indexes != expected_indexes:
                raise ValueError("complete block states must cover every block in order")
            states = tuple(item.state for item in self.required_block_states)
            completed = sum(state is ModuleBlockState.COMPLETE for state in states)
            evaluable = sum(state is not ModuleBlockState.NOT_EVALUABLE for state in states)
            if (
                completed != self.completion.completed_required_blocks
                or evaluable != self.completion.evaluable_required_blocks
            ):
                raise ValueError("complete block states must match the completion summary")
        elif self.required_block_states:
            raise ValueError("incomplete block states must not contain a misleading prefix")
        optional_indexes = tuple(item.component_index for item in self.optional_component_states)
        if self.optional_component_states_complete:
            if optional_indexes != tuple(range(1, len(optional_indexes) + 1)):
                raise ValueError("optional component states must use contiguous output order")
        elif self.optional_component_states:
            raise ValueError("incomplete optional states must not contain a misleading prefix")

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
                raise ValueError("version 6 renders only KO-reference pathway targets")
            if self.evaluation_status is not PathwayCoverageStatus.EVALUATED:
                raise ValueError("renderable pathway targets require an evaluated denominator")
        elif self.not_renderable_reason is None:
            raise ValueError("non-renderable pathway targets require a machine reason")
        return self


class RenderExecutionProvenance(FrozenModel):
    """Core analysis parameters and renderer-handoff builder identity."""

    analysis: AnalysisExecutionProvenance
    handoff_builder_name: Literal["kegg_render_handoff"]
    handoff_builder_version: Literal["5"]


class RenderInput(FrozenModel):
    """Complete, typed renderer input produced by the core analysis service."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:render-input:6",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    schema_version: Literal["6"]
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
        if len(self.evidence.accepted_ko_ids) > self.limits.max_evidence_ko_ids:
            raise ValueError("visualization evidence exceeds the recorded renderer limit")
        module_ids = tuple(item.module_id for item in self.modules)
        pathway_ids = tuple(item.pathway_id for item in self.pathways)
        if module_ids != tuple(sorted(set(module_ids))):
            raise ValueError("MODULE targets must use sorted unique identifiers")
        if pathway_ids != tuple(sorted(set(pathway_ids))):
            raise ValueError("pathway targets must use sorted unique identifiers")
        accepted = set(self.evidence.accepted_ko_ids)
        pathway_parameters = self.execution.analysis.pathway_parameters
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
    module_graphs: tuple[ResolvedModuleGraph, ...],
    pathway_references: tuple[PathwayKoReference, ...],
    execution: AnalysisExecutionProvenance,
    *,
    limits: RenderInputLimits | None = None,
) -> RenderInput:
    """Build one handoff from the canonical compact accepted-KO analysis view."""
    bounds = limits or RenderInputLimits()
    _validate_target_counts(module_graphs, pathway_references, bounds)

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

    graph_by_id = _unique_by_id(module_graphs, lambda item: item.root_module_id, "MODULE graph")
    module_targets = tuple(
        _module_target(
            evidence,
            graph_by_id[module_id],
            execution.module_analysis_limits,
            bounds,
        )
        for module_id in sorted(graph_by_id)
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
        modules=module_targets,
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
    evidence: KoAnalysisView,
    graph: ResolvedModuleGraph,
    analysis_limits: ModuleAnalysisLimits,
    bounds: RenderInputLimits,
) -> ModuleRenderTarget:
    if graph.limits != analysis_limits.resolution or any(
        item.parse_result.limits != analysis_limits.parsing for item in graph.modules
    ):
        _fail_identity("MODULE graph limits do not match analysis execution provenance")
    result = _complete_render_evaluation(graph, evidence, analysis_limits, bounds)
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
    completion = _completion_summary(result)

    renderability: RenderabilityStatus = RenderabilityStatus.RENDERABLE
    reason: str | None = None
    blocks_complete = False
    optional_complete = False
    block_states: tuple[ModuleRequiredBlockRenderState, ...] = ()
    optional_states: tuple[ModuleOptionalComponentRenderState, ...] = ()
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
        block_states = _required_block_states(result)
        optional_states, optional_complete = _optional_states(result, optional_count)
        blocks_complete = len(block_states) == result.required_block_count
        if not optional_complete:
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

    warnings = tuple(sorted(result.warnings, key=lambda item: item.code.value))
    return ModuleRenderTarget(
        module_id=graph.root_module_id,
        module_name=result.module_name,
        renderability=renderability,
        not_renderable_reason=reason,
        definition_graph_complete=definitions_fit,
        definitions=definitions,
        reference_edges=edges,
        reference_issues=issues,
        completion=completion,
        required_block_states_complete=blocks_complete,
        required_block_states=block_states,
        optional_component_states_complete=optional_complete,
        optional_component_states=optional_states,
        parser_name=MODULE_PARSER_NAME,
        parser_version=MODULE_PARSER_VERSION,
        resolver_version=MODULE_RESOLVER_VERSION,
        reference_retrieval_provenance=graph.retrieval_provenance,
        warnings=warnings,
    )


def _complete_render_evaluation(
    graph: ResolvedModuleGraph,
    evidence: KoAnalysisView,
    original_limits: ModuleAnalysisLimits,
    bounds: RenderInputLimits,
) -> ModuleEvaluationResult:
    evaluation = original_limits.evaluation.model_copy(
        update={
            "max_block_previews": bounds.max_module_required_blocks_per_target,
            "max_optional_components": bounds.max_module_optional_components_per_target,
        }
    )
    limits = ModuleAnalysisLimits(
        parsing=original_limits.parsing,
        resolution=original_limits.resolution,
        evaluation=evaluation,
    )
    return evaluate_module(graph, evidence, limits)


def _required_block_states(
    result: ModuleEvaluationResult,
) -> tuple[ModuleRequiredBlockRenderState, ...]:
    states = _block_state_map(result)
    return tuple(
        ModuleRequiredBlockRenderState(
            block_index=index,
            state=states[index],
        )
        for index in range(1, result.required_block_count + 1)
    )


def _block_state_map(result: ModuleEvaluationResult) -> dict[int, ModuleBlockState]:
    states = {index: ModuleBlockState.COMPLETE for index in result.present_blocks_preview}
    states.update({item.block_index: item.state for item in result.missing_blocks_preview})
    states.update({item.block_index: item.state for item in result.not_evaluable_blocks_preview})
    expected = set(range(1, result.required_block_count + 1))
    if set(states) != expected:
        _fail_identity("complete renderer block evaluation did not retain every required block")
    return states


def _optional_states(
    result: ModuleEvaluationResult,
    expected_count: int,
) -> tuple[tuple[ModuleOptionalComponentRenderState, ...], bool]:
    optional_components = result.optional_components
    if len(optional_components) != expected_count:
        return (), False
    return (
        tuple(
            ModuleOptionalComponentRenderState(
                component_index=item.component_index,
                source_module_id=item.source_module_id,
                state=item.state,
            )
            for item in optional_components
        ),
        True,
    )


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


def _completion_summary(result: ModuleEvaluationResult) -> ModuleCompletionRenderResult:
    return ModuleCompletionRenderResult(
        evaluation_status=result.evaluation_status,
        is_complete=result.is_complete,
        block_coverage=result.block_coverage,
        completed_required_blocks=result.completed_required_blocks,
        evaluable_required_blocks=result.evaluable_required_blocks,
        required_block_count=result.required_block_count,
        calculation_method=result.calculation_method,
    )


def _validate_target_counts(
    graphs: tuple[ResolvedModuleGraph, ...],
    references: tuple[PathwayKoReference, ...],
    bounds: RenderInputLimits,
) -> None:
    for metric, observed, limit_name, maximum in (
        ("module_targets", len(graphs), "max_module_targets", bounds.max_module_targets),
        ("pathway_targets", len(references), "max_pathway_targets", bounds.max_pathway_targets),
    ):
        if observed > maximum:
            _fail_output_limit(metric, observed, limit_name, maximum)


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
