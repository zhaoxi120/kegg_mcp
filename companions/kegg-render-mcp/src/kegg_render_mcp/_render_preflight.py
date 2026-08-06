"""Fail-closed target capability preflight before retrieval or output allocation."""

from __future__ import annotations

from dataclasses import dataclass

from kegg_mcp.services.render_contracts import ModuleRenderTarget, PathwayRenderTarget

from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError, SafeDetail
from kegg_render_mcp.module_scene import ModuleScene, construct_module_scene
from kegg_render_mcp.pathway_scene import PathwayAssetProvider
from kegg_render_mcp.render_input import ValidatedRenderInput


@dataclass(frozen=True, slots=True)
class PreflightTargets:
    pathways: dict[str, PathwayRenderTarget]
    modules: dict[str, ModuleRenderTarget]
    module_scenes: dict[str, ModuleScene]


def preflight_targets(
    source: ValidatedRenderInput,
    selected: tuple[str, ...],
    *,
    provider: PathwayAssetProvider,
    max_svg_nodes: int,
) -> PreflightTargets:
    """Resolve every selection and validate local capabilities in one bounded pass."""
    pathways = {str(item.pathway_id): item for item in source.document.pathways}
    modules = {str(item.module_id): item for item in source.document.modules}
    module_scenes: dict[str, ModuleScene] = {}
    for target_id in selected:
        pathway = pathways.get(target_id)
        if pathway is not None:
            if pathway.renderability.value != "renderable":
                raise RenderMcpError(
                    ErrorDetail(
                        code=ErrorCode.TARGET_NOT_RENDERABLE,
                        message=(
                            "This pathway target is not eligible for a static evidence overlay."
                        ),
                        suggested_action=(
                            "Use the core summary or select a renderable pathway target."
                        ),
                        safe_details=(
                            SafeDetail(name="target_id", value=target_id),
                            SafeDetail(
                                name="reason",
                                value=str(
                                    pathway.not_renderable_reason or "pathway_is_not_renderable"
                                )[:160],
                            ),
                        ),
                    )
                )
            if not provider.configured:
                raise RenderMcpError(
                    ErrorDetail(
                        code=ErrorCode.ASSET_UNAVAILABLE,
                        message="Pathway asset access is not configured for this renderer.",
                        suggested_action=(
                            "Configure authorized KEGG access or render a MODULE target."
                        ),
                        safe_details=(SafeDetail(name="target_id", value=target_id),),
                    )
                )
            continue
        module = modules.get(target_id)
        if module is not None:
            try:
                module_scenes[target_id] = construct_module_scene(
                    module,
                    analysis_unit=source.document.dataset.analysis_unit,
                    max_nodes=max_svg_nodes,
                )
            except RenderMcpError as error:
                raise with_target_context(error, target_id) from None
            continue
        source.pathway(target_id)
        raise AssertionError("unknown target lookup unexpectedly returned")
    return PreflightTargets(pathways, modules, module_scenes)


def with_target_context(error: RenderMcpError, target_id: str) -> RenderMcpError:
    if any(item.name == "target_id" for item in error.detail.safe_details):
        return error
    return RenderMcpError(
        error.detail.model_copy(
            update={
                "safe_details": (
                    SafeDetail(name="target_id", value=target_id),
                    *error.detail.safe_details,
                )[:8]
            }
        )
    )
