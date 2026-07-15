"""Static contract checks for the default 120-request KEGG campaign."""

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
_LIVE_CONTROLS = _REPOSITORY_ROOT / "tests" / "live" / "conftest.py"
_LIVE_SUITE = _REPOSITORY_ROOT / "tests" / "live" / "test_kegg_api_live.py"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _validate_job_text() -> str:
    return _workflow_text().split("\n  validate-deepkoala-companion:\n", maxsplit=1)[0]


def test_default_and_pull_request_ci_enable_public_academic_access() -> None:
    workflow = _workflow_text()
    validate_job = _validate_job_text()

    assert "pull_request:" in workflow
    assert "KEGG_MCP_ACCESS_MODE: public_academic" in validate_job
    assert 'KEGG_MCP_ACADEMIC_USE_CONFIRMED: "true"' in validate_job
    assert "offline_cache" not in workflow
    assert "validate-kegg-live" not in workflow


def test_default_ci_runs_the_live_campaign_without_an_opt_in_flag() -> None:
    validate_job = _validate_job_text()

    assert "Test, including 120 live KEGG requests" in validate_job
    assert "uv run --frozen pytest" in validate_job
    assert "--run-kegg-live" not in validate_job
    assert "-m live_kegg" not in validate_job
    assert "upload-artifact" not in validate_job


def test_live_suite_has_four_operations_thirty_calls_each_one_rps_and_zero_retries() -> None:
    controls = _LIVE_CONTROLS.read_text(encoding="utf-8")
    suite = _LIVE_SUITE.read_text(encoding="utf-8")

    assert suite.count("@pytest.mark.live_kegg") == 4
    assert "_REQUESTS_PER_OPERATION = 30" in suite
    assert suite.count("range(_REQUESTS_PER_OPERATION):") == 4
    assert "_REFRESH = KeggRequestOptions(refresh=True)" in suite
    assert "_MAX_LIVE_REQUESTS = 120" in controls
    assert "requests_per_second=1.0" in controls
    assert "max_retries=0" in controls
    assert "response.status_code != 200" in controls
    assert "PYTEST_XDIST_WORKER" in controls
