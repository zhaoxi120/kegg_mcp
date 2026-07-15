"""Static contract checks for the separately gated KEGG live CI job."""

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
_LIVE_CONTROLS = _REPOSITORY_ROOT / "tests" / "live" / "conftest.py"
_LIVE_SUITE = _REPOSITORY_ROOT / "tests" / "live" / "test_kegg_api_live.py"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _live_job_text() -> str:
    return _workflow_text().split("\n  validate-kegg-live:\n", maxsplit=1)[1]


def test_default_validation_remains_offline() -> None:
    validate_job = _workflow_text().split("\n  validate-deepkoala-companion:\n", maxsplit=1)[0]

    assert "KEGG_MCP_ACCESS_MODE: offline_cache" in validate_job
    assert "--run-kegg-live" not in validate_job


def test_live_ci_is_main_only_explicit_and_not_a_pull_request_gate() -> None:
    workflow = _workflow_text()
    live_job = _live_job_text()

    assert "workflow_dispatch:" in workflow
    assert "run_kegg_live:" in workflow
    assert "inputs.run_kegg_live == true" in live_job
    assert "- validate" in live_job
    assert "- validate-deepkoala-companion" in live_job
    assert "github.ref == 'refs/heads/main'" in live_job
    assert "github.event_name == 'workflow_dispatch'" in live_job
    assert "github.event_name == 'push'" not in live_job
    assert "github.event_name == 'pull_request'" not in live_job
    assert "vars.KEGG_MCP_LIVE_TESTS_ENABLED == 'true'" in live_job
    assert "name: kegg-live" in live_job


def test_live_ci_is_serialized_bounded_and_does_not_upload_payloads() -> None:
    live_job = _live_job_text()

    assert "group: kegg-live-api" in live_job
    assert "cancel-in-progress: false" in live_job
    assert "timeout-minutes: 5" in live_job
    assert "tests/live/test_kegg_api_live.py" in live_job
    assert "--run-kegg-live" in live_job
    assert "-m live_kegg" in live_job
    assert "four-request" in live_job
    assert "upload-artifact" not in live_job


def test_live_suite_has_four_cases_one_rps_and_zero_retries() -> None:
    controls = _LIVE_CONTROLS.read_text(encoding="utf-8")
    suite = _LIVE_SUITE.read_text(encoding="utf-8")

    assert suite.count("@pytest.mark.live_kegg") == 4
    assert "_MAX_LIVE_REQUESTS = 4" in controls
    assert "requests_per_second=1.0" in controls
    assert "max_retries=0" in controls
    assert "response.status_code == 429" in controls


def test_live_credentials_are_exposed_only_to_the_campaign_step() -> None:
    live_job = _live_job_text()
    setup, campaign = live_job.split(
        "      - name: Run four-request KEGG compatibility check\n", maxsplit=1
    )

    assert "KEGG_MCP_LICENSED_ENDPOINT" not in setup
    assert "KEGG_MCP_ACCESS_MODE: ${{ vars.KEGG_MCP_ACCESS_MODE }}" in campaign
    assert "KEGG_MCP_ACADEMIC_USE_CONFIRMED:" in campaign
    assert "KEGG_MCP_LICENSED_ENDPOINT: ${{ secrets.KEGG_MCP_LICENSED_ENDPOINT }}" in campaign
    assert "KEGG_MCP_LICENSED_USE_CONFIRMED:" in campaign
