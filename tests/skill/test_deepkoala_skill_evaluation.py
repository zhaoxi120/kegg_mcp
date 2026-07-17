from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILL_ROOT = ROOT / ".agents" / "skills" / "deepkoala-annotation"
CORPUS = "\n".join(path.read_text(encoding="utf-8") for path in sorted(SKILL_ROOT.rglob("*.md")))


def test_explicit_request_runs_without_second_confirmation() -> None:
    for fragment in (
        "explicit request to annotate the FASTA as authorization",
        "Do not ask for an ACK",
        "run_deepkoala_job",
        "Poll `get_deepkoala_job`",
    ):
        assert fragment in CORPUS


def test_unready_route_and_handoff_remain_bounded() -> None:
    for fragment in (
        "ask permission only for the missing installation",
        "Never install, download, or replace",
        "controlled absolute paths",
        'input_format="deepkoala_detailed"',
        "must not parse, transform, or validate CSV rows itself",
    ):
        assert fragment in CORPUS


def test_annotation_stage_preserves_scientific_boundaries() -> None:
    for fragment in (
        "computational annotation evidence",
        "not evidence that a function is absent",
        "those decisions belong to the independent `kegg-ko-analysis` stage",
        "Do not launch subprocesses",
    ):
        assert fragment in CORPUS
    assert "notice digest" in CORPUS
    assert "workflow digests" in CORPUS
    assert "python3 -m deepkoala" not in CORPUS
