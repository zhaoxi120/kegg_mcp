"""Render orchestration without evidence normalization or re-evaluation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from kegg_mcp.services.render_contracts import ModuleRenderTarget, PathwayRenderTarget

from kegg_render_mcp._render_preflight import preflight_targets, with_target_context
from kegg_render_mcp.artifacts import (
    ArtifactBlob,
    RenderArtifactStore,
    manifest_byte_reserve,
)
from kegg_render_mcp.config import RendererRuntimeConfig
from kegg_render_mcp.contracts import (
    MAX_TARGETS,
    MAX_WARNINGS,
    ErrorCode,
    ErrorDetail,
    RenderFormat,
    RenderMcpError,
    RenderResult,
    SafeDetail,
)
from kegg_render_mcp.kgml import KGML_PARSER_NAME, KGML_PARSER_VERSION
from kegg_render_mcp.module_scene import ModuleScene
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
    artifacts: tuple[ArtifactBlob, ...]
    warnings: tuple[str, ...]
    provenance: dict[str, object]


class _EncodedArtifact(Protocol):
    @property
    def content(self) -> bytes: ...

    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


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
        if (
            not selected
            or len(selected) > MAX_TARGETS
            or len(selected) != len(set(selected))
            or not formats
            or len(formats) != len(set(formats))
        ):
            raise RenderMcpError(
                ErrorDetail(
                    code=ErrorCode.INVALID_REQUEST,
                    message=(
                        "The selected target or format set is empty, duplicated, or too large."
                    ),
                    suggested_action=(
                        f"Select one through {MAX_TARGETS} retained target identifiers."
                    ),
                )
            )
        output = resolve_output_directory(output_directory, self.config.allowed_roots)
        prepared = preflight_targets(
            source,
            selected,
            provider=self.provider,
            max_svg_nodes=self.config.limits.max_svg_nodes,
        )
        artifacts: list[ArtifactBlob] = []
        remaining_artifact_bytes = self.config.limits.max_result_bytes - manifest_byte_reserve(
            self.config.limits.max_result_bytes
        )
        warnings: list[str] = []
        target_provenance: list[dict[str, object]] = []
        for target_id in selected:
            try:
                if target := prepared.pathways.get(target_id):
                    rendered = await _render_pathway_target(
                        source,
                        target,
                        formats=formats,
                        config=self.config,
                        provider=self.provider,
                        max_artifact_bytes=remaining_artifact_bytes,
                    )
                elif target := prepared.modules.get(target_id):
                    rendered = _render_module_target(
                        target,
                        scene=prepared.module_scenes[target_id],
                        formats=formats,
                        config=self.config,
                        max_artifact_bytes=remaining_artifact_bytes,
                    )
                else:
                    raise AssertionError("preflight target lookup unexpectedly changed")
            except RenderMcpError as error:
                raise with_target_context(error, target_id) from None
            rendered_bytes = sum(len(item.content) for item in rendered.artifacts)
            remaining_artifact_bytes -= rendered_bytes
            artifacts.extend(rendered.artifacts)
            warnings.extend(rendered.warnings)
            target_provenance.append(rendered.provenance)
        safe_warnings = tuple(dict.fromkeys(item[:1000] for item in warnings))[:MAX_WARNINGS]
        return self.store.retain(
            target_ids=selected,
            artifacts=tuple(artifacts),
            warnings=safe_warnings,
            manifest_context={
                "render_input_schema_version": source.document.schema_version,
                "producer": source.document.producer.model_dump(mode="json"),
                "dataset_id": source.document.dataset.dataset_id,
                "analysis_unit": source.document.dataset.analysis_unit.value,
                "taxon_id": source.document.dataset.taxon_id,
                "kegg_organism_code": source.document.dataset.kegg_organism_code,
                "decision_policy": source.document.decision_policy.model_dump(mode="json"),
                "annotation_retention": (
                    source.document.execution.analysis.annotation_retention.value
                ),
                "record_level_evidence_retained": (
                    source.document.execution.analysis.annotation_retention.value
                    == "full_records"
                ),
                "accepted_unique_ko_count": len(source.document.evidence.accepted_ko_ids),
                "targets": target_provenance,
            },
            output_directory=output,
        )


async def _render_pathway_target(
    source: ValidatedRenderInput,
    target: PathwayRenderTarget,
    *,
    formats: tuple[RenderFormat, ...],
    config: RendererRuntimeConfig,
    provider: PathwayAssetProvider,
    max_artifact_bytes: int,
) -> RenderedTarget:
    target_id = str(target.pathway_id)
    scene = await construct_pathway_scene(
        source,
        target,
        provider,
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
    artifacts = _render_target_artifacts(
        target_id,
        formats,
        max_artifact_bytes=max_artifact_bytes,
        max_svg_bytes=config.limits.max_svg_bytes,
        svg_encoder=lambda limit: render_pathway_svg(
            scene, max_bytes=limit, max_nodes=config.limits.max_svg_nodes
        ),
        png_encoder=lambda limit: render_pathway_png(
            scene,
            max_pixels=config.limits.max_pixels,
            max_output_bytes=limit,
        ),
    )
    return RenderedTarget(
        artifacts=artifacts,
        warnings=scene.warnings,
        provenance={
            "target_id": target_id,
            "kind": "pathway",
            "reference_namespace": target.reference_namespace.value,
            "reference_scope": target.reference_scope.value,
            "coverage_numerator": target.coverage_numerator,
            "coverage_denominator": target.coverage_denominator,
            "coverage_ratio": target.coverage_ratio,
            "calculation_method": target.calculation_method,
            "calculation_version": target.calculation_version,
            "kgml_parser_name": KGML_PARSER_NAME,
            "kgml_parser_version": KGML_PARSER_VERSION,
            "retained_box_graphic_count": scene.retained_box_graphic_count,
            "retained_polyline_graphic_count": scene.retained_polyline_graphic_count,
            "mapped_detected_ko_ids": scene.mapped_detected_ko_ids,
            "box_overlay_count": scene.box_overlay_count,
            "polyline_overlay_count": scene.polyline_overlay_count,
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
    target: ModuleRenderTarget,
    *,
    scene: ModuleScene,
    formats: tuple[RenderFormat, ...],
    config: RendererRuntimeConfig,
    max_artifact_bytes: int,
) -> RenderedTarget:
    target_id = str(target.module_id)
    artifacts = _render_target_artifacts(
        target_id,
        formats,
        max_artifact_bytes=max_artifact_bytes,
        max_svg_bytes=config.limits.max_svg_bytes,
        svg_encoder=lambda limit: render_module_svg(
            scene, max_bytes=limit, max_nodes=config.limits.max_svg_nodes
        ),
        png_encoder=lambda limit: render_module_png(
            scene,
            max_pixels=config.limits.max_pixels,
            max_output_bytes=limit,
        ),
    )
    return RenderedTarget(
        artifacts=artifacts,
        warnings=scene.warnings,
        provenance={
            "target_id": target_id,
            "kind": "module",
            "evaluation_status": scene.status,
            "exact_completion": scene.exact_completion,
            "block_coverage": scene.block_coverage,
            "parser_name": target.parser_name,
            "parser_version": target.parser_version,
            "resolver_version": target.resolver_version,
            "calculation_method": target.completion.calculation_method.model_dump(mode="json"),
            "reference_retrieval_provenance": [
                safe_batch_provenance(item) for item in target.reference_retrieval_provenance
            ],
        },
    )


def _render_target_artifacts(
    target_id: str,
    formats: tuple[RenderFormat, ...],
    *,
    max_artifact_bytes: int,
    max_svg_bytes: int,
    svg_encoder: Callable[[int], _EncodedArtifact],
    png_encoder: Callable[[int], _EncodedArtifact],
) -> tuple[ArtifactBlob, ...]:
    artifacts: list[ArtifactBlob] = []
    remaining = max_artifact_bytes
    for render_format, asset_kind, suffix, mime_type, limit, encoder in (
        (
            RenderFormat.SVG,
            "svg_output",
            ".svg",
            "image/svg+xml",
            max_svg_bytes,
            svg_encoder,
        ),
        (
            RenderFormat.PNG,
            "png_output",
            ".png",
            "image/png",
            max_artifact_bytes,
            png_encoder,
        ),
    ):
        if render_format not in formats:
            continue
        artifact, remaining = _encode_artifact(
            target_id=target_id,
            asset_kind=asset_kind,
            name=f"{target_id}{suffix}",
            mime_type=mime_type,
            remaining=remaining,
            format_limit=limit,
            encode=encoder,
        )
        artifacts.append(artifact)
    return tuple(artifacts)


def _encode_artifact(
    *,
    target_id: str,
    asset_kind: str,
    name: str,
    mime_type: str,
    remaining: int,
    format_limit: int,
    encode: Callable[[int], _EncodedArtifact],
) -> tuple[ArtifactBlob, int]:
    if remaining < 1:
        raise _target_output_limit(target_id, asset_kind)
    try:
        encoded = encode(min(remaining, format_limit))
    except RenderMcpError as error:
        if error.detail.code is not ErrorCode.OUTPUT_LIMIT_EXCEEDED:
            raise
        contextual = (
            SafeDetail(name="target_id", value=target_id),
            SafeDetail(name="asset_kind", value=asset_kind),
        )
        existing = tuple(
            item
            for item in error.detail.safe_details
            if item.name not in {"target_id", "asset_kind"}
        )
        raise RenderMcpError(
            error.detail.model_copy(update={"safe_details": (*contextual, *existing)[:8]})
        ) from None
    if len(encoded.content) > remaining:
        raise _target_output_limit(target_id, asset_kind)
    return (
        ArtifactBlob(name, mime_type, encoded.content, encoded.width, encoded.height),
        remaining - len(encoded.content),
    )


def _target_output_limit(target_id: str, asset_kind: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.OUTPUT_LIMIT_EXCEEDED,
            message="The selected render outputs exceed the remaining bounded result budget.",
            suggested_action="Select fewer targets, request SVG only, or increase the safe limit.",
            safe_details=(
                SafeDetail(name="target_id", value=target_id),
                SafeDetail(name="asset_kind", value=asset_kind),
            ),
        )
    )
