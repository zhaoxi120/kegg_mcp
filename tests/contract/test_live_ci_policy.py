"""Static contract checks for the GitHub-only live KEGG campaign."""

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
_LIVE_CONTROLS = _REPOSITORY_ROOT / "tests" / "live" / "conftest.py"
_LIVE_SUITE = _REPOSITORY_ROOT / "tests" / "live" / "test_kegg_api_live.py"
_SCIENTIST_SUITE = _REPOSITORY_ROOT / "tests" / "live" / "test_v07_scientist_workflows_live.py"
_STDIO_SUITE = _REPOSITORY_ROOT / "tests" / "live" / "test_stdio_scientist_live.py"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _validate_job_text() -> str:
    return _workflow_text().split("\n  validate-deepkoala-companion:\n", maxsplit=1)[0]


def test_pull_request_ci_enables_public_academic_access_once() -> None:
    workflow = _workflow_text()
    validate_job = _validate_job_text()

    assert "pull_request:" in workflow
    assert "push:" not in workflow
    assert "KEGG_MCP_ACCESS_MODE: public_academic" in validate_job
    assert 'KEGG_MCP_ACADEMIC_USE_CONFIRMED: "true"' in validate_job
    assert 'KEGG_MCP_RUN_LIVE_TESTS: "true"' in validate_job
    assert "offline_cache" not in workflow
    assert "validate-kegg-live" not in workflow


def test_github_ci_runs_the_opt_in_live_campaign_without_artifacts() -> None:
    validate_job = _validate_job_text()

    assert "Test, including at most 120 live KEGG requests" in validate_job
    assert 'KEGG_MCP_LIVE_REQUESTS_PER_OPERATION: "20"' in validate_job
    assert "uv run --frozen pytest" in validate_job
    assert "KEGG_MCP_RUN_LIVE_STDIO_E2E" not in validate_job
    assert "-m live_kegg" not in validate_job
    assert "upload-artifact" not in validate_job


def test_live_suite_is_opt_in_configurable_bounded_and_rate_limited() -> None:
    controls = _LIVE_CONTROLS.read_text(encoding="utf-8")
    suite = _LIVE_SUITE.read_text(encoding="utf-8")

    assert "pytest.mark.live_kegg" in suite
    assert "KEGG_MCP_RUN_LIVE_TESTS" in suite
    assert suite.count("for request in _rotated_requests(") == 6
    for matrix_name in (
        "_INFO_REQUESTS",
        "_LIST_REQUESTS",
        "_FIND_REQUESTS",
        "_GET_REQUESTS",
        "_LINK_REQUESTS",
        "_CONV_REQUESTS",
    ):
        assert matrix_name in suite
    assert "list_organism_pathways" in suite
    assert '("hsa", "eco")' in suite
    for mode in ("FORMULA", "EXACT_MASS", "MOL_WEIGHT"):
        assert f"KeggFindMode.{mode}" in suite
    for target_database in ("COMPOUND", "GLYCAN", "DRUG"):
        assert f"target_database=KeggConvDatabase.{target_database}" in suite
    for relationship in (
        "KO_TO_BRITE",
        "GENE_TO_PATHWAY",
        "COMPOUND_TO_REACTION",
        "TAXONOMY_TO_GENOME",
        "KO_TO_GENE",
        "PATHWAY_TO_GENE",
        "MODULE_TO_KO",
        "MODULE_TO_REACTION",
        "MODULE_TO_PATHWAY",
        "PATHWAY_TO_MODULE",
        "GLYCAN_TO_REACTION",
        "REACTION_TO_GLYCAN",
        "GLYCAN_TO_PATHWAY",
        "PATHWAY_TO_GLYCAN",
        "DRUG_TO_PATHWAY",
    ):
        assert f"KeggLinkRelationship.{relationship}" in suite
    assert "_REFRESH = KeggRequestOptions(refresh=True)" in suite
    assert "_DEFAULT_REQUESTS_PER_OPERATION = 20" in controls
    assert "_MAX_REQUESTS_PER_OPERATION = 20" in controls
    assert "_OPERATION_COUNT = 6" in controls
    assert "requests_per_second=1.0" in controls
    assert "max_retries=0" in controls
    assert "response.status_code != 200" in controls
    assert "PYTEST_XDIST_WORKER" in controls


def test_live_campaign_includes_high_level_mcp_and_manual_stdio_boundaries() -> None:
    scientist_suite = _SCIENTIST_SUITE.read_text(encoding="utf-8")
    stdio_suite = _STDIO_SUITE.read_text(encoding="utf-8")

    assert "create_connected_server_and_client_session" in scientist_suite
    for tool_name in (
        "get_kegg_entries",
        "search_kegg_entries",
        "resolve_kegg_entities",
        "trace_kegg_relations",
        "map_brite_hierarchy",
        "audit_annotation_mapping",
        "compare_kegg_reference_snapshots",
    ):
        assert f'"{tool_name}"' in scientist_suite
    assert "read_resource" in scientist_suite
    assert "artifact_requires_pagination" in scientist_suite
    assert "visited_uris" in scientist_suite
    assert 'page["offset"] == expected_offset' in scientist_suite
    assert 'page["returned_bytes"]' in scientist_suite
    for namespace in ("kegg_compound", "kegg_glycan", "kegg_drug"):
        assert f'"source_namespace": "{namespace}"' in scientist_suite
    for pubchem_sid in ("124490636", "7847177"):
        assert f'"identifiers": ["{pubchem_sid}"]' in scientist_suite
    assert "delete_scope" in scientist_suite
    assert "stdio_client" in stdio_suite
    assert "KEGG_MCP_RUN_LIVE_STDIO_E2E" in stdio_suite
