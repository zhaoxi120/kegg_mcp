"""Safe, concise, output-directory bundles for cross-process KO workflows."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import Field

from kegg_mcp.analysis import (
    KoModuleRelationship,
    KoPathwayRelationship,
    ModuleRankingResult,
    ModuleRankingRow,
    PairedModuleEvaluation,
    PathwayCoverageResult,
    PathwayKoReference,
    PathwayRankingResult,
    PathwayRankingRow,
    ResolvedModuleGraph,
)
from kegg_mcp.domain.annotations import AnnotationDataset, FrozenModel
from kegg_mcp.execution import AnalysisExecutionProvenance
from kegg_mcp.services._atomic_bundle import write_text_bundle
from kegg_mcp.services.render_contracts import (
    RENDER_INPUT_MIME_TYPE,
    RENDER_INPUT_SCHEMA_VERSION,
    RenderInputLimits,
    build_render_input,
    serialize_render_input,
)

OUTPUT_BUNDLE_SCHEMA_VERSION = "3"


class ManifestPathMode(StrEnum):
    """How private source paths are represented in a portable bundle manifest."""

    REDACTED = "redacted"
    ABSOLUTE = "absolute"


class OutputBundle(FrozenModel):
    """Absolute paths for the stable files written by one analysis stage."""

    output_directory: str = Field(min_length=1, max_length=4_096)
    normalized_annotations: str = Field(min_length=1, max_length=4_096)
    protein_ko_mapping: str = Field(min_length=1, max_length=4_096)
    module_ranking: str | None = Field(default=None, max_length=4_096)
    ko_module_relationships: str | None = Field(default=None, max_length=4_096)
    pathway_ranking: str | None = Field(default=None, max_length=4_096)
    ko_pathway_relationships: str | None = Field(default=None, max_length=4_096)
    pathway_coverage: str | None = Field(default=None, max_length=4_096)
    module_completion: str | None = Field(default=None, max_length=4_096)
    analysis_report: str | None = Field(default=None, max_length=4_096)
    render_input: str | None = Field(default=None, max_length=4_096)
    manifest: str = Field(min_length=1, max_length=4_096)
    artifacts: Annotated[
        tuple[OutputBundleArtifact, ...],
        Field(min_length=3, max_length=11),
    ]


class OutputBundleArtifact(FrozenModel):
    """MIME type, exact byte size, and controlled path for one bundle file."""

    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    mime_type: str = Field(min_length=1, max_length=100)
    byte_size: int = Field(strict=True, ge=0)
    path: str = Field(min_length=1, max_length=4_096)


def write_normalization_bundle(
    dataset: AnnotationDataset,
    output_directory: Path,
    *,
    manifest_path_mode: ManifestPathMode = ManifestPathMode.REDACTED,
    remove_created_directory_on_failure: bool = False,
) -> OutputBundle:
    """Write the reusable normalization stage without cache or result-store files."""
    normalized = _normalized_annotations_tsv(dataset)
    mapping = _protein_ko_mapping_tsv(dataset)
    files = {
        "normalized_annotations.tsv": normalized,
        "protein_ko_mapping.tsv": mapping,
    }
    manifest = _manifest(
        dataset,
        (*files, "bundle_manifest.json"),
        stage="normalization",
        manifest_path_mode=manifest_path_mode,
    )
    files["bundle_manifest.json"] = manifest
    _write_files(
        output_directory,
        files,
        remove_created_directory_on_failure=remove_created_directory_on_failure,
    )
    return _bundle_paths(output_directory, files=files)


def write_analysis_bundle(
    dataset: AnnotationDataset,
    module_graphs: tuple[ResolvedModuleGraph, ...],
    modules: tuple[PairedModuleEvaluation, ...],
    pathway_references: tuple[PathwayKoReference, ...],
    pathways: tuple[PathwayCoverageResult, ...],
    *,
    execution: AnalysisExecutionProvenance,
    analysis_report: str,
    output_directory: Path,
    render_limits: RenderInputLimits | None = None,
    module_ranking: ModuleRankingResult | None = None,
    pathway_ranking: PathwayRankingResult | None = None,
    manifest_path_mode: ManifestPathMode = ManifestPathMode.REDACTED,
    remove_created_directory_on_failure: bool = False,
) -> OutputBundle:
    """Write canonical handoff tables, report, and renderer input as one stable bundle."""
    render_input = build_render_input(
        dataset,
        module_graphs,
        modules,
        pathway_references,
        pathways,
        execution,
        limits=render_limits,
    )
    files = {
        "normalized_annotations.tsv": _normalized_annotations_tsv(dataset),
        "protein_ko_mapping.tsv": _protein_ko_mapping_tsv(dataset),
        "pathway_coverage.tsv": _pathway_coverage_tsv(pathways),
        "module_completion.tsv": _module_completion_tsv(modules),
        "analysis_report.md": analysis_report,
        "render_input.json": serialize_render_input(render_input),
    }
    if pathway_ranking is not None:
        files["pathway_ranking.tsv"] = _pathway_ranking_tsv(pathway_ranking.rows)
        files["ko_pathway_relationships.tsv"] = _ko_pathway_relationships_tsv(
            pathway_ranking.relationships
        )
    if module_ranking is not None:
        files["module_ranking.tsv"] = _module_ranking_tsv(module_ranking.rows)
        files["ko_module_relationships.tsv"] = _ko_module_relationships_tsv(
            module_ranking.relationships
        )
    files["bundle_manifest.json"] = _manifest(
        dataset,
        (*files, "bundle_manifest.json"),
        stage="analysis",
        manifest_path_mode=manifest_path_mode,
        render_input_schema=(RENDER_INPUT_SCHEMA_VERSION, RENDER_INPUT_MIME_TYPE),
        analysis_execution=execution,
    )
    _write_files(
        output_directory,
        files,
        remove_created_directory_on_failure=remove_created_directory_on_failure,
    )
    return _bundle_paths(
        output_directory,
        files=files,
        include_analysis=True,
        include_module_ranking=module_ranking is not None,
        include_pathway_ranking=pathway_ranking is not None,
    )


def _normalized_annotations_tsv(dataset: AnnotationDataset) -> str:
    rows = [
        (
            record.record_id,
            record.sample_id,
            record.sequence_id or "",
            record.protein_name or "",
            record.ko_id or "",
            record.normalized_status.value,
            record.status_reason,
            "" if record.score is None else str(record.score),
            "" if record.threshold is None else str(record.threshold),
            "" if record.rank is None else str(record.rank),
            "" if record.domain_start is None else str(record.domain_start),
            "" if record.domain_end is None else str(record.domain_end),
        )
        for record in dataset.records
    ]
    return _tsv(
        (
            "record_id",
            "sample_id",
            "sequence_id",
            "protein_name",
            "ko_id",
            "normalized_status",
            "status_reason",
            "score",
            "threshold",
            "rank",
            "domain_start",
            "domain_end",
        ),
        rows,
    )


def _protein_ko_mapping_tsv(dataset: AnnotationDataset) -> str:
    rows = [
        (
            record.sample_id,
            record.sequence_id or "",
            record.protein_name or "",
            record.ko_id or "",
            record.normalized_status.value,
            record.record_id,
        )
        for record in dataset.records
        if record.ko_id is not None
    ]
    return _tsv(
        ("sample_id", "sequence_id", "protein_name", "ko_id", "evidence_status", "record_id"),
        rows,
    )


def _module_completion_tsv(modules: tuple[PairedModuleEvaluation, ...]) -> str:
    rows: list[tuple[object, ...]] = []
    for pair in modules:
        selected = (
            (pair.strict,) if not pair.strict_to_lenient_changed else (pair.strict, pair.lenient)
        )
        for result in selected:
            rows.append(
                (
                    result.module_id,
                    result.module_name or "",
                    result.evidence_mode.value,
                    result.evaluation_status.value,
                    "" if result.is_complete is None else str(result.is_complete).lower(),
                    "" if result.block_coverage is None else result.block_coverage,
                    result.completed_required_blocks,
                    result.evaluable_required_blocks,
                    result.required_block_count,
                )
            )
    return _tsv(
        (
            "module_id",
            "module_name",
            "evidence_mode",
            "evaluation_status",
            "exact_completion",
            "block_coverage",
            "completed_required_blocks",
            "evaluable_required_blocks",
            "required_block_count",
        ),
        rows,
    )


def _pathway_coverage_tsv(pathways: tuple[PathwayCoverageResult, ...]) -> str:
    rows = [
        (
            result.pathway_id,
            result.pathway_name,
            result.pathway_id[-5:],
            result.reference_namespace.value,
            result.evidence_mode.value,
            result.evaluation_status.value,
            result.detected_unique_ko_count,
            result.reference_unique_ko_count,
            "" if result.coverage_ratio is None else result.coverage_ratio,
        )
        for result in pathways
    ]
    return _tsv(
        (
            "pathway_id",
            "pathway_name",
            "pathway_number",
            "reference_namespace",
            "evidence_mode",
            "evaluation_status",
            "detected_unique_ko_count",
            "reference_unique_ko_count",
            "coverage_ratio",
        ),
        rows,
    )


def _pathway_ranking_tsv(rows: tuple[PathwayRankingRow, ...]) -> str:
    return _tsv(
        (
            "rank",
            "pathway_id",
            "pathway_number",
            "detected_unique_ko_count",
            "detected_ko_ids",
            "relationship_row_count",
        ),
        (
            (
                row.rank,
                row.pathway_id,
                row.pathway_number,
                row.detected_unique_ko_count,
                ";".join(row.detected_ko_ids),
                row.relationship_row_count,
            )
            for row in rows
        ),
    )


def _ko_pathway_relationships_tsv(rows: tuple[KoPathwayRelationship, ...]) -> str:
    return _tsv(
        (
            "source_ko_id",
            "target_id",
            "pathway_number",
            "canonical_pathway_id",
            "target_namespace",
            "batch_index",
            "line_number",
        ),
        (
            (
                row.source_ko_id,
                row.target_id,
                row.pathway_number,
                row.canonical_pathway_id,
                row.target_namespace,
                row.batch_index,
                row.line_number,
            )
            for row in rows
        ),
    )


def _module_ranking_tsv(rows: tuple[ModuleRankingRow, ...]) -> str:
    return _tsv(
        (
            "rank",
            "module_id",
            "detected_unique_ko_count",
            "detected_ko_ids",
            "relationship_row_count",
        ),
        (
            (
                row.rank,
                row.module_id,
                row.detected_unique_ko_count,
                ";".join(row.detected_ko_ids),
                row.relationship_row_count,
            )
            for row in rows
        ),
    )


def _ko_module_relationships_tsv(rows: tuple[KoModuleRelationship, ...]) -> str:
    return _tsv(
        (
            "source_ko_id",
            "module_id",
            "target_namespace",
            "batch_index",
            "line_number",
        ),
        (
            (
                row.source_ko_id,
                row.module_id,
                row.target_namespace,
                row.batch_index,
                row.line_number,
            )
            for row in rows
        ),
    )


def _manifest(
    dataset: AnnotationDataset,
    files: tuple[str, ...],
    *,
    stage: str,
    manifest_path_mode: ManifestPathMode,
    render_input_schema: tuple[str, str] | None = None,
    analysis_execution: AnalysisExecutionProvenance | None = None,
) -> str:
    input_paths = tuple(
        sorted({source.input_path for source in dataset.sources if source.input_path is not None})
    )
    manifest_paths = (
        input_paths
        if manifest_path_mode is ManifestPathMode.ABSOLUTE
        else tuple(f"input-{index}" for index in range(1, len(input_paths) + 1))
    )
    value: dict[str, object] = {
        "schema_version": OUTPUT_BUNDLE_SCHEMA_VERSION,
        "stage": stage,
        "input_path_provenance": {
            "mode": manifest_path_mode.value,
            "source_count": len(input_paths),
            "values": manifest_paths,
        },
        "analysis_unit": dataset.analysis_unit.value,
        "files": list(files),
    }
    if render_input_schema is not None:
        schema_version, mime_type = render_input_schema
        value["render_input"] = {
            "schema_version": schema_version,
            "mime_type": mime_type,
        }
    if analysis_execution is not None and analysis_execution.module_ranking is not None:
        value["module_selection"] = analysis_execution.module_ranking.model_dump(mode="json")
    pathway_execution = (
        None if analysis_execution is None else analysis_execution.pathway_parameters
    )
    if pathway_execution is not None and pathway_execution.ranking is not None:
        value["pathway_selection"] = pathway_execution.ranking.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _tsv(header: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> str:
    target = io.StringIO(newline="")
    writer = csv.writer(target, delimiter="\t", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return target.getvalue()


def _write_files(
    output_directory: Path,
    files: dict[str, str],
    *,
    remove_created_directory_on_failure: bool = False,
) -> None:
    write_text_bundle(
        output_directory,
        files,
        remove_created_directory_on_failure=remove_created_directory_on_failure,
    )


def _bundle_paths(
    output_directory: Path,
    *,
    files: dict[str, str],
    include_analysis: bool = False,
    include_module_ranking: bool = False,
    include_pathway_ranking: bool = False,
) -> OutputBundle:
    directory = str(output_directory)
    return OutputBundle(
        output_directory=directory,
        normalized_annotations=str(output_directory / "normalized_annotations.tsv"),
        protein_ko_mapping=str(output_directory / "protein_ko_mapping.tsv"),
        module_ranking=(
            str(output_directory / "module_ranking.tsv") if include_module_ranking else None
        ),
        ko_module_relationships=(
            str(output_directory / "ko_module_relationships.tsv")
            if include_module_ranking
            else None
        ),
        pathway_ranking=(
            str(output_directory / "pathway_ranking.tsv") if include_pathway_ranking else None
        ),
        ko_pathway_relationships=(
            str(output_directory / "ko_pathway_relationships.tsv")
            if include_pathway_ranking
            else None
        ),
        pathway_coverage=(
            str(output_directory / "pathway_coverage.tsv") if include_analysis else None
        ),
        module_completion=(
            str(output_directory / "module_completion.tsv") if include_analysis else None
        ),
        analysis_report=(
            str(output_directory / "analysis_report.md") if include_analysis else None
        ),
        render_input=(str(output_directory / "render_input.json") if include_analysis else None),
        manifest=str(output_directory / "bundle_manifest.json"),
        artifacts=tuple(
            OutputBundleArtifact(
                name=name,
                mime_type=_bundle_mime_type(name),
                byte_size=len(content.encode("utf-8")),
                path=str(output_directory / name),
            )
            for name, content in files.items()
        ),
    )


def _bundle_mime_type(name: str) -> str:
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".md"):
        return "text/markdown"
    if name.endswith(".tsv"):
        return "text/tab-separated-values"
    raise AssertionError("output bundle contains an unsupported file extension")


__all__ = (
    "ManifestPathMode",
    "OutputBundle",
    "OutputBundleArtifact",
    "write_analysis_bundle",
    "write_normalization_bundle",
)
