"""Tests for the stable pure-analysis import surface."""

import kegg_mcp.analysis as analysis


def test_analysis_public_api_exports_milestone_four_workflows_and_contracts() -> None:
    expected = {
        "ComparisonDatasetInput",
        "ComparisonLimits",
        "KoSetComparisonDetail",
        "ModuleComparisonResult",
        "ModuleAnalysisLimits",
        "ModuleDefinition",
        "ModuleDefinitionCollection",
        "ModuleEvaluationResult",
        "ModuleParseResult",
        "PathwayCoverageParameters",
        "PathwayCoverageResult",
        "PathwayComparisonResult",
        "PathwayKoReference",
        "PairedModuleEvaluation",
        "build_pathway_reference",
        "compare_ko_datasets",
        "compare_module_graphs",
        "compare_pathway_references",
        "evaluate_module",
        "evaluate_module_pair",
        "evaluate_pathway_coverage",
        "parse_module_definition",
        "resolve_module_definitions",
        "summarize_ko_comparison",
        "tokenize_module_definition",
    }

    assert expected <= set(analysis.__all__)
    assert all(hasattr(analysis, name) for name in expected)


def test_milestone_four_result_schemas_have_stable_identifiers() -> None:
    assert analysis.KoSetComparisonDetail.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:ko-set-comparison-detail:1"
    )
    assert analysis.ModuleComparisonResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:module-comparison-result:2"
    )
    assert analysis.PathwayCoverageResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-coverage-result:2"
    )
    assert analysis.PathwayComparisonResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-comparison-result:1"
    )
