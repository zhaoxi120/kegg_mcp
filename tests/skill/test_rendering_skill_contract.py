from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-pathway-rendering"

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


def _corpus() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*")) if path.is_file()
    )


def test_rendering_skill_is_complete_and_instruction_only() -> None:
    files = {
        path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("*") if path.is_file()
    }
    assert files == EXPECTED_FILES
    assert not (SKILL_ROOT / "scripts").exists()
    assert not (SKILL_ROOT / "assets").exists()
    assert "TODO" not in _corpus()


def test_rendering_frontmatter_and_single_dependency() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, _ = skill.split("---", maxsplit=2)
    keys = [line.split(":", maxsplit=1)[0] for line in frontmatter.splitlines() if line]
    assert keys == ["name", "description"]
    assert "name: kegg-pathway-rendering" in frontmatter
    for fragment in ("render_input.json", "SVG or PNG", "Do not use for protein annotation"):
        assert fragment in frontmatter

    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    for key in ("display_name", "short_description", "default_prompt"):
        assert re.search(rf'^  {key}: "[^"\n]+"$', metadata, flags=re.MULTILINE)
    assert "$kegg-pathway-rendering" in metadata
    assert metadata.count('type: "mcp"') == 1
    assert 'value: "kegg-render-mcp"' in metadata
    assert 'value: "kegg-mcp"' not in metadata
    assert 'value: "deepkoala-mcp"' not in metadata


def test_rendering_skill_uses_only_renderer_tools_and_stable_handoff() -> None:
    corpus = _corpus()
    assert all(tool in corpus for tool in RENDERER_TOOLS)
    for forbidden in (
        "analyze_ko_annotations",
        "normalize_ko_annotations",
        "run_deepkoala_job",
    ):
        assert forbidden not in corpus
    assert "kegg-render://results/{render_id}" in corpus
    assert "kegg-render://results/{render_id}/{artifact}" in corpus
    assert "Do not call either earlier MCP" in corpus
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for reference in EXPECTED_FILES - {"SKILL.md", "agents/openai.yaml"}:
        assert f"({reference})" in skill


def test_rendering_skill_finishes_cross_skill_requests_without_upstream_calls() -> None:
    corpus = _corpus()
    for fragment in (
        "immediately preceding `kegg-ko-analysis`",
        "use that handoff path unchanged",
        "formats and target scope",
        "stable image files and manifest",
        "Do not call either earlier MCP",
    ):
        assert fragment in corpus


def test_rendering_skill_distinguishes_live_and_offline_asset_preflights() -> None:
    corpus = _corpus()
    for fragment in (
        "In a live access mode",
        "In `offline_cache`",
        "probe makes zero requests",
        "requested cache entries exist",
        "stale-disallowed",
        "deployment configuration",
    ):
        assert fragment in corpus


def test_rendering_skill_treats_multi_target_bundle_as_atomic() -> None:
    corpus = _corpus()
    for fragment in (
        "all-or-nothing bundle",
        "preflights every target capability",
        "do not return or reconstruct a partial result",
        "typed failing `target_id`",
        "smaller `target_ids` set",
        "never merge partial work",
    ):
        assert fragment in corpus


def test_rendering_skill_requires_authoritative_v4_total_map_handoff() -> None:
    corpus = _corpus()
    for fragment in (
        "version 4 handoff",
        "`allow_global_or_overview=True`",
        "bounded KGML `line` coordinates",
        "Accepted evidence has deterministic precedence",
        "uncertain evidence retains the renderer's dashed non-color cue",
        "pathway-category colors remain background context",
        "Arrows already present in the validated PNG remain background context",
        "does not reconstruct arrow direction",
        "do not create a model-native conceptual fallback",
    ):
        assert fragment in corpus
