"""Deterministic MODULE logic scenes from authoritative core AST and state vectors."""

from __future__ import annotations

from dataclasses import dataclass

from kegg_mcp.analysis import ModuleExpression, ResolvedModuleDefinition
from kegg_mcp.domain import AnalysisUnit
from kegg_mcp.services.render_contracts import (
    ModuleRenderTarget,
    module_scene_fits_renderer,
    module_scene_layout,
)

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
    state: str


@dataclass(frozen=True, slots=True)
class ModuleOptionalPanel:
    component_index: int
    source_module_id: str
    state: str


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
    node_y: int
    nodes: tuple[ModuleNode, ...]
    blocks: tuple[ModuleBlockPanel, ...]
    optional_components: tuple[ModuleOptionalPanel, ...]
    reference_edges: tuple[ModuleReferencePanel, ...]
    status: str
    exact_completion: bool | None
    block_coverage: float | None
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
    blocks = (
        ()
        if summary_only
        else tuple(
            ModuleBlockPanel(
                block_index=item.block_index,
                state=item.state.value,
            )
            for item in target.required_block_states
        )
    )
    optional_components = (
        ()
        if summary_only
        else tuple(
            ModuleOptionalPanel(
                component_index=item.component_index,
                source_module_id=item.source_module_id,
                state=item.state.value,
            )
            for item in target.optional_component_states
        )
    )
    reference_edges = (
        ()
        if summary_only
        else tuple(
            ModuleReferencePanel(
                source_module_id=edge.source_module_id,
                target_module_id=edge.target_module_id,
            )
            for edge in target.reference_edges
        )
    )
    completion = target.completion
    status = completion.evaluation_status.value
    exact_completion = completion.is_complete
    block_coverage = completion.block_coverage
    title = target.module_name or module_id
    issue_warnings = tuple(
        f"{issue.kind.value}: {issue.message[:600]}" for issue in target.reference_issues
    )
    warnings = tuple(item.message[:1000] for item in target.warnings) + issue_warnings
    if summary_only:
        warnings = (f"Summary only: {str(reason or 'not_renderable')[:160]}", *warnings)
    max_depth = max((node.depth for node in nodes), default=0)
    width, height, _ = module_scene_layout(
        node_count=len(nodes),
        max_depth=max_depth,
        required_block_count=len(blocks),
        optional_component_count=len(optional_components),
        reference_edge_count=len(reference_edges),
    )
    node_y = _module_node_y(
        required_block_count=len(blocks),
        optional_component_count=len(optional_components),
        reference_edge_count=len(reference_edges),
    )
    if not module_scene_fits_renderer(
        node_count=len(nodes),
        max_depth=max_depth,
        required_block_count=len(blocks),
        optional_component_count=len(optional_components),
        reference_edge_count=len(reference_edges),
    ):
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
        f"{module_id} {diagram_kind}. Exact completion: "
        f"{_display_optional(exact_completion)}; project block coverage: "
        f"{_display_ratio(block_coverage)}. Optional components are excluded from the required "
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
        node_y=node_y,
        nodes=nodes,
        blocks=blocks,
        optional_components=optional_components,
        reference_edges=reference_edges,
        status=status,
        exact_completion=exact_completion,
        block_coverage=block_coverage,
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


def _module_node_y(
    *,
    required_block_count: int,
    optional_component_count: int,
    reference_edge_count: int,
) -> int:
    panel_counts = (
        required_block_count,
        optional_component_count,
        reference_edge_count,
    )
    populated_panel_count = sum(bool(count) for count in panel_counts)
    if not populated_panel_count:
        return 150
    panel_bottom = (
        140
        + sum(26 + count * 28 for count in panel_counts if count)
        + (populated_panel_count - 1) * 12
    )
    return panel_bottom + 30
