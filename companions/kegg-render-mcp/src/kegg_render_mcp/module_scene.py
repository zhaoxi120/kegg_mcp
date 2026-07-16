"""Deterministic MODULE logic scenes from authoritative core AST and state vectors."""

from __future__ import annotations

from dataclasses import dataclass

from kegg_mcp.analysis import ModuleExpression, ResolvedModuleDefinition
from kegg_mcp.domain import AnalysisUnit
from kegg_mcp.services.render_contracts import ModuleRenderTarget

from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError


@dataclass(frozen=True, slots=True)
class ModuleNode:
    node_id: int
    parent_id: int | None
    depth: int
    label: str
    kind: str
    optional: bool
    unsupported: bool


@dataclass(frozen=True, slots=True)
class ModuleBlockPanel:
    block_index: int
    strict_state: str
    lenient_state: str
    uncertain_support_ko_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ModuleOptionalPanel:
    component_index: int
    source_module_id: str
    strict_state: str
    lenient_state: str


@dataclass(frozen=True, slots=True)
class ModuleReferencePanel:
    source_module_id: str
    target_module_id: str


@dataclass(frozen=True, slots=True)
class ModuleScene:
    target_id: str
    title: str
    analysis_unit: str
    width: int
    height: int
    nodes: tuple[ModuleNode, ...]
    blocks: tuple[ModuleBlockPanel, ...]
    optional_components: tuple[ModuleOptionalPanel, ...]
    reference_edges: tuple[ModuleReferencePanel, ...]
    strict_status: str
    strict_exact_completion: bool | None
    strict_block_coverage: float | None
    lenient_status: str
    lenient_exact_completion: bool | None
    lenient_block_coverage: float | None
    caption: str
    warnings: tuple[str, ...]


def construct_module_scene(
    target: ModuleRenderTarget,
    *,
    analysis_unit: AnalysisUnit,
    max_nodes: int = 50_000,
) -> ModuleScene:
    module_id = target.module_id
    renderability = target.renderability.value
    reason = target.not_renderable_reason
    definitions = target.definitions
    summary_only = renderability != "renderable"
    nodes = (
        (
            ModuleNode(
                node_id=0,
                parent_id=None,
                depth=0,
                label=f"SUMMARY ONLY: {str(reason or 'not_renderable')[:120]}",
                kind="summary_only",
                optional=False,
                unsupported=True,
            ),
        )
        if summary_only
        else _flatten_definitions(definitions, max_nodes=max_nodes)
    )
    blocks = tuple(
        ModuleBlockPanel(
            block_index=item.block_index,
            strict_state=item.strict_state.value,
            lenient_state=item.lenient_state.value,
            uncertain_support_ko_ids=item.uncertain_support_ko_ids,
        )
        for item in target.required_block_states
    )
    optional_components = tuple(
        ModuleOptionalPanel(
            component_index=item.component_index,
            source_module_id=item.source_module_id,
            strict_state=item.strict_state.value,
            lenient_state=item.lenient_state.value,
        )
        for item in target.optional_component_states
    )
    reference_edges = tuple(
        ModuleReferencePanel(
            source_module_id=edge.source_module_id,
            target_module_id=edge.target_module_id,
        )
        for edge in target.reference_edges
    )
    strict = target.strict
    lenient = target.lenient
    strict_status = strict.evaluation_status.value
    lenient_status = lenient.evaluation_status.value
    strict_complete = strict.is_complete
    lenient_complete = lenient.is_complete
    strict_coverage = strict.block_coverage
    lenient_coverage = lenient.block_coverage
    title = target.module_name or module_id
    issue_warnings = tuple(
        f"{issue.kind.value}: {issue.message[:600]}" for issue in target.reference_issues
    )
    warnings = tuple(item.message[:1000] for item in target.warnings) + issue_warnings
    if summary_only:
        warnings = (f"Summary only: {str(reason or 'not_renderable')[:160]}", *warnings)
    max_depth = max((node.depth for node in nodes), default=0)
    maximum_node_x = 50 + max_depth * 220
    has_panels = bool(blocks or optional_components or reference_edges)
    panel_height = (
        len(blocks) * 34
        + len(optional_components) * 28
        + len(reference_edges) * 28
        + 34 * int(bool(optional_components))
        + 34 * int(bool(reference_edges))
    )
    width = max(900, maximum_node_x + (950 if has_panels else 260))
    height = max(620, 360 + max(len(nodes) * 58, panel_height))
    if width > 20_000 or height > 20_000:
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                message="The MODULE scene dimensions exceed the renderer canvas limit.",
                suggested_action="Use a summary-only target or select a smaller MODULE.",
            )
        )
    diagram_kind = (
        "summary-only diagram" if summary_only else "logic diagram from the authoritative core AST"
    )
    community_limit = (
        " Community-level evidence represents pooled encoded potential, not a complete pathway "
        "in one organism."
        if analysis_unit is AnalysisUnit.METAGENOMIC_COMMUNITY
        else ""
    )
    caption = (
        f"{module_id} {diagram_kind}. Strict exact completion: "
        f"{_display_optional(strict_complete)}; strict project block coverage: "
        f"{_display_ratio(strict_coverage)}. Lenient exact completion: "
        f"{_display_optional(lenient_complete)}; lenient project block coverage: "
        f"{_display_ratio(lenient_coverage)}. Optional components are excluded from the required "
        f"denominator. Analysis unit: {analysis_unit.value}.{community_limit} "
        "This is an annotation-evidence diagram, not biochemical topology or proof "
        "of pathway activity."
    )
    return ModuleScene(
        target_id=module_id,
        title=title,
        analysis_unit=analysis_unit.value,
        width=width,
        height=height,
        nodes=nodes,
        blocks=blocks,
        optional_components=optional_components,
        reference_edges=reference_edges,
        strict_status=strict_status,
        strict_exact_completion=strict_complete,
        strict_block_coverage=strict_coverage,
        lenient_status=lenient_status,
        lenient_exact_completion=lenient_complete,
        lenient_block_coverage=lenient_coverage,
        caption=caption,
        warnings=warnings,
    )


