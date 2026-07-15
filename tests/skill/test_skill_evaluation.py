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
            ("independent annotation Skill", "controlled absolute"),
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
    assert "Do not execute or describe a DeepKOALA workflow here" in CORPUS
    assert "Do not implement rendering here" in CORPUS
    assert "python3 -m deepkoala" not in CORPUS
    assert "prepare_deepkoala" not in CORPUS
    assert "Never infer a K number" in CORPUS
    assert "Source-rejected" in CORPUS
    assert "workflow hashes" in CORPUS
    assert "Do not equate coverage with pathway presence" in CORPUS
