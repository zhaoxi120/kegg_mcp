"""Tests for conservative annotation and KEGG mapping audits."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import GenericColumnMapping, SourceProvenanceInput
from kegg_mcp.kegg import (
    KeggClientConfig,
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
)
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    KeggPairRow,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services import annotation_audit
from kegg_mcp.services.annotation_audit import (
    AnnotationAuditWarningCode,
    AnnotationMappingExecution,
    AnnotationMappingExecutionStatus,
    AnnotationMappingTarget,
    AnnotationQualityContext,
    GenomeType,
    audit_annotation_mapping,
)
from kegg_mcp.services.models import (
    AnnotationInputFormat,
    DatasetSource,
    GenericDecisionPolicy,
    NormalizeAnnotationsRequest,
)
from kegg_mcp.services.normalization import normalize_annotations
from kegg_mcp.services.result_store import SQLiteResultStore

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"assembly_completeness": None},
        {"gene_caller": " "},
        {"annotation_tool": "tool\nname"},
        {"annotation_database_version": "\x7f"},
    ],
)
def test_quality_context_requires_one_non_null_value(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AnnotationQualityContext.model_validate(values)


def test_quality_context_normalizes_outer_whitespace() -> None:
    context = AnnotationQualityContext(gene_caller="  Prodigal  ")

    assert context.gene_caller == "Prodigal"


def test_completed_mapping_execution_requires_at_least_one_target() -> None:
    with pytest.raises(ValidationError):
        AnnotationMappingExecution(
            status=AnnotationMappingExecutionStatus.COMPLETED,
            requested_targets=(),
            completed_targets=(),
            skipped_targets=(),
            selected_unique_ko_count=0,
            planned_request_count=0,
            request_limit=100,
        )


def _provenance(marker: int) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=KeggOperation.LINK,
        request_key=f"synthetic:{marker}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        response_bytes=100,
        parser_name="pair_table",
        parser_version=PARSER_VERSION,
        database_release=None,
        attempt_count=1,
        is_stale=False,
    )


_TARGET_IDS = {
    KeggLinkRelationship.KO_TO_PATHWAY: ("path:ko00010", "path:ko00020"),
    KeggLinkRelationship.KO_TO_MODULE: ("md:M00001", "md:M00002"),
    KeggLinkRelationship.KO_TO_REACTION: ("rn:R00001", "rn:R00002"),
    KeggLinkRelationship.KO_TO_ENZYME: ("ec:1.1.1.1", "ec:2.2.2.2"),
    KeggLinkRelationship.KO_TO_BRITE: ("br:ko00001", "br:ko00002"),
}


class _AuditClient:
    def __init__(
        self,
        *,
        released_call_count: int = 0,
        max_identifiers: int = 2,
    ) -> None:
        self._config = KeggClientConfig.model_validate(
            {"limits": {"max_identifiers": max_identifiers}}
        )
        self._released_call_count = released_call_count
        self.requests: list[LinkRequest] = []

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        del options
        self.requests.append(request)
        first_target, second_target = _TARGET_IDS[request.relationship]
        rows: list[KeggPairRow] = []
        for source_identifier in request.source_identifiers:
            if source_identifier == "K00001":
                rows.append(
                    KeggPairRow(
                        line_number=len(rows) + 1,
                        source_id="ko:K00001",
                        target_id=first_target,
                    )
                )
            elif (
                source_identifier == "K00005"
                and request.relationship is KeggLinkRelationship.KO_TO_PATHWAY
            ):
                rows.extend(
                    (
                        KeggPairRow(
                            line_number=len(rows) + 1,
                            source_id="ko:K00005",
                            target_id=first_target,
                        ),
                        KeggPairRow(
                            line_number=len(rows) + 2,
                            source_id="ko:K00005",
                            target_id=second_target,
                        ),
                    )
                )
        provenance = _provenance(len(self.requests))
        if len(self.requests) <= self._released_call_count:
            provenance = provenance.model_copy(update={"database_release": "KEGG Release 110.0"})
        return LinkResult(
            request=request,
            rows=tuple(rows),
            batches=(provenance,),
        )


def _normalized_result(store: SQLiteResultStore, scope_id: str) -> str:
    payload = (
        "sequence,ko,decision,rank\n"
        "p1,K00001,accepted,1\n"
        "p2,K00002,uncertain,1\n"
        "p3,K00003,rejected,1\n"
        "p4,not-a-ko,accepted,1\n"
        "p5,K00004,mystery,1\n"
        "p6,K00005,accepted,1\n"
        "p6,K00006,accepted,1\n"
        "p1,K00001,accepted,1\n"
    )
    normalized = normalize_annotations(
        NormalizeAnnotationsRequest(
            text=payload,
            input_format=AnnotationInputFormat.GENERIC_CSV,
            column_mapping=GenericColumnMapping(
                sequence_id="sequence",
                ko_id="ko",
                raw_decision="decision",
                rank="rank",
            ),
            decision_policy=GenericDecisionPolicy.CANONICAL_SOURCE_STATUS,
            source=SourceProvenanceInput(source_name="synthetic-annotator"),
        ),
        result_store=store,
        scope_id=scope_id,
    )
    return normalized.result.result_id


def test_audit_reuses_one_lenient_mapping_union_per_target_and_reports_losses(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    scope_id = "audit-scope"
    result_id = _normalized_result(store, scope_id)
    client = _AuditClient()

    result = audit_annotation_mapping(
        DatasetSource(result_id=result_id),
        client=client,
        result_store=store,
        scope_id=scope_id,
        quality_context=AnnotationQualityContext(
            assembly_completeness=82.5,
            assembly_contamination=1.2,
            genome_type=GenomeType.MAG,
            gene_caller="synthetic-caller",
            annotation_tool="synthetic-annotator",
            annotation_database_version="2026-07",
        ),
    )

    assert result.detail.evidence.strict_unique_ko_count == 3
    assert result.detail.evidence.lenient_unique_ko_count == 4
    assert result.detail.evidence.rejected_unique_ko_count == 1
    assert result.detail.evidence.duplicate_assignment_count == 1
    assert result.detail.evidence.conflicting_assignment_count == 1
    assert tuple(item.target for item in result.detail.mappings) == tuple(AnnotationMappingTarget)
    pathway = result.detail.mappings[0]
    assert pathway.strict.mapped_unique_ko_count == 2
    assert pathway.strict.one_to_many_ko_count == 1
    assert tuple(
        (item.target_count, item.ko_count) for item in pathway.strict.target_degree_distribution
    ) == ((1, 1), (2, 1))
    assert pathway.lenient.unmapped_ko_count == 2
    assert result.detail.lenient_only_ko_count == 1
    assert result.detail.lenient_only_ko_preview == ("K00002",)
    assert result.detail.strict_without_any_audited_relationship_preview == ("K00006",)
    assert result.detail.lenient_without_any_audited_relationship_preview == (
        "K00002",
        "K00006",
    )
    assert len(client.requests) == 10
    assert {request.relationship for request in client.requests} == set(_TARGET_IDS)
    assert all(
        set(request.source_identifiers) <= {"K00001", "K00002", "K00005", "K00006"}
        for request in client.requests
    )
    warning_codes = {warning.code for warning in result.detail.warnings}
    assert AnnotationAuditWarningCode.MISSING_SOURCE_VERSION in warning_codes
    assert AnnotationAuditWarningCode.KEGG_RELEASE_UNAVAILABLE in warning_codes
    assert AnnotationAuditWarningCode.INCOMPLETE_ASSEMBLY_CONTEXT in warning_codes
    assert AnnotationAuditWarningCode.CONTAMINATION_CONTEXT in warning_codes
    assert result.detail.quality_context is not None
    assert result.detail.quality_context.genome_type is GenomeType.MAG
    artifact = store.read_artifact(
        scope_id,
        result.result.result_id,
        "detail",
        offset=0,
        limit=store.limits.max_range_bytes,
    )
    assert b'"complete_relationship_rows"' in artifact.content
    assert b'"strict_ko_ids":["K00001","K00005","K00006"]' in artifact.content
    assert b'"lenient_only_ko_ids":["K00002"]' in artifact.content
    assert b'"provenance"' in artifact.content


def test_audit_warns_for_each_mapping_batch_without_a_database_release(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    client = _AuditClient(released_call_count=1)

    result = audit_annotation_mapping(
        DatasetSource(ko_text="K00001"),
        client=client,
        result_store=store,
        scope_id="mixed-release-audit-scope",
    )

    warning = next(
        item
        for item in result.detail.warnings
        if item.code is AnnotationAuditWarningCode.KEGG_RELEASE_UNAVAILABLE
    )
    warning_codes = {item.code for item in result.detail.warnings}
    assert len(client.requests) == 5
    assert warning.affected_count == 4
    assert AnnotationAuditWarningCode.MISSING_MODEL_NAME not in warning_codes
    assert AnnotationAuditWarningCode.MISSING_MODEL_VERSION not in warning_codes


def test_audit_preserves_evidence_when_planned_mapping_exceeds_request_bound(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    client = _AuditClient()
    ko_text = "\n".join(f"K{index:05d}" for index in range(1, 502))

    result = audit_annotation_mapping(
        DatasetSource(ko_text=ko_text),
        client=client,
        result_store=store,
        scope_id="audit-limit-scope",
    )

    assert result.detail.evidence.lenient_unique_ko_count == 501
    assert (
        result.detail.mapping_execution.status
        is AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT
    )
    assert result.detail.mappings == ()
    assert result.detail.strict_without_any_audited_relationship_count is None
    assert client.requests == []


def test_audit_maps_more_than_500_kos_for_one_selected_target(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "selected-target.sqlite3")
    client = _AuditClient(max_identifiers=100)
    ko_text = "\n".join(f"K{index:05d}" for index in range(1, 502))

    result = audit_annotation_mapping(
        DatasetSource(ko_text=ko_text),
        client=client,
        result_store=store,
        scope_id="selected-target-scope",
        mapping_targets=(AnnotationMappingTarget.PATHWAY,),
    )

    assert result.detail.mapping_execution.status is AnnotationMappingExecutionStatus.COMPLETED
    assert result.detail.mapping_execution.completed_targets == (AnnotationMappingTarget.PATHWAY,)
    assert tuple(item.target for item in result.detail.mappings) == (
        AnnotationMappingTarget.PATHWAY,
    )
    assert len(client.requests) == 6
    assert {request.relationship for request in client.requests} == {
        KeggLinkRelationship.KO_TO_PATHWAY
    }
    retained = json.loads(
        store.read_artifact(
            "selected-target-scope",
            result.result.result_id,
            "detail",
            limit=store.limits.max_range_bytes,
        ).content
    )
    assert set(retained["complete_relationship_rows"]) == {"pathway"}
    assert retained["detail"]["mapping_execution"]["requested_targets"] == ["pathway"]


def test_audit_can_run_evidence_only_without_kegg_mapping(
    tmp_path: Path,
) -> None:
    store = SQLiteResultStore(tmp_path / "evidence-only.sqlite3")
    client = _AuditClient()

    result = audit_annotation_mapping(
        DatasetSource(ko_text="K00001\nK00002"),
        client=client,
        result_store=store,
        scope_id="evidence-only-scope",
        mapping_targets=(),
    )

    assert result.detail.mapping_execution.status is AnnotationMappingExecutionStatus.NOT_REQUESTED
    assert result.detail.evidence.strict_unique_ko_count == 2
    assert result.detail.mappings == ()
    assert result.detail.strict_without_any_audited_relationship_count is None
    assert client.requests == []


def test_audit_fails_before_retention_when_artifact_exceeds_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    scope_id = "audit-artifact-limit-scope"
    result_id = _normalized_result(store, scope_id)
    result_count_before = store.list_results(scope_id).total_items
    monkeypatch.setattr(annotation_audit, "MAX_AUDIT_ARTIFACT_BYTES", 1)

    with pytest.raises(KeggMcpError) as captured:
        audit_annotation_mapping(
            DatasetSource(result_id=result_id),
            client=_AuditClient(),
            result_store=store,
            scope_id=scope_id,
        )

    assert captured.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert store.list_results(scope_id).total_items == result_count_before


def test_audit_request_budget_skip_is_reported_before_network_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    scope_id = "audit-request-limit-scope"
    result_id = _normalized_result(store, scope_id)
    client = _AuditClient()
    monkeypatch.setattr(annotation_audit, "MAX_AUDIT_KEGG_REQUESTS", 1)

    result = audit_annotation_mapping(
        DatasetSource(result_id=result_id),
        client=client,
        result_store=store,
        scope_id=scope_id,
    )

    assert (
        result.detail.mapping_execution.status
        is AnnotationMappingExecutionStatus.SKIPPED_REQUEST_LIMIT
    )
    assert client.requests == []
