"""Format and safely persist bounded inputs for KEGG Mapper and KEGG Syntax."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from kegg_mcp import __version__
from kegg_mcp.services._atomic_bundle import write_text_bundle
from kegg_mcp.services._text_artifact import TextArtifactSpec
from kegg_mcp.services.external_handoff_models import (
    EXTERNAL_HANDOFF_SCHEMA_VERSION,
    MAX_EXTERNAL_HANDOFF_DATA_BYTES,
    ExternalHandoffBundle,
    ExternalHandoffRequest,
    ExternalHandoffTarget,
    MapperColorRequest,
    MapperJoinRequest,
    MapperMwsearchRequest,
    MapperReconstructRequest,
    MapperSearchRequest,
    SyntaxKoCompositionRequest,
    SyntaxKoSequenceRequest,
)
from kegg_mcp.services.output_bundle import OutputBundleArtifact

_MANIFEST_NAME = "handoff_manifest.json"
_MAX_EXTERNAL_HANDOFF_TOTAL_BYTES = MAX_EXTERNAL_HANDOFF_DATA_BYTES + 100_000
_FORMAT_SOURCE_RETRIEVED_ON = "2026-07-31"
_MAPPER_FORMAT_SOURCE = "https://www.kegg.jp/kegg/mapper/"
_SYNTAX_FORMAT_SOURCE = "https://www.kegg.jp/kegg/syntax/synteny.html"

_DATA_FILE_NAMES = {
    ExternalHandoffTarget.MAPPER_RECONSTRUCT: "mapper_reconstruct.tsv",
    ExternalHandoffTarget.MAPPER_SEARCH: "mapper_search.txt",
    ExternalHandoffTarget.MAPPER_COLOR: "mapper_color.tsv",
    ExternalHandoffTarget.MAPPER_JOIN: "mapper_join.tsv",
    ExternalHandoffTarget.MAPPER_MWSEARCH: "mapper_mwsearch.txt",
    ExternalHandoffTarget.SYNTAX_KO_COMPOSITION: "syntax_ko_composition.txt",
    ExternalHandoffTarget.SYNTAX_KO_SEQUENCE: "syntax_ko_sequence.tsv",
}


def prepare_external_handoff(
    request: ExternalHandoffRequest,
    *,
    output_directory: Path,
    remove_created_directory_on_failure: bool = False,
) -> ExternalHandoffBundle:
    """Write one local input file and manifest without invoking an external service."""
    data_name = _DATA_FILE_NAMES[request.target]
    data_artifact = TextArtifactSpec(
        name=data_name,
        mime_type=_mime_type(data_name),
        content=serialize_external_handoff(request),
    )
    item_count = _item_count(request)
    manifest_artifact = TextArtifactSpec(
        name=_MANIFEST_NAME,
        mime_type=_mime_type(_MANIFEST_NAME),
        content=_serialize_manifest(
            request,
            data_artifact=data_artifact,
            item_count=item_count,
        ),
    )
    artifacts = (data_artifact, manifest_artifact)
    files = {artifact.name: artifact.content for artifact in artifacts}
    write_text_bundle(
        output_directory,
        files,
        manifest_name=_MANIFEST_NAME,
        remove_created_directory_on_failure=remove_created_directory_on_failure,
        max_artifact_bytes=MAX_EXTERNAL_HANDOFF_DATA_BYTES,
        max_total_bytes=_MAX_EXTERNAL_HANDOFF_TOTAL_BYTES,
    )
    return ExternalHandoffBundle(
        target=request.target,
        output_directory=str(output_directory),
        data_file=str(output_directory / data_artifact.name),
        manifest=str(output_directory / manifest_artifact.name),
        item_count=item_count,
        data_byte_size=data_artifact.byte_size,
        artifacts=tuple(
            OutputBundleArtifact(
                name=artifact.name,
                mime_type=artifact.mime_type,
                byte_size=artifact.byte_size,
                path=str(output_directory / artifact.name),
            )
            for artifact in artifacts
        ),
    )


def serialize_external_handoff(request: ExternalHandoffRequest) -> str:
    """Serialize one already validated request in the official upload-file shape."""
    if isinstance(request, MapperReconstructRequest):
        return _tabular(
            ((row.ko_id,) if row.user_id is None else (row.user_id, row.ko_id))
            for row in request.rows
        )
    if isinstance(request, MapperSearchRequest):
        return _line_items(request.identifiers)
    if isinstance(request, MapperColorRequest):
        return _tabular(
            (
                row.identifier,
                (
                    row.background_color or ""
                    if row.foreground_color is None
                    else f"{row.background_color or ''},{row.foreground_color}"
                ),
            )
            for row in request.rows
        )
    if isinstance(request, MapperJoinRequest):
        return _tabular((row.identifier, row.attribute) for row in request.rows)
    if isinstance(request, MapperMwsearchRequest):
        return _line_items(request.values)
    if isinstance(request, SyntaxKoCompositionRequest):
        return _line_items(request.ko_ids)
    return _tabular((row.gene_id, row.ko_id) for row in request.rows)


def _line_items(values: tuple[str, ...]) -> str:
    return "".join(f"{value}\n" for value in values)


def _tabular(rows: Iterable[tuple[str, ...]]) -> str:
    return "".join("\t".join(row) + "\n" for row in rows)


def _item_count(request: ExternalHandoffRequest) -> int:
    if isinstance(
        request,
        (
            MapperReconstructRequest,
            MapperColorRequest,
            MapperJoinRequest,
            SyntaxKoSequenceRequest,
        ),
    ):
        return len(request.rows)
    if isinstance(request, MapperSearchRequest):
        return len(request.identifiers)
    if isinstance(request, MapperMwsearchRequest):
        return len(request.values)
    return len(request.ko_ids)


def _serialize_manifest(
    request: ExternalHandoffRequest,
    *,
    data_artifact: TextArtifactSpec,
    item_count: int,
) -> str:
    source_url = (
        _MAPPER_FORMAT_SOURCE
        if request.target.value.startswith("mapper_")
        else _SYNTAX_FORMAT_SOURCE
    )
    value = {
        "schema_version": EXTERNAL_HANDOFF_SCHEMA_VERSION,
        "bundle_kind": "kegg_external_tool_input",
        "producer": {
            "name": "kegg-mcp",
            "version": __version__,
        },
        "target": request.target.value,
        "parameters": _manifest_parameters(request),
        "input": {
            "caller_supplied": True,
            "item_count": item_count,
            "order_preserved": True,
            "duplicate_semantics": _duplicate_semantics(request),
        },
        "format": {
            "data_file": data_artifact.name,
            "mime_type": data_artifact.mime_type,
            "header": False,
            "delimiter": "\t" if data_artifact.name.endswith(".tsv") else "newline",
            "official_source": source_url,
            "source_retrieved_on": _FORMAT_SOURCE_RETRIEVED_ON,
        },
        "files": [
            data_artifact.integrity_record(),
            {
                "name": _MANIFEST_NAME,
                "mime_type": _mime_type(_MANIFEST_NAME),
                "commit_marker": True,
            },
        ],
        "execution_boundary": {
            "external_tool_executed": False,
            "uploaded": False,
            "browser_started": False,
            "external_result_parsed": False,
        },
    }
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _manifest_parameters(request: ExternalHandoffRequest) -> dict[str, object]:
    if isinstance(request, (MapperSearchRequest, MapperColorRequest)):
        return {
            "scope": request.scope.value,
            "organism": request.organism,
        }
    if isinstance(request, (MapperJoinRequest, MapperMwsearchRequest)):
        return {"mode": request.mode.value}
    if isinstance(request, SyntaxKoSequenceRequest):
        return {"order_semantics": request.order_semantics}
    return {}


def _duplicate_semantics(request: ExternalHandoffRequest) -> str:
    if isinstance(request, MapperColorRequest):
        return "exact_rows_rejected_repeated_identifier_colors_preserved"
    if isinstance(request, MapperJoinRequest):
        return "exact_rows_rejected_repeated_identifier_attributes_preserved"
    if isinstance(request, SyntaxKoSequenceRequest):
        return "gene_ids_unique_repeated_ko_ids_preserved"
    return "duplicates_rejected"


def _mime_type(name: str) -> str:
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".tsv"):
        return "text/tab-separated-values"
    if name.endswith(".txt"):
        return "text/plain"
    raise AssertionError("external handoff contains an unsupported file extension")


__all__ = ("prepare_external_handoff", "serialize_external_handoff")
