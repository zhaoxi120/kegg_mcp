"""Core MCP resources, templates, canonical URI routing, and bounded reads."""

from __future__ import annotations

import base64
import re

from anyio import lowlevel, to_thread
from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl, BaseModel, ValidationError

from kegg_mcp import __version__
from kegg_mcp.domain.errors import ErrorCode, ErrorDetail, KeggMcpError
from kegg_mcp.kegg import GetRequest, KeggBriteEntryKind, KeggEntryRef, KeggGetDatabase
from kegg_mcp.mcp.contracts import (
    ArtifactRangeEnvelope,
    CacheInfoResource,
    OversizedArtifactNotice,
    ResultResourceIndex,
)
from kegg_mcp.mcp.responses import internal_error
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.tool_registry import TOOL_NAMES
from kegg_mcp.services.kegg_entries import read_cached_kegg_entry
from kegg_mcp.services.operational import get_server_status_service
from kegg_mcp.services.result_store import (
    RESULT_ID_FRAGMENT,
    ResultArtifactMetadata,
    ResultStoreError,
)

MAX_INLINE_RESOURCE_BYTES = 64 * 1024
_RESOURCE_NOT_FOUND = -32002

_RESULT_RE = re.compile(rf"ko-analysis://results/({RESULT_ID_FRAGMENT})\Z")
_SECTION_RE = re.compile(
    rf"ko-analysis://results/({RESULT_ID_FRAGMENT})/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})\Z"
)
_RANGE_RE = re.compile(
    rf"ko-analysis://results/({RESULT_ID_FRAGMENT})/"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,127})/([0-9]{1,19})/([0-9]{1,5})\Z"
)
_CACHE_ENTRY_RE = re.compile(r"kegg-cache://entries/([a-z]+)/([A-Za-z0-9:._-]{1,100})\Z")


class InvalidResourceUri(ValueError):
    """One rejected non-canonical resource identifier."""


def resource_definitions() -> list[types.Resource]:
    return [
        types.Resource(
            name="server-status",
            title="KEGG MCP server status",
            uri=AnyUrl("ko-analysis://status"),
            description="Redacted server capabilities and access mode.",
            mimeType="application/json",
        ),
        types.Resource(
            name="cache-info",
            title="KEGG cache information",
            uri=AnyUrl("ko-analysis://cache/info"),
            description="Redacted local cache configuration without paths or credentials.",
            mimeType="application/json",
        ),
    ]


def resource_templates() -> list[types.ResourceTemplate]:
    return [
        types.ResourceTemplate(
            name="result-index",
            title="Scoped retained result",
            uriTemplate="ko-analysis://results/{result_id}",
            description="Metadata and validated section links for one scoped result.",
            mimeType="application/json",
        ),
        types.ResourceTemplate(
            name="result-section",
            title="Scoped retained result section",
            uriTemplate="ko-analysis://results/{result_id}/{section}",
            description=(
                "Returns a small section directly or a pagination notice for a large section."
            ),
        ),
        types.ResourceTemplate(
            name="result-section-range",
            title="Bounded retained result byte range",
            uriTemplate="ko-analysis://results/{result_id}/{section}/{offset}/{limit}",
            description="Returns at most 65536 bytes as base64 with an explicit continuation URI.",
            mimeType="application/json",
        ),
        types.ResourceTemplate(
            name="cached-kegg-entry",
            title="Cached KEGG entry",
            uriTemplate="kegg-cache://entries/{database}/{identifier}",
            description=(
                "Reads only the configured local cache namespace; never triggers network I/O."
            ),
            mimeType="application/json",
        ),
    ]


async def read_resource(uri: AnyUrl, runtime: McpRuntime) -> list[ReadResourceContents]:
    try:
        try:
            result = await to_thread.run_sync(
                _read_resource,
                str(uri),
                runtime,
                abandon_on_cancel=False,
                limiter=runtime.local_handler_limiter,
            )
        finally:
            await lowlevel.checkpoint_if_cancelled()
        return result
    except KeggMcpError as exception:
        raise McpError(
            types.ErrorData(
                code=(
                    _RESOURCE_NOT_FOUND
                    if exception.detail.code
                    in {ErrorCode.RESULT_NOT_FOUND, ErrorCode.CACHE_ENTRY_NOT_FOUND}
                    else types.INTERNAL_ERROR
                ),
                message=f"{exception.detail.code.value}: {exception.detail.message}",
                data=exception.detail.model_dump(mode="json"),
            )
        ) from None
    except ResultStoreError:
        detail = ErrorDetail(
            code=ErrorCode.RESULT_STORE_FAILED,
            message="The local retained-result store could not be used safely.",
            recoverable=True,
            suggested_action="Check local storage permissions and retry.",
        )
        raise McpError(
            types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"{detail.code.value}: {detail.message}",
                data=detail.model_dump(mode="json"),
            )
        ) from None
    except InvalidResourceUri:
        raise McpError(
            types.ErrorData(
                code=types.INVALID_PARAMS,
                message="INVALID_RESOURCE_URI: unknown or non-canonical resource URI",
            )
        ) from None
    except Exception as exception:
        detail = internal_error(exception, stage="resource_read")
        raise McpError(
            types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"{detail.code.value}: {detail.message}",
                data=detail.model_dump(mode="json"),
            )
        ) from None


