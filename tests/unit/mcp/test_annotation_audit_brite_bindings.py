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
    AnnotationMappingExecution,
    AnnotationMappingExecutionStatus,
    AnnotationMappingLimitKind,
    AnnotationMappingTarget,
)
from kegg_mcp.services.brite_hierarchy import MapBriteHierarchyResult
from kegg_mcp.services.models import DatasetSource
from kegg_mcp.services.query_models import (
    KeggEntityKind,
    KeggEntityRef,
    KeggSearchDatabase,
    KeggSearchMode,
    SearchKeggEntriesRequest,
    SearchKeggEntriesResult,
)
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


def _mapping_execution(
    status: AnnotationMappingExecutionStatus,
) -> AnnotationMappingExecution:
    if status is AnnotationMappingExecutionStatus.NOT_REQUESTED:
        requested = ()
        completed = ()
        skipped = ()
        planned = 0
        limit = 100
    elif status is AnnotationMappingExecutionStatus.COMPLETED:
        requested = (AnnotationMappingTarget.PATHWAY,)
        completed = requested
        skipped = ()
        planned = 1
        limit = 100
    elif status is AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT:
        requested = (AnnotationMappingTarget.PATHWAY,)
        completed = ()
        skipped = requested
        planned = 2
        limit = 1
    else:
        requested = (AnnotationMappingTarget.PATHWAY,)
        completed = ()
        skipped = ()
        planned = 1
        limit = 100
    incomplete_target = (
        AnnotationMappingTarget.PATHWAY
        if status
        in {
            AnnotationMappingExecutionStatus.INCOMPLETE_ROW_LIMIT,
            AnnotationMappingExecutionStatus.INCOMPLETE_RESPONSE_LIMIT,
        }
        else None
    )
    if status is AnnotationMappingExecutionStatus.INCOMPLETE_ROW_LIMIT:
        limit_kind = AnnotationMappingLimitKind.ROW_COUNT
    elif status is AnnotationMappingExecutionStatus.INCOMPLETE_RESPONSE_LIMIT:
        limit_kind = AnnotationMappingLimitKind.RESPONSE_BYTES
    else:
        limit_kind = None
    return AnnotationMappingExecution(
        status=status,
        requested_targets=requested,
        completed_targets=completed,
        skipped_targets=skipped,
        incomplete_target=incomplete_target,
        selected_unique_ko_count=0,
        planned_request_count=planned,
        request_limit=limit,
        limit_kind=limit_kind,
        limit_observed=2 if limit_kind is not None else None,
        limit_value=1 if limit_kind is not None else None,
    )


def test_registry_uses_typed_brite_and_audit_contracts() -> None:
    specs = {spec.name: spec for spec in registry_module.TOOL_SPECS}

    assert specs["map_brite_hierarchy"].input_model is MapBriteHierarchyInput
    assert specs["map_brite_hierarchy"].output_model is BriteHierarchyToolEnvelope
    assert specs["audit_annotation_mapping"].input_model is AuditAnnotationMappingInput
    assert specs["audit_annotation_mapping"].output_model is AnnotationAuditToolEnvelope
    with pytest.raises(ValidationError, match="mapping_targets must be unique"):
        AuditAnnotationMappingInput(
            source=DatasetSource(ko_text="K00001"),
            mapping_targets=(
                AnnotationMappingTarget.PATHWAY,
                AnnotationMappingTarget.PATHWAY,
            ),
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
        mapping_execution=_mapping_execution(AnnotationMappingExecutionStatus.COMPLETED),
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            AnnotationMappingExecutionStatus.COMPLETED,
            "Audited annotation evidence and completed the selected KEGG relationship mappings.",
        ),
        (
            AnnotationMappingExecutionStatus.NOT_REQUESTED,
            "Completed the annotation evidence audit; no KEGG relationship mapping was requested.",
        ),
        (
            AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT,
            (
                "Completed the annotation evidence audit; KEGG relationship mapping was skipped "
                "before network access because the planned request count exceeded the limit."
            ),
        ),
        (
            AnnotationMappingExecutionStatus.INCOMPLETE_ROW_LIMIT,
            (
                "Completed the annotation evidence audit and retained only fully completed KEGG "
                "relationship mappings; an in-progress target exceeded the relationship-row "
                "limit, so no partial mapping yield was reported for it."
            ),
        ),
        (
            AnnotationMappingExecutionStatus.INCOMPLETE_RESPONSE_LIMIT,
            (
                "Completed the annotation evidence audit and retained only fully completed KEGG "
                "relationship mappings; an in-progress target exceeded the response-byte limit, "
                "so no partial mapping yield was reported for it."
            ),
        ),
    ],
)
def test_audit_handler_summary_reflects_mapping_execution(
    monkeypatch: pytest.MonkeyPatch,
    status: AnnotationMappingExecutionStatus,
    expected: str,
) -> None:
    execution = _mapping_execution(status)
    result = AnnotationMappingAuditResult.model_construct(
        result=_result_metadata(artifact_count=1),
        mapping_execution=execution,
    )

    def fake_audit(*args: object, **kwargs: object) -> AnnotationMappingAuditResult:
        return result

    monkeypatch.setattr(handler_module, "audit_annotation_mapping", fake_audit)
    outcome = handler_module.audit_mapping(
        _context(
            cast(KeggPrimitiveClient, object()),
            cast(SQLiteResultStore, object()),
        ),
        AuditAnnotationMappingInput(
            source=DatasetSource(ko_text="K00001"),
            mapping_targets=execution.requested_targets,
        ),
    )

    assert outcome.summary == expected


def test_exact_mass_search_handler_summary_marks_candidates_as_not_identifications(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = SearchKeggEntriesResult.model_construct(
        result=_result_metadata(artifact_count=1),
        mode=KeggSearchMode.EXACT_MASS,
        candidate_count=2,
    )

    def fake_search(*args: object, **kwargs: object) -> SearchKeggEntriesResult:
        return result

    monkeypatch.setattr(handler_module, "search_kegg_entries", fake_search)
    outcome = handler_module.search_entries(
        _context(
            cast(KeggPrimitiveClient, object()),
            cast(SQLiteResultStore, object()),
        ),
        SearchKeggEntriesRequest(
            database=KeggSearchDatabase.COMPOUND,
            query="180.063",
            mode=KeggSearchMode.EXACT_MASS,
        ),
    )

    assert "compound candidates, not compound identifications" in outcome.summary
