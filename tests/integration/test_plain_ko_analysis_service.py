"""Offline integration tests for the one-call plain-KO analysis service."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis import PathwayReferenceNamespace
from kegg_mcp.domain import AnalysisUnit
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import ImportLimits, SourceProvenanceInput
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggGetDatabase,
    KeggOperation,
    KeggPairRow,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.parsers import parse_flat_file_response
from kegg_mcp.services.contracts import PlainKoAnalysisRequest, PlainKoAnalysisResult
from kegg_mcp.services.orchestration import analyze_plain_ko
from kegg_mcp.services.reference_loading import PathwaySpec, ReferenceLoadingLimits
from kegg_mcp.services.result_store import ResultStoreLimits, SQLiteResultStore

_NOW = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
_IMPORT_LIMITS = ImportLimits(
    max_bytes=10_000,
    max_rows=100,
    max_columns=10,
    max_field_length=1_000,
)


def _provenance(operation: KeggOperation, marker: str) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW - timedelta(minutes=5),
        served_at=_NOW - timedelta(minutes=5),
        expires_at=_NOW + timedelta(days=1),
        response_bytes=256,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release 116.0+/07-14",
        attempt_count=1,
        is_stale=False,
    )


class _FakeReferenceClient:
    def __init__(self) -> None:
        self.call_log: list[tuple[str, str]] = []
        self.options: list[KeggRequestOptions | None] = []

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        self.options.append(options)
        first = request.entries[0]
        self.call_log.append(("get", first.identifier))
        if first.database is KeggGetDatabase.MODULE:
            body = (
                b"ENTRY       M00001            Module\n"
                b"NAME        Synthetic two-block module\n"
                b"DEFINITION  K00001 K00002\n"
                b"///\n"
            )
            marker = "1"
        elif first.database is KeggGetDatabase.PATHWAY:
            body = (
                b"ENTRY       ko00010                    Pathway\n"
                b"NAME        Synthetic carbohydrate pathway\n"
                b"CLASS       Metabolism; Carbohydrate metabolism\n"
                b"///\n"
            )
            marker = "2"
        else:  # pragma: no cover - detects a future service-layer regression
            raise AssertionError("unexpected GET database")
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
        self.options.append(options)
        pathway_id = request.source_identifiers[0]
        self.call_log.append(("link", pathway_id))
        return LinkResult(
            request=request,
            rows=(
                KeggPairRow(
                    line_number=1,
                    source_id=f"path:{pathway_id}",
                    target_id="ko:K00001",
                ),
                KeggPairRow(
                    line_number=2,
                    source_id=f"path:{pathway_id}",
                    target_id="ko:K00003",
                ),
            ),
            batches=(_provenance(KeggOperation.LINK, "3"),),
        )


def _request() -> PlainKoAnalysisRequest:
    return PlainKoAnalysisRequest(
        ko_text="K00001\nK00002\ninvalid\nK00001\n",
        import_limits=_IMPORT_LIMITS,
        analysis_unit=AnalysisUnit.METAGENOMIC_COMMUNITY,
        source=SourceProvenanceInput(
            source_name="maintainer-supplied",
            input_uri="inline://request/ko-list",
        ),
        module_ids=("M00001",),
        pathways=(
            PathwaySpec(
                pathway_id="ko00010",
                reference_namespace=PathwayReferenceNamespace.KO,
            ),
        ),
        kegg_options=KeggRequestOptions(refresh=True),
    )


def test_one_call_service_retains_complete_artifacts_and_returns_bounded_preview(
    tmp_path: Path,
) -> None:
    client = _FakeReferenceClient()
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    request = _request()

    result = analyze_plain_ko(
        request,
        client=client,
        result_store=store,
        scope_id="session-a",
        now=_NOW,
    )

    assert result.import_summary.input_rows == 4
    assert result.import_summary.emitted_records == 4
    assert result.import_summary.duplicate_count == 1
    assert result.module_previews[0].strict_is_complete is True
    assert result.module_previews[0].strict_block_coverage == 1.0
    assert result.module_previews[0].lenient_is_complete is True
    assert result.pathway_previews[0].detected_unique_ko_count == 1
    assert result.pathway_previews[0].reference_unique_ko_count == 2
    assert result.pathway_previews[0].coverage_ratio == 0.5
    assert tuple(artifact.section for artifact in result.artifacts) == (
        "structured",
        "summary",
        "annotations",
    )
    assert client.call_log == [
        ("get", "M00001"),
        ("link", "ko00010"),
        ("get", "ko00010"),
    ]
    assert all(option is request.kegg_options for option in client.options)
    assert request.kegg_options.refresh is True

    structured_range = store.read_artifact(
        "session-a",
        result.result.result_id,
        "structured",
        limit=1024 * 1024,
        now=_NOW,
    )
    structured = json.loads(structured_range.content)
    assert structured["format_name"] == "kegg_mcp_analysis_report"
    assert structured["format_version"] == "2"
    assert structured["renderer_name"] == "kegg_mcp_reporting"
    execution = structured["report"]["execution"]
    assert execution["service_name"] == "kegg_mcp_plain_ko_analysis"
    assert execution["service_version"] == "1"
    assert execution["import_limits"] == request.import_limits.model_dump(mode="json")
    assert execution["kegg_request_options"] == {"allow_stale": False, "refresh": True}
    assert execution["reference_loading_limits"] == request.reference_limits.model_dump(mode="json")
    assert execution["direct_result_limits"] == result.limits.model_dump(mode="json")
    assert structured["report"]["dataset"]["analysis_unit"] == "metagenomic_community"
    module_result = structured["report"]["module_evaluations"][0]["strict"]
    assert module_result["is_complete"] is True
    module_retrieval = module_result["provenance"][0]["provenance"]["retrieval"]
    assert module_retrieval["database_release"] == "Release 116.0+/07-14"
    assert module_retrieval["cache_lookup_state"] == "miss"
    assert module_retrieval["parser_version"] == PARSER_VERSION
    assert module_result["reference_retrieval_provenance"] == [module_retrieval]
    pathway_result = structured["report"]["pathway_coverages"][0]
    assert pathway_result["coverage_ratio"] == 0.5
    assert pathway_result["reference_link_provenance"][0]["origin"] == "network"

    summary_range = store.read_artifact(
        "session-a",
        result.result.result_id,
        "summary",
        limit=1024 * 1024,
        now=_NOW,
    )
    summary = summary_range.content.decode("utf-8")
    assert "does not establish pathway presence" in summary
    assert "pooled encoded potential" in summary

    annotations_range = store.read_artifact(
        "session-a",
        result.result.result_id,
        "annotations",
        limit=1024 * 1024,
        now=_NOW,
    )
    assert len(annotations_range.content.decode("utf-8").splitlines()) == 5
    assert PlainKoAnalysisResult.model_validate_json(result.model_dump_json()) == result


def test_one_call_result_is_not_visible_to_another_scope(tmp_path: Path) -> None:
    result = analyze_plain_ko(
        _request(),
        client=_FakeReferenceClient(),
        result_store=(store := SQLiteResultStore(tmp_path / "results.sqlite3")),
        scope_id="session-a",
        now=_NOW,
    )

    with pytest.raises(KeggMcpError) as caught:
        store.read_artifact(
            "session-b",
            result.result.result_id,
            "structured",
            now=_NOW,
        )

    assert caught.value.detail.code is ErrorCode.RESULT_NOT_FOUND


def test_plain_ko_request_rejects_missing_targets_and_organism_pathways() -> None:
    with pytest.raises(ValidationError, match="at least one MODULE or pathway"):
        PlainKoAnalysisRequest(ko_text="K00001\n", import_limits=_IMPORT_LIMITS)

    with pytest.raises(ValidationError, match="organism-specific pathway"):
        PlainKoAnalysisRequest(
            ko_text="K00001\n",
            import_limits=_IMPORT_LIMITS,
            pathways=(
                PathwaySpec(
                    pathway_id="hsa00010",
                    reference_namespace=PathwayReferenceNamespace.ORGANISM,
                ),
            ),
        )


def test_incompatible_report_and_store_limits_fail_before_reference_io(tmp_path: Path) -> None:
    client = _FakeReferenceClient()
    database = tmp_path / "results.sqlite3"
    store = SQLiteResultStore(
        database,
        limits=ResultStoreLimits(
            quota_bytes=1024 * 1024,
            max_artifact_bytes=1024 * 1024,
            max_result_bytes=1024 * 1024,
        ),
    )

    with pytest.raises(KeggMcpError) as caught:
        analyze_plain_ko(
            _request(),
            client=client,
            result_store=store,
            scope_id="session-a",
            now=_NOW,
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert client.call_log == []
    assert not database.exists()


def test_one_call_applies_one_shared_request_budget_across_both_loaders(tmp_path: Path) -> None:
    client = _FakeReferenceClient()
    store = SQLiteResultStore(tmp_path / "results.sqlite3")
    payload = _request().model_dump()
    payload["reference_limits"] = ReferenceLoadingLimits(max_total_kegg_requests=2)
    request = PlainKoAnalysisRequest.model_validate(payload, strict=True)

    with pytest.raises(KeggMcpError) as caught:
        analyze_plain_ko(
            request,
            client=client,
            result_store=store,
            scope_id="session-a",
            now=_NOW,
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert client.call_log == [("get", "M00001"), ("link", "ko00010")]
    assert store.list_results("session-a", now=_NOW).total_items == 0
