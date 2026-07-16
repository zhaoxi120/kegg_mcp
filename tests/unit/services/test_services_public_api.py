"""Tests for the stable Milestone 5 service import surface."""

import kegg_mcp.services as services
from kegg_mcp.services import (
    contracts,
    orchestration,
    reference_loading,
    render_contracts,
    result_store,
)


def test_services_package_exports_orchestration_and_contracts() -> None:
    expected = {
        "ANALYSIS_SERVICE_NAME",
        "ANALYSIS_SERVICE_VERSION",
        "AnalysisExecutionProvenance",
        "AnalysisServiceLimits",
        "ExecutionStage",
        "ImportSummary",
        "KeggReferenceClient",
        "ModuleAnalysisPreview",
        "ManifestPathMode",
        "PathwayAnalysisPreview",
        "PathwayExecutionParameters",
        "PathwayRankingExecution",
        "PathwaySpec",
        "PlainKoAnalysisRequest",
        "PlainKoAnalysisResult",
        "ReferenceLoadingLimits",
        "RenderInputLimits",
        "RenderInputV2",
        "ModuleRenderTarget",
        "PathwayRenderTarget",
        "ResultArtifactInput",
        "ResultArtifactMetadata",
        "ResultArtifactPage",
        "ResultArtifactRange",
        "ResultMetadata",
        "ResultMetadataPage",
        "ResultStoreLimits",
        "ScopeDeletionSummary",
        "SQLiteResultStore",
        "SelectedPathwaySummary",
        "StageMetric",
        "analyze_plain_ko",
        "delete_analysis_result",
        "load_module_graphs",
        "load_pathway_references",
    }

    assert expected <= set(services.__all__)
    assert all(hasattr(services, name) for name in expected)
    assert services.PlainKoAnalysisRequest is contracts.PlainKoAnalysisRequest
    assert services.analyze_plain_ko is orchestration.analyze_plain_ko
    assert services.PathwaySpec is reference_loading.PathwaySpec
    assert services.SQLiteResultStore is result_store.SQLiteResultStore
    assert services.RenderInputV2 is render_contracts.RenderInputV2


def test_services_package_exports_store_defaults_and_lifecycle_results() -> None:
    expected = {
        "DEFAULT_MAX_ARTIFACTS_PER_RESULT",
        "DEFAULT_MAX_ARTIFACT_BYTES",
        "DEFAULT_MAX_DATABASE_BYTES",
        "DEFAULT_MAX_PAGE_SIZE",
        "DEFAULT_MAX_RANGE_BYTES",
        "DEFAULT_MAX_RESULT_BYTES",
        "DEFAULT_MAX_RESULTS",
        "DEFAULT_PAGE_SIZE",
        "DEFAULT_QUOTA_BYTES",
        "DEFAULT_RANGE_BYTES",
        "DEFAULT_RETENTION_SECONDS",
        "CleanupSummary",
        "DeletedResult",
        "ScopeDeletionSummary",
        "ResultStoreError",
    }

    assert expected <= set(services.__all__)
    assert all(hasattr(services, name) for name in expected)
    assert services.DEFAULT_RETENTION_SECONDS == 24 * 60 * 60
    assert services.DEFAULT_QUOTA_BYTES == 512 * 1024 * 1024
    assert services.DEFAULT_MAX_DATABASE_BYTES == 640 * 1024 * 1024
    assert services.DEFAULT_MAX_RESULTS == 10_000


def test_service_request_and_result_schemas_have_stable_identifiers() -> None:
    assert services.AnalysisExecutionProvenance.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:analysis-execution-provenance:2"
    )
    assert services.PlainKoAnalysisRequest.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:plain-ko-analysis-request:1"
    )
    assert services.PlainKoAnalysisResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:plain-ko-analysis-result:1"
    )
    assert services.RenderInputV2.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:render-input:2"
    )
    assert services.ModuleRenderTarget.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:module-render-target:2"
    )
    assert services.PathwayRenderTarget.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-render-target:2"
    )
