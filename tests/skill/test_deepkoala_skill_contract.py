from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "deepkoala-annotation"


def _corpus() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*")) if path.is_file()
    )


def test_deepkoala_skill_is_complete_and_instruction_only() -> None:
    files = {
        path.relative_to(SKILL_ROOT).as_posix() for path in SKILL_ROOT.rglob("*") if path.is_file()
    }
    assert files == {
        "SKILL.md",
        "agents/openai.yaml",
        "references/deployment-and-handoff.md",
    }
    assert not (SKILL_ROOT / "scripts").exists()
    assert "TODO" not in _corpus()


def test_deepkoala_frontmatter_and_single_dependency() -> None:
    skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    _, frontmatter, _ = skill.split("---", maxsplit=2)
    keys = [line.split(":", maxsplit=1)[0] for line in frontmatter.splitlines() if line]
    assert keys == ["name", "description"]
    assert "name: deepkoala-annotation" in frontmatter
    assert "protein FASTA" in frontmatter
    assert "Do not use for KO normalization" in frontmatter

    metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
    for key in ("display_name", "short_description", "default_prompt"):
        assert re.search(rf'^  {key}: "[^"\n]+"$', metadata, flags=re.MULTILINE)
    assert "$deepkoala-annotation" in metadata
    assert metadata.count('type: "mcp"') == 1
    assert 'value: "deepkoala-mcp"' in metadata
    assert 'value: "kegg-mcp"' not in metadata
    assert 'value: "kegg-render-mcp"' not in metadata


def test_deepkoala_skill_uses_only_companion_tools_and_stable_files() -> None:
    corpus = _corpus()
    for tool in (
        "get_deepkoala_runner_status",
        "run_deepkoala_job",
        "get_deepkoala_job",
        "cancel_deepkoala_job",
        "delete_deepkoala_job",
    ):
        assert tool in corpus
    for forbidden in ("analyze_ko_annotations", "render_analysis_bundle"):
        assert forbidden not in corpus
    assert "deepkoala_annotations.csv" in corpus
    assert "deepkoala_run_report.md" in corpus
    assert "Omit `model_date` for the default call" in corpus
    assert "resolved model name and model version" in corpus
    assert "not a private identifier" in corpus
    assert 'handoff `schema_version="2"`' in corpus
    assert "`output_coverage`" in corpus
    assert "all and only the unique input FASTA IDs" in corpus


def test_deepkoala_skill_allows_stable_cross_skill_continuation() -> None:
    corpus = _corpus()
    for fragment in (
        "original request also asks for KO analysis",
        "automatically continue with the installed `kegg-ko-analysis` Skill",
        "Do not ask the user to copy",
        "source` object unchanged",
        "not call either downstream MCP itself",
    ):
        assert fragment in corpus
