"""Tests for deterministic bounded in-memory reporting."""

import csv
import inspect
import io
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis.comparison import compare_ko_datasets, summarize_ko_comparison
from kegg_mcp.analysis.comparison_contracts import ComparisonDatasetInput
from kegg_mcp.analysis.contracts import ModuleDefinition, ModuleDefinitionCollection
from kegg_mcp.analysis.functional_comparison import (
    compare_module_graphs,
    compare_pathway_references,
)
from kegg_mcp.analysis.module_evaluation import evaluate_module
from kegg_mcp.analysis.module_resolution import resolve_module_definitions
from kegg_mcp.analysis.pathway_coverage import (
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    evaluate_pathway_coverage,
)
from kegg_mcp.analysis.pathway_ranking import PathwayRankingRow, PathwaySelection
from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS,
    AnalysisUnit,
    AnnotationDataset,
    KoAnalysisView,
    build_ko_analysis_view,
)
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    SourceProvenanceInput,
    TableDialect,
    import_generic_table,
)
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.reporting.contracts import (
    RenderedReport,
    ReportInput,
    ReportLimits,
    ReportSection,
    StructuredReport,
)
from kegg_mcp.reporting.render import render_report

_NOW = datetime(2026, 7, 14, 6, 0, tzinfo=UTC)
_IMPORT_LIMITS = ImportLimits(
    max_bytes=200_000,
    max_rows=1_000,
    max_columns=20,
    max_field_length=10_000,
)


def _dataset(
    rows: tuple[tuple[str, str, str], ...] = (
        ("accepted-one", "K00001", "accepted"),
        ("unclassified-two", "K00002", "unclassified"),
        ("rejected-three", "K00003", "rejected"),
    ),
    *,
    analysis_unit: AnalysisUnit = AnalysisUnit.METAGENOMIC_COMMUNITY,
    source_name: str = "synthetic_annotations",
    source_version: str = "1.0",
    model_name: str = "model-alpha",
):
    payload = "sequence,ko,status\n" + "".join(
        f"{sequence},{ko_id},{status}\n" for sequence, ko_id, status in rows
    )
    return import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=CANONICAL_SOURCE_STATUS,
        limits=_IMPORT_LIMITS,
        analysis_unit=analysis_unit,
        source=SourceProvenanceInput(
            source_name=source_name,
            source_version=source_version,
            model_name=model_name,
            model_version="2026.07",
            input_uri="inline://report-test/input",
        ),
    )


def _graph(module_id: str, definition: str, *, name: str | None = None):
    return resolve_module_definitions(
        ModuleDefinitionCollection(
            root_module_id=module_id,
            definitions=(
                ModuleDefinition.from_text(
                    module_id=module_id,
                    module_name=name or f"Synthetic {module_id}",
                    definition=definition,
                ),
            ),
        )
    )


def _view(dataset: AnnotationDataset | None = None) -> KoAnalysisView:
    return build_ko_analysis_view(
        dataset if dataset is not None else _dataset(),
        input_bytes=200,
    )


def _provenance(operation: KeggOperation, *, stale: bool = False) -> KeggBatchProvenance:
    expires_at = _NOW + timedelta(days=1)
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.CACHE,
        cache_lookup_state=(CacheLookupState.STALE_HIT if stale else CacheLookupState.FRESH_HIT),
        retrieved_at=_NOW,
        served_at=expires_at + timedelta(hours=1) if stale else _NOW + timedelta(hours=1),
        expires_at=expires_at,
        response_bytes=123,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release test-2026-07-14",
        attempt_count=0,
        is_stale=stale,
    )


def _reference(
    pathway_id: str = "ko00010",
    *,
    ko_ids: tuple[str, ...] = ("K00001", "K00002"),
    stale: bool = False,
    pathway_name: str | None = None,
) -> PathwayKoReference:
    return PathwayKoReference(
        reference_namespace=PathwayReferenceNamespace.KO,
        reference_scope=PathwayReferenceScope.STANDARD,
        pathway_id=pathway_id,
        pathway_name=pathway_name or f"Synthetic pathway β {pathway_id}",
        pathway_class=("Metabolism; Carbohydrate metabolism",),
        reference_kos=ko_ids,
        relationship_row_count=len(ko_ids),
        link_provenance=(_provenance(KeggOperation.LINK, stale=stale),),
        metadata_provenance=(_provenance(KeggOperation.GET),),
    )


