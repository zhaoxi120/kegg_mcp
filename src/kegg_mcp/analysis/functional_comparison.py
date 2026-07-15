"""Shared-reference comparison of deterministic MODULE and pathway analyses."""

from __future__ import annotations

from typing import Annotated, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from kegg_mcp.analysis.comparison import (
    ComparisonDatasetInput,
    ComparisonDatasetProvenance,
    ComparisonLimits,
    ComparisonWarning,
    compare_ko_datasets,
)
from kegg_mcp.analysis.contracts import (
    CalculationMethodReference,
    EvaluatedDefinitionProvenance,
    ModuleAnalysisLimits,
    ModuleEvaluationLimits,
    ModuleEvaluationResult,
    ModuleEvaluationStatus,
    ModuleId,
    ModuleReferenceIssue,
    ResolvedModuleGraph,
)
from kegg_mcp.analysis.module_evaluation import evaluate_module_pair
from kegg_mcp.analysis.pathway_coverage import (
    PATHWAY_COVERAGE_METHOD,
    PATHWAY_COVERAGE_VERSION,
    OrganismGeneContext,
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    PathwayCoverageResult,
    PathwayCoverageStatus,
    PathwayCoverageWarning,
    PathwayInputContext,
    PathwayInputKind,
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    evaluate_pathway_coverage,
)
from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    EvidenceMode,
    FrozenModel,
    normalize_identifier_label,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import KeggBatchProvenance, KeggOperation

MODULE_COMPARISON_METHOD = "shared_definition_module_outcome_comparison"
MODULE_COMPARISON_VERSION = "1"
PATHWAY_COMPARISON_METHOD = "shared_reference_pathway_coverage_comparison"
PATHWAY_COMPARISON_VERSION = "1"

NonNegativeCount = Annotated[int, Field(strict=True, ge=0)]


class FunctionalComparisonLimits(FrozenModel):
    """Hard target bounds for shared-reference functional comparisons."""

    max_modules: int = Field(default=100, strict=True, gt=0, le=10_000)
    max_pathways: int = Field(default=100, strict=True, gt=0, le=10_000)
    max_total_pathway_reference_kos: int = Field(
        default=500_000,
        strict=True,
        gt=0,
        le=10_000_000,
    )
    max_total_pathway_reference_exclusions: int = Field(
        default=100_000,
        strict=True,
        ge=0,
        le=1_000_000,
    )


class SetModuleOutcome(FrozenModel):
    """One dataset's exact MODULE summary under one evidence mode."""

    input_index: NonNegativeCount
    label: str = Field(min_length=1, max_length=128)
    evaluation_status: ModuleEvaluationStatus
    is_complete: bool | None
    completed_required_blocks: NonNegativeCount
    evaluable_required_blocks: NonNegativeCount
    required_block_count: NonNegativeCount
    block_coverage: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if not (
            self.completed_required_blocks
            <= self.evaluable_required_blocks
            <= self.required_block_count
        ):
            raise ValueError("MODULE outcome block counts are inconsistent")
        expected_complete = {
            ModuleEvaluationStatus.COMPLETE: True,
            ModuleEvaluationStatus.INCOMPLETE: False,
            ModuleEvaluationStatus.PARTIALLY_EVALUABLE: None,
            ModuleEvaluationStatus.NOT_EVALUABLE: None,
        }[self.evaluation_status]
        if self.is_complete is not expected_complete:
            raise ValueError("MODULE outcome completion is inconsistent with its status")
        fully_evaluable = (
            self.required_block_count > 0
            and self.evaluable_required_blocks == self.required_block_count
        )
        if fully_evaluable:
            expected_coverage = self.completed_required_blocks / self.required_block_count
            if self.block_coverage != expected_coverage:
                raise ValueError("MODULE outcome coverage must use all required blocks")
        elif self.block_coverage is not None:
            raise ValueError("partially evaluable MODULE outcomes cannot report coverage")
        return self


