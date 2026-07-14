"""Pure, bounded resolution of references between supplied KEGG MODULE definitions."""

from collections.abc import Iterator

from kegg_mcp.analysis.contracts import (
    MODULE_RESOLVER_VERSION,
    ModuleAnalysisLimits,
    ModuleDefinition,
    ModuleDefinitionAst,
    ModuleDefinitionCollection,
    ModuleExpression,
    ModuleExpressionKind,
    ModuleParseLimits,
    ModuleParseResult,
    ModuleReferenceEdge,
    ModuleReferenceIssue,
    ModuleReferenceIssueKind,
    ModuleResolutionLimits,
    ResolvedModuleDefinition,
    ResolvedModuleGraph,
    SourceSpan,
)
from kegg_mcp.analysis.module_syntax import parse_module_definition

__all__ = ["resolve_module_definitions"]


def resolve_module_definitions(
    collection: ModuleDefinitionCollection,
    limits: ModuleAnalysisLimits | ModuleResolutionLimits | None = None,
    *,
    parse_limits: ModuleParseLimits | None = None,
) -> ResolvedModuleGraph:
    """Resolve the locally supplied definition graph without performing any I/O.

    References are traversed depth first in source order. Every definition is parsed at
    most once, and only definitions reachable within the configured bounds are returned.
    A reference that cannot be followed is retained as an edge, when the reference budget
    permits, and as a structured issue so downstream evaluation can fail closed.

    ``ModuleAnalysisLimits`` keeps parsing and resolution bounds together for normal
    analysis calls. A standalone ``ModuleResolutionLimits`` may be supplied by callers
    that want default parsing bounds; ``parse_limits`` can override those defaults.
    """
    parsing, resolution = _split_limits(limits, parse_limits=parse_limits)
    return _ModuleResolver(collection, parse_limits=parsing, limits=resolution).resolve()


def _split_limits(
    limits: ModuleAnalysisLimits | ModuleResolutionLimits | None,
    *,
    parse_limits: ModuleParseLimits | None,
) -> tuple[ModuleParseLimits, ModuleResolutionLimits]:
    if limits is None:
        analysis_limits = ModuleAnalysisLimits()
        parsing = parse_limits or analysis_limits.parsing
        resolution = analysis_limits.resolution
    elif isinstance(limits, ModuleAnalysisLimits):
        parsing = parse_limits or limits.parsing
        resolution = limits.resolution
    else:
        parsing = parse_limits or ModuleParseLimits()
        resolution = limits

    if parsing.max_ast_nodes > resolution.max_total_ast_nodes:
        raise ValueError(
            "parse max_ast_nodes must not exceed resolution max_total_ast_nodes; "
            "coordinate the parsing and resolution limits"
        )
    return parsing, resolution


