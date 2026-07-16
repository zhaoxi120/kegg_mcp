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

from kegg_mcp.analysis import (
    PairedModuleEvaluation,
    PathwayCoverageResult,
    PathwayKoReference,
    ResolvedModuleGraph,
)
from kegg_mcp.domain.annotations import AnnotationDataset, FrozenModel
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.execution import AnalysisExecutionProvenance
from kegg_mcp.services.render_contracts import (
    RENDER_INPUT_MIME_TYPE,
    RENDER_INPUT_SCHEMA_VERSION,
    RenderInputLimits,
    build_render_input,
    serialize_render_input,
)

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
    module_graphs: tuple[ResolvedModuleGraph, ...],
    modules: tuple[PairedModuleEvaluation, ...],
    pathway_references: tuple[PathwayKoReference, ...],
    pathways: tuple[PathwayCoverageResult, ...],
    *,
    execution: AnalysisExecutionProvenance,
    analysis_report: str,
    output_directory: Path,
    render_limits: RenderInputLimits | None = None,
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
    files["bundle_manifest.json"] = _manifest(
        dataset,
        (*files, "bundle_manifest.json"),
        stage="analysis",
        render_input_schema=(RENDER_INPUT_SCHEMA_VERSION, RENDER_INPUT_MIME_TYPE),
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


def _manifest(
    dataset: AnnotationDataset,
    files: tuple[str, ...],
    *,
    stage: str,
    render_input_schema: tuple[str, str] | None = None,
) -> str:
    value: dict[str, object] = {
        "schema_version": OUTPUT_BUNDLE_SCHEMA_VERSION,
        "stage": stage,
        "input_paths": sorted(
            {source.input_path for source in dataset.sources if source.input_path is not None}
        ),
        "analysis_unit": dataset.analysis_unit.value,
        "files": list(files),
    }
    if render_input_schema is not None:
        schema_version, mime_type = render_input_schema
        value["render_input"] = {
            "schema_version": schema_version,
            "mime_type": mime_type,
        }
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _tsv(header: tuple[str, ...], rows: Iterable[tuple[object, ...]]) -> str:
    target = io.StringIO(newline="")
    writer = csv.writer(target, delimiter="\t", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return target.getvalue()


def _write_files(output_directory: Path, files: dict[str, str]) -> None:
    manifest_name = "bundle_manifest.json"
    if manifest_name not in files:
        raise AssertionError("output bundles require a commit manifest")
    temporary_names: list[str] = []
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory_fd(output_directory)
        for name, content in files.items():
            temporary = f".{name}.{secrets.token_hex(8)}.tmp"
            temporary_names.append(temporary)
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
                    os.close(file_fd)
                raise
        os.fsync(directory_fd)

        # The manifest is the bundle commit marker. Remove any previous marker,
        # install every payload, and publish the new manifest only after success.
        with suppress(FileNotFoundError):
            os.unlink(manifest_name, dir_fd=directory_fd)
        for name in files:
            if name == manifest_name:
                continue
            temporary = next(item for item in temporary_names if item.startswith(f".{name}."))
            os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            temporary_names.remove(temporary)
        manifest_temporary = next(
            item for item in temporary_names if item.startswith(f".{manifest_name}.")
        )
        os.replace(
            manifest_temporary,
            manifest_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_names.remove(manifest_temporary)
        os.fsync(directory_fd)
    except OSError:
        fail(
            ErrorCode.OUTPUT_WRITE_FAILED,
            "The requested output bundle could not be written safely.",
            suggested_action="Check the output directory permissions and available storage.",
        )
    finally:
        if directory_fd is not None:
            for temporary in temporary_names:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=directory_fd)
            os.close(directory_fd)


def _open_directory_fd(path: Path) -> int:
    """Create and open an absolute directory without following any path component."""
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("output directory must be an absolute normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(os.sep, flags)
    try:
        for component in path.parts[1:]:
            if component in {"", ".", ".."}:
                raise OSError("output directory contains an invalid component")
            with suppress(FileExistsError):
                os.mkdir(component, mode=0o700, dir_fd=current_fd)
            next_fd = os.open(component, flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


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
