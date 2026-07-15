"""Contract tests for the separately gated KEGG live CI job."""

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"


def _workflow_text() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _live_job_text() -> str:
    return _workflow_text().split("\n  validate-kegg-live:\n", maxsplit=1)[1]


def test_live_ci_is_main_only_explicitly_enabled_and_not_a_pull_request_gate() -> None:
    workflow = _workflow_text()
    live_job = _live_job_text()

    assert "workflow_dispatch:" in workflow
    assert "needs: validate" in live_job
    assert "github.ref == 'refs/heads/main'" in live_job
    assert "github.event_name == 'push'" in live_job
    assert "github.event_name == 'workflow_dispatch'" in live_job
    assert "github.event_name == 'pull_request'" not in live_job
    assert "vars.KEGG_MCP_LIVE_TESTS_ENABLED == 'true'" in live_job
    assert "name: kegg-live" in live_job


def test_live_ci_serializes_campaigns_without_cancelling_an_active_run() -> None:
    workflow = _workflow_text()
    live_job = _live_job_text()

    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in workflow
    assert "group: kegg-live-api" in live_job
    assert "cancel-in-progress: false" in live_job
    assert "queue: single" not in workflow
    assert "timeout-minutes: 10" in live_job


def test_live_ci_uses_the_bounded_suite_and_explicit_access_contract() -> None:
    live_job = _live_job_text()
    setup_steps, campaign_step = live_job.split(
        "      - name: Run bounded KEGG live compatibility campaign\n", maxsplit=1
    )

    assert "tests/live/test_kegg_api_live.py" in live_job
    assert "--run-kegg-live" in live_job
    assert "-m live_kegg" in live_job
    assert "--kegg-live-use-env-proxy" not in live_job
    assert "KEGG_MCP_ACCESS_MODE: ${{ vars.KEGG_MCP_ACCESS_MODE }}" in live_job
    assert "KEGG_MCP_ACADEMIC_USE_CONFIRMED:" in live_job
    assert "KEGG_MCP_LICENSED_ENDPOINT: ${{ secrets.KEGG_MCP_LICENSED_ENDPOINT }}" in live_job
    assert "KEGG_MCP_LICENSED_USE_CONFIRMED:" in live_job
    assert "KEGG_MCP_LICENSED_ENDPOINT" not in setup_steps
    assert "KEGG_MCP_LICENSED_ENDPOINT" in campaign_step
    assert "upload-artifact" not in live_job
