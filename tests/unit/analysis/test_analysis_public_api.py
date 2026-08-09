"""Tests for the stable pure-analysis import surface."""

import kegg_mcp.analysis as analysis


def test_analysis_public_api_exports_milestone_four_workflows_and_contracts() -> None:
    expected = {
        "ComparisonDatasetInput",
        "ComparisonLimits",
        "KoSetComparisonDetail",
        "KoPathwayRelationship",
        "KoModuleRelationship",
        "ModuleComparisonResult",
        "ModuleAnalysisLimits",
        "ModuleDefinition",
        "ModuleDefinitionCollection",
        "ModuleEvaluationResult",
        "ModuleParseResult",
        "ModuleRankingResult",
        "ModuleRankingRow",
        "ModuleSelection",
        "PathwayCoverageParameters",
        "PathwayCoverageResult",
        "PathwayComparisonResult",
        "PathwayKoReference",
        "PathwayRankingResult",
        "PathwayRankingRow",
        "PathwaySelection",
        "build_pathway_reference",
        "compare_ko_datasets",
        "compare_module_graphs",
        "compare_pathway_references",
        "evaluate_module",
        "evaluate_pathway_coverage",
        "parse_module_definition",
        "rank_pathways",
        "rank_modules",
        "resolve_module_definitions",
        "summarize_ko_comparison",
        "tokenize_module_definition",
    }

    assert expected <= set(analysis.__all__)
    assert all(hasattr(analysis, name) for name in expected)


def test_milestone_four_result_schemas_have_stable_identifiers() -> None:
    assert analysis.KoSetComparisonDetail.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:ko-set-comparison-detail:2"
    )
    assert analysis.ModuleComparisonResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:module-comparison-result:3"
    )
    assert analysis.PathwayCoverageResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-coverage-result:3"
    )
    assert analysis.PathwayComparisonResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-comparison-result:2"
    )
    assert analysis.PathwaySelection.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-selection:2"
    )
    assert analysis.PathwayRankingResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-ranking-result:2"
    )
    assert analysis.ModuleSelection.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:module-selection:2"
    )
    assert analysis.ModuleRankingResult.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:module-ranking-result:2"
    )
