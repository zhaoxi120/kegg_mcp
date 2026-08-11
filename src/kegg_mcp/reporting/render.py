"""Pure deterministic rendering for bounded JSON, Markdown, and accepted-KO CSV artifacts."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Sequence
from itertools import chain
from typing import Literal, NoReturn

from kegg_mcp.analysis.contracts import ModuleEvaluationResult
from kegg_mcp.analysis.functional_comparison import (
    ModuleTargetComparison,
    PathwayTargetComparison,
)
from kegg_mcp.analysis.pathway_coverage import PathwayCoverageResult
from kegg_mcp.analysis.pathway_ranking import PATHWAY_RANKING_METHOD
from kegg_mcp.domain.annotations import (
    AnalysisUnit,
    NormalizedStatus,
)
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.reporting.contracts import (
    REPORT_FORMAT_NAME,
    REPORT_FORMAT_VERSION,
    REPORT_RENDERER_NAME,
    REPORT_RENDERER_VERSION,
    RenderedReport,
    ReportArtifact,
    ReportInput,
    ReportLimits,
    ReportSection,
    StructuredReport,
)

_ANALYSIS_CSV_HEADER = ("ko_id", "normalized_status")
_MARKDOWN_TRUNCATION_NOTICE = (
    "> Markdown summary truncated at the recorded preview or UTF-8 byte limit. "
    "The structured JSON and accepted-KO CSV artifacts remain complete within their hard limits."
)
_MARKDOWN_TEXT_TRANSLATION = str.maketrans(
    {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        "`": "&#96;",
        "[": "&#91;",
        "]": "&#93;",
        "(": "&#40;",
        ")": "&#41;",
        "!": "&#33;",
        "|": "&#124;",
        "\\": "&#92;",
        "*": "&#42;",
        "_": "&#95;",
        "~": "&#126;",
        "\r": " ",
        "\n": " ",
    }
)


def _limit_exceeded(name: str, actual: int, maximum: int) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        f"Report input exceeds the configured {name} limit.",
        suggested_action="Reduce the report input or raise the explicit bounded report limit.",
        safe_details=(
            SafeDetail(name="limit_name", value=name),
            SafeDetail(name="actual", value=str(actual)),
            SafeDetail(name="maximum", value=str(maximum)),
        ),
    )


def _source_entry_count(report: ReportInput) -> int:
    count = len(report.dataset.sources)
    count += sum(len(result.sources) for result in report.pathway_coverages)
    for comparison in (
        report.ko_comparison,
        report.module_comparison,
        report.pathway_comparison,
    ):
        if comparison is not None:
            count += sum(len(dataset.sources) for dataset in comparison.datasets)
    return count


def _warning_entry_count(report: ReportInput) -> int:
    count = len(report.dataset.diagnostic_preview)
    for result in report.module_evaluations:
        count += len(result.warnings) + len(result.unresolved_references)
    for result in report.pathway_coverages:
        count += len(result.warnings)
    if report.ko_comparison is not None:
        count += len(report.ko_comparison.warnings)
    if report.module_comparison is not None:
        count += len(report.module_comparison.context_warnings)
        count += sum(
            len(target.unresolved_references) for target in report.module_comparison.targets
        )
    if report.pathway_comparison is not None:
        count += len(report.pathway_comparison.context_warnings)
        for target in report.pathway_comparison.targets:
            count += sum(len(outcome.warnings) for outcome in target.comparison.outcomes)
    return count


def _preflight(report: ReportInput, limits: ReportLimits) -> None:
    input_rows = report.dataset.input_rows
    if input_rows > limits.max_input_rows:
        _limit_exceeded("input rows", input_rows, limits.max_input_rows)
    accepted_ko_count = len(report.dataset.accepted_ko_ids)
    if accepted_ko_count > limits.max_accepted_ko_ids:
        _limit_exceeded(
            "accepted K numbers",
            accepted_ko_count,
            limits.max_accepted_ko_ids,
        )

    source_entries = _source_entry_count(report)
    if source_entries > limits.max_source_entries:
        _limit_exceeded("source entries", source_entries, limits.max_source_entries)

    module_targets = len(report.module_evaluations)
    if report.module_comparison is not None:
        module_targets += len(report.module_comparison.targets)
    if module_targets > limits.max_module_targets:
        _limit_exceeded("MODULE targets", module_targets, limits.max_module_targets)

    pathway_targets = len(report.pathway_coverages)
    if report.pathway_comparison is not None:
        pathway_targets += len(report.pathway_comparison.targets)
    if pathway_targets > limits.max_pathway_targets:
        _limit_exceeded("pathway targets", pathway_targets, limits.max_pathway_targets)
    if len(report.pathway_ranking) > limits.max_pathway_ranking_rows:
        _limit_exceeded(
            "pathway ranking rows",
            len(report.pathway_ranking),
            limits.max_pathway_ranking_rows,
        )
    total_targets = module_targets + pathway_targets
    if total_targets > limits.max_total_targets:
        _limit_exceeded("total analysis targets", total_targets, limits.max_total_targets)

    warning_entries = _warning_entry_count(report)
    if warning_entries > limits.max_warning_entries:
        _limit_exceeded("warning entries", warning_entries, limits.max_warning_entries)


def _render_canonical_json(report: ReportInput, limits: ReportLimits) -> str:
    payload = StructuredReport(
        format_name=REPORT_FORMAT_NAME,
        format_version=REPORT_FORMAT_VERSION,
        renderer_name=REPORT_RENDERER_NAME,
        renderer_version=REPORT_RENDERER_VERSION,
        limits=limits,
        report=report,
    ).model_dump(mode="json")
    encoder = json.JSONEncoder(
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    chunks: list[str] = []
    byte_count = 0
    for chunk in encoder.iterencode(payload):
        byte_count += len(chunk.encode("utf-8"))
        if byte_count > limits.max_structured_json_bytes:
            _limit_exceeded(
                "structured JSON bytes",
                byte_count,
                limits.max_structured_json_bytes,
            )
        chunks.append(chunk)
    return "".join(chunks)


def _csv_line(values: Sequence[str]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, dialect="excel", lineterminator="\n")
    writer.writerow(values)
    return stream.getvalue()


def _render_accepted_ko_csv(report: ReportInput, limits: ReportLimits) -> str:
    chunks: list[str] = []
    byte_count = 0
    rows: Iterable[Sequence[str]] = chain(
        (_ANALYSIS_CSV_HEADER,),
        ((ko_id, NormalizedStatus.ACCEPTED.value) for ko_id in report.dataset.accepted_ko_ids),
    )
    for values in rows:
        line = _csv_line(values)
        byte_count += len(line.encode("utf-8"))
        if byte_count > limits.max_accepted_ko_csv_bytes:
            _limit_exceeded(
                "accepted-KO CSV bytes",
                byte_count,
                limits.max_accepted_ko_csv_bytes,
            )
        chunks.append(line)
    return "".join(chunks)


def _markdown_cell(value: object) -> str:
    """Encode untrusted text so Markdown cannot reinterpret it as active syntax."""
    return str(value).translate(_MARKDOWN_TEXT_TRANSLATION)


def _markdown_path(value: str) -> str:
    """Keep ordinary absolute paths readable while neutralizing active Markdown syntax."""
    return _markdown_cell(value).replace("&#95;", "_")


def _ratio(value: float | None) -> str:
    return "not_evaluable" if value is None else f"{value:.1%}"


def _exact_completion(result: ModuleEvaluationResult) -> str:
    if result.is_complete is True:
        return "complete"
    if result.is_complete is False:
        return "incomplete"
    return "not_evaluable"


def _block_coverage(result: ModuleEvaluationResult) -> str:
    if result.block_coverage is None:
        return (
            f"not_evaluable ({result.completed_required_blocks}/"
            f"{result.required_block_count} required blocks completed)"
        )
    return (
        f"{result.completed_required_blocks}/{result.required_block_count} "
        f"({_ratio(result.block_coverage)})"
    )


def _analysis_unit_caveat(unit: AnalysisUnit) -> str:
    messages = {
        AnalysisUnit.ISOLATE_GENOME: (
            "The isolate-genome result summarizes encoded KO annotations and remains sensitive "
            "to sequence recovery, gene calling, and annotation quality."
        ),
        AnalysisUnit.ISOLATE_PROTEOME: (
            "The isolate-proteome result summarizes supplied protein annotations and remains "
            "sensitive to proteome and annotation coverage."
        ),
        AnalysisUnit.MAG: (
            "The MAG result is sensitive to assembly, binning, recovery, gene calling, and "
            "annotation quality."
        ),
        AnalysisUnit.PANGENOME: (
            "The pangenome result represents pooled encoded potential and is not attributable "
            "to one isolate."
        ),
        AnalysisUnit.METAGENOMIC_COMMUNITY: (
            "The metagenomic-community result represents pooled encoded potential and does not "
            "describe a complete pathway in one organism."
        ),
        AnalysisUnit.MIXED: (
            "The mixed analysis unit limits organism- and sample-level interpretation."
        ),
        AnalysisUnit.UNKNOWN: (
            "The analysis unit is unknown, so organism-level interpretation is limited."
        ),
    }
    return messages[unit]


def _warning_items(report: ReportInput) -> tuple[tuple[str, str], ...]:
    items: list[tuple[str, str]] = []
    for diagnostic in report.dataset.diagnostic_preview:
        items.append((diagnostic.code.value, diagnostic.message))
    for result in report.module_evaluations:
        items.extend((warning.code.value, warning.message) for warning in result.warnings)
        for issue in result.unresolved_references:
            items.append((f"MODULE_{issue.kind.value.upper()}", issue.message))
    for result in report.pathway_coverages:
        items.extend((warning.code.value, warning.message) for warning in result.warnings)
    if report.ko_comparison is not None:
        items.extend(
            (warning.code.value, warning.message) for warning in report.ko_comparison.warnings
        )
    if report.module_comparison is not None:
        items.extend(
            (warning.code.value, warning.message)
            for warning in report.module_comparison.context_warnings
        )
        for target in report.module_comparison.targets:
            for issue in target.unresolved_references:
                items.append((f"MODULE_{issue.kind.value.upper()}", issue.message))
    if report.pathway_comparison is not None:
        items.extend(
            (warning.code.value, warning.message)
            for warning in report.pathway_comparison.context_warnings
        )
        for target in report.pathway_comparison.targets:
            for outcome in target.comparison.outcomes:
                items.extend((warning.code.value, warning.message) for warning in outcome.warnings)

    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return tuple(unique)


def _append_primary_modules(
    lines: list[str],
    report: ReportInput,
    limits: ReportLimits,
) -> bool:
    lines.extend(
        (
            "## KEGG MODULE evaluation",
            "",
            "Exact completion is Boolean evaluation of the KEGG MODULE definition. Project "
            "block coverage is the descriptive ratio of completed required top-level blocks to "
            "all required top-level blocks when every required block is evaluable; it is not an "
            "official KEGG completeness percentage.",
            "",
        )
    )
    if not report.module_evaluations:
        lines.extend(("No MODULE evaluation was supplied.", ""))
        return False
    lines.extend(
        (
            "| MODULE | Evaluation status | Exact completion | Project block coverage |",
            "| --- | --- | --- | --- |",
        )
    )
    selected = report.module_evaluations[: limits.max_markdown_module_targets]
    for result in selected:
        label = result.module_id
        if result.module_name:
            label = f"{label} — {result.module_name}"
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(label),
                    result.evaluation_status.value,
                    _exact_completion(result),
                    _block_coverage(result),
                )
            )
            + " |"
        )
    truncated = len(selected) < len(report.module_evaluations)
    if truncated:
        lines.append(
            f"\nMODULE preview shows {len(selected)} of {len(report.module_evaluations)} targets."
        )
    lines.append("")
    return truncated


def _pathway_coverage_text(result: PathwayCoverageResult) -> str:
    if result.coverage_ratio is None:
        return f"0/{result.reference_unique_ko_count} (not_evaluable)"
    return (
        f"{result.detected_unique_ko_count}/{result.reference_unique_ko_count} "
        f"({_ratio(result.coverage_ratio)})"
    )


def _append_primary_pathways(
    lines: list[str],
    report: ReportInput,
    limits: ReportLimits,
) -> bool:
    lines.extend(
        (
            "## Descriptive pathway KO coverage",
            "",
            "Pathway KO coverage is a descriptive unique-KO intersection against the recorded "
            "reference namespace and denominator. It does not establish pathway presence, "
            "completeness, expression, activity, flux, phenotype, or statistical significance.",
            "",
        )
    )
    if not report.pathway_coverages:
        lines.extend(("No pathway coverage result was supplied.", ""))
        return False
    lines.extend(
        (
            "| Pathway | Namespace | Evaluation status | Detected/reference KOs |",
            "| --- | --- | --- | --- |",
        )
    )
    selected = report.pathway_coverages[: limits.max_markdown_pathway_targets]
    for result in selected:
        label = f"{result.pathway_id} — {result.pathway_name}"
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(label),
                    result.reference_namespace.value,
                    result.evaluation_status.value,
                    _pathway_coverage_text(result),
                )
            )
            + " |"
        )
    truncated = len(selected) < len(report.pathway_coverages)
    if truncated:
        lines.append(
            f"\nPathway preview shows {len(selected)} of {len(report.pathway_coverages)} targets."
        )
    lines.append("")
    return truncated


def _append_pathway_ranking(
    lines: list[str],
    report: ReportInput,
    limits: ReportLimits,
) -> bool:
    """Append a bounded candidate ranking without placing full KO lists in Markdown."""
    if report.pathway_selection is None:
        return False
    lines.extend(("", "## Pathway target selection", ""))
    selection = report.pathway_selection
    lines.append(
        f"Ranking method: `{PATHWAY_RANKING_METHOD}`. Requested target count: "
        f"{selection.top_n}. Candidate pathway count: {len(report.pathway_ranking)}."
    )
    if not report.pathway_ranking:
        lines.extend(("", "No ranked pathway candidate was retained.", ""))
        return False
    lines.extend(
        (
            "",
            "| Rank | Canonical pathway | Detected unique selected KOs "
            "| Relationship rows | Selected |",
            "| ---: | --- | ---: | ---: | --- |",
        )
    )
    ranking_execution = (
        report.execution.pathway_parameters.ranking if report.execution is not None else None
    )
    selected_pathway_ids = (
        frozenset(ranking_execution.selected_pathway_ids) if ranking_execution is not None else None
    )
    selected = report.pathway_ranking[: limits.max_markdown_pathway_ranking_rows]
    for row in selected:
        is_selected = (
            row.pathway_id in selected_pathway_ids
            if selected_pathway_ids is not None
            else row.rank <= selection.top_n
        )
        lines.append(
            f"| {row.rank} | `{row.pathway_id}` | {row.detected_unique_ko_count} | "
            f"{row.relationship_row_count} | "
            f"{'yes' if is_selected else 'no'} |"
        )
    truncated = len(selected) < len(report.pathway_ranking)
    if truncated:
        lines.append(
            f"\nPathway ranking preview shows {len(selected)} of "
            f"{len(report.pathway_ranking)} candidates."
        )
    lines.extend(
        (
            "",
            "The complete detected-KO sets and relationship rows are retained across the "
            "structured report and dedicated ranking artifacts.",
            "",
        )
    )
    return truncated


def _append_module_selection(lines: list[str], report: ReportInput) -> None:
    """Append compact automatic MODULE-selection provenance."""
    if report.execution is None or report.execution.module_ranking is None:
        return
    ranking = report.execution.module_ranking
    lines.extend(("", "## MODULE target selection", ""))
    lines.append(
        f"Ranking method: `{ranking.method}` version `{ranking.method_version}` using accepted "
        f"unique-KO evidence. Requested target count: "
        f"{ranking.selection.top_n}. Candidate MODULE count: {ranking.candidate_module_count}."
    )
    selected = ", ".join(f"`{module_id}`" for module_id in ranking.selected_module_ids)
    lines.extend(
        (
            f"Selected MODULE targets: {selected or 'none'}.",
            "",
            "This unique selected-KO overlap ranking chooses candidates only. It is not "
            "MODULE completion, enrichment, activity, or validation; exact completion and "
            "required-block coverage are evaluated separately below.",
            "",
        )
    )


def _module_comparison_outcomes(target: ModuleTargetComparison) -> str:
    return "; ".join(
        (
            f"{_markdown_cell(outcome.label)}={outcome.evaluation_status.value},"
            f" exact:"
            f"{'unknown' if outcome.is_complete is None else str(outcome.is_complete).lower()},"
            f" blocks:{outcome.completed_required_blocks}/{outcome.required_block_count}"
        )
        for outcome in target.comparison.outcomes
    )


def _pathway_comparison_outcomes(target: PathwayTargetComparison) -> str:
    return "; ".join(
        (
            f"{_markdown_cell(outcome.label)}={outcome.evaluation_status.value},"
            f" {outcome.detected_reference_ko_count}/{outcome.reference_unique_ko_count}"
            f" ({_ratio(outcome.coverage_ratio)})"
        )
        for outcome in target.comparison.outcomes
    )


def _append_comparisons(
    lines: list[str],
    report: ReportInput,
    limits: ReportLimits,
) -> bool:
    lines.extend(
        (
            "## Deterministic comparisons",
            "",
            "KO-set and functional comparisons are deterministic descriptive outcomes, not "
            "statistical tests. A missing annotation is not evidence of biological absence, and "
            "no p-value, fold change, enrichment, or differential-function claim is made.",
            "",
        )
    )
    truncated = False
    if report.ko_comparison is not None:
        labels = ", ".join(_markdown_cell(item.label) for item in report.ko_comparison.datasets)
        lines.extend((f"Compared datasets, in caller order: {labels}.", ""))
        lines.extend(
            (
                "| Accepted-KO union count | Shared by all | Partial membership patterns |",
                "| ---: | ---: | ---: |",
            )
        )
        partition = report.ko_comparison.partition
        lines.append(
            f"| {partition.union_count} | {partition.shared_by_all.count} | "
            f"{partition.partially_shared_pattern_count} |"
        )
        lines.append("")

    if report.module_comparison is not None:
        lines.extend(
            (
                "### Shared-definition MODULE outcomes",
                "",
                "| MODULE | Ordered accepted-KO outcomes |",
                "| --- | --- |",
            )
        )
        selected = report.module_comparison.targets[: limits.max_markdown_comparison_targets]
        for target in selected:
            lines.append(f"| {target.module_id} | {_module_comparison_outcomes(target)} |")
        if len(selected) < len(report.module_comparison.targets):
            truncated = True
            lines.append(
                f"\nMODULE comparison preview shows {len(selected)} of "
                f"{len(report.module_comparison.targets)} targets."
            )
        lines.append("")

    if report.pathway_comparison is not None:
        lines.extend(
            (
                "### Shared-denominator pathway outcomes",
                "",
                "| Pathway reference | Ordered accepted-KO coverage outcomes |",
                "| --- | --- |",
            )
        )
        selected = report.pathway_comparison.targets[: limits.max_markdown_comparison_targets]
        for target in selected:
            lines.append(
                f"| {target.reference.pathway_id} "
                f"({target.reference.reference_namespace.value}) | "
                f"{_pathway_comparison_outcomes(target)} |"
            )
        if len(selected) < len(report.pathway_comparison.targets):
            truncated = True
            lines.append(
                f"\nPathway comparison preview shows {len(selected)} of "
                f"{len(report.pathway_comparison.targets)} targets."
            )
        lines.append("")

    if all(
        value is None
        for value in (
            report.ko_comparison,
            report.module_comparison,
            report.pathway_comparison,
        )
    ):
        lines.extend(("No comparison result was supplied.", ""))
    return truncated


def _append_warnings_and_provenance(
    lines: list[str],
    report: ReportInput,
    limits: ReportLimits,
) -> bool:
    lines.extend(("## Caveats and unresolved data", ""))
    lines.append(
        "A K-number assignment is annotation evidence, not experimental validation; a rejected "
        "or missing prediction does not demonstrate functional absence."
    )
    lines.append(_analysis_unit_caveat(report.dataset.analysis_unit))
    warning_items = _warning_items(report)
    selected_warnings = warning_items[: limits.max_markdown_warnings]
    if selected_warnings:
        lines.append("")
        for code, message in selected_warnings:
            lines.append(f"- `{code}`: {_markdown_cell(message)}")
    else:
        lines.append("\nNo source warning or unresolved MODULE reference was reported.")
    truncated = len(selected_warnings) < len(warning_items)
    if truncated:
        lines.append(
            f"\nWarning preview shows {len(selected_warnings)} of "
            f"{len(warning_items)} unique entries."
        )

    lines.extend(("", "## Provenance", ""))
    lines.append(
        f"Decision policy: `{report.dataset.decision_policy.identifier}`. "
        f"Taxon ID: "
        f"`{report.dataset.taxon_id if report.dataset.taxon_id is not None else 'unknown'}`. "
        f"KEGG organism code: "
        f"`{report.dataset.kegg_organism_code or 'unknown'}`."
    )
    selected_sources = report.dataset.sources[: limits.max_markdown_sources]
    for source in selected_sources:
        source_version = source.source_version or "unknown"
        model = source.model_name or "unknown"
        model_version = source.model_version or "unknown"
        input_location = source.input_path or source.input_uri or "inline"
        rendered_input = (
            _markdown_path(input_location)
            if source.input_path is not None
            else _markdown_cell(input_location)
        )
        lines.append(
            f"- Source: {_markdown_cell(source.source_name)}; software: "
            f"{_markdown_cell(source_version)}; model: {_markdown_cell(model)}; "
            f"model/database version: {_markdown_cell(model_version)}; input: "
            f"{rendered_input}; importer: "
            f"{_markdown_cell(source.importer_name)} {_markdown_cell(source.importer_version)}."
        )
    if len(selected_sources) < len(report.dataset.sources):
        truncated = True
        lines.append(
            f"\nSource preview shows {len(selected_sources)} of "
            f"{len(report.dataset.sources)} entries."
        )
    if report.execution is None:
        lines.append(
            "\nAvailable serialized source, KEGG retrieval, algorithm, parameter, denominator, "
            "and retrieval provenance is retained in the structured JSON artifact."
        )
    else:
        lines.append(
            "\nComplete one-call execution parameters plus serialized source, KEGG retrieval, "
            "algorithm, denominator, and retrieval provenance are retained in the "
            "structured JSON artifact."
        )
    return truncated


def _bound_markdown(lines: Iterable[str], maximum_bytes: int) -> tuple[str, bool]:
    materialized = tuple(lines)
    full = "\n".join(materialized).rstrip() + "\n"
    if len(full.encode("utf-8")) <= maximum_bytes:
        return full, False

    notice = f"\n{_MARKDOWN_TRUNCATION_NOTICE}\n"
    notice_size = len(notice.encode("utf-8"))
    budget = maximum_bytes - notice_size
    selected: list[str] = []
    byte_count = 0
    for line in materialized:
        candidate = f"{line}\n"
        candidate_size = len(candidate.encode("utf-8"))
        if byte_count + candidate_size > budget:
            break
        selected.append(line)
        byte_count += candidate_size
    content = "\n".join(selected).rstrip() + notice
    return content, True


def _render_markdown(report: ReportInput, limits: ReportLimits) -> tuple[str, bool]:
    counts = {item.status.value: item.count for item in report.dataset.status_counts}
    normalized_count_label = "Normalized assignment counts"
    intake_summary = (
        f"Assignments classified: {report.dataset.assignment_count}. "
        "Retained accepted unique K numbers: "
        f"{len(report.dataset.accepted_ko_ids)}. Record-level evidence, "
        "protein-to-KO mappings, and duplicate/conflict accounting were not retained."
    )
    lines = [
        "# KEGG-aware KO annotation report",
        "",
        "This report describes supplied annotation evidence conservatively. It does not infer "
        "experimental validation or biological absence.",
        "",
        "## Interpretation boundaries",
        "",
        "KEGG MODULE exact completion is a Boolean logical result and is separate from the "
        "project's descriptive required-block coverage metric.",
        "",
        "Pathway KO coverage is descriptive only. It does not establish pathway presence, "
        "completeness, expression, activity, flux, phenotype, or statistical significance.",
        "",
        "Comparisons are deterministic set or shared-reference outcomes, not statistical tests.",
        "",
        "## Input and normalization",
        "",
        f"Dataset: `{report.dataset.dataset_id}`. Analysis unit: "
        f"`{report.dataset.analysis_unit.value}`. Input rows: "
        f"{report.dataset.input_rows}. {intake_summary}",
        "",
        f"{normalized_count_label}: "
        + ", ".join(f"{name}={count}" for name, count in counts.items())
        + ".",
        "",
    ]
    _append_module_selection(lines, report)
    preview_truncated = _append_primary_modules(lines, report, limits)
    preview_truncated |= _append_pathway_ranking(lines, report, limits)
    preview_truncated |= _append_primary_pathways(lines, report, limits)
    preview_truncated |= _append_comparisons(lines, report, limits)
    preview_truncated |= _append_warnings_and_provenance(lines, report, limits)
    if preview_truncated:
        lines.extend(("", _MARKDOWN_TRUNCATION_NOTICE))
    content, byte_truncated = _bound_markdown(lines, limits.max_markdown_bytes)
    return content, preview_truncated or byte_truncated


def _artifact(
    section: ReportSection,
    mime_type: Literal["application/json", "text/markdown", "text/csv"],
    content: str,
    *,
    truncated: bool,
) -> ReportArtifact:
    encoded = content.encode("utf-8")
    return ReportArtifact(
        section=section,
        mime_type=mime_type,
        utf8_byte_size=len(encoded),
        content=content,
        truncated=truncated,
    )


def render_report(
    report: ReportInput,
    *,
    limits: ReportLimits | None = None,
) -> RenderedReport:
    """Render three deterministic in-memory artifacts after enforcing all hard limits."""
    effective_limits = limits or ReportLimits()
    _preflight(report, effective_limits)
    structured_content = _render_canonical_json(report, effective_limits)
    markdown_content, markdown_truncated = _render_markdown(report, effective_limits)
    accepted_ko_content = _render_accepted_ko_csv(report, effective_limits)
    artifacts = (
        _artifact(
            ReportSection.STRUCTURED,
            "application/json",
            structured_content,
            truncated=False,
        ),
        _artifact(
            ReportSection.SUMMARY,
            "text/markdown",
            markdown_content,
            truncated=markdown_truncated,
        ),
        _artifact(
            ReportSection.ACCEPTED_KOS,
            "text/csv",
            accepted_ko_content,
            truncated=False,
        ),
    )
    return RenderedReport(
        renderer_name=REPORT_RENDERER_NAME,
        renderer_version=REPORT_RENDERER_VERSION,
        limits=effective_limits,
        artifacts=artifacts,
    )


__all__ = ["render_report"]
