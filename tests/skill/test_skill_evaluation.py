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
            (
                "Prefer `analyze_ko_annotations`",
                "Top-5 MODULEs and Top-5 canonical KO",
                "descriptive pathway coverage",
            ),
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
            ("installed `deepkoala-annotation` Skill", "never call `deepkoala-mcp`"),
        ),
        (
            "Render this completed render_input.json.",
            (
                "existing compatible `render_input.json`",
                "`kegg-pathway-rendering` Skill",
                "do not repeat analysis",
            ),
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


def test_preceding_annotation_handoff_is_consumed_without_user_repetition() -> None:
    for fragment in (
        "consume its stable CSV handoff directly",
        "source` object unchanged",
        "do not ask the user to restate the path",
        "Do not rerun annotation or rewrite the CSV",
    ):
        assert fragment in CORPUS


def test_graphics_goal_continues_only_after_successful_core_analysis() -> None:
    for fragment in (
        "original request also asks to render",
        "successfully written, compatible",
        "requested formats and target scope",
        "Do not ask the user to copy the path",
        "asks only for a core report",
        "continue downstream",
    ):
        assert fragment in CORPUS
