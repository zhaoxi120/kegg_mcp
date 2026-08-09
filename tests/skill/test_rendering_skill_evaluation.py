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
            "Render this existing render_input.json version 6 bundle.",
            ("Require readiness", "Let the renderer validate the handoff"),
        ),
        (
            "I only have K numbers; draw the pathway.",
            (
                "original request starts with only protein FASTA or KO evidence",
                "earlier stages through the installed focused Skills",
                "never call those MCP servers here",
            ),
        ),
        (
            "Render this schema-mismatched handoff.",
            ("Accept only schema version 6", "Do not patch, repair, or reinterpret"),
        ),
        (
            "Show every annotation decision status on the pathway.",
            ("Use only the versioned legend", "Rejected, unclassified, and invalid"),
        ),
        (
            "One target failed; return the other images from this bundle.",
            (
                "one all-or-nothing bundle",
                "returns no partial `RenderResult`",
                "smaller bounded `target_ids` set",
            ),
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
        "Never parse, repair, reinterpret, or recompute",
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


def test_preceding_core_handoff_finishes_the_original_graphics_request() -> None:
    for fragment in (
        "use that handoff path unchanged",
        "formats and target scope",
        "from the original request",
        "without asking the user to copy the path",
        "do not rerun or revise any upstream analysis",
        "final stage unless the user requests a new or different analysis",
        "did not ask for graphics",
    ):
        assert fragment in CORPUS


def test_renderer_output_defaults_to_a_fresh_configured_root_child() -> None:
    normalized = " ".join(CORPUS.split())
    for fragment in (
        "user-specified output directory wins",
        "omit `output_directory`",
        "renderer allocate a fresh directory beneath its configured project output root",
        "Do not guess a root from the handoff path",
        "reuse a non-empty directory",
        "explicit or default output directory",
    ):
        assert fragment in normalized


def test_missing_suite_stage_requests_installation_or_repair() -> None:
    for fragment in (
        "Prefer DeepKOALA as the",
        "required focused Skill or declared MCP dependency",
        "request explicit permission once",
        "complete repository suite",
        "new Codex task",
        "declared `kegg-render-mcp` dependency",
        "stop before rendering",
    ):
        assert fragment in CORPUS


def test_registered_suite_with_stale_task_snapshot_does_not_reinstall() -> None:
    normalized = " ".join(CORPUS.split())
    for fragment in (
        "task_reload_required",
        "new_task_required=true",
        "current_task_reload_supported=false",
        "repeat_installation_required=false",
        "stale tool snapshot",
        "do not request or perform another installation",
        "new Codex task outside the source checkout",
    ):
        assert fragment in normalized
    assert normalized.index("task_reload_required") < normalized.index("incomplete deployment")