class ModuleModeComparison(FrozenModel):
    """Ordered outcomes and status membership for one MODULE evidence mode."""

    evidence_mode: EvidenceMode
    outcomes: Annotated[tuple[SetModuleOutcome, ...], Field(min_length=2)]
    outcomes_differ: bool
    complete_in_set_indexes: tuple[NonNegativeCount, ...]
    incomplete_in_set_indexes: tuple[NonNegativeCount, ...]
    partially_evaluable_in_set_indexes: tuple[NonNegativeCount, ...]
    not_evaluable_in_set_indexes: tuple[NonNegativeCount, ...]

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        indexes = tuple(item.input_index for item in self.outcomes)
        if indexes != tuple(range(len(self.outcomes))):
            raise ValueError("MODULE outcomes must retain contiguous comparison input order")
        groups = {
            ModuleEvaluationStatus.COMPLETE: self.complete_in_set_indexes,
            ModuleEvaluationStatus.INCOMPLETE: self.incomplete_in_set_indexes,
            ModuleEvaluationStatus.PARTIALLY_EVALUABLE: (self.partially_evaluable_in_set_indexes),
            ModuleEvaluationStatus.NOT_EVALUABLE: self.not_evaluable_in_set_indexes,
        }
        for status, group in groups.items():
            expected = tuple(
                item.input_index for item in self.outcomes if item.evaluation_status is status
            )
            if group != expected:
                raise ValueError("MODULE status indexes must match ordered outcomes")
        signatures = tuple(_module_outcome_signature(item) for item in self.outcomes)
        if self.outcomes_differ != any(item != signatures[0] for item in signatures[1:]):
            raise ValueError("outcomes_differ must match ordered MODULE summaries")
        return self


class ModuleTargetComparison(FrozenModel):
    """Strict and lenient outcome differences for one shared resolved MODULE graph."""

    module_id: ModuleId
    module_name: str | None = Field(default=None, max_length=1_000)
    strict: ModuleModeComparison
    lenient: ModuleModeComparison
    module_calculation_method: CalculationMethodReference
    definition_provenance: Annotated[
        tuple[EvaluatedDefinitionProvenance, ...],
        Field(min_length=1),
    ]
    reference_retrieval_provenance: Annotated[
        tuple[KeggBatchProvenance, ...],
        Field(max_length=5_000),
    ]
    unresolved_references: tuple[ModuleReferenceIssue, ...]
    analysis_limits: ModuleAnalysisLimits

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.strict.evidence_mode is not EvidenceMode.STRICT:
            raise ValueError("strict MODULE comparison requires strict outcomes")
        if self.lenient.evidence_mode is not EvidenceMode.LENIENT:
            raise ValueError("lenient MODULE comparison requires lenient outcomes")
        if tuple(item.label for item in self.strict.outcomes) != tuple(
            item.label for item in self.lenient.outcomes
        ):
            raise ValueError("strict and lenient MODULE outcomes must use the same input order")
        provenance_ids = tuple(item.module_id for item in self.definition_provenance)
        if self.module_id not in provenance_ids:
            raise ValueError("MODULE comparison provenance must include the root definition")
        if any(
            batch.operation is not KeggOperation.GET
            for batch in self.reference_retrieval_provenance
        ):
            raise ValueError("MODULE comparison retrieval provenance requires GET batches")
        return self


