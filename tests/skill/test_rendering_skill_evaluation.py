from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-pathway-rendering"
CORPUS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md")))


@pytest.mark.parametrize(
    ("prompt", "required"),
    [
        (
            "Render this existing render_input.json version 2 bundle.",
            ("Require readiness", "Let the renderer validate the handoff"),
        ),
        (
            "I only have K numbers; draw the pathway.",
            ("independent annotation or KO-analysis Skill", "never call those MCP servers here"),
        ),
        (
            "Render this old version 1 handoff.",
            ("Version 1 cannot be upgraded losslessly", "Request a new bundle"),
        ),
        (
            "Show accepted and uncertain evidence.",
            ("Use only the versioned legend", "policy-defined uncertain evidence"),
        ),
        (
            "Mark rejected predictions as absent genes.",
            ("Rejected, unclassified", "invalid predictions are excluded", "biological absence"),
        ),
        (
            "Draw MODULE M00001.",
            ("top-level AND blocks", "OR alternatives", "optional components", "MODULE references"),
        ),
    ],
)
def test_rendering_guidance_covers_real_routes(prompt: str, required: tuple[str, ...]) -> None:
    assert prompt
    assert all(fragment in CORPUS for fragment in required)


def test_rendering_never_reimplements_analysis_or_active_output() -> None:
    for fragment in (
        "Never parse, repair, upgrade, or recompute",
        "Never recompute pathway coverage from KGML",
        "Do not choose colors",
        "static SVG or PNG",
        "not proof of pathway",
    ):
        assert fragment in CORPUS
    assert "interactive HTML" not in CORPUS.replace(
        "Do not use for protein annotation, KO normalization, KEGG biological analysis, "
        "statistical enrichment, flux inference, arbitrary image editing, interactive HTML, "
        "or non-KEGG diagrams.",
        "",
    )