def _complete_report_input() -> ReportInput:
    dataset = _dataset()
    view = _view(dataset)
    module_graph = _graph("M00001", "K00001 K00002", name="Unicode module β")
    unresolved_graph = _graph("M00002", "M99999")
    module_results = (
        evaluate_module(module_graph, view),
        evaluate_module(unresolved_graph, view),
    )
    reference = _reference(stale=True)
    pathway_results = (
        evaluate_pathway_coverage(reference, view),
        evaluate_pathway_coverage(_reference("ko00020", ko_ids=()), view),
    )
    second = _dataset(
        (("second-one", "K00001", "accepted"),),
        source_name="second_annotations",
    )
    inputs = (
        ComparisonDatasetInput(label="primary", dataset=dataset),
        ComparisonDatasetInput(label="second", dataset=second),
    )
    return ReportInput(
        dataset=view,
        module_evaluations=module_results,
        pathway_coverages=pathway_results,
        ko_comparison=summarize_ko_comparison(compare_ko_datasets(inputs)),
        module_comparison=compare_module_graphs(inputs, (module_graph,)),
        pathway_comparison=compare_pathway_references(inputs, (reference,)),
    )


def _artifact(rendered: RenderedReport, section: ReportSection):
    return next(item for item in rendered.artifacts if item.section is section)


def _pathway_ranking_row(pathway_id: str, rank: int) -> PathwayRankingRow:
    return PathwayRankingRow(
        pathway_id=pathway_id,
        pathway_number=pathway_id.removeprefix("ko"),
        detected_unique_ko_count=1,
        detected_ko_ids=("K00001",),
        relationship_row_count=1,
        rank=rank,
    )


def _schema_property_names(node: object) -> set[str]:
    if isinstance(node, dict):
        mapping = cast(dict[str, object], node)
        properties = mapping.get("properties")
        names = set(cast(dict[str, object], properties)) if isinstance(properties, dict) else set()
        for value in mapping.values():
            names.update(_schema_property_names(value))
        return names
    if isinstance(node, list):
        names: set[str] = set()
        for value in cast(list[object], node):
            names.update(_schema_property_names(value))
        return names
    return set()


def test_canonical_artifacts_round_trip_with_exact_sizes_and_provenance() -> None:
    report = _complete_report_input()

    first = render_report(report)
    second = render_report(report)

    assert first == second
    assert tuple(item.section for item in first.artifacts) == tuple(ReportSection)
    assert tuple(item.mime_type for item in first.artifacts) == (
        "application/json",
        "text/markdown",
        "text/csv",
    )
    for artifact in first.artifacts:
        encoded = artifact.content.encode("utf-8")
        assert artifact.utf8_byte_size == len(encoded)
    structured_artifact = _artifact(first, ReportSection.STRUCTURED)
    structured = StructuredReport.model_validate_json(structured_artifact.content)
    assert structured.report == report
    assert structured.limits == first.limits
    assert structured.report.pathway_coverages[0].reference_link_provenance[0].is_stale
    assert structured.report.module_evaluations[0].module_id == "M00001"
    assert RenderedReport.model_validate_json(first.model_dump_json()) == first


@pytest.mark.parametrize(
    "missing_field",
    ("format_name", "format_version", "renderer_name", "renderer_version"),
)
def test_structured_report_requires_current_format_and_renderer_identity(
    missing_field: str,
) -> None:
    rendered = render_report(_complete_report_input())
    payload = json.loads(_artifact(rendered, ReportSection.STRUCTURED).content)
    del payload[missing_field]

    with pytest.raises(ValidationError):
        StructuredReport.model_validate(payload)


@pytest.mark.parametrize("missing_field", ("renderer_name", "renderer_version"))
def test_rendered_report_requires_current_renderer_identity(missing_field: str) -> None:
    payload = render_report(_complete_report_input()).model_dump(mode="json")
    del payload[missing_field]

    with pytest.raises(ValidationError):
        RenderedReport.model_validate(payload)