class ModuleComparisonResult(FrozenModel):
    """Bounded MODULE comparisons recomputed under shared definitions and parameters."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:module-comparison-result:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    datasets: Annotated[tuple[ComparisonDatasetProvenance, ...], Field(min_length=2)]
    targets: tuple[ModuleTargetComparison, ...]
    calculation_method: CalculationMethodReference
    context_warnings: tuple[ComparisonWarning, ...]
    comparison_limits: ComparisonLimits
    functional_limits: FunctionalComparisonLimits
    requested_evaluation_limits: ModuleEvaluationLimits | None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        module_ids = tuple(item.module_id for item in self.targets)
        if len(module_ids) != len(set(module_ids)):
            raise ValueError("MODULE comparison targets must be unique")
        if len(self.targets) > self.functional_limits.max_modules:
            raise ValueError("MODULE targets exceed the recorded functional comparison limit")
        labels = tuple(item.label for item in self.datasets)
        for target in self.targets:
            if tuple(item.label for item in target.strict.outcomes) != labels:
                raise ValueError("MODULE comparison target inputs must match dataset order")
        if self.calculation_method != CalculationMethodReference(
            name=MODULE_COMPARISON_METHOD,
            version=MODULE_COMPARISON_VERSION,
        ):
            raise ValueError("calculation_method is incompatible with this result schema")
        return self


class PathwayComparisonOrganismContext(FrozenModel):
    """One input-aligned organism gene context for organism pathway references."""

    input_index: NonNegativeCount
    label: str = Field(min_length=1, max_length=128)
    gene_context: OrganismGeneContext

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        return normalize_identifier_label(value, field_name="pathway comparison label")


class SetPathwayOutcome(FrozenModel):
    """One dataset's exact descriptive pathway outcome under one evidence mode."""

    input_index: NonNegativeCount
    label: str = Field(min_length=1, max_length=128)
    evaluation_status: PathwayCoverageStatus
    input_record_count: NonNegativeCount
    input_unique_ko_count: NonNegativeCount
    detected_reference_ko_count: NonNegativeCount
    missing_reference_ko_count: NonNegativeCount
    reference_unique_ko_count: NonNegativeCount
    coverage_ratio: float | None = Field(default=None, strict=True, ge=0.0, le=1.0)
    warnings: Annotated[tuple[PathwayCoverageWarning, ...], Field(max_length=12)]

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.detected_reference_ko_count > self.input_unique_ko_count:
            raise ValueError("detected pathway KOs cannot exceed selected input KOs")
        if self.detected_reference_ko_count + self.missing_reference_ko_count != (
            self.reference_unique_ko_count
        ):
            raise ValueError("detected and missing pathway KOs must partition the reference")
        if self.reference_unique_ko_count == 0:
            if (
                self.evaluation_status is not PathwayCoverageStatus.NOT_EVALUABLE
                or self.coverage_ratio is not None
            ):
                raise ValueError("an empty pathway reference must be not evaluable without a ratio")
        elif (
            self.evaluation_status is not PathwayCoverageStatus.EVALUATED
            or self.coverage_ratio
            != self.detected_reference_ko_count / self.reference_unique_ko_count
        ):
            raise ValueError("an evaluated pathway ratio must use the complete shared reference")
        warning_codes = tuple(item.code for item in self.warnings)
        if len(warning_codes) != len(set(warning_codes)):
            raise ValueError("pathway outcome warning codes must be unique")
        return self


class PathwayModeComparison(FrozenModel):
    """Ordered descriptive outcomes for one shared-reference pathway evidence mode."""

    evidence_mode: EvidenceMode
    outcomes: Annotated[tuple[SetPathwayOutcome, ...], Field(min_length=2, max_length=100)]
    outcomes_differ: bool
    evaluated_in_set_indexes: tuple[NonNegativeCount, ...]
    not_evaluable_in_set_indexes: tuple[NonNegativeCount, ...]

    @model_validator(mode="after")
    def validate_mode(self) -> Self:
        indexes = tuple(item.input_index for item in self.outcomes)
        if indexes != tuple(range(len(self.outcomes))):
            raise ValueError("pathway outcomes must retain contiguous comparison input order")
        evaluated = tuple(
            item.input_index
            for item in self.outcomes
            if item.evaluation_status is PathwayCoverageStatus.EVALUATED
        )
        not_evaluable = tuple(
            item.input_index
            for item in self.outcomes
            if item.evaluation_status is PathwayCoverageStatus.NOT_EVALUABLE
        )
        if self.evaluated_in_set_indexes != evaluated:
            raise ValueError("evaluated pathway indexes must match ordered outcomes")
        if self.not_evaluable_in_set_indexes != not_evaluable:
            raise ValueError("not-evaluable pathway indexes must match ordered outcomes")
        signatures = tuple(_pathway_outcome_signature(item) for item in self.outcomes)
        if self.outcomes_differ != any(item != signatures[0] for item in signatures[1:]):
            raise ValueError("outcomes_differ must match ordered pathway summaries")
        return self