class _ModuleResolver:
    """Mutable traversal state hidden behind the immutable public graph contract."""

    def __init__(
        self,
        collection: ModuleDefinitionCollection,
        *,
        parse_limits: ModuleParseLimits,
        limits: ModuleResolutionLimits,
    ) -> None:
        self._collection = collection
        self._definitions = {
            definition.module_id: definition for definition in collection.definitions
        }
        self._parse_limits = parse_limits
        self._limits = limits
        self._parse_cache: dict[str, ModuleParseResult] = {}
        self._resolved_by_id: dict[str, ResolvedModuleDefinition] = {}
        self._modules: list[ResolvedModuleDefinition] = []
        self._edges: list[ModuleReferenceEdge] = []
        self._issues: list[ModuleReferenceIssue] = []
        self._active_path: list[str] = []
        self._visited: set[str] = set()
        self._total_ast_nodes = 0
        self._reference_budget_exhausted = False

    def resolve(self) -> ResolvedModuleGraph:
        root_definition = self._definitions[self._collection.root_module_id]
        root = self._append_definition(root_definition)

        if root.parse_result.is_valid:
            self._visit(root_definition.module_id)

        return ResolvedModuleGraph(
            root_module_id=self._collection.root_module_id,
            modules=tuple(self._modules),
            edges=tuple(self._edges),
            issues=tuple(self._issues),
            total_ast_nodes=self._total_ast_nodes,
            resolver_version=MODULE_RESOLVER_VERSION,
            limits=self._limits,
        )

    def _visit(self, module_id: str) -> None:
        if module_id in self._visited or self._reference_budget_exhausted:
            return

        resolved = self._resolved_by_id[module_id]
        ast = resolved.parse_result.ast
        if not resolved.parse_result.is_valid or ast is None:
            return

        self._active_path.append(module_id)
        try:
            for target_id, source_span in _iter_module_references(ast):
                if not self._reserve_reference(
                    source_module_id=module_id,
                    target_module_id=target_id,
                    source_span=source_span,
                ):
                    break
                intended_path = (*self._active_path, target_id)

                if target_id in self._active_path:
                    self._issues.append(
                        ModuleReferenceIssue(
                            kind=ModuleReferenceIssueKind.CYCLE,
                            source_module_id=module_id,
                            target_module_id=target_id,
                            path=intended_path,
                            source_span=source_span,
                            message=(
                                "Module reference cycle detected; inspect path for the complete "
                                "traversal."
                            ),
                        )
                    )
                    continue

                target_depth = len(self._active_path)
                if target_depth > self._limits.max_reference_depth:
                    self._issues.append(
                        ModuleReferenceIssue(
                            kind=ModuleReferenceIssueKind.DEPTH_LIMIT,
                            source_module_id=module_id,
                            target_module_id=target_id,
                            path=intended_path,
                            source_span=source_span,
                            message=(
                                f"Resolving {target_id} would exceed "
                                f"max_reference_depth={self._limits.max_reference_depth}."
                            ),
                        )
                    )
                    continue

                definition = self._definitions.get(target_id)
                if definition is None:
                    self._issues.append(
                        ModuleReferenceIssue(
                            kind=ModuleReferenceIssueKind.UNRESOLVED,
                            source_module_id=module_id,
                            target_module_id=target_id,
                            path=intended_path,
                            source_span=source_span,
                            message=(
                                f"Definition for referenced module {target_id} was not supplied."
                            ),
                        )
                    )
                    continue

                target = self._resolved_by_id.get(target_id)
                if target is None:
                    target = self._try_append_referenced_definition(
                        definition,
                        source_module_id=module_id,
                        source_span=source_span,
                        path=intended_path,
                    )
                    if target is None:
                        continue

                if not target.parse_result.is_valid:
                    self._issues.append(
                        ModuleReferenceIssue(
                            kind=ModuleReferenceIssueKind.INVALID_DEFINITION,
                            source_module_id=module_id,
                            target_module_id=target_id,
                            path=intended_path,
                            source_span=source_span,
                            message=(
                                f"Referenced module {target_id} has an invalid MODULE definition."
                            ),
                        )
                    )
                    continue

                self._visit(target_id)
                if self._reference_budget_exhausted:
                    break
        finally:
            self._active_path.pop()
            self._visited.add(module_id)

    def _reserve_reference(
        self,
        *,
        source_module_id: str,
        target_module_id: str,
        source_span: SourceSpan,
    ) -> bool:
        intended_path = (*self._active_path, target_module_id)
        if len(self._edges) >= self._limits.max_references:
            self._issues.append(
                ModuleReferenceIssue(
                    kind=ModuleReferenceIssueKind.REFERENCE_LIMIT,
                    source_module_id=source_module_id,
                    target_module_id=target_module_id,
                    path=intended_path,
                    source_span=source_span,
                    message=(
                        "Reference traversal stopped after reaching "
                        f"max_references={self._limits.max_references}."
                    ),
                )
            )
            self._reference_budget_exhausted = True
            return False

        self._edges.append(
            ModuleReferenceEdge(
                source_module_id=source_module_id,
                target_module_id=target_module_id,
                source_span=source_span,
            )
        )
        return True

    def _try_append_referenced_definition(
        self,
        definition: ModuleDefinition,
        *,
        source_module_id: str,
        source_span: SourceSpan,
        path: tuple[str, ...],
    ) -> ResolvedModuleDefinition | None:
        if len(self._modules) >= self._limits.max_modules:
            self._issues.append(
                ModuleReferenceIssue(
                    kind=ModuleReferenceIssueKind.MODULE_LIMIT,
                    source_module_id=source_module_id,
                    target_module_id=definition.module_id,
                    path=path,
                    source_span=source_span,
                    message=(
                        f"Resolving {definition.module_id} would exceed "
                        f"max_modules={self._limits.max_modules}."
                    ),
                )
            )
            return None

        parse_result = self._parse(definition)
        projected_node_count = self._total_ast_nodes + parse_result.ast_node_count
        if projected_node_count > self._limits.max_total_ast_nodes:
            self._issues.append(
                ModuleReferenceIssue(
                    kind=ModuleReferenceIssueKind.TOTAL_NODE_LIMIT,
                    source_module_id=source_module_id,
                    target_module_id=definition.module_id,
                    path=path,
                    source_span=source_span,
                    message=(
                        f"Resolving {definition.module_id} would exceed "
                        f"max_total_ast_nodes={self._limits.max_total_ast_nodes}."
                    ),
                )
            )
            return None

        return self._append_definition(definition, parse_result=parse_result)

    def _append_definition(
        self,
        definition: ModuleDefinition,
        *,
        parse_result: ModuleParseResult | None = None,
    ) -> ResolvedModuleDefinition:
        resolved = ResolvedModuleDefinition(
            definition=definition,
            parse_result=parse_result or self._parse(definition),
        )
        self._modules.append(resolved)
        self._resolved_by_id[definition.module_id] = resolved
        self._total_ast_nodes += resolved.parse_result.ast_node_count
        return resolved

    def _parse(self, definition: ModuleDefinition) -> ModuleParseResult:
        cached = self._parse_cache.get(definition.module_id)
        if cached is not None:
            return cached
        parsed = parse_module_definition(definition.definition, limits=self._parse_limits)
        self._parse_cache[definition.module_id] = parsed
        return parsed


def _iter_module_references(ast: ModuleDefinitionAst) -> Iterator[tuple[str, SourceSpan]]:
    for block in ast.required_blocks:
        yield from _iter_expression_references(block)


def _iter_expression_references(
    expression: ModuleExpression,
) -> Iterator[tuple[str, SourceSpan]]:
    if expression.kind is ModuleExpressionKind.MODULE_REFERENCE:
        if expression.value is None:  # pragma: no cover - guarded by the contract
            raise AssertionError("module-reference expressions require a value")
        yield expression.value, expression.span
    for child in expression.children:
        yield from _iter_expression_references(child)
