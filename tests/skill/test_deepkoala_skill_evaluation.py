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


def test_first_run_discloses_cpu_and_gpu_requires_explicit_ready_request() -> None:
    normalized = " ".join(CORPUS.split())
    for fragment in (
        "Before the first `run_deepkoala_job` call in the current Codex task",
        "default job uses the `cpu` device",
        "explicit request to the LLM",
        "informational first-run notice, not a confirmation gate",
        "repeat the notice in the same task",
        "`cuda` in `allowed_devices`",
        "`cuda_available=true`",
        "`mps` in `allowed_devices`",
        "`mps_available=true`",
        "never silently substitute CPU or `device=auto`",
        "does not authorize installing or replacing",
    ):
        assert fragment in normalized
    assert normalized.index("Before the first `run_deepkoala_job` call") < normalized.index(
        "Treat an explicit request to annotate the FASTA as authorization"
    )


def test_model_is_selected_before_run_from_provenance_without_length_guessing() -> None:
    normalized = " ".join(CORPUS.split())
    for fragment in (
        "Before the first `run_deepkoala_job` call",
        "small LLM routing decision between `frag` and `full`",
        "explicit user model choice wins",
        "fragment-prone metagenomic",
        "complete, reference, or isolate proteins",
        "conservative default when provenance is ambiguous",
        "Do not invent a sequence-length cutoff",
        "pass `model` explicitly",
        "selected model to appear in `allowed_models`",
        "rather than silently substituting another model",
    ):
        assert fragment in normalized
    assert normalized.index("small LLM routing decision") < normalized.index(
        "Treat an explicit request to annotate the FASTA as authorization"
    )


def test_default_output_is_fresh_project_local_and_explicit_path_wins() -> None:
    normalized = " ".join(CORPUS.split())
    for fragment in (
        "user-specified output directory always wins",
        "omit `output_directory`",
        "allocates a fresh directory beneath its configured project output root",
        "Do not guess an output root from the FASTA path",
        "new or empty and owner-only",
        "never select an existing non-empty directory",
        "let Core allocate its fresh project output directory",
        "renderer allocate its fresh project output directory",
    ):
        assert fragment in normalized


def test_unready_route_and_handoff_remain_bounded() -> None:
    normalized = " ".join(CORPUS.split())
    for fragment in (
        "ask permission only for the missing installation",
        "Never install, download, or replace",
        "controlled absolute paths",
        'input_format="deepkoala_detailed"',
        "must not parse, transform, or validate CSV rows itself",
    ):
        assert fragment in normalized


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


def test_original_fasta_to_analysis_request_continues_without_reprompting() -> None:
    for fragment in (
        "If the original request ends at protein annotation",
        "automatically continue with the installed `kegg-ko-analysis` Skill",
        'input_format="deepkoala_detailed"',
        "send another prompt",
        "parse, or rewrite the CSV",
        "preserve its requested formats and target scope",
    ):
        assert fragment in CORPUS


def test_multi_domain_mode_requires_explicit_ready_opt_in() -> None:
    for fragment in (
        "Omit `multi` by default",
        "requests multi-domain annotation",
        "`allow_multi=true`",
        "`multi_ready=true`",
        "keep `batch_size=1`",
        "actual reported `multi` value",
    ):
        assert fragment in CORPUS


def test_missing_companion_requests_complete_suite_installation() -> None:
    normalized = " ".join(CORPUS.split())
    for fragment in (
        "declared `deepkoala-mcp` dependency",
        "stop before annotation",
        "incomplete suite deployment",
        "request explicit permission once",
        "complete repository suite",
        "new Codex task",
        "preferred first FASTA annotation route",
        "explicitly selected another annotator",
        "selected route supplies supported KO evidence",
    ):
        assert fragment in normalized
    assert normalized.index("explicitly selected another annotator") < normalized.index(
        "Otherwise require the declared `deepkoala-mcp` dependency"
    )


def test_post_install_tool_absence_never_requests_reinstallation() -> None:
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
    assert normalized.index("task_reload_required") < normalized.index(
        "incomplete suite deployment"
    )
