"""Pure deterministic rendering for bounded JSON, Markdown, and annotation CSV artifacts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterable, Sequence
from enum import Enum
from itertools import chain
from typing import Any, Literal, NoReturn

from kegg_mcp.analysis.contracts import ModuleEvaluationResult
from kegg_mcp.analysis.functional_comparison import (
    ModuleTargetComparison,
    PathwayTargetComparison,
)
from kegg_mcp.analysis.pathway_coverage import PathwayCoverageResult
from kegg_mcp.domain.annotations import AnalysisUnit, AnnotationRecord, NormalizedStatus
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.reporting.contracts import (
    REPORT_RENDERER_NAME,
    REPORT_RENDERER_VERSION,
    RenderedReport,
    ReportArtifact,
    ReportInput,
    ReportLimits,
    ReportSection,
    StructuredReport,
)

_CSV_HEADER = (
    "record_id",
    "sample_id",
    "sequence_id",
    "ko_id",
    "raw_ko",
    "raw_decision",
    "normalized_status",
    "status_reason",
    "decision_policy_name",
    "decision_policy_version",
    "score",
    "score_type",
    "threshold",
    "threshold_rule",
    "rank",
    "domain_start",
    "domain_end",
    "evidence_json",
    "source_json",
)
_CSV_DANGEROUS_PREFIXES = frozenset({"=", "+", "-", "@", "\t", "\r", "\n", "'"})
_MARKDOWN_TRUNCATION_NOTICE = (
    "> Markdown summary truncated at the recorded preview or UTF-8 byte limit. "
    "The structured JSON and annotation CSV artifacts remain complete within their hard limits."
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
    count = len(report.dataset.import_report.diagnostics)
    for pair in report.module_evaluations:
        count += len(pair.strict.warnings) + len(pair.lenient.warnings)
        count += len(pair.strict.unresolved_references) + len(pair.lenient.unresolved_references)
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
            count += sum(len(outcome.warnings) for outcome in target.strict.outcomes)
            count += sum(len(outcome.warnings) for outcome in target.lenient.outcomes)
    return count


def _preflight(report: ReportInput, limits: ReportLimits) -> None:
    input_rows = report.dataset.import_report.input_rows
    if input_rows > limits.max_input_rows:
        _limit_exceeded("input rows", input_rows, limits.max_input_rows)
    record_count = len(report.dataset.records)
    if record_count > limits.max_annotation_records:
        _limit_exceeded(
            "annotation records",
            record_count,
            limits.max_annotation_records,
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
    total_targets = module_targets + pathway_targets
    if total_targets > limits.max_total_targets:
        _limit_exceeded("total analysis targets", total_targets, limits.max_total_targets)

    warning_entries = _warning_entry_count(report)
    if warning_entries > limits.max_warning_entries:
        _limit_exceeded("warning entries", warning_entries, limits.max_warning_entries)


def _render_canonical_json(report: ReportInput, limits: ReportLimits) -> str:
    payload = StructuredReport(limits=limits, report=report).model_dump(mode="json")
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


def _canonical_json_cell(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _flat_csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return json.dumps(value, allow_nan=False, separators=(",", ":"))
    return str(value)


def _escape_spreadsheet_formula(value: str) -> str:
    # Apostrophes are escaped as well, making one-prefix removal reversible for consumers.
    if value and value[0] in _CSV_DANGEROUS_PREFIXES:
        return f"'{value}"
    return value


def _record_csv_row(record: AnnotationRecord) -> tuple[str, ...]:
    values: tuple[object, ...] = (
        record.record_id,
        record.sample_id,
        record.sequence_id,
        record.ko_id,
        record.raw_ko,
        record.raw_decision,
        record.normalized_status,
        record.status_reason,
        record.decision_policy.name,
        record.decision_policy.version,
        record.score,
        record.score_type,
        record.threshold,
        record.threshold_rule,
        record.rank,
        record.domain_start,
        record.domain_end,
        _canonical_json_cell(record.evidence.model_dump(mode="json")),
        _canonical_json_cell(record.source.model_dump(mode="json")),
    )
    return tuple(_escape_spreadsheet_formula(_flat_csv_cell(value)) for value in values)


def _csv_line(values: Sequence[str]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, dialect="excel", lineterminator="\n")
    writer.writerow(values)
    return stream.getvalue()


def _render_annotation_csv(report: ReportInput, limits: ReportLimits) -> str:
    chunks: list[str] = []
    byte_count = 0
    rows = chain((_CSV_HEADER,), (_record_csv_row(record) for record in report.dataset.records))
    for values in rows:
        line = _csv_line(values)
        byte_count += len(line.encode("utf-8"))
        if byte_count > limits.max_annotation_csv_bytes:
            _limit_exceeded(
                "annotation CSV bytes",
                byte_count,
                limits.max_annotation_csv_bytes,
            )
        chunks.append(line)
    return "".join(chunks)


def _markdown_cell(value: object) -> str:
    """Encode untrusted text so Markdown cannot reinterpret it as active syntax."""
    return str(value).translate(_MARKDOWN_TEXT_TRANSLATION)


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
    for diagnostic in report.dataset.import_report.diagnostics:
        items.append((diagnostic.code.value, diagnostic.message))
    for pair in report.module_evaluations:
        for result in (pair.strict, pair.lenient):
            items.extend((warning.code.value, warning.message) for warning in result.warnings)
        for issue in pair.strict.unresolved_references:
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
            for mode in (target.strict, target.lenient):
                for outcome in mode.outcomes:
                    items.extend(
                        (warning.code.value, warning.message) for warning in outcome.warnings
                    )

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
            "| MODULE | Evidence mode | Evaluation status | Exact completion | "
            "Project block coverage |",
            "| --- | --- | --- | --- | --- |",
        )
    )
    selected = report.module_evaluations[: limits.max_markdown_module_targets]
    for pair in selected:
        for result in (pair.strict, pair.lenient):
            label = result.module_id
            if result.module_name:
                label = f"{label} — {result.module_name}"
            lines.append(
                "| "
                + " | ".join(
                    (
                        _markdown_cell(label),
                        result.evidence_mode.value,
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
            "| Pathway | Namespace | Evidence mode | Evaluation status | Detected/reference KOs |",
            "| --- | --- | --- | --- | --- |",
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
                    result.evidence_mode.value,
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


def _module_comparison_outcomes(target: ModuleTargetComparison, strict: bool) -> str:
    mode = target.strict if strict else target.lenient
    return "; ".join(
        (
            f"{_markdown_cell(outcome.label)}={outcome.evaluation_status.value},"
            f" exact:"
            f"{'unknown' if outcome.is_complete is None else str(outcome.is_complete).lower()},"
            f" blocks:{outcome.completed_required_blocks}/{outcome.required_block_count}"
        )
        for outcome in mode.outcomes
    )


def _pathway_comparison_outcomes(target: PathwayTargetComparison, strict: bool) -> str:
    mode = target.strict if strict else target.lenient
    return "; ".join(
        (
            f"{_markdown_cell(outcome.label)}={outcome.evaluation_status.value},"
            f" {outcome.detected_reference_ko_count}/{outcome.reference_unique_ko_count}"
            f" ({_ratio(outcome.coverage_ratio)})"
        )
        for outcome in mode.outcomes
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
                "| KO evidence class | Union count | Shared by all | Partial membership patterns |",
                "| --- | ---: | ---: | ---: |",
            )
        )
        for partition in report.ko_comparison.partitions:
            lines.append(
                f"| {partition.ko_class.value} | {partition.union_count} | "
                f"{partition.shared_by_all.count} | {partition.partially_shared_pattern_count} |"
            )
        lines.append("")

    if report.module_comparison is not None:
        lines.extend(
            (
                "### Shared-definition MODULE outcomes",
                "",
                "| MODULE | Evidence mode | Ordered outcomes |",
                "| --- | --- | --- |",
            )
        )
        selected = report.module_comparison.targets[: limits.max_markdown_comparison_targets]
        for target in selected:
            for strict in (True, False):
                lines.append(
                    f"| {target.module_id} | {'strict' if strict else 'lenient'} | "
                    f"{_module_comparison_outcomes(target, strict)} |"
                )
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
                "| Pathway reference | Evidence mode | Ordered descriptive coverage outcomes |",
                "| --- | --- | --- |",
            )
        )
        selected = report.pathway_comparison.targets[: limits.max_markdown_comparison_targets]
        for target in selected:
            for strict in (True, False):
                lines.append(
                    f"| {target.reference.pathway_id} "
                    f"({target.reference.reference_namespace.value}) "
                    f"| {'strict' if strict else 'lenient'} | "
                    f"{_pathway_comparison_outcomes(target, strict)} |"
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
        f"Decision policy: `{report.dataset.import_report.decision_policy.identifier}`. "
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
        digest = source.input_sha256 or "unknown"
        logical_name = source.input_uri or "unknown"
        lines.append(
            f"- Source: {_markdown_cell(source.source_name)}; software: "
            f"{_markdown_cell(source_version)}; model: {_markdown_cell(model)}; "
            f"model/database version: {_markdown_cell(model_version)}; logical input: "
            f"{_markdown_cell(logical_name)}; SHA-256: `{digest}`; importer: "
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
            "and definition-digest provenance is retained in the structured JSON artifact."
        )
    else:
        lines.append(
            "\nComplete one-call execution parameters plus serialized source, KEGG retrieval, "
            "algorithm, denominator, and definition-digest provenance are retained in the "
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
    if len(content.encode("utf-8")) > maximum_bytes:  # pragma: no cover - defensive
        _limit_exceeded("Markdown bytes", len(content.encode("utf-8")), maximum_bytes)
    return content, True


def _render_markdown(report: ReportInput, limits: ReportLimits) -> tuple[str, bool]:
    counts = {
        status.value: report.dataset.import_report.count_for(status) for status in NormalizedStatus
    }
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
        f"{report.dataset.import_report.input_rows}. Emitted annotation records: "
        f"{len(report.dataset.records)}.",
        "",
        "Normalized record counts: "
        + ", ".join(f"{name}={count}" for name, count in counts.items())
        + ".",
        "",
    ]
    preview_truncated = _append_primary_modules(lines, report, limits)
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
        sha256=hashlib.sha256(encoded).hexdigest(),
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
    annotation_content = _render_annotation_csv(report, effective_limits)
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
            ReportSection.ANNOTATIONS,
            "text/csv",
            annotation_content,
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
