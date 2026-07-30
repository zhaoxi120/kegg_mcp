"""Focused MCP bindings for annotation audit and BRITE hierarchy services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from pydantic import ValidationError

import kegg_mcp.mcp.tool_handlers as handler_module
import kegg_mcp.mcp.tool_registry as registry_module
from kegg_mcp.mcp.contracts import (
    AnnotationAuditToolEnvelope,
    AuditAnnotationMappingInput,
    BriteHierarchyToolEnvelope,
    MapBriteHierarchyInput,
)
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.tool_handlers import ToolContext
from kegg_mcp.services.annotation_audit import (
    AnnotationMappingAuditResult,
    AnnotationMappingTarget,
)
from kegg_mcp.services.brite_hierarchy import MapBriteHierarchyResult
from kegg_mcp.services.models import DatasetSource
from kegg_mcp.services.query_models import KeggEntityKind, KeggEntityRef
from kegg_mcp.services.reference_budget import KeggPrimitiveClient, KeggRelationClient
from kegg_mcp.services.result_store import ResultMetadata, SQLiteResultStore


def _result_metadata(*, artifact_count: int) -> ResultMetadata:
    created_at = datetime(2026, 7, 30, 6, 0, tzinfo=UTC)
    return ResultMetadata(
        result_id="res_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        created_at=created_at,
        expires_at=created_at + timedelta(days=1),
        total_bytes=100,
        artifact_count=artifact_count,
    )


def _context(
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
) -> ToolContext:
    runtime = cast(
        McpRuntime,
        SimpleNamespace(
            client=client,
            result_store=result_store,
            scope_id="binding-scope",
        ),
    )
    return ToolContext(runtime=runtime, supported_tools=registry_module.TOOL_NAMES)


def test_registry_uses_typed_open_world_client_contracts() -> None:
    specs = {spec.name: spec for spec in registry_module.TOOL_SPECS}
    tools = {tool.name: tool for tool in registry_module.tool_definitions()}

    assert specs["map_brite_hierarchy"].input_model is MapBriteHierarchyInput
    assert specs["map_brite_hierarchy"].output_model is BriteHierarchyToolEnvelope
    assert specs["audit_annotation_mapping"].input_model is AuditAnnotationMappingInput
    assert specs["audit_annotation_mapping"].output_model is AnnotationAuditToolEnvelope

    for name in ("map_brite_hierarchy", "audit_annotation_mapping"):
        assert specs[name].annotations.openWorldHint is True
        tool = tools[name]
        annotations = tool.annotations
        input_schema = tool.inputSchema
        output_schema = tool.outputSchema
        assert annotations is not None
        assert input_schema is not None
        assert output_schema is not None
        assert annotations.openWorldHint is True
        assert input_schema["additionalProperties"] is False
        assert output_schema["additionalProperties"] is False

    brite_input_schema = tools["map_brite_hierarchy"].inputSchema
    audit_input_schema = tools["audit_annotation_mapping"].inputSchema
    brite_output_schema = tools["map_brite_hierarchy"].outputSchema
    audit_output_schema = tools["audit_annotation_mapping"].outputSchema
    assert brite_input_schema is not None
    assert audit_input_schema is not None
    assert brite_output_schema is not None
    assert audit_output_schema is not None
    assert set(brite_input_schema["properties"]) == {
        "entity_ids",
        "brite_ids",
        "include_all_paths",
        "include_unmatched",
        "preview_limit",
    }
    assert set(audit_input_schema["properties"]) == {
        "source",
        "quality_context",
        "mapping_targets",
    }
    assert audit_input_schema["properties"]["mapping_targets"]["maxItems"] == 5
    with pytest.raises(ValidationError, match="mapping_targets must be unique"):
        AuditAnnotationMappingInput(
            source=DatasetSource(ko_text="K00001"),
            mapping_targets=(
                AnnotationMappingTarget.PATHWAY,
                AnnotationMappingTarget.PATHWAY,
            ),
        )
    assert "MapBriteHierarchyResult" in brite_output_schema["$defs"]
    assert "AnnotationMappingAuditResult" in audit_output_schema["$defs"]

    for envelope in (BriteHierarchyToolEnvelope, AnnotationAuditToolEnvelope):
        with pytest.raises(ValidationError):
            envelope.model_validate(
                {
                    "ok": True,
                    "result": {
                        "data": {"unexpected": "shape"},
                        "resource_uri": None,
                    },
                    "error": None,
                },
                strict=True,
            )


def test_handlers_delegate_to_services_with_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = cast(KeggPrimitiveClient, object())
    relation_client = cast(KeggRelationClient, client)
    result_store = cast(SQLiteResultStore, object())
    context = _context(client, result_store)
    brite_request = MapBriteHierarchyInput(
        entity_ids=(KeggEntityRef(kind=KeggEntityKind.KO, identifier="K00001"),),
        brite_ids=("ko00001",),
    )
    audit_request = AuditAnnotationMappingInput(
        source=DatasetSource(ko_text="K00001"),
    )
    brite_result = MapBriteHierarchyResult.model_construct(
        result=_result_metadata(artifact_count=2),
        entity_count=1,
        resolved_brite_count=1,
    )
    audit_result = AnnotationMappingAuditResult.model_construct(
        result=_result_metadata(artifact_count=1),
    )
    captured: dict[str, object] = {}

    def fake_map_brite_hierarchy(
        request: MapBriteHierarchyInput,
        *,
        client: KeggPrimitiveClient,
        result_store: SQLiteResultStore,
        scope_id: str,
    ) -> MapBriteHierarchyResult:
        captured["brite"] = (request, client, result_store, scope_id)
        return brite_result

    def fake_audit_annotation_mapping(
        source: DatasetSource,
        *,
        client: KeggRelationClient,
        result_store: SQLiteResultStore,
        scope_id: str,
        quality_context: object,
        mapping_targets: object,
    ) -> AnnotationMappingAuditResult:
        captured["audit"] = (
            source,
            client,
            result_store,
            scope_id,
            quality_context,
            mapping_targets,
        )
        return audit_result

    monkeypatch.setattr(
        handler_module,
        "map_brite_hierarchy",
        fake_map_brite_hierarchy,
    )
    monkeypatch.setattr(
        handler_module,
        "audit_annotation_mapping",
        fake_audit_annotation_mapping,
    )

    brite_outcome = handler_module.map_brite(context, brite_request)
    audit_outcome = handler_module.audit_mapping(context, audit_request)

    assert captured["brite"] == (
        brite_request,
        client,
        result_store,
        "binding-scope",
    )
    assert captured["audit"] == (
        audit_request.source,
        relation_client,
        result_store,
        "binding-scope",
        None,
        audit_request.mapping_targets,
    )
    assert brite_outcome.data is brite_result
    assert audit_outcome.data is audit_result
    assert brite_outcome.result_id == brite_result.result.result_id
    assert audit_outcome.result_id == audit_result.result.result_id
