from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-visualization"

EXPECTED_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "references/evidence-color-policy.md",
    "references/module-rendering.md",
    "references/pathway-rendering.md",
    "references/rendering-workflow.md",
    "references/rights-and-reporting.md",
}

RENDERER_TOOLS = {
    "get_renderer_status",
    "probe_renderer_kegg_connectivity",
    "render_analysis_bundle",
    "render_pathway",
    "render_module",
    "delete_render_result",
}

RENDERER_RESOURCES = {
    "kegg-render://results/{render_id}",
    "kegg-render://results/{render_id}/{artifact}",
}


def _skill_files() -> set[str]:
    return {
        path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("*") if path.is_file()
    }


def _corpus() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*")) if path.is_file()
    )


def test_visualization_skill_is_complete_and_instruction_only() -> None:
    assert _skill_files() == EXPECTED_FILES
    assert not (SKILL_ROOT / "scripts").exists()
    assert not (SKILL_ROOT / "assets").exists()
    corpus = _corpus()
    assert "TODO" not in corpus
    assert "Structuring This Skill" not in corpus
    assert not any(path.suffix in {".py", ".sh", ".js", ".ts"} for path in SKILL_ROOT.rglob("*"))


def test_visualization_frontmatter_has_only_name_and_trigger_description() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, _ = skill.split("---", maxsplit=2)
    keys = [line.split(":", maxsplit=1)[0] for line in frontmatter.splitlines() if line]
    assert keys == ["name", "description"]
    assert "name: kegg-visualization" in frontmatter

    positive_triggers = (
        "Render, draw, color, visualize, or export",
        "KEGG pathway overlays",
        "MODULE logic diagrams",
        "existing KO evidence",
        "render_input.json",
        "SVG or PNG",
    )
    negative_boundaries = (
        "protein inference",
        "KO normalization implementation",
        "statistical enrichment",
        "flux or phenotype inference",
        "arbitrary image editing",
        "non-KEGG diagrams",
    )
    assert all(trigger in frontmatter for trigger in positive_triggers)
    assert all(boundary in frontmatter for boundary in negative_boundaries)


def test_visualization_metadata_declares_both_real_stdio_dependencies() -> None:
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    for key in ("display_name", "short_description", "default_prompt"):
        assert re.search(rf'^  {key}: "[^"\n]+"$', metadata, flags=re.MULTILINE)
    assert "$kegg-visualization" in metadata
    assert metadata.count('type: "mcp"') == 2
    assert metadata.count('transport: "stdio"') == 2
    assert 'value: "kegg-mcp"' in metadata
    assert 'value: "kegg-render-mcp"' in metadata
    assert "allow_implicit_invocation: true" in metadata


def test_visualization_skill_references_all_tools_resources_and_guides() -> None:
    corpus = _corpus()
    assert all(tool in corpus for tool in RENDERER_TOOLS)
    assert "analyze_ko_annotations" in corpus
    assert all(resource in corpus for resource in RENDERER_RESOURCES)
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for reference in EXPECTED_FILES - {"SKILL.md", "agents/openai.yaml"}:
        assert f"({reference})" in skill


def test_cross_skill_handoff_uses_versioned_absolute_file_not_private_id() -> None:
    analysis_corpus = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".agents" / "skills" / "kegg-ko-analysis").rglob("*.md"))
    )
    assert "controlled absolute `render_input.json` version 2" in analysis_corpus
    assert (
        "Never pass a private or session-scoped `result_id` between MCP processes"
        in analysis_corpus
    )
    assert "get_renderer_status" in analysis_corpus
    assert "stop with an actionable deployment result" in analysis_corpus
    assert "Do not parse KGML, manipulate pixels, assign display colors" in analysis_corpus
