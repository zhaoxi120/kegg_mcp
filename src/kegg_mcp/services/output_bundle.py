"""Safe, concise, output-directory bundles for cross-process KO workflows."""

from __future__ import annotations

import csv
import io
import json
import os
import secrets
import stat
from collections.abc import Iterable
from contextlib import suppress
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
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.execution import AnalysisExecutionProvenance
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
    manifest_name = "bundle_manifest.json"
    if manifest_name not in files:
        raise AssertionError("output bundles require a commit manifest")
    temporary_names: list[str] = []
    installed_names: list[tuple[str, str]] = []
    directory_fd: int | None = None
    directory_created = False
    committed = False
    try:
        directory_fd, directory_created = _open_directory_fd_with_creation(output_directory)
        if os.listdir(directory_fd):
            fail(
                ErrorCode.OUTPUT_ALREADY_EXISTS,
                "The requested output bundle directory is not empty.",
                suggested_action="Choose a new or empty output_directory.",
            )
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

        # Hard-link every temporary into place without replacement, then publish
        # the manifest last as the bundle commit marker.
        for name in files:
            if name == manifest_name:
                continue
            temporary = next(item for item in temporary_names if item.startswith(f".{name}."))
            os.link(
                temporary,
                name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            installed_names.append((name, temporary))
        manifest_temporary = next(
            item for item in temporary_names if item.startswith(f".{manifest_name}.")
        )
        os.link(
            manifest_temporary,
            manifest_name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
        installed_names.append((manifest_name, manifest_temporary))
        os.fsync(directory_fd)
        committed = True
    except FileExistsError:
        fail(
            ErrorCode.OUTPUT_ALREADY_EXISTS,
            "The requested output bundle would replace an existing file.",
            suggested_action="Choose a new or empty output_directory.",
        )
    except OSError:
        fail(
            ErrorCode.OUTPUT_WRITE_FAILED,
            "The requested output bundle could not be written safely.",
            suggested_action="Check the output directory permissions and available storage.",
        )
    finally:
        if directory_fd is not None:
            if not committed:
                for name, temporary in installed_names:
                    with suppress(OSError):
                        temporary_stat = os.stat(
                            temporary,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        installed_stat = os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        if (temporary_stat.st_dev, temporary_stat.st_ino) == (
                            installed_stat.st_dev,
                            installed_stat.st_ino,
                        ):
                            os.unlink(name, dir_fd=directory_fd)
            for temporary in temporary_names:
                with suppress(OSError):
                    os.unlink(temporary, dir_fd=directory_fd)
            with suppress(OSError):
                os.fsync(directory_fd)
            if not committed and directory_created and remove_created_directory_on_failure:
                _remove_created_empty_directory(output_directory, directory_fd)
            os.close(directory_fd)


def _open_directory_fd_with_creation(path: Path) -> tuple[int, bool]:
    """Create and open a directory and report whether this call created the final entry."""
    return _walk_output_directory(path, create_missing=True)


def _open_existing_directory_fd(path: Path) -> int:
    descriptor, created = _walk_output_directory(path, create_missing=False)
    if created:  # pragma: no cover - impossible when creation is disabled
        os.close(descriptor)
        raise AssertionError("existing-directory walk unexpectedly created an entry")
    return descriptor


def _walk_output_directory(path: Path, *, create_missing: bool) -> tuple[int, bool]:
    if not path.is_absolute() or ".." in path.parts:
        raise OSError("output directory must be an absolute normalized path")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    current_fd = os.open(os.sep, flags)
    created_final = False
    private_boundary = _validate_output_directory_fd(
        current_fd,
        private_boundary=False,
    )
    try:
        components = path.parts[1:]
        for index, component in enumerate(components):
            if component in {"", ".", ".."}:
                raise OSError("output directory contains an invalid component")
            created = False
            created_identity: tuple[int, int, int] | None = None
            if create_missing:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                    created = True
                    created_metadata = os.stat(
                        component,
                        dir_fd=current_fd,
                        follow_symlinks=False,
                    )
                    if not stat.S_ISDIR(created_metadata.st_mode):
                        raise OSError("created output entry is not a directory")
                    created_identity = (
                        created_metadata.st_dev,
                        created_metadata.st_ino,
                        created_metadata.st_uid,
                    )
                except FileExistsError:
                    pass
            try:
                next_fd = os.open(component, flags, dir_fd=current_fd)
            except BaseException:
                if created and created_identity is not None:
                    _remove_named_empty_directory_if_identity(
                        current_fd,
                        component,
                        created_identity,
                    )
                raise
            try:
                if created_identity is not None:
                    opened_metadata = os.fstat(next_fd)
                    if (
                        opened_metadata.st_dev,
                        opened_metadata.st_ino,
                        opened_metadata.st_uid,
                    ) != created_identity:
                        raise OSError("created output directory was replaced before opening")
                private_boundary = _validate_output_directory_fd(
                    next_fd,
                    private_boundary=private_boundary,
                )
            except BaseException:
                os.close(next_fd)
                if created and created_identity is not None:
                    _remove_named_empty_directory_if_identity(
                        current_fd,
                        component,
                        created_identity,
                    )
                raise
            os.close(current_fd)
            current_fd = next_fd
            if index == len(components) - 1:
                created_final = created
        if not private_boundary:
            raise OSError("output directory must establish a private ownership boundary")
        return current_fd, created_final
    except BaseException:
        os.close(current_fd)
        raise


def _remove_created_empty_directory(path: Path, descriptor: int) -> bool:
    """Remove a still-empty service-created directory only while its identity remains pinned."""
    try:
        if os.listdir(descriptor):
            return False
        pinned = os.fstat(descriptor)
        parent_fd = _open_existing_directory_fd(path.parent)
    except OSError:
        return False
    try:
        return _remove_named_empty_directory_if_identity(
            parent_fd,
            path.name,
            (pinned.st_dev, pinned.st_ino, pinned.st_uid),
        )
    finally:
        os.close(parent_fd)


def _remove_named_empty_directory_if_identity(
    parent_fd: int,
    name: str,
    identity: tuple[int, int, int],
) -> bool:
    """Best-effort rmdir for one unchanged named directory; rmdir itself enforces emptiness."""
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_uid,
            )
            != identity
        ):
            return False
        os.rmdir(name, dir_fd=parent_fd)
        return True
    except OSError:
        return False


def _validate_output_directory_fd(descriptor: int, *, private_boundary: bool) -> bool:
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError("output ancestor must be a directory")
    owned = metadata.st_uid == os.geteuid()
    privately_owned = owned and not stat.S_IMODE(metadata.st_mode) & 0o022
    if private_boundary and not owned:
        raise OSError("output ancestors below the private boundary must retain ownership")
    if private_boundary and not privately_owned:
        raise OSError("output ancestors must not be group- or world-writable")
    return private_boundary or privately_owned


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
