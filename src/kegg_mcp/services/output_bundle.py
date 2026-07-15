"""Safe, concise, output-directory bundles for cross-process KO workflows."""

from __future__ import annotations

import csv
import io
import json
import os
import secrets
from collections.abc import Iterable
from contextlib import suppress
from pathlib import Path

from pydantic import Field

from kegg_mcp.analysis import PairedModuleEvaluation, PathwayCoverageResult
from kegg_mcp.domain.annotations import AnnotationDataset, FrozenModel, NormalizedStatus
from kegg_mcp.domain.errors import ErrorCode, fail

OUTPUT_BUNDLE_SCHEMA_VERSION = "1"


class OutputBundle(FrozenModel):
    """Absolute paths for the stable files written by one analysis stage."""

    output_directory: str = Field(min_length=1, max_length=4_096)
    normalized_annotations: str = Field(min_length=1, max_length=4_096)
    protein_ko_mapping: str = Field(min_length=1, max_length=4_096)
    pathway_coverage: str | None = Field(default=None, max_length=4_096)
    module_completion: str | None = Field(default=None, max_length=4_096)
    analysis_report: str | None = Field(default=None, max_length=4_096)
    render_input: str | None = Field(default=None, max_length=4_096)
    manifest: str = Field(min_length=1, max_length=4_096)


def write_normalization_bundle(dataset: AnnotationDataset, output_directory: Path) -> OutputBundle:
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
    )
    files["bundle_manifest.json"] = manifest
    _write_files(output_directory, files)
    return _bundle_paths(output_directory)


def write_analysis_bundle(
    dataset: AnnotationDataset,
    modules: tuple[PairedModuleEvaluation, ...],
    pathways: tuple[PathwayCoverageResult, ...],
    *,
    analysis_report: str,
    output_directory: Path,
) -> OutputBundle:
    """Write canonical handoff tables, report, and renderer input as one stable bundle."""
    files = {
        "normalized_annotations.tsv": _normalized_annotations_tsv(dataset),
        "protein_ko_mapping.tsv": _protein_ko_mapping_tsv(dataset),
        "pathway_coverage.tsv": _pathway_coverage_tsv(pathways),
        "module_completion.tsv": _module_completion_tsv(modules),
        "analysis_report.md": analysis_report,
        "render_input.json": _render_input_json(dataset, modules, pathways),
    }
    files["bundle_manifest.json"] = _manifest(
        dataset,
        (*files, "bundle_manifest.json"),
        stage="analysis",
    )
    _write_files(output_directory, files)
    return _bundle_paths(output_directory, include_analysis=True)


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


def _render_input_json(
    dataset: AnnotationDataset,
    modules: tuple[PairedModuleEvaluation, ...],
    pathways: tuple[PathwayCoverageResult, ...],
) -> str:
    accepted_kos = sorted(
        {
            record.ko_id
            for record in dataset.records
            if record.ko_id is not None and record.normalized_status is NormalizedStatus.ACCEPTED
        }
    )
    value = {
        "schema_version": OUTPUT_BUNDLE_SCHEMA_VERSION,
        "analysis_unit": dataset.analysis_unit.value,
        "input_paths": sorted(
            {source.input_path for source in dataset.sources if source.input_path is not None}
        ),
        "accepted_ko_ids": accepted_kos,
        "pathways": [
            {
                "pathway_id": item.pathway_id,
                "pathway_number": item.pathway_id[-5:],
                "reference_namespace": item.reference_namespace.value,
                "detected_ko_ids": list(item.detected_kos_preview),
                "detected_preview_truncated": item.detected_preview_truncated,
            }
            for item in pathways
        ],
        "modules": [
            {
                "module_id": item.strict.module_id,
                "strict_is_complete": item.strict.is_complete,
                "strict_block_coverage": item.strict.block_coverage,
                "lenient_differs": item.strict_to_lenient_changed,
            }
            for item in modules
        ],
    }
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _manifest(dataset: AnnotationDataset, files: tuple[str, ...], *, stage: str) -> str:
    value = {
        "schema_version": OUTPUT_BUNDLE_SCHEMA_VERSION,
        "stage": stage,
        "input_paths": sorted(
            {source.input_path for source in dataset.sources if source.input_path is not None}
        ),
        "analysis_unit": dataset.analysis_unit.value,
        "files": list(files),
    }
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _tsv(header: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> str:
    target = io.StringIO(newline="")
    writer = csv.writer(target, delimiter="\t", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return target.getvalue()


def _write_files(output_directory: Path, files: dict[str, str]) -> None:
    try:
        output_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory_fd = os.open(output_directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for name, content in files.items():
                temporary = f".{name}.{secrets.token_hex(8)}.tmp"
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                file_fd = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
                try:
                    encoded = content.encode("utf-8")
                    with os.fdopen(file_fd, "wb", closefd=True) as handle:
                        handle.write(encoded)
                        handle.flush()
                        os.fsync(handle.fileno())
                except BaseException:
                    with suppress(OSError):
                        os.unlink(temporary, dir_fd=directory_fd)
                    raise
                os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        fail(
            ErrorCode.OUTPUT_WRITE_FAILED,
            "The requested output bundle could not be written safely.",
            suggested_action="Check the output directory permissions and available storage.",
        )


def _bundle_paths(output_directory: Path, *, include_analysis: bool = False) -> OutputBundle:
    directory = str(output_directory)
    return OutputBundle(
        output_directory=directory,
        normalized_annotations=str(output_directory / "normalized_annotations.tsv"),
        protein_ko_mapping=str(output_directory / "protein_ko_mapping.tsv"),
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
    )


__all__ = (
    "OutputBundle",
    "write_analysis_bundle",
    "write_normalization_bundle",
)
