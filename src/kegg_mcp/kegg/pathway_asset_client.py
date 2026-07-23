"""Cache-aware execution for typed KEGG pathway asset requests."""

from __future__ import annotations

from typing import NoReturn

from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg.contracts import (
    CacheLookupState,
    HttpMetadata,
    KeggRequestOptions,
)
from kegg_mcp.kegg.executor import KeggRequestExecutor
from kegg_mcp.kegg.operations import PreparedRequest
from kegg_mcp.kegg.pathway_assets import (
    PATHWAY_ASSET_PARSER_VERSION,
    PathwayAssetRequest,
    PathwayAssetResult,
    validate_pathway_asset_content,
)


class PathwayAssetClient:
    """Retrieve one validated pathway asset through the shared executor policy."""

    def __init__(
        self,
        executor: KeggRequestExecutor,
    ) -> None:
        self._executor = executor

    def execute(
        self,
        request: PathwayAssetRequest,
        prepared: PreparedRequest,
        options: KeggRequestOptions,
    ) -> PathwayAssetResult:
        executed = self._executor._execute_payload(  # pyright: ignore[reportPrivateUsage]
            prepared,
            options,
            parser_version=PATHWAY_ASSET_PARSER_VERSION,
            decode=lambda body, metadata: self._validate_payload(request, body, metadata),
            database_release=lambda _payload: None,
            cache_only_failure=self._raise_asset_cache_miss,
            cached_validation_failure=self._raise_cached_asset_failure,
        )
        mime_type, width, height = executed.value
        return PathwayAssetResult(
            request=request,
            content=executed.body,
            mime_type=mime_type,
            width=width,
            height=height,
            provenance=executed.provenance,
        )

    @staticmethod
    def _raise_asset_cache_miss(cache_state: CacheLookupState) -> NoReturn:
        fail(
            ErrorCode.CACHE_ENTRY_NOT_FOUND,
            "The requested pathway asset is unavailable in the selected cache namespace.",
            suggested_action="Fetch the asset through an ordinary network-enabled request.",
            safe_details=(SafeDetail(name="cache_state", value=cache_state.value),),
        )

    @staticmethod
    def _raise_cached_asset_failure() -> NoReturn:
        fail(
            ErrorCode.CACHE_FAILED,
            "A cached pathway asset failed content validation.",
            suggested_action="Refresh or remove the affected local cache entry.",
            safe_details=(SafeDetail(name="stage", value="pathway_asset_validation"),),
        )

    @staticmethod
    def _validate_payload(
        request: PathwayAssetRequest,
        body: bytes,
        http_metadata: tuple[HttpMetadata, ...],
    ) -> tuple[str, int | None, int | None]:
        content_types = tuple(item.value for item in http_metadata if item.name == "content-type")
        try:
            if len(content_types) > 1:
                raise ValueError("multiple content types are not supported")
            return validate_pathway_asset_content(
                request,
                body,
                content_type=content_types[0] if content_types else None,
            )
        except ValueError:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The pathway asset response failed bounded content validation.",
                suggested_action="Refresh the asset or verify endpoint compatibility.",
                safe_details=(SafeDetail(name="asset_kind", value=request.kind.value),),
            )


__all__ = ["PathwayAssetClient"]