def _read_resource(value: str, runtime: McpRuntime) -> list[ReadResourceContents]:
    if value == "ko-analysis://status":
        return [_json_resource(_status(runtime))]
    if value == "ko-analysis://cache/info":
        status = _status(runtime)
        return [
            _json_resource(
                CacheInfoResource(
                    access_mode=status.access_mode.value,
                    cache_endpoint_class=status.cache_endpoint_class.value,
                    network_enabled=status.network_enabled,
                )
            )
        ]
    if match := _RESULT_RE.fullmatch(value):
        result_id = match.group(1)
        metadata = runtime.result_store.get_result(runtime.scope_id, result_id)
        artifacts = _list_all_artifacts(
            runtime,
            result_id,
            expected_count=metadata.artifact_count,
        )
        return [
            _json_resource(
                ResultResourceIndex(
                    result=metadata,
                    artifacts=artifacts,
                    section_uris=tuple(
                        f"ko-analysis://results/{result_id}/{item.section}" for item in artifacts
                    ),
                )
            )
        ]
    if match := _RANGE_RE.fullmatch(value):
        result_id, section, offset_text, limit_text = match.groups()
        offset = int(offset_text)
        limit = int(limit_text)
        if offset > (1 << 63) - 1 or limit < 1 or limit > MAX_INLINE_RESOURCE_BYTES:
            raise InvalidResourceUri
        page = runtime.result_store.read_artifact(
            runtime.scope_id,
            result_id,
            section,
            offset=offset,
            limit=limit,
        )
        next_uri = (
            None
            if page.next_offset is None
            else f"ko-analysis://results/{result_id}/{section}/{page.next_offset}/{limit}"
        )
        return [
            _json_resource(
                ArtifactRangeEnvelope(
                    result_id=result_id,
                    section=section,
                    mime_type=page.mime_type,
                    total_bytes=page.total_bytes,
                    offset=page.offset,
                    returned_bytes=page.returned_bytes,
                    content_base64=base64.b64encode(page.content).decode("ascii"),
                    next_uri=next_uri,
                )
            )
        ]
    if match := _SECTION_RE.fullmatch(value):
        result_id, section = match.groups()
        page = runtime.result_store.read_artifact(
            runtime.scope_id,
            result_id,
            section,
            limit=MAX_INLINE_RESOURCE_BYTES,
        )
        if page.next_offset is None:
            content: str | bytes
            if page.mime_type.startswith("text/") or page.mime_type == "application/json":
                content = page.content.decode("utf-8", errors="strict")
            else:
                content = page.content
            return [ReadResourceContents(content=content, mime_type=page.mime_type)]
        return [
            _json_resource(
                OversizedArtifactNotice(
                    result_id=result_id,
                    section=section,
                    mime_type=page.mime_type,
                    total_bytes=page.total_bytes,
                    next_uri=(
                        f"ko-analysis://results/{result_id}/{section}/0/{MAX_INLINE_RESOURCE_BYTES}"
                    ),
                    maximum_range_bytes=MAX_INLINE_RESOURCE_BYTES,
                )
            )
        ]
    if match := _CACHE_ENTRY_RE.fullmatch(value):
        database_text, identifier = match.groups()
        try:
            database = KeggGetDatabase(database_text)
            entry = KeggEntryRef(
                database=database,
                identifier=identifier,
                brite_kind=(
                    KeggBriteEntryKind.HIERARCHY if database is KeggGetDatabase.BRITE else None
                ),
            )
        except (ValueError, ValidationError):
            raise InvalidResourceUri from None
        result = read_cached_kegg_entry(GetRequest(entries=(entry,)), client=runtime.client)
        return [_json_resource(result)]
    raise InvalidResourceUri


def _list_all_artifacts(
    runtime: McpRuntime,
    result_id: str,
    *,
    expected_count: int,
) -> tuple[ResultArtifactMetadata, ...]:
    """Collect every bounded artifact metadata page for one immutable result."""
    artifacts: list[ResultArtifactMetadata] = []
    offset = 0
    while len(artifacts) < expected_count:
        page = runtime.result_store.list_artifacts(
            runtime.scope_id,
            result_id,
            offset=offset,
            limit=runtime.result_store.limits.max_page_size,
        )
        if page.offset != offset or page.total_items != expected_count or not page.items:
            raise ResultStoreError("integrity_check")
        artifacts.extend(page.items)
        if page.next_offset is None:
            break
        if page.next_offset != offset + len(page.items) or len(artifacts) >= expected_count:
            raise ResultStoreError("integrity_check")
        offset = page.next_offset
    if len(artifacts) != expected_count:
        raise ResultStoreError("integrity_check")
    return tuple(artifacts)


def _status(runtime: McpRuntime):  # type annotation is inferred from the service contract
    return get_server_status_service(
        server_version=__version__,
        client=runtime.client,
        result_store=runtime.result_store,
        supported_tools=TOOL_NAMES,
        allowed_root_count=len(runtime.allowed_roots),
    )


def _json_resource(model: BaseModel) -> ReadResourceContents:
    return ReadResourceContents(content=model.model_dump_json(), mime_type="application/json")


__all__ = [
    "MAX_INLINE_RESOURCE_BYTES",
    "read_resource",
    "resource_definitions",
    "resource_templates",
]
