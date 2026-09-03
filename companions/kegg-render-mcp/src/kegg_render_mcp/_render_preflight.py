"""Fail-closed target capability preflight before retrieval or output allocation."""

from __future__ import annotations

from dataclasses import dataclass

from kegg_mcp.services.render_contracts import PathwayRenderTarget

from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError, SafeDetail
from kegg_render_mcp.pathway_scene import PathwayAssetProvider
from kegg_render_mcp.render_input import ValidatedRenderInput


@dataclass(frozen=True, slots=True)
class PreflightTargets:
    pathways: dict[str, PathwayRenderTarget]


def preflight_targets(
    source: ValidatedRenderInput,
    selected: tuple[str, ...],
    *,
    provider: PathwayAssetProvider,
) -> PreflightTargets:
    """Resolve every selection and validate local capabilities in one bounded pass."""
    pathways = {str(item.pathway_id): item for item in source.document.pathways}
    for target_id in selected:
        pathway = source.pathway(target_id)
        if pathway.renderability.value != "renderable":
            raise RenderMcpError(
                ErrorDetail(
                    code=ErrorCode.TARGET_NOT_RENDERABLE,
                    message="This pathway target is not eligible for a static evidence overlay.",
                    suggested_action="Use the core summary or select a renderable pathway target.",
                    safe_details=(
                        SafeDetail(name="target_id", value=target_id),
                        SafeDetail(
                            name="reason",
                            value=str(pathway.not_renderable_reason or "pathway_is_not_renderable")[
                                :160
                            ],
                        ),
                    ),
                )
            )
        if not provider.configured:
            raise RenderMcpError(
                ErrorDetail(
                    code=ErrorCode.ASSET_UNAVAILABLE,
                    message="Pathway asset access is not configured for this renderer.",
                    suggested_action="Configure authorized KEGG pathway access.",
                    safe_details=(SafeDetail(name="target_id", value=target_id),),
                )
            )
    return PreflightTargets(pathways)


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