class PathwayTargetComparison(FrozenModel):
    """Strict and lenient outcomes recomputed against one immutable pathway reference."""

    reference: PathwayKoReference
    strict: PathwayModeComparison
    lenient: PathwayModeComparison
    pathway_calculation_method: CalculationMethodReference
    coverage_limits: PathwayCoverageLimits
    allow_global_or_overview: bool

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if self.strict.evidence_mode is not EvidenceMode.STRICT:
            raise ValueError("strict pathway comparison requires strict outcomes")
        if self.lenient.evidence_mode is not EvidenceMode.LENIENT:
            raise ValueError("lenient pathway comparison requires lenient outcomes")
        strict_labels = tuple(item.label for item in self.strict.outcomes)
        if strict_labels != tuple(item.label for item in self.lenient.outcomes):
            raise ValueError("strict and lenient pathway outcomes must use the same input order")
        outcomes = (*self.strict.outcomes, *self.lenient.outcomes)
        reference_count = len(self.reference.reference_kos)
        if any(item.reference_unique_ko_count != reference_count for item in outcomes):
            raise ValueError("all pathway outcomes must use the complete shared denominator")
        if (
            self.reference.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
            and not self.allow_global_or_overview
        ):
            raise ValueError("broad pathway comparisons require recorded explicit opt-in")
        if self.pathway_calculation_method != CalculationMethodReference(
            name=PATHWAY_COVERAGE_METHOD,
            version=PATHWAY_COVERAGE_VERSION,
        ):
            raise ValueError("pathway calculation identity is incompatible with this schema")
        if reference_count > self.coverage_limits.max_reference_kos:
            raise ValueError("shared pathway denominator exceeds the recorded coverage limit")
        if len(self.reference.exclusions) > self.coverage_limits.max_reference_exclusions:
            raise ValueError("shared pathway exclusions exceed the recorded coverage limit")
        if self.reference.relationship_row_count > self.coverage_limits.max_relationship_rows:
            raise ValueError("shared pathway relationships exceed the recorded coverage limit")
        return self


