from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-ko-analysis"
CORPUS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md")))


@pytest.mark.parametrize(
    ("prompt", "required_guidance"),
    [
        (
            "I have a protein FASTA file and want to analyze metabolic functions.",
            (
                "get_deepkoala_runner_status",
                "first annotation-tool call",
                "controlled absolute",
            ),
        ),
        (
            "Here is detailed DeepKOALA output; analyze KEGG modules.",
            ("file_path", "normalize_ko_annotations", "analyze_modules"),
        ),
        (
            "I have one column of K numbers; check carbon-metabolism coverage.",
            ("Do not recommend annotation software", "analyze_ko_annotations"),
        ),
        (
            "Compare these two KO sets.",
            ("compare_ko_sets", "deterministic set differences"),
        ),
        (
            "Does K00844 prove that glycolysis is active?",
            ("does not prove pathway activity", "not experimentally validated"),
        ),
        (
            "Map this gene name to a KO for me.",
            ("Never assign or guess a K number from a gene name", "stable identifier"),
        ),
    ],
)
def test_evaluation_prompt_has_conservative_routing(
    prompt: str, required_guidance: tuple[str, ...]
) -> None:
    assert prompt
    assert all(fragment in CORPUS for fragment in required_guidance)


def test_skill_never_duplicates_analysis_or_executes_external_annotators() -> None:
    assert "Let the tools perform validation, normalization, and analysis exactly once" in CORPUS
    assert "does not implement inference" in CORPUS
    assert "Do not install, download, or repair dependencies silently" in CORPUS
    assert "Do not implement rendering here" in CORPUS
    assert "python3 -m deepkoala" not in CORPUS
    assert "prepare_deepkoala_job" in CORPUS
    assert "Never infer a K number" in CORPUS
    assert "Source-rejected" in CORPUS
    assert "workflow hashes" in CORPUS
    assert "Do not equate coverage with pathway presence" in CORPUS


def test_deepkoala_routes_are_local_first_and_permission_gated() -> None:
    for state in (
        "local_ready",
        "local_runner_misconfigured",
        "local_deepkoala_present_companion_missing",
        "local_not_installed",
        "installation_declined",
        "remote_api_unavailable",
    ):
        assert state in CORPUS
    assert "ask whether the user wants to install and register local" in CORPUS
    assert "preserve the original\nFASTA, stop annotation, and make no local change" in CORPUS
    assert "GenomeNet does not provide a DeepKOALA API" in CORPUS
    assert "refuse simulated form submission" in CORPUS
    assert "Obtain permission before any package install" in CORPUS
    assert "there is no remote\nupload branch" in CORPUS
    assert "Never send FASTA to the core `kegg-mcp` server" in CORPUS


def test_top_pathway_route_keeps_full_relationships_out_of_model_context() -> None:
    assert 'pathway_selection={"mode":"top_detected"' in CORPUS
    assert "Do not parse or rank relationship rows in the Skill" in CORPUS
    assert "Do not read the CSV,\nKO-to-pathway relationships" in CORPUS
    assert "Do not call `map_ko_ids` and aggregate its full rows" in CORPUS