def _flatten_definitions(
    definitions: tuple[ResolvedModuleDefinition, ...], *, max_nodes: int
) -> tuple[ModuleNode, ...]:
    output: list[ModuleNode] = []
    for definition in definitions:
        module_id = definition.definition.module_id
        ast = definition.parse_result.ast
        root_id = len(output)
        output.append(
            ModuleNode(
                node_id=root_id,
                parent_id=None,
                depth=0,
                label=module_id,
                kind="module_definition",
                optional=False,
                unsupported=False,
            )
        )
        if ast is None:
            output.append(
                ModuleNode(
                    node_id=len(output),
                    parent_id=root_id,
                    depth=1,
                    label="unsupported definition",
                    kind="unsupported",
                    optional=False,
                    unsupported=True,
                )
            )
            continue
        pending: list[tuple[ModuleExpression, int, int, bool]] = [
            (block, root_id, 1, False) for block in reversed(ast.required_blocks)
        ]
        while pending:
            expression, parent_id, depth, inherited_optional = pending.pop()
            if len(output) >= max_nodes:
                raise RenderMcpError(
                    ErrorDetail(
                        code=ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                        message="The MODULE scene exceeds the configured SVG node limit.",
                        suggested_action="Select a smaller MODULE target.",
                    )
                )
            kind = expression.kind.value
            value = expression.value
            optional = inherited_optional or kind == "optional"
            label = _expression_label(kind, value, expression)
            node_id = len(output)
            output.append(
                ModuleNode(
                    node_id=node_id,
                    parent_id=parent_id,
                    depth=depth,
                    label=label,
                    kind=kind,
                    optional=optional,
                    unsupported=kind == "unsupported",
                )
            )
            pending.extend(
                (child, node_id, depth + 1, optional) for child in reversed(expression.children)
            )
    if not output:
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.TARGET_NOT_RENDERABLE,
                message="The MODULE handoff contains no authoritative definition AST.",
                suggested_action="Rerun core analysis with complete MODULE render targets.",
            )
        )
    return tuple(output)


def _expression_label(kind: str, value: str | None, expression: ModuleExpression) -> str:
    if value is not None:
        return str(value)[:1000]
    if kind == "and":
        operators = tuple(item.kind.value for item in expression.operators)
        return "AND (" + ", ".join(operators) + ")"
    return {
        "or": "OR (,)",
        "optional": "OPTIONAL (-)",
        "group": "GROUP ( )",
    }.get(kind, kind.upper())


def _display_optional(value: bool | None) -> str:
    return "complete" if value is True else "incomplete" if value is False else "not evaluable"


def _display_ratio(value: float | None) -> str:
    return "not evaluable" if value is None else f"{value:.1%}"