class PathwayComparisonResult(FrozenModel):
    """Bounded pathway outcomes recomputed under shared denominators and parameters."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-comparison-result:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    datasets: Annotated[
        tuple[ComparisonDatasetProvenance, ...],
        Field(min_length=2, max_length=100),
    ]
    targets: tuple[PathwayTargetComparison, ...]
    organism_contexts: tuple[PathwayComparisonOrganismContext, ...]
    allow_global_or_overview: bool
    calculation_method: CalculationMethodReference
    context_warnings: tuple[ComparisonWarning, ...]
    comparison_limits: ComparisonLimits
    functional_limits: FunctionalComparisonLimits
    coverage_limits: PathwayCoverageLimits

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        pathway_ids = tuple(item.reference.pathway_id for item in self.targets)
        if len(pathway_ids) != len(set(pathway_ids)):
            raise ValueError("pathway comparison targets must be unique")
        if len(self.targets) > self.functional_limits.max_pathways:
            raise ValueError("pathway targets exceed the recorded functional comparison limit")
        total_reference_kos = sum(len(item.reference.reference_kos) for item in self.targets)
        if total_reference_kos > self.functional_limits.max_total_pathway_reference_kos:
            raise ValueError("pathway denominators exceed the recorded aggregate limit")
        total_exclusions = sum(len(item.reference.exclusions) for item in self.targets)
        if total_exclusions > self.functional_limits.max_total_pathway_reference_exclusions:
            raise ValueError("pathway exclusions exceed the recorded aggregate limit")
        labels = tuple(item.label for item in self.datasets)
        for target in self.targets:
            if tuple(item.label for item in target.strict.outcomes) != labels:
                raise ValueError("pathway comparison target inputs must match dataset order")
            if target.allow_global_or_overview != self.allow_global_or_overview:
                raise ValueError("pathway targets must retain the comparison-wide broad-map opt-in")
            if target.coverage_limits != self.coverage_limits:
                raise ValueError("pathway targets must use the comparison-wide coverage limits")
        organism_targets = tuple(
            item
            for item in self.targets
            if item.reference.reference_namespace is PathwayReferenceNamespace.ORGANISM
        )
        context_indexes = tuple(item.input_index for item in self.organism_contexts)
        context_labels = tuple(item.label for item in self.organism_contexts)
        if organism_targets:
            if context_indexes != tuple(range(len(self.datasets))) or context_labels != labels:
                raise ValueError("organism contexts must align with every comparison dataset")
        elif self.organism_contexts:
            raise ValueError("KO and map comparisons cannot retain organism gene contexts")
        if self.calculation_method != CalculationMethodReference(
            name=PATHWAY_COMPARISON_METHOD,
            version=PATHWAY_COMPARISON_VERSION,
        ):
            raise ValueError("calculation_method is incompatible with this result schema")
        return self


def compare_module_graphs(
    inputs: tuple[ComparisonDatasetInput, ...],
    graphs: tuple[ResolvedModuleGraph, ...],
    *,
    comparison_limits: ComparisonLimits | None = None,
    functional_limits: FunctionalComparisonLimits | None = None,
    evaluation_limits: ModuleEvaluationLimits | None = None,
) -> ModuleComparisonResult:
    """Recompute every dataset against the same resolved MODULE graphs before comparison."""
    effective_comparison_limits = comparison_limits or ComparisonLimits()
    effective_functional_limits = functional_limits or FunctionalComparisonLimits()
    ko_detail = compare_ko_datasets(inputs, limits=effective_comparison_limits)
    if len(graphs) > effective_functional_limits.max_modules:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The comparison contains too many MODULE targets.",
            suggested_action="Compare fewer MODULEs or raise the bounded target limit.",
            safe_details=(
                SafeDetail(name="module_count", value=str(len(graphs))),
                SafeDetail(
                    name="max_modules",
                    value=str(effective_functional_limits.max_modules),
                ),
            ),
        )
    module_ids = tuple(graph.root_module_id for graph in graphs)
    if len(module_ids) != len(set(module_ids)):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "MODULE comparison targets must be unique.",
            suggested_action="Supply each resolved root MODULE graph once.",
        )

    targets = tuple(
        _compare_one_module(graph, inputs, evaluation_limits=evaluation_limits) for graph in graphs
    )
    return ModuleComparisonResult(
        datasets=ko_detail.datasets,
        targets=targets,
        calculation_method=CalculationMethodReference(
            name=MODULE_COMPARISON_METHOD,
            version=MODULE_COMPARISON_VERSION,
        ),
        context_warnings=ko_detail.warnings,
        comparison_limits=effective_comparison_limits,
        functional_limits=effective_functional_limits,
        requested_evaluation_limits=evaluation_limits,
    )


def compare_pathway_references(
    inputs: tuple[ComparisonDatasetInput, ...],
    references: tuple[PathwayKoReference, ...],
    *,
    comparison_limits: ComparisonLimits | None = None,
    functional_limits: FunctionalComparisonLimits | None = None,
    coverage_limits: PathwayCoverageLimits | None = None,
    organism_contexts: tuple[PathwayComparisonOrganismContext, ...] = (),
    allow_global_or_overview: bool = False,
) -> PathwayComparisonResult:
    """Recompute every dataset against the same immutable pathway references."""
    effective_comparison_limits = comparison_limits or ComparisonLimits()
    effective_functional_limits = functional_limits or FunctionalComparisonLimits()
    effective_coverage_limits = coverage_limits or PathwayCoverageLimits()
    ko_detail = compare_ko_datasets(inputs, limits=effective_comparison_limits)

    if len(references) > effective_functional_limits.max_pathways:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The comparison contains too many pathway targets.",
            suggested_action="Compare fewer pathways or raise the bounded target limit.",
            safe_details=(
                SafeDetail(name="pathway_count", value=str(len(references))),
                SafeDetail(
                    name="max_pathways",
                    value=str(effective_functional_limits.max_pathways),
                ),
            ),
        )
    pathway_ids = tuple(reference.pathway_id for reference in references)
    if len(pathway_ids) != len(set(pathway_ids)):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Pathway comparison targets must be unique.",
            suggested_action="Supply each immutable pathway reference once.",
        )
    total_reference_kos = sum(len(reference.reference_kos) for reference in references)
    if total_reference_kos > effective_functional_limits.max_total_pathway_reference_kos:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The comparison exceeds the aggregate pathway-denominator limit.",
            suggested_action="Compare fewer or smaller pathway references.",
            safe_details=(
                SafeDetail(name="reference_ko_count", value=str(total_reference_kos)),
                SafeDetail(
                    name="max_total_pathway_reference_kos",
                    value=str(effective_functional_limits.max_total_pathway_reference_kos),
                ),
            ),
        )
    total_exclusions = sum(len(reference.exclusions) for reference in references)
    if total_exclusions > effective_functional_limits.max_total_pathway_reference_exclusions:
        fail(
            ErrorCode.INPUT_LIMIT_EXCEEDED,
            "The comparison exceeds the aggregate pathway-exclusion limit.",
            suggested_action="Compare fewer pathway references or reduce retained exclusions.",
            safe_details=(
                SafeDetail(name="reference_exclusion_count", value=str(total_exclusions)),
                SafeDetail(
                    name="max_total_pathway_reference_exclusions",
                    value=str(effective_functional_limits.max_total_pathway_reference_exclusions),
                ),
            ),
        )

    _validate_pathway_organism_contexts(inputs, references, organism_contexts)
    targets = tuple(
        _compare_one_pathway(
            reference,
            inputs,
            coverage_limits=effective_coverage_limits,
            organism_contexts=organism_contexts,
            allow_global_or_overview=allow_global_or_overview,
        )
        for reference in references
    )
    return PathwayComparisonResult(
        datasets=ko_detail.datasets,
        targets=targets,
        organism_contexts=organism_contexts,
        allow_global_or_overview=allow_global_or_overview,
        calculation_method=CalculationMethodReference(
            name=PATHWAY_COMPARISON_METHOD,
            version=PATHWAY_COMPARISON_VERSION,
        ),
        context_warnings=ko_detail.warnings,
        comparison_limits=effective_comparison_limits,
        functional_limits=effective_functional_limits,
        coverage_limits=effective_coverage_limits,
    )


def _compare_one_module(
    graph: ResolvedModuleGraph,
    inputs: tuple[ComparisonDatasetInput, ...],
    *,
    evaluation_limits: ModuleEvaluationLimits | None,
) -> ModuleTargetComparison:
    pairs = tuple(
        evaluate_module_pair(graph, item.dataset, limits=evaluation_limits) for item in inputs
    )
    strict_results = tuple(pair.strict for pair in pairs)
    lenient_results = tuple(pair.lenient for pair in pairs)
    identity_fields = (
        "module_id",
        "module_name",
        "required_block_count",
        "calculation_method",
        "unresolved_references",
        "reference_retrieval_provenance",
        "provenance",
        "limits",
    )
    first = strict_results[0]
    if any(
        any(getattr(result, field) != getattr(first, field) for field in identity_fields)
        for result in (*strict_results[1:], *lenient_results)
    ):
        fail(
            ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE,
            "MODULE evaluations did not retain one shared definition and algorithm identity.",
            suggested_action="Recompute every dataset from the same resolved MODULE graph.",
            safe_details=(SafeDetail(name="module_id", value=graph.root_module_id),),
        )
    labels = tuple(item.label for item in inputs)
    return ModuleTargetComparison(
        module_id=first.module_id,
        module_name=first.module_name,
        strict=_module_mode_comparison(EvidenceMode.STRICT, labels, strict_results),
        lenient=_module_mode_comparison(EvidenceMode.LENIENT, labels, lenient_results),
        module_calculation_method=first.calculation_method,
        definition_provenance=first.provenance,
        reference_retrieval_provenance=first.reference_retrieval_provenance,
        unresolved_references=first.unresolved_references,
        analysis_limits=first.limits,
    )


def _module_mode_comparison(
    mode: EvidenceMode,
    labels: tuple[str, ...],
    results: tuple[ModuleEvaluationResult, ...],
) -> ModuleModeComparison:
    outcomes = tuple(
        SetModuleOutcome(
            input_index=index,
            label=labels[index],
            evaluation_status=result.evaluation_status,
            is_complete=result.is_complete,
            completed_required_blocks=result.completed_required_blocks,
            evaluable_required_blocks=result.evaluable_required_blocks,
            required_block_count=result.required_block_count,
            block_coverage=result.block_coverage,
        )
        for index, result in enumerate(results)
    )
    signatures = tuple(_module_outcome_signature(item) for item in outcomes)
    return ModuleModeComparison(
        evidence_mode=mode,
        outcomes=outcomes,
        outcomes_differ=any(item != signatures[0] for item in signatures[1:]),
        complete_in_set_indexes=_indexes_for_status(outcomes, ModuleEvaluationStatus.COMPLETE),
        incomplete_in_set_indexes=_indexes_for_status(
            outcomes,
            ModuleEvaluationStatus.INCOMPLETE,
        ),
        partially_evaluable_in_set_indexes=_indexes_for_status(
            outcomes,
            ModuleEvaluationStatus.PARTIALLY_EVALUABLE,
        ),
        not_evaluable_in_set_indexes=_indexes_for_status(
            outcomes,
            ModuleEvaluationStatus.NOT_EVALUABLE,
        ),
    )


def _indexes_for_status(
    outcomes: tuple[SetModuleOutcome, ...],
    status: ModuleEvaluationStatus,
) -> tuple[int, ...]:
    return tuple(item.input_index for item in outcomes if item.evaluation_status is status)


def _module_outcome_signature(outcome: SetModuleOutcome) -> tuple[object, ...]:
    return (
        outcome.evaluation_status,
        outcome.is_complete,
        outcome.completed_required_blocks,
        outcome.evaluable_required_blocks,
        outcome.required_block_count,
        outcome.block_coverage,
    )


def _validate_pathway_organism_contexts(
    inputs: tuple[ComparisonDatasetInput, ...],
    references: tuple[PathwayKoReference, ...],
    contexts: tuple[PathwayComparisonOrganismContext, ...],
) -> None:
    organism_reference_requested = any(
        reference.reference_namespace is PathwayReferenceNamespace.ORGANISM
        for reference in references
    )
    if not organism_reference_requested:
        if contexts:
            fail(
                ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
                "KO and map pathway comparisons cannot claim organism gene context.",
                suggested_action="Remove organism contexts or select an organism pathway.",
            )
        return
    expected_indexes = tuple(range(len(inputs)))
    expected_labels = tuple(item.label for item in inputs)
    observed_indexes = tuple(item.input_index for item in contexts)
    observed_labels = tuple(item.label for item in contexts)
    if observed_indexes != expected_indexes or observed_labels != expected_labels:
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Organism gene contexts must align with every comparison input.",
            suggested_action=(
                "Provide one ordered context per input with the same input index and label."
            ),
            safe_details=(
                SafeDetail(name="input_count", value=str(len(inputs))),
                SafeDetail(name="context_count", value=str(len(contexts))),
            ),
        )


def _compare_one_pathway(
    reference: PathwayKoReference,
    inputs: tuple[ComparisonDatasetInput, ...],
    *,
    coverage_limits: PathwayCoverageLimits,
    organism_contexts: tuple[PathwayComparisonOrganismContext, ...],
    allow_global_or_overview: bool,
) -> PathwayTargetComparison:
    strict_results = tuple(
        evaluate_pathway_coverage(
            reference,
            item.dataset,
            _pathway_parameters(
                reference,
                EvidenceMode.STRICT,
                index,
                organism_contexts,
                allow_global_or_overview,
            ),
            coverage_limits,
        )
        for index, item in enumerate(inputs)
    )
    lenient_results = tuple(
        evaluate_pathway_coverage(
            reference,
            item.dataset,
            _pathway_parameters(
                reference,
                EvidenceMode.LENIENT,
                index,
                organism_contexts,
                allow_global_or_overview,
            ),
            coverage_limits,
        )
        for index, item in enumerate(inputs)
    )
    identity_fields = (
        "pathway_id",
        "pathway_name",
        "pathway_class",
        "reference_namespace",
        "reference_scope",
        "reference_kegg_organism_code",
        "reference_unique_ko_count",
        "excluded_entry_count",
        "relationship_row_count",
        "duplicate_relationship_count",
        "reference_link_provenance",
        "reference_metadata_provenance",
        "calculation_method",
        "calculation_version",
        "limits",
    )
    first = strict_results[0]
    if any(
        any(getattr(result, field) != getattr(first, field) for field in identity_fields)
        for result in (*strict_results[1:], *lenient_results)
    ):
        fail(
            ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE,
            "Pathway evaluations did not retain one shared denominator and algorithm identity.",
            suggested_action="Recompute every dataset from the same pathway reference.",
            safe_details=(SafeDetail(name="pathway_id", value=reference.pathway_id),),
        )
    labels = tuple(item.label for item in inputs)
    return PathwayTargetComparison(
        reference=reference,
        strict=_pathway_mode_comparison(EvidenceMode.STRICT, labels, strict_results),
        lenient=_pathway_mode_comparison(EvidenceMode.LENIENT, labels, lenient_results),
        pathway_calculation_method=CalculationMethodReference(
            name=first.calculation_method,
            version=first.calculation_version,
        ),
        coverage_limits=first.limits,
        allow_global_or_overview=allow_global_or_overview,
    )


def _pathway_parameters(
    reference: PathwayKoReference,
    mode: EvidenceMode,
    input_index: int,
    organism_contexts: tuple[PathwayComparisonOrganismContext, ...],
    allow_global_or_overview: bool,
) -> PathwayCoverageParameters:
    input_context = PathwayInputContext()
    if reference.reference_namespace is PathwayReferenceNamespace.ORGANISM:
        input_context = PathwayInputContext(
            kind=PathwayInputKind.ORGANISM_GENE_CONTEXT,
            organism_gene_context=organism_contexts[input_index].gene_context,
        )
    return PathwayCoverageParameters(
        reference_namespace=reference.reference_namespace,
        evidence_mode=mode,
        input_context=input_context,
        allow_global_or_overview=allow_global_or_overview,
    )


def _pathway_mode_comparison(
    mode: EvidenceMode,
    labels: tuple[str, ...],
    results: tuple[PathwayCoverageResult, ...],
) -> PathwayModeComparison:
    outcomes = tuple(
        SetPathwayOutcome(
            input_index=index,
            label=labels[index],
            evaluation_status=result.evaluation_status,
            input_record_count=result.input_record_count,
            input_unique_ko_count=result.input_unique_ko_count,
            detected_reference_ko_count=result.detected_unique_ko_count,
            missing_reference_ko_count=result.missing_unique_ko_count,
            reference_unique_ko_count=result.reference_unique_ko_count,
            coverage_ratio=result.coverage_ratio,
            warnings=result.warnings,
        )
        for index, result in enumerate(results)
    )
    signatures = tuple(_pathway_outcome_signature(item) for item in outcomes)
    return PathwayModeComparison(
        evidence_mode=mode,
        outcomes=outcomes,
        outcomes_differ=any(item != signatures[0] for item in signatures[1:]),
        evaluated_in_set_indexes=tuple(
            item.input_index
            for item in outcomes
            if item.evaluation_status is PathwayCoverageStatus.EVALUATED
        ),
        not_evaluable_in_set_indexes=tuple(
            item.input_index
            for item in outcomes
            if item.evaluation_status is PathwayCoverageStatus.NOT_EVALUABLE
        ),
    )


def _pathway_outcome_signature(outcome: SetPathwayOutcome) -> tuple[object, ...]:
    return (
        outcome.evaluation_status,
        outcome.detected_reference_ko_count,
        outcome.missing_reference_ko_count,
        outcome.reference_unique_ko_count,
        outcome.coverage_ratio,
    )


__all__ = [
    "MODULE_COMPARISON_METHOD",
    "MODULE_COMPARISON_VERSION",
    "PATHWAY_COMPARISON_METHOD",
    "PATHWAY_COMPARISON_VERSION",
    "FunctionalComparisonLimits",
    "ModuleComparisonResult",
    "ModuleModeComparison",
    "ModuleTargetComparison",
    "PathwayComparisonOrganismContext",
    "PathwayComparisonResult",
    "PathwayModeComparison",
    "PathwayTargetComparison",
    "SetModuleOutcome",
    "SetPathwayOutcome",
    "compare_module_graphs",
    "compare_pathway_references",
]
