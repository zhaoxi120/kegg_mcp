from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-ko-analysis"

EXPECTED_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "references/confidence-policy.md",
    "references/module-interpretation.md",
    "references/reporting-policy.md",
    "references/workflow-selection.md",
}

CORE_TOOLS = {
    "analyze_ko_annotations",
    "normalize_ko_annotations",
    "get_kegg_entries",
    "map_ko_ids",
    "analyze_modules",
    "analyze_pathways",
    "compare_ko_sets",
    "probe_kegg_connectivity",
    "get_server_status",
    "list_analysis_results",
    "delete_analysis_result",
}

CORE_RESOURCES = {
    "ko-analysis://status",
    "ko-analysis://cache/info",
    "ko-analysis://results/{result_id}",
    "ko-analysis://results/{result_id}/{section}",
    "ko-analysis://results/{result_id}/{section}/{offset}/{limit}",
    "kegg-cache://entries/{database}/{identifier}",
}


def _files() -> set[str]:
    return {
        path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("*") if path.is_file()
    }


def _corpus() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*")) if path.is_file()
    )


def test_repository_contains_exactly_the_three_independent_skills() -> None:
    skill_directories = {
        path.name for path in (ROOT / ".agents" / "skills").iterdir() if path.is_dir()
    }
    assert skill_directories == {
        "deepkoala-annotation",
        "kegg-ko-analysis",
        "kegg-pathway-rendering",
    }


def test_ko_analysis_skill_is_complete_and_instruction_only() -> None:
    assert _files() == EXPECTED_FILES
    assert not (SKILL_ROOT / "scripts").exists()
    corpus = _corpus()
    assert "TODO" not in corpus
    assert "Structuring This Skill" not in corpus


def test_ko_analysis_frontmatter_has_only_name_and_trigger_description() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, _ = skill.split("---", maxsplit=2)
    keys = [line.split(":", maxsplit=1)[0] for line in frontmatter.splitlines() if line]
    assert keys == ["name", "description"]
    assert "name: kegg-ko-analysis" in frontmatter
    for fragment in (
        "existing K numbers",
        "KO annotation tables",
        "MODULE logic",
        "pathway KO coverage",
        "Do not use for protein-sequence annotation",
        "rendering",
        "statistical enrichment",
    ):
        assert fragment in frontmatter


def test_ko_analysis_metadata_declares_only_core_stdio_dependency() -> None:
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    for key in ("display_name", "short_description", "default_prompt"):
        assert re.search(rf'^  {key}: "[^"\n]+"$', metadata, flags=re.MULTILINE)
    assert "$kegg-ko-analysis" in metadata
    assert metadata.count('type: "mcp"') == 1
    assert metadata.count('transport: "stdio"') == 1
    assert 'value: "kegg-mcp"' in metadata
    assert 'value: "deepkoala-mcp"' not in metadata
    assert 'value: "kegg-render-mcp"' not in metadata
    assert "allow_implicit_invocation: true" in metadata


def test_ko_analysis_skill_references_only_core_tools_and_all_guides() -> None:
    corpus = _corpus()
    assert all(tool in corpus for tool in CORE_TOOLS)
    assert all(resource in corpus for resource in CORE_RESOURCES)
    for forbidden in (
        "run_deepkoala_job",
        "prepare_deepkoala_job",
        "submit_deepkoala_job",
        "render_analysis_bundle",
        "render_pathway",
        "render_module",
    ):
        assert forbidden not in corpus
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    for reference in EXPECTED_FILES - {"SKILL.md", "agents/openai.yaml"}:
        assert f"({reference})" in skill
