"""Render orchestration without evidence normalization or re-evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from kegg_mcp.services.render_contracts import ModuleRenderTarget, PathwayRenderTarget

from kegg_render_mcp.artifacts import ArtifactBlob, RenderArtifactStore
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    ErrorCode,
    ErrorDetail,
    RenderFormat,
    RenderMcpError,
    RenderResult,
)
from kegg_render_mcp.module_scene import construct_module_scene
from kegg_render_mcp.pathway_scene import PathwayAssetProvider, construct_pathway_scene
from kegg_render_mcp.provenance import safe_batch_provenance
from kegg_render_mcp.raster import render_module_png, render_pathway_png, validate_png
from kegg_render_mcp.render_input import (
    ValidatedRenderInput,
    load_render_input,
    resolve_output_directory,
)
from kegg_render_mcp.svg import render_module_svg, render_pathway_svg


@dataclass(frozen=True, slots=True)
class RenderedTarget:
    target_id: str
    artifacts: tuple[ArtifactBlob, ...]
    warnings: tuple[str, ...]
    provenance: dict[str, object]


class RendererService:
    """Coordinate scene construction, encoding, and bounded artifact retention."""

    def __init__(
        self,
        config: RendererRuntimeConfig,
        provider: PathwayAssetProvider,
        store: RenderArtifactStore | None = None,
    ) -> None:
        self.config = config
        self.provider = provider
        self.store = store or RenderArtifactStore(config)

    def open(self) -> None:
        self.store.open()

    def close(self) -> None:
        self.store.close()

    async def render(
        self,
        *,
        render_input_path: str | None,
        render_input_json: str | None = None,
        target_ids: tuple[str, ...] | None,
        formats: tuple[RenderFormat, ...],
        output_directory: str | None,
    ) -> RenderResult:
        source = load_render_input(
            render_input_path,
            self.config,
            render_input_json=render_input_json,
        )
        selected = source.target_ids if target_ids is None else target_ids
        if not selected or len(selected) > 32 or len(selected) != len(set(selected)):
            raise RenderMcpError(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message="The selected render target set is empty, duplicated, or too large.",
                    suggested_action="Select one through 32 retained target identifiers.",
                )
            )
        output = resolve_output_directory(output_directory, self.config.allowed_roots)
        output_was_allocated = output_directory is None
        artifacts: list[ArtifactBlob] = []
        warnings: list[str] = []
        target_provenance: list[dict[str, object]] = []
        pathway_ids = {str(item.pathway_id) for item in source.document.pathways}
        module_ids = {str(item.module_id) for item in source.document.modules}
        for target_id in selected:
            if target_id in pathway_ids:
                rendered = await _render_pathway_target(
                    source,
                    source.pathway(target_id),
                    formats=formats,
                    config=self.config,
                    provider=self.provider,
                )
            elif target_id in module_ids:
                rendered = _render_module_target(
                    source,
                    source.module(target_id),
                    formats=formats,
                    config=self.config,
                )
            else:
                source.pathway(target_id)
                raise AssertionError("unknown render target lookup unexpectedly returned")
            artifacts.extend(rendered.artifacts)
            warnings.extend(rendered.warnings)
            target_provenance.append(rendered.provenance)
        safe_warnings = tuple(dict.fromkeys(item[:1000] for item in warnings))[:32]
        return self.store.retain(
            target_ids=selected,
            artifacts=tuple(artifacts),
            warnings=safe_warnings,
            manifest_context={
                "render_input_schema_version": "3",
                "producer": source.document.producer.model_dump(mode="json"),
                "dataset_id": source.document.dataset.dataset_id,
                "analysis_unit": source.document.dataset.analysis_unit.value,
                "taxon_id": source.document.dataset.taxon_id,
                "kegg_organism_code": source.document.dataset.kegg_organism_code,
                "decision_policy": source.document.decision_policy.model_dump(mode="json"),
                "targets": target_provenance,
            },
            output_directory=output,
            remove_created_output_directory_on_failure=output_was_allocated,
        )


async def _render_pathway_target(
    source: ValidatedRenderInput,
    target: PathwayRenderTarget,
    *,
    formats: tuple[RenderFormat, ...],
    config: RendererRuntimeConfig,
    provider: PathwayAssetProvider,
) -> RenderedTarget:
    target_id = str(target.pathway_id)
    if not target_id.startswith("ko"):
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.TARGET_NOT_RENDERABLE,
                message="Only regular KO reference pathways are renderable in this release.",
                suggested_action="Select a retained koNNNNN regular pathway target.",
            )
        )
    scene = await construct_pathway_scene(
        source,
        target,
        provider,
        max_asset_bytes=config.limits.max_asset_bytes,
        max_pixels=config.limits.max_pixels,
        limits=config.limits,
    )
    decoded = validate_png(
        scene.source_png,
        max_bytes=config.limits.max_asset_bytes,
        max_pixels=config.limits.max_pixels,
    )
    if decoded != (scene.width, scene.height):
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.ASSET_INVALID,
                message="Pathway PNG dimensions do not match asset metadata.",
                suggested_action="Refresh the matching pathway assets.",
            )
        )
    artifacts: list[ArtifactBlob] = []
    if RenderFormat.SVG in formats:
        svg = render_pathway_svg(
            scene,
            max_bytes=config.limits.max_svg_bytes,
            max_nodes=config.limits.max_svg_nodes,
        )
        artifacts.append(
            ArtifactBlob(
                f"{target_id}.svg",
                "image/svg+xml",
                svg.content,
                svg.width,
                svg.height,
            )
        )
    if RenderFormat.PNG in formats:
        png = render_pathway_png(
            scene,
            max_asset_bytes=config.limits.max_asset_bytes,
            max_pixels=config.limits.max_pixels,
            max_output_bytes=config.limits.max_result_bytes,
        )
        artifacts.append(
            ArtifactBlob(
                f"{target_id}.png",
                "image/png",
                png.content,
                png.width,
                png.height,
            )
        )
    return RenderedTarget(
        target_id=target_id,
        artifacts=tuple(artifacts),
        warnings=tuple(scene.warnings),
        provenance={
            "target_id": target_id,
            "kind": "pathway",
            "reference_namespace": scene.reference_namespace,
            "reference_scope": target.reference_scope.value,
            "evidence_mode": scene.evidence_mode,
            "coverage_numerator": scene.coverage_numerator,
            "coverage_denominator": scene.coverage_denominator,
            "coverage_ratio": scene.coverage_ratio,
            "calculation_method": target.calculation_method,
            "calculation_version": target.calculation_version,
            "reference_link_provenance": [
                safe_batch_provenance(item) for item in target.reference_link_provenance
            ],
            "reference_metadata_provenance": [
                safe_batch_provenance(item) for item in target.reference_metadata_provenance
            ],
            "assets": scene.asset_provenance,
        },
    )


def _render_module_target(
    source: ValidatedRenderInput,
    target: ModuleRenderTarget,
    *,
    formats: tuple[RenderFormat, ...],
    config: RendererRuntimeConfig,
) -> RenderedTarget:
    target_id = str(target.module_id)
    scene = construct_module_scene(
        target,
        analysis_unit=source.document.dataset.analysis_unit,
        max_nodes=config.limits.max_svg_nodes,
    )
    artifacts: list[ArtifactBlob] = []
    if RenderFormat.SVG in formats:
        svg = render_module_svg(
            scene,
            max_bytes=config.limits.max_svg_bytes,
            max_nodes=config.limits.max_svg_nodes,
        )
        artifacts.append(
            ArtifactBlob(
                f"{target_id}.svg",
                "image/svg+xml",
                svg.content,
                svg.width,
                svg.height,
            )
        )
    if RenderFormat.PNG in formats:
        png = render_module_png(
            scene,
            max_pixels=config.limits.max_pixels,
            max_output_bytes=config.limits.max_result_bytes,
        )
        artifacts.append(
            ArtifactBlob(
                f"{target_id}.png",
                "image/png",
                png.content,
                png.width,
                png.height,
            )
        )
    return RenderedTarget(
        target_id=target_id,
        artifacts=tuple(artifacts),
        warnings=tuple(scene.warnings),
        provenance={
            "target_id": target_id,
            "kind": "module",
            "strict_exact_completion": scene.strict_exact_completion,
            "strict_block_coverage": scene.strict_block_coverage,
            "lenient_exact_completion": scene.lenient_exact_completion,
            "lenient_block_coverage": scene.lenient_block_coverage,
            "parser_name": target.parser_name,
            "parser_version": target.parser_version,
            "resolver_version": target.resolver_version,
            "strict_calculation_method": target.strict.calculation_method.model_dump(mode="json"),
            "lenient_calculation_method": target.lenient.calculation_method.model_dump(mode="json"),
            "reference_retrieval_provenance": [
                safe_batch_provenance(item) for item in target.reference_retrieval_provenance
            ],
        },
    )