def test_pathway_ranking_falls_back_to_requested_top_n_without_execution_provenance() -> None:
    report = ReportInput(
        dataset=_view(),
        pathway_selection=PathwaySelection(top_n=1),
        pathway_ranking=(
            _pathway_ranking_row("ko00010", 1),
            _pathway_ranking_row("ko00020", 2),
        ),
    )

    summary = _artifact(render_report(report), ReportSection.SUMMARY).content

    assert "| 1 | `ko00010` | 1 | 1 | yes |" in summary
    assert "| 2 | `ko00020` | 1 | 1 | no |" in summary


def test_markdown_distinguishes_metrics_and_claim_boundaries() -> None:
    summary = _artifact(render_report(_complete_report_input()), ReportSection.SUMMARY).content
    lower = summary.lower()

    assert "exact completion is boolean" in lower
    assert "project block coverage" in lower
    assert "not an official kegg completeness percentage" in lower
    assert "accepted-ko" in lower
    assert "not_evaluable" in lower
    assert "metagenomic_community" in lower
    assert "pooled encoded potential" in lower
    assert "stale_reference" in lower
    assert "unresolved_reference" in lower
    for denied_claim in (
        "pathway presence",
        "completeness",
        "expression",
        "activity",
        "flux",
        "phenotype",
        "statistical significance",
    ):
        assert denied_claim in lower
    assert "not statistical tests" in lower
    assert "no p-value, fold change, enrichment, or differential-function claim" in lower


def test_markdown_previews_and_utf8_bytes_are_bounded_without_cutting_artifacts() -> None:
    report = _complete_report_input()
    limits = ReportLimits(
        max_markdown_bytes=1_500,
        max_markdown_sources=0,
        max_markdown_module_targets=1,
        max_markdown_pathway_targets=1,
        max_markdown_comparison_targets=0,
        max_markdown_warnings=1,
    )

    rendered = render_report(report, limits=limits)
    summary = _artifact(rendered, ReportSection.SUMMARY)

    assert summary.truncated is True
    assert summary.utf8_byte_size == len(summary.content.encode("utf-8"))
    assert summary.utf8_byte_size <= limits.max_markdown_bytes
    assert "Markdown summary truncated" in summary.content
    assert _artifact(rendered, ReportSection.STRUCTURED).truncated is False
    assert _artifact(rendered, ReportSection.ACCEPTED_KOS).truncated is False


def test_markdown_encodes_untrusted_html_links_images_and_backticks() -> None:
    source_name = "source`<script>![x](javascript:1)"
    source_version = "v`<img src=x>[click](javascript:1)"
    model_name = "model`<script>"
    module_name = "module`<img src=x>![m](javascript:1)"
    pathway_name = "pathway`<script>![p](javascript:1)"
    dataset = _dataset(
        source_name=source_name,
        source_version=source_version,
        model_name=model_name,
    )
    view = _view(dataset)
    report = ReportInput(
        dataset=view,
        module_evaluations=(evaluate_module(_graph("M00009", "K00001", name=module_name), view),),
        pathway_coverages=(
            evaluate_pathway_coverage(
                _reference("ko00090", pathway_name=pathway_name),
                view,
            ),
        ),
    )

    summary = _artifact(render_report(report), ReportSection.SUMMARY)
    lower = summary.content.lower()

    assert source_name not in summary.content
    assert source_version not in summary.content
    assert module_name not in summary.content
    assert pathway_name not in summary.content
    assert "<script" not in lower
    assert "<img" not in lower
    assert "![" not in summary.content
    assert "](javascript" not in lower
    assert "source&#96;&lt;script&gt;&#33;&#91;x&#93;&#40;javascript:1&#41;" in summary.content
    assert "module&#96;&lt;img src=x&gt;" in summary.content
    assert summary.utf8_byte_size == len(summary.content.encode("utf-8"))
    assert summary.utf8_byte_size <= ReportLimits().max_markdown_bytes


