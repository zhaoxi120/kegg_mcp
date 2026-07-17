from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-ko-analysis"
CORPUS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md")))


@pytest.mark.parametrize(
    ("prompt", "required"),
    [
        (
            "Here is detailed DeepKOALA output; analyze KEGG modules.",
            ("controlled absolute path", "analyze_ko_annotations", "Do not parse"),
        ),
        (
            "I have one column of K numbers; check pathway coverage.",
            ("Prefer `analyze_ko_annotations`", "descriptive pathway coverage"),
        ),
        (
            "Compare these two KO sets.",
            ("compare_ko_sets", "deterministic set membership"),
        ),
        (
            "Does K00844 prove that glycolysis is active?",
            ("not experimental validation", "Do not equate coverage with pathway presence"),
        ),
        (
            "I only have protein FASTA.",
            ("independent `deepkoala-annotation` Skill", "never call `deepkoala-mcp` here"),
        ),
        (
            "Render this completed render_input.json.",
            ("independent `kegg-pathway-rendering` Skill", "do not repeat analysis"),
        ),
    ],
)
def test_ko_analysis_guidance_covers_real_routes(prompt: str, required: tuple[str, ...]) -> None:
    assert prompt
    assert all(fragment in CORPUS for fragment in required)


def test_ko_analysis_preserves_scientific_and_process_boundaries() -> None:
    for fragment in (
        "Use accepted K numbers only for strict analysis",
        "Source-rejected",
        "exact completion",
        "block coverage separately",
        "artifact digests",
        "result identifier is opaque",
        "Never infer a K number",
    ):
        assert fragment in CORPUS
    assert "python3 -m deepkoala" not in CORPUS
    assert "prepare_deepkoala_job" not in CORPUS
    assert "render_analysis_bundle" not in CORPUS
