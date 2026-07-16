from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-visualization"
CORPUS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md")))


@pytest.mark.parametrize(
    ("prompt", "required_guidance"),
    [
        (
            "Render a KEGG pathway image from this protein FASTA.",
            (
                "deepkoala-mcp -> kegg-mcp -> kegg-render-mcp",
                "Keep the successful DeepKOALA job",
                "complete version 2 output bundle",
            ),
        ),
        (
            "Color a KEGG pathway from these K numbers.",
            (
                "Never invoke DeepKOALA when usable KO evidence already exists",
                "analyze_ko_annotations",
                "`output_directory`",
            ),
        ),
        (
            "Render this existing render_input.json version 2 bundle.",
            (
                "Skip annotation and core analysis",
                "Let the renderer validate the handoff",
            ),
        ),
        (
            "Make the image even though kegg-render-mcp is unavailable.",
            (
                "Stop without attempting local rendering in the Skill",
                "do not install,\ndownload, synthesize, or invoke an unrelated image tool",
            ),
        ),
        (
            "Render this old render_input.json version 1 file.",
            (
                "Version 1 cannot be upgraded losslessly",
                "Request a new analysis bundle",
            ),
        ),
        (
            "Show accepted and uncertain KO evidence on the map.",
            (
                "Accepted and uncertain evidence require distinct renderer-provided visual states",
                "redundant\n  non-color cues",
            ),
        ),
        (
            "Mark rejected DeepKOALA predictions as missing genes.",
            (
                "Never color\nrejected predictions",
                "not biological absence",
            ),
        ),
        (
            "Render the KEGG overview map as ordinary KO boxes.",
            (
                "Global and overview maps require a separately reviewed line-overlay policy",
                "rejection or explicit summary-only result",
            ),
        ),
        (
            "Draw the logic for MODULE M00001.",
            (
                "top-level spaces and plus signs are AND",
                "commas are OR",
                "a minus sign marks an optional component",
                "parentheses preserve grouping",
                "MODULE references remain distinct nodes",
            ),
        ),
    ],
)
def test_visualization_route_has_required_guidance(
    prompt: str, required_guidance: tuple[str, ...]
) -> None:
    assert prompt
    assert all(fragment in CORPUS for fragment in required_guidance)


def test_visualization_never_duplicates_renderer_or_analysis_logic() -> None:
    assert "Never implement inference, normalization, KGML parsing" in CORPUS
    assert "Never recompute the supplied pathway coverage" in CORPUS
    assert "The Skill does not\nchoose colors" in CORPUS
    assert "Do not claim validated\n   pathway activity" in CORPUS
    assert "Never claim that\nadding the shown genes will activate a biological process" in CORPUS