@pytest.mark.parametrize(
    ("limits", "expected_limit_name"),
    (
        (
            ReportLimits(max_input_rows=1),
            "input rows",
        ),
        (
            ReportLimits(max_source_entries=1),
            "source entries",
        ),
        (
            ReportLimits(
                max_module_targets=0,
                max_pathway_targets=1_000,
                max_total_targets=1_000,
            ),
            "MODULE targets",
        ),
        (
            ReportLimits(max_warning_entries=0),
            "warning entries",
        ),
        (
            ReportLimits(max_structured_json_bytes=128),
            "structured JSON bytes",
        ),
    ),
)
def test_hard_limits_fail_with_safe_input_limit_error(
    limits: ReportLimits,
    expected_limit_name: str,
) -> None:
    with pytest.raises(KeggMcpError) as caught:
        render_report(_complete_report_input(), limits=limits)

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    details = {item.name: item.value for item in caught.value.detail.safe_details}
    assert details["limit_name"] == expected_limit_name


def test_accepted_ko_csv_contains_only_sorted_unique_accepted_kos() -> None:
    payload = (
        "sequence,ko,status\n"
        "seq1,K00002,accepted\n"
        "seq2,K00001,accepted\n"
        "seq3,K00002,accepted\n"
        "seq4,K00003,rejected\n"
        "seq5,BAD,accepted\n"
    )
    dataset = import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=CANONICAL_SOURCE_STATUS,
        limits=_IMPORT_LIMITS,
        source=SourceProvenanceInput(source_name="formula_safety"),
    )
    csv_artifact = _artifact(
        render_report(ReportInput(dataset=_view(dataset))),
        ReportSection.ACCEPTED_KOS,
    )
    rows = list(csv.DictReader(io.StringIO(csv_artifact.content, newline="")))

    assert rows == [
        {"ko_id": "K00001", "normalized_status": "accepted"},
        {"ko_id": "K00002", "normalized_status": "accepted"},
    ]
    assert csv_artifact.content.endswith("\n")
    assert csv_artifact.truncated is False


def test_compact_report_retains_only_sorted_unique_accepted_kos() -> None:
    view = _view()
    rendered = render_report(ReportInput(dataset=view))
    rows = list(
        csv.DictReader(
            io.StringIO(_artifact(rendered, ReportSection.ACCEPTED_KOS).content, newline="")
        )
    )
    structured = StructuredReport.model_validate_json(
        _artifact(rendered, ReportSection.STRUCTURED).content
    )
    summary = _artifact(rendered, ReportSection.SUMMARY).content

    assert rows == [{"ko_id": "K00001", "normalized_status": "accepted"}]
    assert structured.report.dataset == view
    assert "Record-level evidence" in summary
    assert "Normalized assignment counts" in summary
    assert "Normalized record counts" not in summary


def test_csv_byte_limit_fails_instead_of_returning_a_lossy_prefix() -> None:
    report = ReportInput(dataset=_view())

    with pytest.raises(KeggMcpError) as caught:
        render_report(report, limits=ReportLimits(max_accepted_ko_csv_bytes=1))

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert {item.name: item.value for item in caught.value.detail.safe_details}["limit_name"] == (
        "accepted-KO CSV bytes"
    )


def test_contracts_reject_output_paths_and_schemas_exclude_claim_fields() -> None:
    dataset = _view()
    with pytest.raises(ValidationError):
        ReportInput.model_validate({"dataset": dataset, "output_path": "/tmp/report.json"})

    assert tuple(inspect.signature(render_report).parameters) == ("report", "limits")
    report_properties = ReportInput.model_json_schema()["properties"]
    assert set(report_properties).isdisjoint(
        {"path", "file_path", "output_path", "destination", "result_id", "resource_uri"}
    )
    property_names = _schema_property_names(StructuredReport.model_json_schema())
    assert property_names.isdisjoint(
        {
            "pathway_present",
            "pathway_complete",
            "pathway_activity",
            "pathway_flux",
            "phenotype_prediction",
            "p_value",
            "fold_change",
            "enrichment_score",
        }
    )


def test_report_input_requires_primary_dataset_identity_and_unique_targets() -> None:
    first = _dataset()
    second = _dataset((("other", "K00001", "accepted"),))
    first_view = _view(first)
    second_view = _view(second)
    reference = _reference()
    second_pathway = evaluate_pathway_coverage(reference, second_view)

    with pytest.raises(ValidationError):
        ReportInput(dataset=first_view, pathway_coverages=(second_pathway,))

    first_pathway = evaluate_pathway_coverage(reference, first_view)
    with pytest.raises(ValidationError):
        ReportInput(dataset=first_view, pathway_coverages=(first_pathway, first_pathway))
