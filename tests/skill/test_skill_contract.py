from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "kegg-ko-analysis"

EXPECTED_FILES = {
    "SKILL.md",
    "agents/openai.yaml",
    "references/confidence-policy.md",
    "references/deepkoala-companion.md",
    "references/module-interpretation.md",
    "references/reporting-policy.md",
    "references/visualization-handoff.md",
    "references/workflow-selection.md",
}

PUBLIC_TOOLS = {
    "analyze_ko_annotations",
    "normalize_ko_annotations",
    "get_kegg_entries",
    "map_ko_ids",
    "analyze_modules",
    "analyze_pathways",
    "compare_ko_sets",
    "probe_kegg_connectivity",
    "get_server_status",
}

PUBLIC_RESOURCES = {
    "ko-analysis://status",
    "ko-analysis://cache/info",
    "ko-analysis://results/{result_id}",
    "ko-analysis://results/{result_id}/{section}",
    "ko-analysis://results/{result_id}/{section}/{offset}/{limit}",
    "kegg-cache://entries/{database}/{identifier}",
}


def _skill_files() -> set[str]:
    return {
        path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("*") if path.is_file()
    }


def _corpus() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*")) if path.is_file()
    )


def test_skill_contains_only_the_approved_instruction_files() -> None:
    assert _skill_files() == EXPECTED_FILES
    assert not (SKILL_ROOT / "scripts").exists()
    corpus = _corpus()
    assert "TODO" not in corpus
    assert "Structuring This Skill" not in corpus


def test_frontmatter_has_only_name_and_trigger_description() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, _ = skill.split("---", maxsplit=2)
    keys = [line.split(":", maxsplit=1)[0] for line in frontmatter.splitlines() if line]
    assert keys == ["name", "description"]
    assert "name: kegg-ko-analysis" in frontmatter

    positive_triggers = (
        "K numbers",
        "KO annotation tables",
        "KEGG module or pathway",
        "metabolic reconstruction",
        "multiple KO sets",
    )
    negative_boundaries = (
        "general gene-expression analysis",
        "nucleotide assembly",
        "sequence alignment",
        "statistical enrichment",
        "non-KEGG ontology analysis",
        "pathway rendering",
        "MODULE rendering",
    )
    assert all(trigger in frontmatter for trigger in positive_triggers)
    assert all(boundary in frontmatter for boundary in negative_boundaries)
    assert "has KO evidence" in frontmatter
    assert "optional local DeepKOALA companion" in frontmatter


def test_openai_metadata_declares_real_stdio_dependency() -> None:
    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    for key in ("display_name", "short_description", "default_prompt"):
        assert re.search(rf'^  {key}: "[^"\n]+"$', metadata, flags=re.MULTILINE)
    assert "$kegg-ko-analysis" in metadata
    assert 'type: "mcp"' in metadata
    assert 'value: "kegg-mcp"' in metadata
    assert 'transport: "stdio"' in metadata
    assert "allow_implicit_invocation: true" in metadata


def test_skill_references_every_approved_tool_and_resource_file() -> None:
    corpus = _corpus()
    assert all(tool in corpus for tool in PUBLIC_TOOLS)
    assert all(resource in corpus for resource in PUBLIC_RESOURCES)
    for reference in EXPECTED_FILES - {"SKILL.md", "agents/openai.yaml"}:
        assert f"({reference})" in (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
