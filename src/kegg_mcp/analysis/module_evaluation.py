"""Pure, bounded evaluation of resolved KEGG MODULE definition graphs."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum

from kegg_mcp.analysis.contracts import (
    MODULE_CALCULATION_METHOD,
    MODULE_CALCULATION_VERSION,
    CalculationMethodReference,
    EvaluatedDefinitionProvenance,
    MinimalMissingAlternatives,
    MissingKoAlternative,
    ModuleAnalysisLimits,
    ModuleBlockResult,
    ModuleBlockState,
    ModuleEvaluationLimits,
    ModuleEvaluationResult,
    ModuleEvaluationStatus,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleReferenceIssue,
    ModuleReferenceIssueKind,
    ModuleWarning,
    ModuleWarningCode,
    OptionalComponentResult,
    OptionalComponentState,
    PairedModuleEvaluation,
    ResolvedModuleDefinition,
    ResolvedModuleGraph,
    SourceSpan,
    UncertainSupport,
)
from kegg_mcp.domain.annotations import (
    AnnotationDataset,
    EvidenceMode,
    NormalizedStatus,
    build_ko_evidence_view,
    select_ko_ids,
)

__all__ = ["evaluate_module", "evaluate_module_pair"]


class _Truth(Enum):
    TRUE = 1
    FALSE = 0
    UNKNOWN = -1


@dataclass(frozen=True, slots=True)
class _Missing:
    alternatives: tuple[frozenset[str], ...]
    truncated: bool = False
    combination_expansions: int = 0


@dataclass(frozen=True, slots=True)
class _ExpressionResult:
    truth: _Truth
    matched: frozenset[str] = frozenset()
    causal: frozenset[str] = frozenset()
    missing: _Missing | None = None
    required_component_count: int = 0


@dataclass(frozen=True, slots=True)
class _BlockEvaluation:
    block_index: int
    source_span: SourceSpan
    result: _ExpressionResult


@dataclass(frozen=True, slots=True)
class _EvaluationRun:
    result: ModuleEvaluationResult
    blocks: tuple[_BlockEvaluation, ...]


@dataclass(slots=True)
class _EvaluationContext:
    graph: ResolvedModuleGraph
    ko_ids: frozenset[str]
    limits: ModuleEvaluationLimits
    modules: dict[str, ResolvedModuleDefinition] = field(init=False)
    issues_by_reference: dict[tuple[str, str, int, int], tuple[ModuleReferenceIssue, ...]] = field(
        init=False
    )
    optional_results: dict[tuple[str, int, int], OptionalComponentResult] = field(
        default_factory=lambda: dict[tuple[str, int, int], OptionalComponentResult]()
    )
    module_result_cache: dict[str, _ExpressionResult] = field(
        default_factory=lambda: dict[str, _ExpressionResult]()
    )
    combination_expansions: int = 0
    optional_output_truncated: bool = False

    def __post_init__(self) -> None:
        self.modules = {item.definition.module_id: item for item in self.graph.modules}
        grouped: dict[tuple[str, str, int, int], list[ModuleReferenceIssue]] = defaultdict(list)
        for issue in self.graph.issues:
            key = (
                issue.source_module_id,
                issue.target_module_id,
                issue.source_span.start_offset,
                issue.source_span.end_offset,
            )
            grouped[key].append(issue)
        self.issues_by_reference = {key: tuple(value) for key, value in grouped.items()}


_WARNING_MESSAGES = {
    ModuleWarningCode.UNSUPPORTED_CONTENT: (
        "At least one reachable definition contains unsupported or invalid content; "
        "the evaluator failed closed."
    ),
    ModuleWarningCode.UNRESOLVED_REFERENCE: (
        "At least one referenced MODULE definition could not be evaluated."
    ),
    ModuleWarningCode.REFERENCE_CYCLE: (
        "At least one MODULE reference cycle was retained as not evaluable."
    ),
    ModuleWarningCode.REFERENCE_LIMIT: (
        "At least one MODULE reference could not be followed within the configured bounds."
    ),
    ModuleWarningCode.PARTIAL_EVALUATION: (
        "Some required top-level blocks are not evaluable; block coverage is not reported."
    ),
    ModuleWarningCode.NO_REQUIRED_COMPONENT: (
        "The definition contains no evaluable required component after excluding optional terms."
    ),
    ModuleWarningCode.MISSING_ALTERNATIVES_TRUNCATED: (
        "Minimal missing KO alternatives were truncated within the configured bounds."
    ),
    ModuleWarningCode.OUTPUT_PREVIEW_TRUNCATED: (
        "One or more block or optional-component previews were truncated."
    ),
    ModuleWarningCode.UNCERTAIN_SUPPORT_TRUNCATED: (
        "Policy-defined uncertain support was truncated within the configured bounds."
    ),
    ModuleWarningCode.STALE_DEFINITION: (
        "At least one evaluated MODULE definition was retrieved from a stale local cache entry."
    ),
}


def evaluate_module(
    graph: ResolvedModuleGraph,
    dataset: AnnotationDataset,
    evidence_mode: EvidenceMode,
    limits: ModuleAnalysisLimits | ModuleEvaluationLimits | None = None,
) -> ModuleEvaluationResult:
    """Evaluate one resolved MODULE graph against strict or lenient KO evidence.

    Lenient mode contains accepted K numbers plus only records explicitly normalized as
    uncertain by the dataset's named policy. Rejected and unclassified predictions are
    never selected. When lenient evidence newly completes a block, the returned support
    identifies the corresponding uncertain records.
    """
    analysis_limits = _coerce_limits(graph, limits)
    view = build_ko_evidence_view(dataset)
    selected = frozenset(select_ko_ids(view, evidence_mode))
    run = _evaluate_mode(graph, dataset, evidence_mode, selected, analysis_limits)
    if evidence_mode is EvidenceMode.STRICT:
        return run.result

    strict_selected = frozenset(select_ko_ids(view, EvidenceMode.STRICT))
    strict = _evaluate_mode(
        graph,
        dataset,
        EvidenceMode.STRICT,
        strict_selected,
        analysis_limits,
    )
    return _attach_uncertain_support(
        strict,
        run,
        dataset,
        accepted_kos=frozenset(view.accepted_kos),
        uncertain_kos=frozenset(view.uncertain_kos),
    ).result


def evaluate_module_pair(
    graph: ResolvedModuleGraph,
    dataset: AnnotationDataset,
    limits: ModuleAnalysisLimits | ModuleEvaluationLimits | None = None,
) -> PairedModuleEvaluation:
    """Return separate strict and policy-defined lenient MODULE evaluations."""
    analysis_limits = _coerce_limits(graph, limits)
    view = build_ko_evidence_view(dataset)
    strict = _evaluate_mode(
        graph,
        dataset,
        EvidenceMode.STRICT,
        frozenset(select_ko_ids(view, EvidenceMode.STRICT)),
        analysis_limits,
    )
    lenient = _evaluate_mode(
        graph,
        dataset,
        EvidenceMode.LENIENT,
        frozenset(select_ko_ids(view, EvidenceMode.LENIENT)),
        analysis_limits,
    )
    lenient = _attach_uncertain_support(
        strict,
        lenient,
        dataset,
        accepted_kos=frozenset(view.accepted_kos),
        uncertain_kos=frozenset(view.uncertain_kos),
    )

    newly_completed = tuple(
        block.block_index
        for strict_block, block in zip(strict.blocks, lenient.blocks, strict=True)
        if strict_block.result.truth is not _Truth.TRUE and block.result.truth is _Truth.TRUE
    )
    preview_limit = analysis_limits.evaluation.max_block_previews
    newly_completed_preview = newly_completed[:preview_limit]
    changed = (
        lenient.result.completed_required_blocks > strict.result.completed_required_blocks
        or any(
            getattr(strict.result, field_name) != getattr(lenient.result, field_name)
            for field_name in ("evaluation_status", "is_complete", "block_coverage")
        )
    )
    return PairedModuleEvaluation(
        strict=strict.result,
        lenient=lenient.result,
        strict_to_lenient_changed=changed,
        newly_completed_block_indexes=newly_completed_preview,
        newly_completed_blocks_truncated=len(newly_completed) > preview_limit,
    )


def _coerce_limits(
    graph: ResolvedModuleGraph,
    limits: ModuleAnalysisLimits | ModuleEvaluationLimits | None,
) -> ModuleAnalysisLimits:
    root = _root_module(graph)
    parsing_limits = root.parse_result.limits
    if isinstance(limits, ModuleAnalysisLimits):
        if limits.parsing != parsing_limits or limits.resolution != graph.limits:
            raise ValueError("analysis parsing and resolution limits must match the resolved graph")
        return limits
    return ModuleAnalysisLimits(
        parsing=parsing_limits,
        resolution=graph.limits,
        evaluation=limits or ModuleEvaluationLimits(),
    )


def _evaluate_mode(
    graph: ResolvedModuleGraph,
    dataset: AnnotationDataset,
    evidence_mode: EvidenceMode,
    ko_ids: frozenset[str],
    limits: ModuleAnalysisLimits,
) -> _EvaluationRun:
    root = _root_module(graph)
    context = _EvaluationContext(graph=graph, ko_ids=ko_ids, limits=limits.evaluation)
    ast = root.parse_result.ast
    warning_codes = _reference_warning_codes(graph.issues)
    if any(item.definition.provenance.is_stale for item in graph.modules):
        warning_codes.add(ModuleWarningCode.STALE_DEFINITION)

    globally_unsupported = any(
        resolved.parse_result.ast is not None
        and _contains_unsupported(resolved.parse_result.ast.required_blocks)
        for resolved in graph.modules
    )
    root_invalid = not root.parse_result.is_valid or ast is None
    root_limited = any(
        issue.source_module_id == graph.root_module_id
        and issue.target_module_id == graph.root_module_id
        and issue.kind is ModuleReferenceIssueKind.TOTAL_NODE_LIMIT
        for issue in graph.issues
    )

    blocks: list[_BlockEvaluation] = []
    if ast is not None:
        if globally_unsupported or root_invalid or root_limited:
            warning_codes.add(ModuleWarningCode.UNSUPPORTED_CONTENT)
            _collect_not_evaluable_optionals(context)
            blocks.extend(
                _BlockEvaluation(
                    block_index=index,
                    source_span=block.span,
                    result=_ExpressionResult(
                        truth=_Truth.UNKNOWN,
                        required_component_count=1,
                    ),
                )
                for index, block in enumerate(ast.required_blocks, start=1)
            )
        else:
            for index, block in enumerate(ast.required_blocks, start=1):
                result = _evaluate_expression(
                    block,
                    source_module_id=graph.root_module_id,
                    context=context,
                    active_modules=(graph.root_module_id,),
                )
                if result.required_component_count == 0:
                    result = _ExpressionResult(truth=_Truth.UNKNOWN)
                    warning_codes.add(ModuleWarningCode.NO_REQUIRED_COMPONENT)
                blocks.append(
                    _BlockEvaluation(
                        block_index=index,
                        source_span=block.span,
                        result=result,
                    )
                )
    elif root_invalid:
        warning_codes.add(ModuleWarningCode.UNSUPPORTED_CONTENT)

    completed = sum(block.result.truth is _Truth.TRUE for block in blocks)
    evaluable = sum(block.result.truth is not _Truth.UNKNOWN for block in blocks)
    required_count = len(blocks)
    status = _evaluation_status(completed, evaluable, required_count)
    if status is ModuleEvaluationStatus.PARTIALLY_EVALUABLE:
        warning_codes.add(ModuleWarningCode.PARTIAL_EVALUATION)

    complete_block_indexes = tuple(
        block.block_index for block in blocks if block.result.truth is _Truth.TRUE
    )
    missing_private = tuple(block for block in blocks if block.result.truth is _Truth.FALSE)
    unknown_private = tuple(block for block in blocks if block.result.truth is _Truth.UNKNOWN)
    preview_limit = limits.evaluation.max_block_previews
    missing_blocks = tuple(
        _public_block(block, limits.evaluation) for block in missing_private[:preview_limit]
    )
    unknown_blocks = tuple(
        _public_block(block, limits.evaluation) for block in unknown_private[:preview_limit]
    )
    output_truncated = (
        any(
            len(items) > preview_limit
            for items in (complete_block_indexes, missing_private, unknown_private)
        )
        or context.optional_output_truncated
        or any(len(block.result.matched) > limits.evaluation.max_matched_ko_ids for block in blocks)
    )
    if output_truncated:
        warning_codes.add(ModuleWarningCode.OUTPUT_PREVIEW_TRUNCATED)
    if any(
        block.result.missing is not None
        and (
            block.result.missing.truncated
            or len(block.result.missing.alternatives) > limits.evaluation.max_missing_alternatives
        )
        for block in missing_private
    ):
        warning_codes.add(ModuleWarningCode.MISSING_ALTERNATIVES_TRUNCATED)

    result = ModuleEvaluationResult(
        module_id=root.definition.module_id,
        module_name=root.definition.module_name,
        dataset_id=dataset.dataset_id,
        decision_policy=dataset.import_report.decision_policy,
        evidence_mode=evidence_mode,
        evidence_ko_count=len(ko_ids),
        evaluation_status=status,
        is_complete=(
            True
            if status is ModuleEvaluationStatus.COMPLETE
            else False
            if status is ModuleEvaluationStatus.INCOMPLETE
            else None
        ),
        block_coverage=(
            completed / required_count
            if required_count > 0 and evaluable == required_count
            else None
        ),
        completed_required_blocks=completed,
        evaluable_required_blocks=evaluable,
        required_block_count=required_count,
        present_blocks_preview=complete_block_indexes[:preview_limit],
        missing_blocks_preview=missing_blocks,
        not_evaluable_blocks_preview=unknown_blocks,
        optional_components=tuple(context.optional_results.values()),
        uncertain_support=(),
        unresolved_references=graph.issues,
        calculation_method=CalculationMethodReference(
            name=MODULE_CALCULATION_METHOD,
            version=MODULE_CALCULATION_VERSION,
        ),
        warnings=_warnings(warning_codes),
        reference_retrieval_provenance=graph.retrieval_provenance,
        provenance=tuple(
            EvaluatedDefinitionProvenance(
                module_id=item.definition.module_id,
                provenance=item.definition.provenance,
            )
            for item in graph.modules
        ),
        limits=limits,
    )
    return _EvaluationRun(result=result, blocks=tuple(blocks))


def _evaluate_expression(
    expression: ModuleExpression,
    *,
    source_module_id: str,
    context: _EvaluationContext,
    active_modules: tuple[str, ...],
) -> _ExpressionResult:
    kind = expression.kind
    if kind is ModuleExpressionKind.KO:
        ko_id = _leaf_value(expression)
        if ko_id in context.ko_ids:
            present = frozenset((ko_id,))
            return _ExpressionResult(
                truth=_Truth.TRUE,
                matched=present,
                causal=present,
                required_component_count=1,
            )
        return _ExpressionResult(
            truth=_Truth.FALSE,
            missing=_Missing(alternatives=(frozenset((ko_id,)),)),
            required_component_count=1,
        )

    if kind is ModuleExpressionKind.MODULE_REFERENCE:
        return _evaluate_reference(
            expression,
            source_module_id=source_module_id,
            context=context,
            active_modules=active_modules,
        )

    if kind is ModuleExpressionKind.UNSUPPORTED:
        return _ExpressionResult(truth=_Truth.UNKNOWN, required_component_count=1)

    if kind is ModuleExpressionKind.GROUP:
        return _evaluate_expression(
            expression.children[0],
            source_module_id=source_module_id,
            context=context,
            active_modules=active_modules,
        )

    if kind is ModuleExpressionKind.OPTIONAL:
        child = _evaluate_expression(
            expression.children[0],
            source_module_id=source_module_id,
            context=context,
            active_modules=active_modules,
        )
        _record_optional(expression, child, source_module_id=source_module_id, context=context)
        return _ExpressionResult(truth=_Truth.TRUE)

    children = tuple(
        _evaluate_expression(
            child,
            source_module_id=source_module_id,
            context=context,
            active_modules=active_modules,
        )
        for child in expression.children
    )
    if kind is ModuleExpressionKind.AND:
        return _evaluate_and(children, context)
    if kind is ModuleExpressionKind.OR:
        return _evaluate_or(children, context)
    raise AssertionError(f"unsupported expression kind: {kind}")


def _evaluate_reference(
    expression: ModuleExpression,
    *,
    source_module_id: str,
    context: _EvaluationContext,
    active_modules: tuple[str, ...],
) -> _ExpressionResult:
    target_id = _leaf_value(expression)
    issue_key = (
        source_module_id,
        target_id,
        expression.span.start_offset,
        expression.span.end_offset,
    )
    if issue_key in context.issues_by_reference or target_id in active_modules:
        return _ExpressionResult(truth=_Truth.UNKNOWN, required_component_count=1)

    target = context.modules.get(target_id)
    if target is None:
        return _ExpressionResult(truth=_Truth.UNKNOWN, required_component_count=1)
    parse_result = target.parse_result
    if not parse_result.is_valid or parse_result.ast is None:
        return _ExpressionResult(truth=_Truth.UNKNOWN, required_component_count=1)

    cached = context.module_result_cache.get(target_id)
    if cached is not None:
        return cached

    target_results = tuple(
        _evaluate_expression(
            block,
            source_module_id=target_id,
            context=context,
            active_modules=(*active_modules, target_id),
        )
        for block in parse_result.ast.required_blocks
    )
    if not target_results:
        return _ExpressionResult(truth=_Truth.UNKNOWN)
    result = _evaluate_and(target_results, context)
    if result.required_component_count == 0:
        result = _ExpressionResult(truth=_Truth.UNKNOWN)
    context.module_result_cache[target_id] = result
    return result


def _evaluate_and(
    children: tuple[_ExpressionResult, ...],
    context: _EvaluationContext,
) -> _ExpressionResult:
    matched = _union_ko_sets(child.matched for child in children)
    required_count = sum(child.required_component_count for child in children)
    false_children = tuple(child for child in children if child.truth is _Truth.FALSE)
    if false_children:
        if any(child.truth is _Truth.UNKNOWN for child in children):
            missing = _Missing(
                alternatives=(),
                truncated=True,
                combination_expansions=context.combination_expansions,
            )
        elif len(false_children) == 1:
            missing = false_children[0].missing
        else:
            missing = _combine_missing(false_children, context)
        return _ExpressionResult(
            truth=_Truth.FALSE,
            matched=matched,
            missing=missing,
            required_component_count=required_count,
        )
    if any(child.truth is _Truth.UNKNOWN for child in children):
        return _ExpressionResult(
            truth=_Truth.UNKNOWN,
            matched=matched,
            required_component_count=required_count,
        )
    causal = _union_ko_sets(child.causal for child in children)
    return _ExpressionResult(
        truth=_Truth.TRUE,
        matched=matched,
        causal=causal,
        required_component_count=required_count,
    )


def _evaluate_or(
    children: tuple[_ExpressionResult, ...],
    context: _EvaluationContext,
) -> _ExpressionResult:
    matched = _union_ko_sets(child.matched for child in children)
    required_count = sum(child.required_component_count for child in children)
    true_children = tuple(child for child in children if child.truth is _Truth.TRUE)
    if true_children:
        causal = _union_ko_sets(child.causal for child in true_children)
        return _ExpressionResult(
            truth=_Truth.TRUE,
            matched=matched,
            causal=causal,
            required_component_count=required_count,
        )
    if any(child.truth is _Truth.UNKNOWN for child in children):
        return _ExpressionResult(
            truth=_Truth.UNKNOWN,
            matched=matched,
            required_component_count=required_count,
        )
    return _ExpressionResult(
        truth=_Truth.FALSE,
        matched=matched,
        missing=_union_missing(children, context),
        required_component_count=required_count,
    )


def _combine_missing(
    children: tuple[_ExpressionResult, ...],
    context: _EvaluationContext,
) -> _Missing:
    alternatives: tuple[frozenset[str], ...] = (frozenset(),)
    oversized = False
    for child in children:
        child_missing = child.missing
        if child_missing is None or child_missing.truncated or not child_missing.alternatives:
            return _Missing(
                alternatives=(),
                truncated=True,
                combination_expansions=context.combination_expansions,
            )
        combined: list[frozenset[str]] = []
        for left in alternatives:
            for right in child_missing.alternatives:
                if not _reserve_combination(context):
                    return _Missing(
                        alternatives=(),
                        truncated=True,
                        combination_expansions=context.combination_expansions,
                    )
                candidate = left | right
                if len(candidate) > context.limits.max_ko_ids_per_alternative:
                    oversized = True
                    continue
                _insert_antichain(combined, candidate)
        alternatives = tuple(sorted(combined, key=_alternative_sort_key))
        if not alternatives:
            break
    return _Missing(
        alternatives=alternatives,
        truncated=oversized or not alternatives,
        combination_expansions=context.combination_expansions,
    )


def _union_missing(
    children: tuple[_ExpressionResult, ...],
    context: _EvaluationContext,
) -> _Missing:
    alternatives: list[frozenset[str]] = []
    oversized = False
    for child in children:
        missing = child.missing
        if missing is None:
            continue
        if missing.truncated:
            return _Missing(
                alternatives=(),
                truncated=True,
                combination_expansions=context.combination_expansions,
            )
        for candidate in missing.alternatives:
            if not _reserve_combination(context):
                return _Missing(
                    alternatives=(),
                    truncated=True,
                    combination_expansions=context.combination_expansions,
                )
            if len(candidate) > context.limits.max_ko_ids_per_alternative:
                oversized = True
                continue
            _insert_antichain(alternatives, candidate)
    return _Missing(
        alternatives=tuple(sorted(alternatives, key=_alternative_sort_key)),
        truncated=oversized or not alternatives,
        combination_expansions=context.combination_expansions,
    )


def _reserve_combination(context: _EvaluationContext) -> bool:
    if context.combination_expansions >= context.limits.max_combination_expansions:
        return False
    context.combination_expansions += 1
    return True


def _insert_antichain(alternatives: list[frozenset[str]], candidate: frozenset[str]) -> None:
    if any(existing <= candidate for existing in alternatives):
        return
    alternatives[:] = [existing for existing in alternatives if not candidate < existing]
    alternatives.append(candidate)


def _alternative_sort_key(value: frozenset[str]) -> tuple[int, tuple[str, ...]]:
    return len(value), tuple(sorted(value))


def _union_ko_sets(values: Iterable[frozenset[str]]) -> frozenset[str]:
    result: set[str] = set()
    for value in values:
        result.update(value)
    return frozenset(result)


def _record_optional(
    expression: ModuleExpression,
    child: _ExpressionResult,
    *,
    source_module_id: str,
    context: _EvaluationContext,
) -> None:
    key = (source_module_id, expression.span.start_offset, expression.span.end_offset)
    if key in context.optional_results:
        return
    if len(context.optional_results) >= context.limits.max_optional_components:
        context.optional_output_truncated = True
        return
    if child.truth is _Truth.TRUE:
        state = OptionalComponentState.PRESENT
    elif child.truth is _Truth.UNKNOWN:
        state = OptionalComponentState.NOT_EVALUABLE
    elif child.matched:
        state = OptionalComponentState.PARTIALLY_PRESENT
    else:
        state = OptionalComponentState.ABSENT
    matched = tuple(sorted(child.matched))
    selected_matched = matched[: context.limits.max_matched_ko_ids]
    matched_truncated = len(matched) > len(selected_matched)
    context.optional_output_truncated = context.optional_output_truncated or matched_truncated
    context.optional_results[key] = OptionalComponentResult(
        component_index=len(context.optional_results) + 1,
        source_module_id=source_module_id,
        source_span=expression.span,
        state=state,
        matched_ko_ids=selected_matched,
        matched_ko_ids_truncated=matched_truncated,
    )


def _collect_not_evaluable_optionals(context: _EvaluationContext) -> None:
    for resolved in context.graph.modules:
        ast = resolved.parse_result.ast
        if ast is None:
            continue
        for expression in _walk_expressions(ast.required_blocks):
            if expression.kind is not ModuleExpressionKind.OPTIONAL:
                continue
            key = (
                resolved.definition.module_id,
                expression.span.start_offset,
                expression.span.end_offset,
            )
            if key in context.optional_results:
                continue
            if len(context.optional_results) >= context.limits.max_optional_components:
                context.optional_output_truncated = True
                return
            context.optional_results[key] = OptionalComponentResult(
                component_index=len(context.optional_results) + 1,
                source_module_id=resolved.definition.module_id,
                source_span=expression.span,
                state=OptionalComponentState.NOT_EVALUABLE,
                matched_ko_ids=(),
            )


def _attach_uncertain_support(
    strict: _EvaluationRun,
    lenient: _EvaluationRun,
    dataset: AnnotationDataset,
    *,
    accepted_kos: frozenset[str],
    uncertain_kos: frozenset[str],
) -> _EvaluationRun:
    causal_blocks: dict[str, set[int]] = defaultdict(set)
    eligible_uncertain = uncertain_kos - accepted_kos
    for strict_block, lenient_block in zip(strict.blocks, lenient.blocks, strict=True):
        if (
            strict_block.result.truth is _Truth.TRUE
            or lenient_block.result.truth is not _Truth.TRUE
        ):
            continue
        for ko_id in lenient_block.result.causal & eligible_uncertain:
            causal_blocks[ko_id].add(lenient_block.block_index)

    records_by_ko: dict[str, list[str]] = defaultdict(list)
    for record in dataset.records:
        if record.normalized_status is NormalizedStatus.UNCERTAIN and record.ko_id in causal_blocks:
            records_by_ko[record.ko_id].append(record.record_id)

    evaluation_limits = lenient.result.limits.evaluation
    support: list[UncertainSupport] = []
    support_truncated = False
    for ko_id in sorted(causal_blocks):
        if len(support) >= evaluation_limits.max_uncertain_support_items:
            support_truncated = True
            break
        record_ids = tuple(sorted(set(records_by_ko[ko_id])))
        if not record_ids:
            continue
        selected_records = record_ids[: evaluation_limits.max_uncertain_records_per_ko]
        records_truncated = len(record_ids) > len(selected_records)
        block_indexes = tuple(sorted(causal_blocks[ko_id]))
        selected_block_indexes = block_indexes[: evaluation_limits.max_block_previews]
        blocks_truncated = len(block_indexes) > len(selected_block_indexes)
        support_truncated = support_truncated or records_truncated or blocks_truncated
        support.append(
            UncertainSupport(
                ko_id=ko_id,
                record_ids=selected_records,
                required_block_indexes=selected_block_indexes,
                record_ids_truncated=records_truncated,
                required_block_indexes_truncated=blocks_truncated,
            )
        )

    warning_codes = {warning.code for warning in lenient.result.warnings}
    if support_truncated:
        warning_codes.add(ModuleWarningCode.UNCERTAIN_SUPPORT_TRUNCATED)
    updated = lenient.result.model_copy(
        update={
            "uncertain_support": tuple(support),
            "warnings": _warnings(warning_codes),
        }
    )
    return _EvaluationRun(result=updated, blocks=lenient.blocks)


def _public_block(
    block: _BlockEvaluation,
    limits: ModuleEvaluationLimits,
) -> ModuleBlockResult:
    truth = block.result.truth
    if truth is _Truth.TRUE:
        state = ModuleBlockState.COMPLETE
        missing = None
    elif truth is _Truth.FALSE:
        state = ModuleBlockState.INCOMPLETE
        private_missing = block.result.missing or _Missing(alternatives=())
        sorted_alternatives = tuple(sorted(private_missing.alternatives, key=_alternative_sort_key))
        selected_alternatives = sorted_alternatives[: limits.max_missing_alternatives]
        missing = MinimalMissingAlternatives(
            alternatives=tuple(
                MissingKoAlternative(ko_ids=tuple(sorted(alternative)))
                for alternative in selected_alternatives
                if alternative
            ),
            truncated=(
                private_missing.truncated or len(sorted_alternatives) > len(selected_alternatives)
            ),
            combination_expansions=private_missing.combination_expansions,
        )
    else:
        state = ModuleBlockState.NOT_EVALUABLE
        missing = None
    matched = tuple(sorted(block.result.matched))
    selected_matched = matched[: limits.max_matched_ko_ids]
    return ModuleBlockResult(
        block_index=block.block_index,
        state=state,
        source_span=block.source_span,
        matched_ko_ids=selected_matched,
        missing=missing,
        matched_ko_ids_truncated=len(matched) > len(selected_matched),
    )


def _evaluation_status(
    completed: int, evaluable: int, required_count: int
) -> ModuleEvaluationStatus:
    if required_count == 0 or evaluable == 0:
        return ModuleEvaluationStatus.NOT_EVALUABLE
    if evaluable < required_count:
        return ModuleEvaluationStatus.PARTIALLY_EVALUABLE
    if completed == required_count:
        return ModuleEvaluationStatus.COMPLETE
    return ModuleEvaluationStatus.INCOMPLETE


def _reference_warning_codes(
    issues: tuple[ModuleReferenceIssue, ...],
) -> set[ModuleWarningCode]:
    codes: set[ModuleWarningCode] = set()
    for issue in issues:
        if issue.kind in {
            ModuleReferenceIssueKind.UNRESOLVED,
            ModuleReferenceIssueKind.INVALID_DEFINITION,
        }:
            codes.add(ModuleWarningCode.UNRESOLVED_REFERENCE)
        elif issue.kind is ModuleReferenceIssueKind.CYCLE:
            codes.add(ModuleWarningCode.REFERENCE_CYCLE)
        else:
            codes.add(ModuleWarningCode.REFERENCE_LIMIT)
    return codes


def _warnings(codes: set[ModuleWarningCode]) -> tuple[ModuleWarning, ...]:
    return tuple(
        ModuleWarning(code=code, message=_WARNING_MESSAGES[code])
        for code in ModuleWarningCode
        if code in codes
    )


def _contains_unsupported(blocks: tuple[ModuleExpression, ...]) -> bool:
    return any(
        expression.kind is ModuleExpressionKind.UNSUPPORTED
        for expression in _walk_expressions(blocks)
    )


def _walk_expressions(
    roots: tuple[ModuleExpression, ...],
) -> tuple[ModuleExpression, ...]:
    found: list[ModuleExpression] = []
    stack = list(reversed(roots))
    while stack:
        expression = stack.pop()
        found.append(expression)
        stack.extend(reversed(expression.children))
    return tuple(found)


def _leaf_value(expression: ModuleExpression) -> str:
    if expression.value is None:
        raise AssertionError("leaf expressions require a value")
    return expression.value


def _root_module(graph: ResolvedModuleGraph) -> ResolvedModuleDefinition:
    for item in graph.modules:
        if item.definition.module_id == graph.root_module_id:
            return item
    raise AssertionError("resolved graph contract requires the root module")
