"""Typed pathway-asset adapter and deterministic pathway scene construction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, cast

from anyio import to_thread
from kegg_mcp.domain import ErrorCode as CoreErrorCode
from kegg_mcp.domain import KeggMcpError as CoreKeggMcpError
from kegg_mcp.kegg import (
    AccessMode,
    InfoRequest,
    KeggClient,
    KeggInfoDatabase,
    KeggRequestOptions,
    PathwayAssetKind,
    PathwayAssetRequest,
    PathwayAssetResult,
)
from kegg_mcp.services.render_contracts import PathwayRenderTarget

from kegg_render_mcp.config import RendererLimits
from kegg_render_mcp.contracts import (
    ConnectivityStatus,
    ErrorCode,
    ErrorDetail,
    RenderMcpError,
    SafeDetail,
)
from kegg_render_mcp.kgml import KgmlDocument, KgmlGraphic, parse_kgml, validate_graphic_bounds
from kegg_render_mcp.provenance import safe_batch_provenance
from kegg_render_mcp.render_input import ValidatedRenderInput


@dataclass(frozen=True, slots=True)
class RetrievedAsset:
    pathway_id: str
    content: bytes
    mime_type: str
    width: int | None
    height: int | None
    provenance: dict[str, object]


class PathwayAssetProvider(Protocol):
    @property
    def configured(self) -> bool: ...

    async def get_asset(self, pathway_id: str, kind: str) -> RetrievedAsset: ...

    async def probe(self) -> ConnectivityStatus: ...


class CorePathwayAssetProvider:
    """Thin async adapter around the core public KEGG asset client."""

    def __init__(self, client: KeggClient, *, allow_stale: bool = False) -> None:
        self._client = client
        self._allow_stale = allow_stale

    @property
    def configured(self) -> bool:
        return True

    @property
    def network_enabled(self) -> bool:
        """Report whether an explicit probe may perform one live request."""
        return self._client.config.access.mode is not AccessMode.OFFLINE_CACHE

    async def get_asset(self, pathway_id: str, kind: str) -> RetrievedAsset:
        selected = PathwayAssetKind(kind)

        def retrieve() -> PathwayAssetResult:
            return self._client.get_pathway_asset(
                PathwayAssetRequest(pathway_id=pathway_id, kind=selected),
                options=KeggRequestOptions(refresh=False, allow_stale=self._allow_stale),
            )

        try:
            result = await to_thread.run_sync(retrieve)
        except CoreKeggMcpError as error:
            raise _translate_core_asset_error(error, kind=kind) from error
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
        provenance = safe_batch_provenance(result.provenance)
        return RetrievedAsset(
            pathway_id=request.pathway_id,
            content=result.content,
            mime_type=result.mime_type,
            width=result.width,
            height=result.height,
            provenance=provenance,
        )

    async def probe(self) -> ConnectivityStatus:
        if not self.network_enabled:
            return ConnectivityStatus.OFFLINE_CACHE

        def request() -> object:
            return self._client.info(
                InfoRequest(database=KeggInfoDatabase.PATHWAY),
                options=KeggRequestOptions(refresh=True),
            )

        try:
            await to_thread.run_sync(request)
        except CoreKeggMcpError as error:
            return _classify_probe_error(error)
        except Exception:
            return ConnectivityStatus.UNKNOWN_FAILURE
        return ConnectivityStatus.REACHABLE


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

    async def probe(self) -> ConnectivityStatus:
        return ConnectivityStatus.NOT_CONFIGURED


@dataclass(frozen=True, slots=True)
class PathwayScene:
    target_id: str
    title: str
    width: int
    height: int
    source_png: bytes
    overlays: tuple[KgmlGraphic, ...]
    caption: str
    retained_box_graphic_count: int
    retained_polyline_graphic_count: int
    mapped_detected_ko_ids: tuple[str, ...]
    box_overlay_count: int
    polyline_overlay_count: int
    asset_provenance: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]

    @property
    def retained_geometry_kinds(self) -> frozenset[str]:
        return frozenset(
            kind
            for kind, count in (
                ("box", self.retained_box_graphic_count),
                ("polyline", self.retained_polyline_graphic_count),
            )
            if count
        )


async def construct_pathway_scene(
    render_input: ValidatedRenderInput,
    target: PathwayRenderTarget,
    provider: PathwayAssetProvider,
    *,
    limits: RendererLimits,
) -> PathwayScene:
    target_id = target.pathway_id
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
    kgml = parse_kgml(kgml_asset.content, target_id, limits)  # type: ignore[arg-type]
    validate_graphic_bounds(kgml, image.width, image.height)
    detected = frozenset(target.detected_ko_ids)
    retained_ko_ids = frozenset(ko_id for graphic in kgml.graphics for ko_id in graphic.ko_ids)
    mapped_detected_ko_ids = tuple(sorted(detected.intersection(retained_ko_ids)))
    numerator = target.coverage_numerator
    denominator = target.coverage_denominator
    if numerator > 0 and not mapped_detected_ko_ids:
        raise _asset_invalid(
            "Core-detected pathway evidence has no safely retained box or polyline geometry in "
            "the matching KGML asset."
        )
    overlays = _accepted_overlays(kgml, detected)
    ratio = cast(float, target.coverage_ratio)
    name = target.pathway_name
    namespace = target.reference_namespace.value
    analysis_unit = render_input.document.dataset.analysis_unit.value
    warnings: list[str] = []
    if any(bool(asset.provenance.get("is_stale")) for asset in (image, kgml_asset)):
        warnings.append(
            "One or more KEGG pathway assets were served from stale offline cache entries."
        )
    warnings.extend(item.message[:1000] for item in target.warnings)
    unmapped_detected_count = len(detected) - len(mapped_detected_ko_ids)
    if unmapped_detected_count:
        warnings.append(
            f"{unmapped_detected_count} of {len(detected)} core-detected K numbers had no "
            "retained box or polyline geometry in the matching KGML asset; core coverage was "
            "preserved and not recomputed."
        )
    broad_map = target.reference_scope.value == "global_or_overview"
    if broad_map:
        warnings.append(
            "This global or overview base map retains KEGG contextual colors and arrowheads; "
            "only the renderer's solid accepted overlays encode input annotation evidence. "
            "Base-map direction must not be interpreted as evidence of "
            "directionality or activity."
        )
    community_limit = (
        " Community-level evidence represents pooled encoded potential, not a complete pathway "
        "in one organism."
        if analysis_unit == "metagenomic_community"
        else ""
    )
    caption = (
        f"{target_id} - {name}. Core descriptive KO coverage: {numerator}/{denominator} "
        f"({ratio:.1%}); reference namespace: {namespace}; analysis unit: "
        f"{analysis_unit}.{community_limit} "
        "This visualization represents annotation evidence, not pathway presence, activity, "
        "flux, phenotype, or experimental validation."
        + (
            " Existing base-map colors and arrowheads are KEGG context; only solid overlays "
            "encode the supplied evidence, without inferring direction or activity."
            if broad_map
            else ""
        )
    )
    retained_box_graphic_count = sum(graphic.kind == "box" for graphic in kgml.graphics)
    retained_polyline_graphic_count = len(kgml.graphics) - retained_box_graphic_count
    box_overlay_count = sum(graphic.kind == "box" for graphic in overlays)
    polyline_overlay_count = len(overlays) - box_overlay_count
    return PathwayScene(
        target_id=target_id,
        title=name,
        width=image.width,
        height=image.height,
        source_png=image.content,
        overlays=overlays,
        caption=caption,
        retained_box_graphic_count=retained_box_graphic_count,
        retained_polyline_graphic_count=retained_polyline_graphic_count,
        mapped_detected_ko_ids=mapped_detected_ko_ids,
        box_overlay_count=box_overlay_count,
        polyline_overlay_count=polyline_overlay_count,
        asset_provenance=(image.provenance, kgml_asset.provenance),
        warnings=tuple(dict.fromkeys(warnings)),
    )


async def _retrieve_pair(
    provider: PathwayAssetProvider, pathway_id: str
) -> tuple[RetrievedAsset, RetrievedAsset]:
    # Sequential retrieval preserves the core client's no-burst deployment-wide rate policy.
    image = await provider.get_asset(pathway_id, "image")
    kgml = await provider.get_asset(pathway_id, "kgml")
    return image, kgml


def _accepted_overlays(kgml: KgmlDocument, accepted: frozenset[str]) -> tuple[KgmlGraphic, ...]:
    return tuple(graphic for graphic in kgml.graphics if accepted.intersection(graphic.ko_ids))


def _classify_probe_error(error: CoreKeggMcpError) -> ConnectivityStatus:
    details = {item.name: item.value for item in error.detail.safe_details}
    if error.detail.code is CoreErrorCode.KEGG_RATE_LIMITED:
        return ConnectivityStatus.RATE_LIMITED
    if details.get("status_code") in {"401", "403"}:
        return ConnectivityStatus.PERMISSION_DENIED
    transport = details.get("transport_kind")
    if transport is None:
        return ConnectivityStatus.UNKNOWN_FAILURE
    classifications: dict[str, ConnectivityStatus] = {
        "dns": ConnectivityStatus.DNS_FAILURE,
        "connection": ConnectivityStatus.CONNECTION_FAILURE,
        "timeout": ConnectivityStatus.TIMEOUT,
        "tls": ConnectivityStatus.TLS_FAILURE,
        "permission": ConnectivityStatus.PERMISSION_DENIED,
        "redirect_rejected": ConnectivityStatus.ENDPOINT_REJECTED,
        "invalid_request": ConnectivityStatus.ENDPOINT_REJECTED,
        "unsupported_encoding": ConnectivityStatus.ENDPOINT_REJECTED,
        "invalid_response": ConnectivityStatus.ENDPOINT_REJECTED,
        "response_too_large": ConnectivityStatus.ENDPOINT_REJECTED,
    }
    return classifications.get(transport, ConnectivityStatus.UNKNOWN_FAILURE)


def _translate_core_asset_error(error: CoreKeggMcpError, *, kind: str) -> RenderMcpError:
    core_code = error.detail.code
    details = {item.name: item.value for item in error.detail.safe_details}
    safe_details = [
        SafeDetail(name="asset_kind", value=kind),
        SafeDetail(name="core_error_code", value=core_code.value),
    ]
    for name in ("cache_state", "stage", "transport_kind", "status_code", "operation"):
        if value := details.get(name):
            safe_details.append(SafeDetail(name=name, value=value[:160]))

    if core_code is CoreErrorCode.CACHE_ENTRY_NOT_FOUND:
        renderer_code = ErrorCode.ASSET_UNAVAILABLE
        message = "The matching KEGG pathway asset is unavailable in the selected cache namespace."
        action = "Populate the selected cache namespace through authorized live access, then retry."
    elif core_code is CoreErrorCode.CACHE_FAILED:
        if details.get("stage") == "pathway_asset_validation":
            renderer_code = ErrorCode.ASSET_INVALID
            message = "A cached KEGG pathway asset failed bounded content validation."
            action = "Refresh or replace the affected cache entry through authorized access."
        else:
            renderer_code = ErrorCode.ASSET_UNAVAILABLE
            message = "The configured KEGG cache could not provide the pathway asset safely."
            action = "Inspect or replace the configured local KEGG cache, then retry."
    elif core_code is CoreErrorCode.KEGG_PARSE_FAILED:
        renderer_code = ErrorCode.ASSET_INVALID
        message = "The KEGG pathway asset failed bounded content validation."
        action = "Refresh the matching asset through an authorized endpoint, then retry."
    elif core_code is CoreErrorCode.INPUT_LIMIT_EXCEEDED:
        renderer_code = ErrorCode.INPUT_LIMIT_EXCEEDED
        message = "The KEGG pathway asset exceeded the active request or response limits."
        action = "Review the renderer asset bounds or select a supported pathway target."
    elif core_code is CoreErrorCode.KEGG_RATE_LIMITED:
        renderer_code = ErrorCode.ASSET_UNAVAILABLE
        message = "The configured KEGG endpoint rate-limited the pathway asset request."
        action = "Retry later while preserving the deployment-wide no-burst rate policy."
    elif core_code is CoreErrorCode.KEGG_REQUEST_FAILED:
        renderer_code = ErrorCode.ASSET_UNAVAILABLE
        message = "The configured KEGG endpoint did not return the pathway asset safely."
        action = "Run the bounded connectivity probe and retry after resolving the failure."
    else:
        renderer_code = ErrorCode.ASSET_UNAVAILABLE
        message = "The core KEGG client could not provide the matching pathway asset."
        action = "Check renderer access status and retry the bounded target."
    return RenderMcpError(
        ErrorDetail(
            code=renderer_code,
            message=message,
            suggested_action=action,
            safe_details=tuple(safe_details),
        )
    )


def _asset_invalid(message: str) -> RenderMcpError:
    return RenderMcpError(
        ErrorDetail(
            code=ErrorCode.ASSET_INVALID,
            message=message,
            suggested_action="Refresh the matching single-pathway assets and retry.",
        )
    )
