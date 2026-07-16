"""MCP 1.x discovery, execution, error, and resource contracts."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from kegg_mcp.kegg import (
    CachePolicy,
    GetRequest,
    GetResult,
    KeggClient,
    KeggClientConfig,
    KeggEntryRef,
    KeggGetDatabase,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    ResponseOrigin,
    RetrievalEndpointClass,
    RetryPolicy,
)
from kegg_mcp.kegg.cache import SQLiteKeggCache
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    KeggPairRow,
)
from kegg_mcp.kegg.operations import prepare_get
from kegg_mcp.kegg.parsers import parse_flat_file_response
from kegg_mcp.kegg.transport import TransportError, TransportErrorKind, TransportResponse
from kegg_mcp.mcp.server import (
    MAX_INLINE_RESOURCE_BYTES,
    TOOL_NAMES,
    McpRuntime,
    create_server,
)
from kegg_mcp.services import (
    RENDER_INPUT_MIME_TYPE,
    RENDER_INPUT_SCHEMA_VERSION,
    RenderInput,
    ResultArtifactInput,
    ResultStoreLimits,
    SQLiteResultStore,
)

_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def _provenance(operation: KeggOperation, marker: str) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        request_key=f"synthetic:{marker}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        response_bytes=256,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="synthetic-contract-release",
        attempt_count=1,
        is_stale=False,
    )


class _FakeReferenceClient:
    def __init__(self) -> None:
        self._config = KeggClientConfig()
        self.call_log: list[tuple[str, str]] = []

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del options
        first = request.entries[0]
        self.call_log.append(("get", first.identifier))
        if first.database is KeggGetDatabase.MODULE:
            body = (
                b"ENTRY       M00001            Module\n"
                b"NAME        Synthetic module\n"
                b"DEFINITION  K00001 K00002\n"
                b"///\n"
            )
            marker = "1"
        elif first.database is KeggGetDatabase.PATHWAY:
            body = (
                f"ENTRY       {first.identifier}                    Pathway\n"
                "NAME        Synthetic pathway\n"
                "CLASS       Metabolism; Carbohydrate metabolism\n"
                "///\n"
            ).encode("ascii")
            marker = "2"
        else:
            entries = b"".join(
                (
                    f"ENTRY       {entry.identifier}            KO\n"
                    f"NAME        Synthetic {entry.identifier}\n"
                    + "".join(
                        f"F{index:03d}        Synthetic field {index}\n" for index in range(70)
                    )
                    + "///\n"
                ).encode("ascii")
                for entry in request.entries
            )
            body = entries
            marker = "4"
        return GetResult(
            request=request,
            documents=(parse_flat_file_response(body),),
            missing_entries=(),
            batches=(_provenance(KeggOperation.GET, marker),),
        )

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        del options
        self.call_log.append(("link", request.relationship.value))
        if request.relationship.value == "pathway_to_ko":
            source = request.source_identifiers[0]
            rows = (
                KeggPairRow(line_number=1, source_id=f"path:{source}", target_id="ko:K00001"),
                KeggPairRow(line_number=2, source_id=f"path:{source}", target_id="ko:K00003"),
            )
        else:
            rows = tuple(
                KeggPairRow(
                    line_number=index,
                    source_id=f"ko:{ko_id}",
                    target_id=f"path:ko{index:05d}",
                )
                for index, ko_id in enumerate(request.source_identifiers, start=1)
            )
        return LinkResult(
            request=request,
            rows=rows,
            batches=(_provenance(KeggOperation.LINK, "3"),),
        )


class _LargeRankingReferenceClient(_FakeReferenceClient):
    """Synthetic 73-KO mapping with 115 candidates and 562 complete rows."""

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        if request.relationship.value != "ko_to_pathway":
            return super().link(request, options=options)
        del options
        self.call_log.append(("link", request.relationship.value))
        rows = tuple(
            KeggPairRow(
                line_number=index + 1,
                source_id=f"ko:{request.source_identifiers[index % 73]}",
                target_id=f"path:ko{(index % 115) + 1:05d}",
            )
            for index in range(562)
        )
        return LinkResult(
            request=request,
            rows=rows,
            batches=(_provenance(KeggOperation.LINK, "ranking-562"),),
        )


class _InternalFailureClient(_FakeReferenceClient):
    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del request, options
        raise ValueError("private implementation detail")


class _UnavailableTransport:
    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        del url, timeout_seconds, max_response_bytes
        raise TransportError(TransportErrorKind.CONNECTION, transient=False)


def _runtime(
    tmp_path: Path,
    *,
    scope_id: str = "contract-scope",
    allowed_roots: tuple[str, ...] = (),
) -> McpRuntime:
    return McpRuntime(
        client=KeggClient(
            KeggClientConfig(
                cache=CachePolicy(path=str(tmp_path / "kegg.sqlite3")),
                retry=RetryPolicy(max_retries=0),
            ),
            transport=_UnavailableTransport(),
        ),
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id=scope_id,
        allowed_roots=allowed_roots,
    )


def _fake_runtime(tmp_path: Path, *, scope_id: str = "contract-scope") -> McpRuntime:
    return McpRuntime(
        client=_FakeReferenceClient(),
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id=scope_id,
        allowed_roots=(str(tmp_path.resolve()),),
    )


def _tool_by_name(tools: list[types.Tool], name: str) -> types.Tool:
    return next(tool for tool in tools if tool.name == name)


def _validate_result(tool: types.Tool, result: types.CallToolResult) -> None:
    assert result.structuredContent is not None
    assert tool.outputSchema is not None
    Draft202012Validator(tool.outputSchema).validate(  # pyright: ignore[reportUnknownMemberType]
        result.structuredContent
    )


@pytest.mark.asyncio
async def test_discovery_declares_all_tools_annotations_and_resources(tmp_path: Path) -> None:
    server = create_server(_runtime(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        assert tuple(tool.name for tool in tools) == TOOL_NAMES
        assert len(TOOL_NAMES) == len(set(TOOL_NAMES))
        for tool in tools:
            assert tool.title
            assert tool.description
            assert tool.inputSchema.get("additionalProperties") is False
            assert tool.outputSchema is not None
            assert tool.annotations is not None
        status = _tool_by_name(tools, "get_server_status")
        assert status.annotations is not None
        assert status.annotations.readOnlyHint is True
        assert status.annotations.idempotentHint is True
        assert status.annotations.openWorldHint is False
        for name in (
            "analyze_ko_annotations",
            "get_kegg_entries",
            "map_ko_ids",
            "analyze_modules",
            "analyze_pathways",
            "compare_ko_sets",
            "probe_kegg_connectivity",
        ):
            annotations = _tool_by_name(tools, name).annotations
            assert annotations is not None
            assert annotations.readOnlyHint is False
            assert annotations.destructiveHint is False
            assert annotations.idempotentHint is False
            assert annotations.openWorldHint is True

        normalize_annotations = _tool_by_name(tools, "normalize_ko_annotations").annotations
        assert normalize_annotations is not None
        assert normalize_annotations.readOnlyHint is False
        assert normalize_annotations.destructiveHint is False
        assert normalize_annotations.idempotentHint is False
        assert normalize_annotations.openWorldHint is False
        delete_annotations = _tool_by_name(tools, "delete_analysis_result").annotations
        assert delete_annotations is not None
        assert delete_annotations.readOnlyHint is False
        assert delete_annotations.destructiveHint is True
        assert delete_annotations.idempotentHint is True
        assert delete_annotations.openWorldHint is False

        normalize_schema = _tool_by_name(tools, "normalize_ko_annotations").inputSchema
        normalize_properties = normalize_schema["properties"]
        assert {"text", "file_path", "output_directory", "manifest_path_mode"} <= set(
            normalize_properties
        )
        for internal_field in ("limits", "refresh", "allow_stale"):
            assert internal_field not in normalize_properties
        mapping_schema = _tool_by_name(tools, "map_ko_ids").inputSchema
        assert set(mapping_schema["properties"]) == {"ko_ids", "target"}
        analysis_schema = _tool_by_name(tools, "analyze_ko_annotations").inputSchema
        assert "pathway_selection" in analysis_schema["properties"]
        selection_schema = analysis_schema["$defs"]["PathwaySelection"]["properties"]
        assert selection_schema["top_n"]["minimum"] == 1
        assert selection_schema["top_n"]["maximum"] == 25
        delete_schema = _tool_by_name(tools, "delete_analysis_result").inputSchema
        assert set(delete_schema["properties"]) == {"result_id"}
        entries_output_schema = _tool_by_name(tools, "get_kegg_entries").outputSchema
        assert entries_output_schema is not None
        assert (
            entries_output_schema["$defs"]["KeggBatchProvenance"]["properties"]["http_metadata"][
                "maxItems"
            ]
            == 16
        )

        for name in (
            "get_kegg_entries",
            "analyze_modules",
            "analyze_pathways",
            "compare_ko_sets",
        ):
            properties = _tool_by_name(tools, name).inputSchema["properties"]
            assert "refresh" not in properties
            assert "allow_stale" not in properties
            assert "limits" not in properties

        entries_output_defs = entries_output_schema["$defs"]
        assert (
            entries_output_defs["KeggEntryPreview"]["properties"]["field_names"]["maxItems"] == 64
        )
        entries_result_properties = entries_output_defs["KeggEntriesServiceResult"]["properties"]
        assert entries_result_properties["missing_identifiers"]["maxItems"] == 50
        assert entries_result_properties["previews"]["maxItems"] == 50
        assert entries_result_properties["provenance"]["maxItems"] == 5

        normalize_output = _tool_by_name(tools, "normalize_ko_annotations").outputSchema
        assert normalize_output is not None
        normalize_output_defs = normalize_output["$defs"]
        status_counts_schema = normalize_output_defs["ImportSummary"]["properties"]["status_counts"]
        assert status_counts_schema["minItems"] == 5
        assert status_counts_schema["maxItems"] == 5
        error_properties = normalize_output_defs["ErrorDetail"]["properties"]
        assert error_properties["safe_details"]["maxItems"] == 32

        module_output = _tool_by_name(tools, "analyze_modules").outputSchema
        assert module_output is not None
        primitive_properties = module_output["$defs"]["PrimitiveAnalysisResult"]["properties"]
        assert primitive_properties["module_previews"]["maxItems"] == 25
        assert primitive_properties["pathway_previews"]["maxItems"] == 25
        assert primitive_properties["caveats"]["maxItems"] == 3
        assert primitive_properties["reference_provenance"]["maxItems"] == 100
        assert primitive_properties["selected_pathways"]["maxItems"] == 25
        assert primitive_properties["execution_metrics"]["minItems"] == 6
        assert primitive_properties["execution_metrics"]["maxItems"] == 6

        mapping_output = _tool_by_name(tools, "map_ko_ids").outputSchema
        assert mapping_output is not None
        mapping_properties = mapping_output["$defs"]["KoMappingServiceResult"]["properties"]
        assert mapping_properties["row_preview"]["maxItems"] == 200
        assert mapping_properties["provenance"]["maxItems"] == 10

        comparison_output = _tool_by_name(tools, "compare_ko_sets").outputSchema
        assert comparison_output is not None
        comparison_output_defs = comparison_output["$defs"]
        assert comparison_output_defs["KoPreview"]["properties"]["ko_ids"]["maxItems"] == 100
        class_properties = comparison_output_defs["KoClassComparisonSummary"]["properties"]
        assert class_properties["set_specific"]["maxItems"] == 10
        assert class_properties["partially_shared_patterns_preview"]["maxItems"] == 256
        membership_properties = comparison_output_defs["KoMembershipPatternPreview"]["properties"]
        assert membership_properties["member_set_indexes"]["maxItems"] == 10
        assert membership_properties["member_labels"]["maxItems"] == 10
        assert membership_properties["member_labels"]["items"]["maxLength"] == 128

        status_output = _tool_by_name(tools, "get_server_status").outputSchema
        assert status_output is not None
        status_properties = status_output["$defs"]["ServerStatusResult"]["properties"]
        assert status_properties["allowed_root_count"]["minimum"] == 0
        assert status_properties["supported_input_formats"]["maxItems"] == 4
        assert status_properties["supported_tools"]["maxItems"] == 16

        resources = (await session.list_resources()).resources
        assert {str(resource.uri) for resource in resources} == {
            "ko-analysis://status",
            "ko-analysis://cache/info",
        }
        templates = (await session.list_resource_templates()).resourceTemplates
        assert {template.uriTemplate for template in templates} == {
            "ko-analysis://results/{result_id}",
            "ko-analysis://results/{result_id}/{section}",
            "ko-analysis://results/{result_id}/{section}/{offset}/{limit}",
            "kegg-cache://entries/{database}/{identifier}",
        }


@pytest.mark.asyncio
async def test_status_and_normalize_return_schema_valid_non_erased_data(tmp_path: Path) -> None:
    server = create_server(_runtime(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        status = await session.call_tool("get_server_status", {})
        _validate_result(_tool_by_name(tools, "get_server_status"), status)
        assert status.isError is False
        assert status.structuredContent is not None
        status_data = status.structuredContent["result"]["data"]
        assert status_data["transport"] == "stdio"
        assert status_data["access_mode"] == "public_academic"
        assert status_data["network_enabled"] is True
        assert status_data["file_handoff_enabled"] is False
        assert status_data["allowed_root_count"] == 0
        assert status_data["connectivity"] == "not_probed"
        assert status_data["inspection_status"] == "not_probed"
        assert status_data["entry_count"] is None
        assert status_data["stored_payload_bytes"] is None
        assert status_data["newest_entry_age_seconds"] is None
        assert status_data["result_scope"] == "stdio_session"
        assert status_data["result_active_ttl_seconds"] == 24 * 60 * 60
        assert status_data["orphan_cleanup_after_seconds"] == 24 * 60 * 60
        assert status_data["normal_exit_scope_cleanup"] is True
        assert status_data["durable_output"] == "output_bundle"
        serialized_status = json.dumps(status.structuredContent)
        assert "/" not in status_data.get("cache_location", "")
        assert "rest.kegg.jp" not in serialized_status
        assert "KEGG_MCP_" not in serialized_status

        probe = await session.call_tool("probe_kegg_connectivity", {})
        _validate_result(_tool_by_name(tools, "probe_kegg_connectivity"), probe)
        assert probe.isError is False
        assert probe.structuredContent is not None
        assert probe.structuredContent["result"]["data"]["state"] == "connection_failure"

        normalized = await session.call_tool(
            "normalize_ko_annotations",
            {"text": "K00844\nko:K01810\ninvalid"},
        )
        _validate_result(_tool_by_name(tools, "normalize_ko_annotations"), normalized)
        assert normalized.isError is False
        assert normalized.structuredContent is not None
        data = normalized.structuredContent["result"]["data"]
        assert data["import_summary"]["input_rows"] == 3
        assert data["import_summary"]["emitted_records"] == 3
        assert data["result"]["result_id"].startswith("res_")
        uri = normalized.structuredContent["result"]["resource_uri"]
        assert uri == f"ko-analysis://results/{data['result']['result_id']}"

        index = await session.read_resource(AnyUrl(uri))
        index_content = index.contents[0]
        assert isinstance(index_content, types.TextResourceContents)
        index_data = json.loads(index_content.text)
        assert index_data["artifacts"][0]["section"] == "dataset"
        dataset_resource = await session.read_resource(AnyUrl(index_data["section_uris"][0]))
        dataset_content = dataset_resource.contents[0]
        assert isinstance(dataset_content, types.TextResourceContents)
        assert (
            json.loads(dataset_content.text)["dataset_id"] == data["import_summary"]["dataset_id"]
        )

        deleted = await session.call_tool(
            "delete_analysis_result",
            {"result_id": data["result"]["result_id"]},
        )
        _validate_result(_tool_by_name(tools, "delete_analysis_result"), deleted)
        assert deleted.isError is False
        assert deleted.structuredContent is not None
        deleted_data = deleted.structuredContent["result"]["data"]
        assert deleted_data["result_id"] == data["result"]["result_id"]
        assert deleted_data["deleted_artifacts"] == 1
        with pytest.raises(McpError, match="RESULT_NOT_FOUND"):
            await session.read_resource(AnyUrl(uri))

        repeated = await session.call_tool(
            "delete_analysis_result",
            {"result_id": data["result"]["result_id"]},
        )
        assert repeated.isError is True
        assert repeated.structuredContent is not None
        assert repeated.structuredContent["error"]["code"] == "RESULT_NOT_FOUND"


@pytest.mark.asyncio
async def test_recoverable_execution_errors_are_typed_and_schema_valid(tmp_path: Path) -> None:
    server = create_server(_runtime(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        invalid = await session.call_tool(
            "normalize_ko_annotations",
            {"text": "K00844", "unexpected": True},
        )
        _validate_result(_tool_by_name(tools, "normalize_ko_annotations"), invalid)
        assert invalid.isError is True
        assert invalid.structuredContent is not None
        assert invalid.structuredContent["ok"] is False
        assert invalid.structuredContent["error"]["code"] == "ANALYSIS_CONFIGURATION_INVALID"
        invalid_details = {
            item["name"]: item["value"]
            for item in invalid.structuredContent["error"]["safe_details"]
        }
        assert invalid_details["stage"] == "input_validation"
        assert invalid_details["field_path"] == "unexpected"

        request_failure = await session.call_tool(
            "get_kegg_entries",
            {"entries": [{"database": "ko", "identifier": "K00844"}]},
        )
        _validate_result(_tool_by_name(tools, "get_kegg_entries"), request_failure)
        assert request_failure.isError is True
        assert request_failure.structuredContent is not None
        assert request_failure.structuredContent["error"]["code"] == "KEGG_REQUEST_FAILED"

        metadata_value = "x" * 16_384
        bounded_metadata = await session.call_tool(
            "normalize_ko_annotations",
            {
                "text": "K00844",
                "source": {
                    "source_name": "manual",
                    "source_metadata": [{"name": "note", "value": metadata_value}],
                },
            },
        )
        _validate_result(_tool_by_name(tools, "normalize_ko_annotations"), bounded_metadata)
        assert bounded_metadata.isError is False
        assert bounded_metadata.structuredContent is not None
        assert metadata_value not in json.dumps(bounded_metadata.structuredContent)
        metadata_uri = bounded_metadata.structuredContent["result"]["resource_uri"]
        metadata_index = await session.read_resource(AnyUrl(metadata_uri))
        metadata_index_content = metadata_index.contents[0]
        assert isinstance(metadata_index_content, types.TextResourceContents)
        metadata_index_data = json.loads(metadata_index_content.text)
        metadata_dataset = await session.read_resource(
            AnyUrl(metadata_index_data["section_uris"][0])
        )
        metadata_dataset_content = metadata_dataset.contents[0]
        assert isinstance(metadata_dataset_content, types.TextResourceContents)
        assert metadata_value in metadata_dataset_content.text
        retained_id = bounded_metadata.structuredContent["result"]["data"]["result"]["result_id"]
        compared = await session.call_tool(
            "compare_ko_sets",
            {
                "inputs": [
                    {"label": "retained", "source": {"result_id": retained_id}},
                    {"label": "inline", "source": {"ko_text": "K01810"}},
                ]
            },
        )
        _validate_result(_tool_by_name(tools, "compare_ko_sets"), compared)
        assert compared.isError is False
        assert compared.structuredContent is not None
        assert metadata_value not in json.dumps(compared.structuredContent)
        comparison_uri = compared.structuredContent["result"]["resource_uri"]
        comparison_index = await session.read_resource(AnyUrl(comparison_uri))
        comparison_index_content = comparison_index.contents[0]
        assert isinstance(comparison_index_content, types.TextResourceContents)
        comparison_detail_uri = json.loads(comparison_index_content.text)["section_uris"][0]
        comparison_detail = await session.read_resource(AnyUrl(comparison_detail_uri))
        comparison_detail_content = comparison_detail.contents[0]
        assert isinstance(comparison_detail_content, types.TextResourceContents)
        assert metadata_value in comparison_detail_content.text
        oversized_metadata = await session.call_tool(
            "normalize_ko_annotations",
            {
                "text": "K00844",
                "source": {
                    "source_name": "manual",
                    "source_metadata": [{"name": "note", "value": "x" * 16_385}],
                },
            },
        )
        assert oversized_metadata.isError is True
        assert oversized_metadata.structuredContent is not None
        assert oversized_metadata.structuredContent["error"]["code"] == (
            "ANALYSIS_CONFIGURATION_INVALID"
        )

        relaxed_limits = await session.call_tool(
            "normalize_ko_annotations",
            {
                "text": "K00844",
                "import_limits": {
                    "max_bytes": 5_000_000,
                    "max_rows": 100_001,
                    "max_columns": 64,
                    "max_field_length": 16_384,
                },
            },
        )
        assert relaxed_limits.isError is True

        relaxed_network_budget = await session.call_tool(
            "analyze_modules",
            {
                "source": {"ko_text": "K00844"},
                "module_ids": ["M00001"],
                "reference_limits": {"max_total_kegg_requests": 101},
            },
        )
        assert relaxed_network_budget.isError is True


@pytest.mark.asyncio
async def test_unexpected_internal_failure_uses_safe_correlation_id(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runtime = McpRuntime(
        client=_InternalFailureClient(),
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id="internal-failure-scope",
    )
    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        result = await session.call_tool(
            "get_kegg_entries",
            {"entries": [{"database": "ko", "identifier": "K00844"}]},
        )
    assert result.isError is True
    assert result.structuredContent is not None
    error = result.structuredContent["error"]
    assert error["code"] == "INTERNAL_ERROR"
    details = {item["name"]: item["value"] for item in error["safe_details"]}
    assert details["correlation_id"].startswith("err_")
    assert details["stage"] == "tool:get_kegg_entries"
    serialized = json.dumps(result.structuredContent)
    assert "private implementation detail" not in serialized
    captured = capsys.readouterr()
    assert details["correlation_id"] in captured.err
    assert "ValueError" in captured.err
    assert "private implementation detail" not in captured.err


@pytest.mark.asyncio
async def test_high_level_schema_accepts_table_input_and_rejects_organism_context(
    tmp_path: Path,
) -> None:
    server = create_server(_runtime(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        result = await session.call_tool(
            "analyze_ko_annotations",
            {
                "annotations": {
                    "text": "sequence,ko\nseq-1,K00844",
                    "input_format": "generic_csv",
                    "column_mapping": {"sequence_id": "sequence", "ko_id": "ko"},
                    "decision_policy": "user_supplied_ko",
                },
                "module_ids": ["M00001"],
            },
        )
        _validate_result(_tool_by_name(tools, "analyze_ko_annotations"), result)
        assert result.isError is True
        assert result.structuredContent is not None
        assert result.structuredContent["error"]["code"] == "KEGG_REQUEST_FAILED"

        conflicting_context = await session.call_tool(
            "analyze_ko_annotations",
            {
                "annotations": {"text": "K00844"},
                "analysis_unit": "metagenomic_community",
                "module_ids": ["M00001"],
            },
        )
        assert conflicting_context.isError is True
        assert conflicting_context.structuredContent is not None
        assert conflicting_context.structuredContent["error"]["code"] == (
            "ANALYSIS_CONFIGURATION_INVALID"
        )

        ignored_preview = await session.call_tool(
            "analyze_ko_annotations",
            {
                "annotations": {"text": "K00844", "preview_limit": 1},
                "module_ids": ["M00001"],
            },
        )
        assert ignored_preview.isError is True
        assert ignored_preview.structuredContent is not None
        assert ignored_preview.structuredContent["error"]["code"] == (
            "ANALYSIS_CONFIGURATION_INVALID"
        )

        organism = await session.call_tool(
            "analyze_pathways",
            {
                "source": {"ko_text": "K00844"},
                "pathways": [{"pathway_id": "hsa00010", "reference_namespace": "organism"}],
            },
        )
        _validate_result(_tool_by_name(tools, "analyze_pathways"), organism)
        assert organism.isError is True
        assert organism.structuredContent is not None
        assert organism.structuredContent["error"]["code"] == ("ANALYSIS_CONFIGURATION_INVALID")

        mixed_selection = await session.call_tool(
            "analyze_ko_annotations",
            {
                "ko_text": "K00844",
                "pathways": [{"pathway_id": "ko00010"}],
                "pathway_selection": {"mode": "top_detected", "top_n": 1},
            },
        )
        assert mixed_selection.isError is True
        assert mixed_selection.structuredContent is not None
        assert mixed_selection.structuredContent["error"]["code"] == (
            "ANALYSIS_CONFIGURATION_INVALID"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("annotation_date", ["2026-07-15T10:20:30Z", "2026-07-15T19:20:30+09:00"])
async def test_file_handoff_json_round_trip_and_normalization_bundle(
    tmp_path: Path,
    annotation_date: str,
) -> None:
    fasta = tmp_path / "proteins.faa"
    annotations = tmp_path / "deepkoala_annotations.csv"
    output = tmp_path / "normalized"
    fasta.write_text(">protein-1 alpha enzyme\nMAAA\n", encoding="utf-8")
    annotations.write_text(
        "sequence_id,protein_name,ko_id\nprotein-1,alpha enzyme,K00001\n",
        encoding="utf-8",
    )
    server = create_server(_runtime(tmp_path, allowed_roots=(str(tmp_path.resolve()),)))

    async with create_connected_server_and_client_session(server) as session:
        status = await session.call_tool("get_server_status", {})
        assert status.structuredContent is not None
        status_data = status.structuredContent["result"]["data"]
        assert status_data["file_handoff_enabled"] is True
        assert status_data["allowed_root_count"] == 1

        result = await session.call_tool(
            "normalize_ko_annotations",
            {
                "file_path": str(annotations),
                "output_directory": str(output),
                "input_format": "generic_csv",
                "source": {
                    "source_name": "deepkoala",
                    "source_version": "2025.02",
                    "model_name": "full",
                    "annotation_date": annotation_date,
                    "input_path": str(fasta),
                },
            },
        )

        assert result.isError is False
        assert result.structuredContent is not None
        data = result.structuredContent["result"]["data"]
        assert data["column_mapping_inferred"] is True
        assert data["provenance"]["source_preview"][0]["input_path"] == str(fasta)
        assert data["provenance"]["source_preview"][0]["annotation_date"].endswith(("Z", "+09:00"))
        bundle = data["output_bundle"]
        assert bundle["output_directory"] == str(output)
        assert Path(bundle["normalized_annotations"]).is_file()
        normalized = Path(bundle["normalized_annotations"]).read_text(encoding="utf-8")
        assert "protein_name" in normalized
        assert "alpha enzyme" in normalized
        manifest_path = Path(bundle["manifest"])
        manifest_text = manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        assert manifest["schema_version"] == "2"
        assert manifest["input_path_provenance"] == {
            "mode": "redacted",
            "source_count": 1,
            "values": ["input-1"],
        }
        assert str(fasta) not in manifest_text
        assert "bundle_manifest.json" in manifest["files"]

        repeated = await session.call_tool(
            "normalize_ko_annotations",
            {
                "file_path": str(annotations),
                "output_directory": str(output),
                "input_format": "generic_csv",
            },
        )
        assert repeated.isError is True
        assert repeated.structuredContent is not None
        assert repeated.structuredContent["error"]["code"] == "OUTPUT_ALREADY_EXISTS"
        assert manifest_path.read_text(encoding="utf-8") == manifest_text

        private_output = tmp_path / "normalized-private-manifest"
        private_manifest_result = await session.call_tool(
            "normalize_ko_annotations",
            {
                "file_path": str(annotations),
                "output_directory": str(private_output),
                "input_format": "generic_csv",
                "manifest_path_mode": "absolute",
                "source": {
                    "source_name": "deepkoala",
                    "input_path": str(fasta),
                },
            },
        )
        assert private_manifest_result.isError is False
        assert private_manifest_result.structuredContent is not None
        private_bundle = private_manifest_result.structuredContent["result"]["data"][
            "output_bundle"
        ]
        private_manifest = json.loads(Path(private_bundle["manifest"]).read_text(encoding="utf-8"))
        assert private_manifest["input_path_provenance"] == {
            "mode": "absolute",
            "source_count": 1,
            "values": [str(fasta)],
        }


@pytest.mark.asyncio
async def test_explicit_inline_source_keeps_null_original_input_path(tmp_path: Path) -> None:
    annotations = tmp_path / "deepkoala_annotations.csv"
    output = tmp_path / "normalized-inline"
    annotations.write_text(
        "sequence_id,ko_id\nprotein-1,K00001\n",
        encoding="utf-8",
    )
    server = create_server(_runtime(tmp_path, allowed_roots=(str(tmp_path.resolve()),)))

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "normalize_ko_annotations",
            {
                "file_path": str(annotations),
                "output_directory": str(output),
                "input_format": "generic_csv",
                "source": {
                    "source_name": "deepkoala",
                    "input_uri": "mcp://deepkoala-mcp/jobs/job_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/output",
                    "input_path": None,
                },
            },
        )

        assert result.isError is False
        assert result.structuredContent is not None
        data = result.structuredContent["result"]["data"]
        assert data["provenance"]["source_preview"][0]["input_path"] is None
        manifest = json.loads(Path(data["output_bundle"]["manifest"]).read_text(encoding="utf-8"))
        assert manifest["input_path_provenance"] == {
            "mode": "redacted",
            "source_count": 0,
            "values": [],
        }


@pytest.mark.asyncio
async def test_high_level_file_workflow_discovers_pathway_and_writes_report(
    tmp_path: Path,
) -> None:
    fasta = tmp_path / "proteins.faa"
    annotations = tmp_path / "deepkoala_annotations.csv"
    output = tmp_path / "analysis"
    fasta.write_text(">protein-1 alpha enzyme\nMAAA\n", encoding="utf-8")
    annotations.write_text(
        "sequence_id,protein_name,ko_id\nprotein-1,alpha enzyme,K00001\n",
        encoding="utf-8",
    )
    server = create_server(_fake_runtime(tmp_path))

    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "analyze_ko_annotations",
            {
                "annotations": {
                    "file_path": str(annotations),
                    "input_format": "generic_csv",
                    "source": {
                        "source_name": "deepkoala",
                        "input_path": str(fasta),
                    },
                },
                "output_directory": str(output),
            },
        )

        assert result.isError is False
        assert result.structuredContent is not None
        data = result.structuredContent["result"]["data"]
        assert data["pathway_target_count"] == 1
        assert data["pathway_previews"][0]["pathway_id"] == "ko00001"
        report_path = Path(data["output_bundle"]["analysis_report"])
        assert str(fasta) in report_path.read_text(encoding="utf-8")
        render_input_path = Path(data["output_bundle"]["render_input"])
        assert render_input_path.is_file()
        render_input = RenderInput.model_validate_json(
            render_input_path.read_text(encoding="utf-8"),
            strict=True,
        )
        assert render_input.schema_version == RENDER_INPUT_SCHEMA_VERSION
        assert render_input.pathways[0].detected_ko_ids == ("K00001",)
        manifest = json.loads(Path(data["output_bundle"]["manifest"]).read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "2"
        assert manifest["render_input"] == {
            "schema_version": RENDER_INPUT_SCHEMA_VERSION,
            "mime_type": RENDER_INPUT_MIME_TYPE,
        }


@pytest.mark.asyncio
async def test_top_one_selection_ranks_large_mapping_before_loading_references(
    tmp_path: Path,
) -> None:
    client = _LargeRankingReferenceClient()
    runtime = McpRuntime(
        client=client,
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id="top-one-contract",
        allowed_roots=(str(tmp_path.resolve()),),
    )
    server = create_server(runtime)
    output = tmp_path / "top-one"
    ko_text = "\n".join(f"K{index:05d}" for index in range(1, 74))

    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        result = await session.call_tool(
            "analyze_ko_annotations",
            {
                "ko_text": ko_text,
                "pathway_selection": {
                    "mode": "top_detected",
                    "top_n": 1,
                    "metric": "unique_selected_ko_count",
                },
                "output_directory": str(output),
            },
        )

        _validate_result(_tool_by_name(tools, "analyze_ko_annotations"), result)
        assert result.isError is False
        assert result.structuredContent is not None
        data = result.structuredContent["result"]["data"]
        assert data["input_records"] == 73
        assert data["accepted_records"] == 73
        assert data["selected_unique_ko_count"] == 73
        assert data["candidate_pathway_count"] == 115
        assert data["selected_pathways"] == [
            {
                "rank": 1,
                "pathway_id": "ko00001",
                "pathway_number": "00001",
                "detected_unique_ko_count": 5,
                "relationship_row_count": 5,
            }
        ]
        assert data["pathway_target_count"] == 1
        assert data["reference_provenance"] == []
        assert data["kegg_request_count"] == 3
        assert data["network_request_count"] == 3
        assert data["cache_hit_count"] == 0
        assert [item["stage"] for item in data["execution_metrics"]] == [
            "annotation_import",
            "ko_pathway_mapping",
            "pathway_ranking",
            "reference_loading",
            "analysis",
            "bundle_write",
        ]
        assert data["execution_metrics"][1]["request_count"] == 1
        assert data["execution_metrics"][3]["request_count"] == 2
        assert client.call_log == [
            ("link", "ko_to_pathway"),
            ("link", "pathway_to_ko"),
            ("get", "ko00001"),
        ]

        serialized_direct = json.dumps(data, separators=(",", ":"))
        assert len(serialized_direct.encode("utf-8")) < 64 * 1024
        assert '"source_ko_id"' not in serialized_direct
        assert '"detected_ko_ids"' not in serialized_direct

        assert tuple(item["section"] for item in data["artifacts"]) == (
            "structured",
            "summary",
            "annotations",
            "pathway_ranking",
            "ko_pathway_relationships",
        )
        retained_id = data["result"]["result_id"]
        retained_ranking = json.loads(
            runtime.result_store.read_artifact(
                "top-one-contract",
                retained_id,
                "pathway_ranking",
                limit=1_000_000,
            ).content
        )
        retained_relationships = json.loads(
            runtime.result_store.read_artifact(
                "top-one-contract",
                retained_id,
                "ko_pathway_relationships",
                limit=1_000_000,
            ).content
        )
        assert len(retained_ranking["ranking"]["rows"]) == 115
        assert "relationships" not in retained_ranking["ranking"]
        assert len(retained_relationships["relationships"]) == 562
        bundle = data["output_bundle"]
        ranking_lines = Path(bundle["pathway_ranking"]).read_text(encoding="utf-8").splitlines()
        relationship_lines = (
            Path(bundle["ko_pathway_relationships"]).read_text(encoding="utf-8").splitlines()
        )
        assert len(ranking_lines) == 116
        assert len(relationship_lines) == 563
        assert all(Path(item["path"]).is_file() for item in bundle["artifacts"])
        assert all(
            item["byte_size"] == Path(item["path"]).stat().st_size for item in bundle["artifacts"]
        )
        report = Path(bundle["analysis_report"]).read_text(encoding="utf-8")
        assert "## Pathway target selection" in report
        assert "Candidate pathway count: 115" in report
        manifest = json.loads(Path(bundle["manifest"]).read_text(encoding="utf-8"))
        assert manifest["pathway_selection"]["candidate_pathway_count"] == 115
        assert manifest["pathway_selection"]["selected_pathway_ids"] == ["ko00001"]


@pytest.mark.asyncio
async def test_file_handoff_rejects_incomplete_csv_and_symlink_escape(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    incomplete = allowed / "incomplete.csv"
    incomplete.write_text("sequence_id,note\nprotein-1,missing KO\n", encoding="utf-8")
    outside = tmp_path / "outside.csv"
    outside.write_text("sequence_id,ko_id\nprotein-1,K00001\n", encoding="utf-8")
    escaped = allowed / "escaped.csv"
    escaped.symlink_to(outside)
    server = create_server(_runtime(tmp_path, allowed_roots=(str(allowed.resolve()),)))

    async with create_connected_server_and_client_session(server) as session:
        malformed = await session.call_tool(
            "normalize_ko_annotations",
            {"file_path": str(incomplete), "input_format": "generic_csv"},
        )
        escaped_result = await session.call_tool(
            "normalize_ko_annotations",
            {"file_path": str(escaped), "input_format": "generic_csv"},
        )

        assert malformed.isError is True
        assert malformed.structuredContent is not None
        assert malformed.structuredContent["error"]["code"] == "MISSING_REQUIRED_COLUMN"
        assert escaped_result.isError is True
        assert escaped_result.structuredContent is not None
        assert escaped_result.structuredContent["error"]["code"] == (
            "ANALYSIS_CONFIGURATION_INVALID"
        )


@pytest.mark.asyncio
async def test_fake_reference_client_exercises_all_live_dependent_success_outputs(
    tmp_path: Path,
) -> None:
    server = create_server(_fake_runtime(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        high_level = await session.call_tool(
            "analyze_ko_annotations",
            {
                "annotations": {
                    "text": "sequence,ko\nseq-1,K00001\nseq-2,K00002",
                    "input_format": "generic_csv",
                    "column_mapping": {"sequence_id": "sequence", "ko_id": "ko"},
                    "decision_policy": "user_supplied_ko",
                },
                "module_ids": ["M00001"],
                "pathways": [{"pathway_id": "ko00010", "reference_namespace": "ko"}],
            },
        )
        _validate_result(_tool_by_name(tools, "analyze_ko_annotations"), high_level)
        assert high_level.isError is False
        assert high_level.structuredContent is not None
        high_data = high_level.structuredContent["result"]["data"]
        assert tuple(item["section"] for item in high_data["artifacts"]) == (
            "structured",
            "summary",
            "annotations",
        )
        assert high_data["import_summary"]["input_rows"] == 2
        assert high_data["execution"]["service_name"] == "kegg_mcp_annotation_analysis"
        assert len(high_data["reference_provenance"]) == 3

        entries = await session.call_tool(
            "get_kegg_entries",
            {"entries": [{"database": "ko", "identifier": "K00001"}]},
        )
        _validate_result(_tool_by_name(tools, "get_kegg_entries"), entries)
        assert entries.isError is False
        assert entries.structuredContent is not None
        entries_data = entries.structuredContent["result"]["data"]
        assert entries_data["returned_count"] == 1
        assert len(entries_data["previews"][0]["field_names"]) == 64
        assert entries_data["previews"][0]["field_names_truncated"] is True

        mapping = await session.call_tool(
            "map_ko_ids",
            {"ko_ids": ["K00001"], "target": "pathway"},
        )
        _validate_result(_tool_by_name(tools, "map_ko_ids"), mapping)
        assert mapping.isError is False
        assert mapping.structuredContent is not None
        mapping_data = mapping.structuredContent["result"]["data"]
        assert mapping_data["raw_relationship_row_count"] == 1
        assert mapping_data["unique_ko_pathway_count"] == 1
        assert mapping_data["unique_map_pathway_count"] == 1
        assert mapping_data["unique_pathway_number_count"] == 1
        assert mapping_data["row_preview"][0] == {
            "source_ko_id": "K00001",
            "target_id": "ko00001",
            "pathway_number": "00001",
            "namespace": "ko",
            "paired_reference_id": "map00001",
        }

        modules = await session.call_tool(
            "analyze_modules",
            {"source": {"ko_text": "K00001\nK00002"}, "module_ids": ["M00001"]},
        )
        _validate_result(_tool_by_name(tools, "analyze_modules"), modules)
        assert modules.isError is False
        assert modules.structuredContent is not None
        assert (
            modules.structuredContent["result"]["data"]["module_previews"][0]["strict_is_complete"]
            is True
        )

        pathways = await session.call_tool(
            "analyze_pathways",
            {
                "source": {"ko_text": "K00001"},
                "pathways": [{"pathway_id": "ko00010", "reference_namespace": "ko"}],
            },
        )
        _validate_result(_tool_by_name(tools, "analyze_pathways"), pathways)
        assert pathways.isError is False
        assert pathways.structuredContent is not None
        assert (
            pathways.structuredContent["result"]["data"]["pathway_previews"][0]["coverage_ratio"]
            == 0.5
        )


@pytest.mark.asyncio
async def test_functional_compare_uses_shared_references_and_bounded_differences(
    tmp_path: Path,
) -> None:
    server = create_server(_fake_runtime(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        compared = await session.call_tool(
            "compare_ko_sets",
            {
                "inputs": [
                    {
                        "label": "complete-module",
                        "source": {"ko_text": "K00001\nK00002"},
                    },
                    {
                        "label": "complete-pathway",
                        "source": {"ko_text": "K00001\nK00003"},
                    },
                ],
                "module_ids": ["M00001"],
                "pathways": [{"pathway_id": "ko00010", "reference_namespace": "ko"}],
            },
        )
        _validate_result(_tool_by_name(tools, "compare_ko_sets"), compared)
        assert compared.isError is False
        assert compared.structuredContent is not None
        functional = compared.structuredContent["result"]["data"]["functional_summary"]
        assert functional["module_target_count"] == 1
        assert functional["strict_module_differences"] == ["M00001"]
        assert functional["pathway_target_count"] == 1
        assert functional["strict_pathway_differences"] == ["ko00010"]


@pytest.mark.asyncio
async def test_compare_supports_reusable_dataset_and_bounded_functional_summary(
    tmp_path: Path,
) -> None:
    server = create_server(_runtime(tmp_path))
    async with create_connected_server_and_client_session(server) as session:
        tools = (await session.list_tools()).tools
        first = await session.call_tool("normalize_ko_annotations", {"text": "K00844\nK01810"})
        assert first.structuredContent is not None
        retained_id = first.structuredContent["result"]["data"]["result"]["result_id"]
        ignored_context = await session.call_tool(
            "analyze_modules",
            {
                "source": {
                    "result_id": retained_id,
                    "analysis_unit": "metagenomic_community",
                },
                "module_ids": ["M00001"],
            },
        )
        assert ignored_context.isError is True
        assert ignored_context.structuredContent is not None
        assert ignored_context.structuredContent["error"]["code"] == (
            "ANALYSIS_CONFIGURATION_INVALID"
        )
        compared = await session.call_tool(
            "compare_ko_sets",
            {
                "inputs": [
                    {"label": "retained", "source": {"result_id": retained_id}},
                    {"label": "inline", "source": {"ko_text": "K00844\nK01623"}},
                ]
            },
        )
        _validate_result(_tool_by_name(tools, "compare_ko_sets"), compared)
        assert compared.isError is False
        assert compared.structuredContent is not None
        summary = compared.structuredContent["result"]["data"]["functional_summary"]
        assert summary == {
            "module_target_count": 0,
            "strict_module_differences": [],
            "lenient_module_differences": [],
            "pathway_target_count": 0,
            "strict_pathway_differences": [],
            "lenient_pathway_differences": [],
        }


@pytest.mark.asyncio
async def test_large_resource_requires_ranges_and_reconstructs_exact_bytes(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    payload = bytes(range(256)) * 700
    metadata = runtime.result_store.create(
        runtime.scope_id,
        (
            ResultArtifactInput(
                section="large",
                mime_type="application/octet-stream",
                content=payload,
            ),
        ),
    )
    server = create_server(runtime)
    async with create_connected_server_and_client_session(server) as session:
        direct = await session.read_resource(
            AnyUrl(f"ko-analysis://results/{metadata.result_id}/large")
        )
        content = direct.contents[0]
        assert isinstance(content, types.TextResourceContents)
        notice = json.loads(content.text)
        assert notice["kind"] == "artifact_requires_pagination"
        assert notice["maximum_range_bytes"] == MAX_INLINE_RESOURCE_BYTES

        reconstructed = bytearray()
        next_uri: str | None = notice["next_uri"]
        while next_uri is not None:
            page_result = await session.read_resource(AnyUrl(next_uri))
            page_content = page_result.contents[0]
            assert isinstance(page_content, types.TextResourceContents)
            page = json.loads(page_content.text)
            reconstructed.extend(base64.b64decode(page["content_base64"], validate=True))
            next_uri = page["next_uri"]
        assert bytes(reconstructed) == payload


@pytest.mark.asyncio
async def test_cached_entry_resource_is_offline_only_and_does_not_consume_result_quota(
    tmp_path: Path,
) -> None:
    cache_path = tmp_path / "kegg.sqlite3"
    config = KeggClientConfig(cache=CachePolicy(path=str(cache_path)))
    request = GetRequest(entries=(KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),))
    prepared = prepare_get(request, config.limits)[0]
    SQLiteKeggCache(cache_path).write(
        KeggOperation.GET,
        prepared.normalized_request_key,
        RetrievalEndpointClass.PUBLIC_ACADEMIC,
        PUBLIC_KEGG_ENDPOINT_LABEL,
        body=(b"ENTRY       K00001            KO\nNAME        Cached synthetic entry\n///\n"),
        retrieved_at=_NOW,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        parser_version=PARSER_VERSION,
        database_release="cached-contract-release",
    )
    runtime = McpRuntime(
        client=KeggClient(config),
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id="cache-contract-scope",
    )
    server = create_server(runtime)
    async with create_connected_server_and_client_session(server) as session:
        assert runtime.result_store.list_results(runtime.scope_id).total_items == 0
        resource = await session.read_resource(AnyUrl("kegg-cache://entries/ko/K00001"))
        content = resource.contents[0]
        assert isinstance(content, types.TextResourceContents)
        value = json.loads(content.text)
        assert value["returned_count"] == 1
        assert value["provenance"][0]["origin"] == "cache"
        assert runtime.result_store.list_results(runtime.scope_id).total_items == 0

        cache_info = await session.read_resource(AnyUrl("ko-analysis://cache/info"))
        info_content = cache_info.contents[0]
        assert isinstance(info_content, types.TextResourceContents)
        info = json.loads(info_content.text)
        assert info["cache_endpoint_class"] == "public_academic"
        serialized = json.dumps(info)
        assert str(cache_path) not in serialized
        assert "https://rest.kegg.jp" not in serialized


@pytest.mark.asyncio
async def test_resource_validation_scoping_and_protocol_errors(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    metadata = runtime.result_store.create(
        "other-scope",
        (ResultArtifactInput(section="detail", mime_type="text/plain", content=b"private"),),
    )
    server = create_server(runtime)
    async with create_connected_server_and_client_session(server) as session:
        cross_scope_delete = await session.call_tool(
            "delete_analysis_result",
            {"result_id": metadata.result_id},
        )
        assert cross_scope_delete.isError is True
        assert cross_scope_delete.structuredContent is not None
        assert cross_scope_delete.structuredContent["error"]["code"] == "RESULT_NOT_FOUND"
        with pytest.raises(McpError, match="RESULT_NOT_FOUND"):
            await session.read_resource(AnyUrl(f"ko-analysis://results/{metadata.result_id}"))
        invalid_uris = (
            "ko-analysis://results/res_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/detail%2Fpart",
            "ko-analysis://results/res_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/detail?offset=0",
            (
                "ko-analysis://results/res_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/detail/"
                "999999999999999999999999/1"
            ),
            "ko-analysis://results/res_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/detail/0/99999",
        )
        for uri in invalid_uris:
            with pytest.raises(McpError, match="INVALID_RESOURCE_URI"):
                await session.read_resource(AnyUrl(uri))
        with pytest.raises(McpError, match="Unknown MCP tool name"):
            await session.call_tool("not_a_tool", {})


@pytest.mark.asyncio
async def test_expired_resource_is_not_found_without_leaking_store_details(tmp_path: Path) -> None:
    store_path = tmp_path / "private-results.sqlite3"
    store = SQLiteResultStore(store_path, limits=ResultStoreLimits(retention_seconds=1))
    expired = store.create(
        "contract-scope",
        (ResultArtifactInput(section="detail", mime_type="text/plain", content=b"expired"),),
        now=datetime(2000, 1, 1, tzinfo=UTC),
    )
    runtime = McpRuntime(
        client=KeggClient(KeggClientConfig(cache=CachePolicy(path=str(tmp_path / "kegg.sqlite3")))),
        result_store=store,
        scope_id="contract-scope",
    )
    server = create_server(runtime)

    async with create_connected_server_and_client_session(server) as session:
        with pytest.raises(McpError, match="RESULT_NOT_FOUND") as error:
            await session.read_resource(AnyUrl(f"ko-analysis://results/{expired.result_id}/detail"))

    serialized = str(error.value)
    assert str(store_path) not in serialized
    assert "expired" not in serialized
