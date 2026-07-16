"""Typed pathway-asset adapter and deterministic regular-map scene construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from anyio import to_thread
from kegg_mcp.kegg import (
    InfoRequest,
    KeggBatchProvenance,
    KeggClient,
    KeggInfoDatabase,
    KeggRequestOptions,
    PathwayAssetKind,
    PathwayAssetRequest,
    PathwayAssetResult,
)
from kegg_mcp.services.render_contracts import PathwayRenderTarget

from kegg_render_mcp.config import RendererLimits
from kegg_render_mcp.contracts import ErrorCode, ErrorDetail, RenderMcpError, SafeDetail
from kegg_render_mcp.kgml import KgmlDocument, parse_kgml, validate_graphic_bounds
from kegg_render_mcp.render_input import ValidatedRenderInput


@dataclass(frozen=True, slots=True)
class RetrievedAsset:
    pathway_id: str
    kind: str
    content: bytes
    mime_type: str
    width: int | None
    height: int | None
    provenance: dict[str, object]


class PathwayAssetProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    async def get_asset(self, pathway_id: str, kind: str) -> RetrievedAsset: ...

    async def probe(self) -> bool: ...


class CorePathwayAssetProvider:
    """Thin async adapter around the core public KEGG asset client."""

    def __init__(self, client: KeggClient) -> None:
        self._client = client

    @property
    def configured(self) -> bool:
        return True

    @property
    def maximum_retries(self) -> int:
        """Expose only the safe numeric retry bound for contract tests and status review."""
        return self._client.config.retry.max_retries

    @property
    def maximum_response_bytes(self) -> int:
        """Expose the effective wire-response bound for capability tests."""
        return self._client.config.limits.max_response_bytes

    async def get_asset(self, pathway_id: str, kind: str) -> RetrievedAsset:
        selected = PathwayAssetKind(kind)

        def retrieve() -> PathwayAssetResult:
            return self._client.get_pathway_asset(
                PathwayAssetRequest(pathway_id=pathway_id, kind=selected),
                options=KeggRequestOptions(refresh=False),
            )

        try:
            result = await to_thread.run_sync(retrieve)
        except Exception as error:
            raise RenderMcpError(
                ErrorDetail(
                    code=ErrorCode.ASSET_UNAVAILABLE,
                    message="The matching KEGG pathway asset could not be retrieved.",
                    suggested_action="Check renderer access status and retry the bounded target.",
                    safe_details=(SafeDetail(name="asset_kind", value=kind),),
                )
            ) from error
        request = result.request
        provenance = _safe_provenance(result.provenance)
        return RetrievedAsset(
            pathway_id=request.pathway_id,
            kind=str(request.kind.value),
            content=result.content,
            mime_type=result.mime_type,
            width=result.width,
            height=result.height,
            provenance=provenance,
        )

    async def probe(self) -> bool:
        def request() -> object:
            return self._client.info(
                InfoRequest(database=KeggInfoDatabase.PATHWAY),
                options=KeggRequestOptions(refresh=True),
            )

        try:
            await to_thread.run_sync(request)
        except Exception:
            return False
        return True


class UnconfiguredAssetProvider:
    @property
    def configured(self) -> bool:
        return False

    async def get_asset(self, pathway_id: str, kind: str) -> RetrievedAsset:
        del pathway_id, kind
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.ASSET_UNAVAILABLE,
                message="Pathway asset access is not configured for this renderer.",
                suggested_action="Configure authorized KEGG access or render a MODULE target.",
            )
        )

    async def probe(self) -> bool:
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.ASSET_UNAVAILABLE,
                message="KEGG access is not configured for this renderer.",
                suggested_action=(
                    "Configure authorized access before probing or rendering pathways."
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class Overlay:
    entry_id: int
    ko_ids: tuple[str, ...]
    state: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True, slots=True)
class PathwayScene:
    target_id: str
    title: str
    analysis_unit: str
    width: int
    height: int
    source_png: bytes
    overlays: tuple[Overlay, ...]
    caption: str
    coverage_numerator: int
    coverage_denominator: int
    coverage_ratio: float
    reference_namespace: str
    evidence_mode: str
    asset_provenance: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]


async def construct_pathway_scene(
    render_input: ValidatedRenderInput,
    target: PathwayRenderTarget,
    provider: PathwayAssetProvider,
    *,
    max_asset_bytes: int,
    max_pixels: int,
    limits: RendererLimits,
) -> PathwayScene:
    target_id = target.pathway_id
    _require_regular_renderable(target)
    image, kgml_asset = await _retrieve_pair(provider, target_id)
    if image.pathway_id != target_id or kgml_asset.pathway_id != target_id:
        raise _asset_invalid("Retrieved pathway asset identities do not match the target.")
    if (
        image.mime_type != "image/png"
        or kgml_asset.mime_type != "application/xml"
        or image.width is None
        or image.height is None
    ):
        raise _asset_invalid("Retrieved pathway assets have incompatible media metadata.")
    if len(image.content) > max_asset_bytes or len(kgml_asset.content) > max_asset_bytes:
        raise _asset_invalid("Retrieved pathway assets exceed the configured byte limit.")
    if image.width * image.height > max_pixels:
        raise _asset_invalid("The pathway PNG exceeds the configured pixel limit.")
    kgml = parse_kgml(kgml_asset.content, target_id, limits)  # type: ignore[arg-type]
    validate_graphic_bounds(kgml, image.width, image.height)
    evidence_mode = target.evidence_mode.value
    detected = frozenset(target.detected_ko_ids)
    accepted = render_input.accepted_ko_ids.intersection(detected)
    eligible_uncertain: frozenset[str] = (
        render_input.uncertain_ko_ids.intersection(detected)
        if evidence_mode == "lenient"
        else frozenset()
    )
    overlays = _overlay_states(kgml, frozenset(accepted), eligible_uncertain)
    numerator = target.coverage_numerator
    denominator = target.coverage_denominator
    if target.coverage_ratio is None:
        raise _asset_invalid("A renderable pathway target omitted its core coverage ratio.")
    ratio = target.coverage_ratio
    name = target.pathway_name
    namespace = target.reference_namespace.value
    analysis_unit = render_input.document.dataset.analysis_unit.value
    warnings = tuple(item.message[:1000] for item in target.warnings)
    community_limit = (
        " Community-level evidence represents pooled encoded potential, not a complete pathway "
        "in one organism."
        if analysis_unit == "metagenomic_community"
        else ""
    )
    caption = (
        f"{target_id} — {name}. Core descriptive KO coverage: {numerator}/{denominator} "
        f"({ratio:.1%}); reference namespace: {namespace}; evidence mode: {evidence_mode}; "
        f"analysis unit: {analysis_unit}.{community_limit} "
        "This visualization represents annotation evidence, not pathway presence, activity, "
        "flux, phenotype, or experimental validation."
    )
    return PathwayScene(
        target_id=target_id,
        title=name,
        analysis_unit=analysis_unit,
        width=image.width,
        height=image.height,
        source_png=image.content,
        overlays=overlays,
        caption=caption,
        coverage_numerator=numerator,
        coverage_denominator=denominator,
        coverage_ratio=ratio,
        reference_namespace=namespace,
        evidence_mode=evidence_mode,
        asset_provenance=(image.provenance, kgml_asset.provenance),
        warnings=warnings,
    )


async def _retrieve_pair(
    provider: PathwayAssetProvider, pathway_id: str
) -> tuple[RetrievedAsset, RetrievedAsset]:
    # Sequential retrieval preserves the core client's no-burst process-wide rate policy.
    image = await provider.get_asset(pathway_id, "image")
    kgml = await provider.get_asset(pathway_id, "kgml")
    return image, kgml


def _overlay_states(
    kgml: KgmlDocument, accepted: frozenset[str], uncertain: frozenset[str]
) -> tuple[Overlay, ...]:
    result: list[Overlay] = []
    for graphic in kgml.graphics:
        state: str | None = None
        if accepted.intersection(graphic.ko_ids):
            state = "accepted"
        elif uncertain.intersection(graphic.ko_ids):
            state = "uncertain"
        if state is not None:
            result.append(
                Overlay(
                    entry_id=graphic.entry_id,
                    ko_ids=graphic.ko_ids,
                    state=state,
                    x=graphic.x,
                    y=graphic.y,
                    width=graphic.width,
                    height=graphic.height,
                )
            )
    return tuple(result)


def _require_regular_renderable(target: PathwayRenderTarget) -> None:
    if target.renderability.value != "renderable":
        safe_reason = str(target.not_renderable_reason or "pathway_is_not_renderable")[:160]
        raise RenderMcpError(
            ErrorDetail(
                code=ErrorCode.TARGET_NOT_RENDERABLE,
                message="This pathway target is unsupported for regular box overlays.",
                suggested_action="Use the core summary or select a regular reference pathway.",
                safe_details=(SafeDetail(name="reason", value=safe_reason),),
            )
        )


def _safe_provenance(provenance: KeggBatchProvenance) -> dict[str, object]:
    safe: dict[str, object] = {
        "request_key": provenance.request_key,
        "access_mode": provenance.access_mode.value,
        "retrieval_endpoint_class": provenance.retrieval_endpoint_class.value,
        "origin": provenance.origin.value,
        "cache_lookup_state": provenance.cache_lookup_state.value,
        "retrieved_at": provenance.retrieved_at.isoformat(),
        "served_at": provenance.served_at.isoformat(),
        "is_stale": provenance.is_stale,
        "parser_name": provenance.parser_name,
        "parser_version": provenance.parser_version,
    }
    if provenance.database_release is not None:
        safe["database_release"] = provenance.database_release
    return safe


def _asset_invalid(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.ASSET_INVALID,
            message=message,
            suggested_action="Refresh the matching single-pathway assets and retry.",
        )
    )
