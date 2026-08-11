"""Bounded BRITE hierarchy mapping with retained JSON and TSV detail."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kegg_mcp._serialization import escape_spreadsheet_formula
from kegg_mcp.domain.annotations import FrozenModel, validate_utf8_text
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.kegg import (
    GetRequest,
    KeggBriteEntryKind,
    KeggEntryRef,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
)
from kegg_mcp.kegg.contracts import (
    KeggBatchProvenance,
    KeggBriteHtextDocument,
    is_kegg_brite_identifier,
)
from kegg_mcp.services.kegg_relations import (
    BoundedRelationResult,
    bounded_relation_batches,
)
from kegg_mcp.services.query_models import (
    MAX_QUERY_PROVENANCE_BATCHES,
    KeggEntityKind,
    KeggEntityRef,
    QueryRetrievalSummary,
)
from kegg_mcp.services.query_support import (
    require_bounded_query_direct_result,
    summarize_query_retrieval,
)
from kegg_mcp.services.reference_budget import KeggPrimitiveClient, effective_query_options
from kegg_mcp.services.result_builders import _artifact_metadata, _json_bytes
from kegg_mcp.services.result_store import (
    ResultArtifactInput,
    ResultArtifactMetadata,
    ResultMetadata,
    SQLiteResultStore,
    create_retained_result,
)

MAX_BRITE_ENTITY_IDS = 100
MAX_BRITE_IDS = 25
MAX_BRITE_PREVIEW_PATHS = 3
MAX_BRITE_PREVIEW_NODE_NAME_CHARACTERS = 128
MAX_BRITE_UNMATCHED_PREVIEW = 10
MAX_BRITE_SOURCE_LINES = 100_000
MAX_BRITE_SOURCE_CHARACTERS = 8_000_000
MAX_BRITE_RELATION_ROWS = 5_000
MAX_BRITE_RELATION_RESPONSE_BYTES = 2_000_000
MAX_BRITE_TOTAL_RESPONSE_BYTES = 8_000_000
MAX_BRITE_PATH_DEPTH = 26
MAX_BRITE_PATHS = 10_000
MAX_BRITE_CLASSIFICATIONS = 50_000
MAX_BRITE_NODE_NAME_CHARACTERS = 2_000
MAX_BRITE_ARTIFACT_BYTES = 16_000_000

BRITE_DETAIL_SECTION = "brite_hierarchy.json"
BRITE_TABLE_SECTION = "brite_hierarchy.tsv"
BRITE_DETAIL_SCHEMA_VERSION = "1"
BRITE_DETAIL_MIME_TYPE = "application/json"
BRITE_TABLE_MIME_TYPE = "text/tab-separated-values; charset=utf-8"

_DATA_LINE = re.compile(r"^(?P<level>[A-Z])(?P<body>.*)$")
_OFFICIAL_NODE_ID = re.compile(r"^(?:[A-Z][0-9]{5}|[0-9]{5}|[1-7](?:\.(?:[0-9]+|-)){3})$")
_COUNT_SEMANTICS = (
    "Counts classify unique supplied entities under each complete BRITE path prefix. "
    "They are descriptive unique-input counts without statistical testing or abundance "
    "weighting."
)
_UNMATCHED_SEMANTICS = (
    "Unmatched means that no selected BRITE hierarchy path was found for the supplied entity; "
    "it does not establish biological absence."
)

_EntityKey = tuple[KeggEntityKind, str]
_NodeKey = tuple[str, str | None, str]
_ClassificationKey = tuple[str, tuple[_NodeKey, ...]]


class MapBriteHierarchyRequest(FrozenModel):
    """Map bounded typed entities into explicit or discovered BRITE hierarchies."""

    entity_ids: Annotated[
        tuple[KeggEntityRef, ...],
        Field(min_length=1, max_length=MAX_BRITE_ENTITY_IDS),
    ]
    brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)] = ()
    include_all_paths: bool = Field(default=True, strict=True)
    include_unmatched: bool = Field(default=True, strict=True)
    preview_limit: int = Field(
        default=MAX_BRITE_PREVIEW_PATHS,
        strict=True,
        ge=0,
        le=MAX_BRITE_PREVIEW_PATHS,
        description=(
            "Number of path and classification previews returned directly, from 0 through 3; "
            "defaults to 3. Complete bounded detail remains retained."
        ),
    )

    @model_validator(mode="after")
    def validate_mapping_scope(self) -> Self:
        entity_keys = tuple((entity.kind, entity.identifier) for entity in self.entity_ids)
        if len(entity_keys) != len(set(entity_keys)):
            raise ValueError("entity_ids must be unique")
        if len(self.brite_ids) != len(set(self.brite_ids)):
            raise ValueError("brite_ids must be unique")
        if any(not is_kegg_brite_identifier(identifier) for identifier in self.brite_ids):
            raise ValueError("brite_ids must contain supported BRITE hierarchy identifiers")
        if not self.brite_ids and any(
            entity.kind is not KeggEntityKind.KO for entity in self.entity_ids
        ):
            raise ValueError("automatic BRITE discovery supports only KO entities")
        return self


class BriteHierarchyNode(FrozenModel):
    """One source-backed BRITE hierarchy node without an invented identifier."""

    depth: int = Field(strict=True, ge=0, lt=MAX_BRITE_PATH_DEPTH)
    level: str = Field(pattern=r"^[A-Z]$", max_length=1)
    node_id: str | None = Field(default=None, min_length=1, max_length=256)
    name: str = Field(max_length=MAX_BRITE_NODE_NAME_CHARACTERS)
    is_input_entity: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_depth(self) -> Self:
        if self.depth != ord(self.level) - ord("A"):
            raise ValueError("BRITE node depth must match its hierarchy level")
        validate_utf8_text(self.name, field_name="BRITE node name")
        return self


class LoadedBriteHtextDocuments(FrozenModel):
    """Bounded explicit BRITE htext documents and their retrieval provenance."""

    selected_brite_ids: Annotated[
        tuple[str, ...],
        Field(min_length=1, max_length=MAX_BRITE_IDS),
    ]
    resolved_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    missing_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    documents: Annotated[
        tuple[KeggBriteHtextDocument, ...],
        Field(max_length=MAX_BRITE_IDS),
    ]
    hierarchy_provenance: Annotated[
        tuple[KeggBatchProvenance, ...],
        Field(max_length=MAX_QUERY_PROVENANCE_BATCHES),
    ]

    @model_validator(mode="after")
    def validate_loaded_documents(self) -> Self:
        if len(self.selected_brite_ids) != len(set(self.selected_brite_ids)):
            raise ValueError("selected BRITE identifiers must be unique")
        if len(self.resolved_brite_ids) != len(set(self.resolved_brite_ids)):
            raise ValueError("resolved BRITE identifiers must be unique")
        if len(self.missing_brite_ids) != len(set(self.missing_brite_ids)):
            raise ValueError("missing BRITE identifiers must be unique")
        if set(self.resolved_brite_ids) & set(self.missing_brite_ids) or set(
            self.resolved_brite_ids
        ) | set(self.missing_brite_ids) != set(self.selected_brite_ids):
            raise ValueError("resolved and missing BRITE identifiers must partition the selection")
        if tuple(document.identifier for document in self.documents) != self.resolved_brite_ids:
            raise ValueError("loaded BRITE documents must follow resolved identifier order")
        return self


class BriteHierarchyPath(FrozenModel):
    """One complete source-ordered hierarchy path for one supplied entity."""

    input_entity: KeggEntityRef
    brite_id: str = Field(min_length=1, max_length=100)
    nodes: Annotated[
        tuple[BriteHierarchyNode, ...],
        Field(min_length=1, max_length=MAX_BRITE_PATH_DEPTH),
    ]

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if not is_kegg_brite_identifier(self.brite_id):
            raise ValueError("path brite_id is invalid")
        if tuple(node.depth for node in self.nodes) != tuple(range(len(self.nodes))):
            raise ValueError("BRITE path nodes must be contiguous from level A")
        if not self.nodes[-1].is_input_entity:
            raise ValueError("BRITE path must terminate at the supplied entity")
        return self


class BriteClassificationCount(FrozenModel):
    """Unique supplied-entity count for one complete hierarchy path prefix."""

    brite_id: str = Field(min_length=1, max_length=100)
    path: Annotated[
        tuple[BriteHierarchyNode, ...],
        Field(min_length=1, max_length=MAX_BRITE_PATH_DEPTH),
    ]
    unique_input_count: int = Field(strict=True, ge=1, le=MAX_BRITE_ENTITY_IDS)

    @model_validator(mode="after")
    def validate_classification_path(self) -> Self:
        if not is_kegg_brite_identifier(self.brite_id):
            raise ValueError("classification brite_id is invalid")
        if tuple(node.depth for node in self.path) != tuple(range(len(self.path))):
            raise ValueError("classification path nodes must be contiguous from level A")
        return self


class BriteHierarchyNodePreview(FrozenModel):
    """One compact BRITE node for a direct-result path preview."""

    depth: int = Field(strict=True, ge=0, lt=MAX_BRITE_PATH_DEPTH)
    level: str = Field(pattern=r"^[A-Z]$", max_length=1)
    node_id: str | None = Field(default=None, min_length=1, max_length=256)
    name: str = Field(max_length=MAX_BRITE_PREVIEW_NODE_NAME_CHARACTERS)
    name_truncated: bool
    is_input_entity: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_depth(self) -> Self:
        if self.depth != ord(self.level) - ord("A"):
            raise ValueError("BRITE preview node depth must match its hierarchy level")
        validate_utf8_text(self.name, field_name="BRITE preview node name")
        if self.name_truncated and len(self.name) != MAX_BRITE_PREVIEW_NODE_NAME_CHARACTERS:
            raise ValueError("truncated BRITE node previews must fill their fixed text bound")
        return self


class BriteHierarchyPathPreview(FrozenModel):
    """One compact complete path preview for a supplied entity."""

    input_entity: KeggEntityRef
    brite_id: str = Field(min_length=1, max_length=100)
    nodes: Annotated[
        tuple[BriteHierarchyNodePreview, ...],
        Field(min_length=1, max_length=MAX_BRITE_PATH_DEPTH),
    ]

    @model_validator(mode="after")
    def validate_path(self) -> Self:
        if not is_kegg_brite_identifier(self.brite_id):
            raise ValueError("preview path brite_id is invalid")
        if tuple(node.depth for node in self.nodes) != tuple(range(len(self.nodes))):
            raise ValueError("BRITE preview path nodes must be contiguous from level A")
        if not self.nodes[-1].is_input_entity:
            raise ValueError("BRITE preview path must terminate at the supplied entity")
        return self


class BriteClassificationCountPreview(FrozenModel):
    """Compact direct classification count for one complete hierarchy path prefix."""

    brite_id: str = Field(min_length=1, max_length=100)
    path: Annotated[
        tuple[BriteHierarchyNodePreview, ...],
        Field(min_length=1, max_length=MAX_BRITE_PATH_DEPTH),
    ]
    unique_input_count: int = Field(strict=True, ge=1, le=MAX_BRITE_ENTITY_IDS)

    @model_validator(mode="after")
    def validate_classification_path(self) -> Self:
        if not is_kegg_brite_identifier(self.brite_id):
            raise ValueError("classification preview brite_id is invalid")
        if tuple(node.depth for node in self.path) != tuple(range(len(self.path))):
            raise ValueError("classification preview nodes must be contiguous from level A")
        return self


class BriteHierarchyDetail(FrozenModel):
    """Complete retained BRITE mapping detail and retrieval provenance."""

    schema_version: Literal["1"]
    request: MapBriteHierarchyRequest
    selected_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    resolved_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    missing_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    paths: Annotated[tuple[BriteHierarchyPath, ...], Field(max_length=MAX_BRITE_PATHS)]
    classifications: Annotated[
        tuple[BriteClassificationCount, ...],
        Field(max_length=MAX_BRITE_CLASSIFICATIONS),
    ]
    unmatched_entities: Annotated[
        tuple[KeggEntityRef, ...],
        Field(max_length=MAX_BRITE_ENTITY_IDS),
    ]
    relation_provenance: Annotated[
        tuple[KeggBatchProvenance, ...],
        Field(max_length=MAX_QUERY_PROVENANCE_BATCHES),
    ] = ()
    hierarchy_provenance: Annotated[
        tuple[KeggBatchProvenance, ...],
        Field(max_length=MAX_QUERY_PROVENANCE_BATCHES),
    ] = ()
    count_semantics: Literal[
        "Counts classify unique supplied entities under each complete BRITE path prefix. "
        "They are descriptive unique-input counts without statistical testing or abundance "
        "weighting."
    ] = _COUNT_SEMANTICS
    unmatched_semantics: Literal[
        "Unmatched means that no selected BRITE hierarchy path was found for the supplied entity; "
        "it does not establish biological absence."
    ] = _UNMATCHED_SEMANTICS

    @model_validator(mode="after")
    def validate_detail(self) -> Self:
        if len(self.selected_brite_ids) != len(set(self.selected_brite_ids)):
            raise ValueError("selected_brite_ids must be unique")
        if len(self.resolved_brite_ids) != len(set(self.resolved_brite_ids)):
            raise ValueError("resolved_brite_ids must be unique")
        if len(self.missing_brite_ids) != len(set(self.missing_brite_ids)):
            raise ValueError("missing_brite_ids must be unique")
        resolved = set(self.resolved_brite_ids)
        missing = set(self.missing_brite_ids)
        if resolved & missing or resolved | missing != set(self.selected_brite_ids):
            raise ValueError("resolved and missing BRITE identifiers must partition the selection")
        if not self.request.include_unmatched and self.unmatched_entities:
            raise ValueError("unmatched entities were excluded by the request")
        return self


class MapBriteHierarchyResult(FrozenModel):
    """Bounded direct preview plus two retained complete detail artifacts."""

    result: ResultMetadata
    artifacts: Annotated[
        tuple[ResultArtifactMetadata, ...],
        Field(min_length=2, max_length=2),
    ]
    entity_count: int = Field(strict=True, ge=1, le=MAX_BRITE_ENTITY_IDS)
    selected_brite_count: int = Field(strict=True, ge=0, le=MAX_BRITE_IDS)
    resolved_brite_count: int = Field(strict=True, ge=0, le=MAX_BRITE_IDS)
    selected_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    resolved_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    missing_brite_ids: Annotated[tuple[str, ...], Field(max_length=MAX_BRITE_IDS)]
    path_count: int = Field(strict=True, ge=0, le=MAX_BRITE_PATHS)
    path_preview: Annotated[
        tuple[BriteHierarchyPathPreview, ...],
        Field(max_length=MAX_BRITE_PREVIEW_PATHS),
    ]
    paths_truncated: bool
    classification_count: int = Field(strict=True, ge=0, le=MAX_BRITE_CLASSIFICATIONS)
    classification_preview: Annotated[
        tuple[BriteClassificationCountPreview, ...],
        Field(max_length=MAX_BRITE_PREVIEW_PATHS),
    ]
    classifications_truncated: bool
    unmatched_count: int = Field(strict=True, ge=0, le=MAX_BRITE_ENTITY_IDS)
    unmatched_preview: Annotated[
        tuple[KeggEntityRef, ...],
        Field(max_length=MAX_BRITE_UNMATCHED_PREVIEW),
    ]
    unmatched_truncated: bool
    unmatched_included: bool = Field(strict=True)
    retrieval: QueryRetrievalSummary
    count_semantics: Literal[
        "Counts classify unique supplied entities under each complete BRITE path prefix. "
        "They are descriptive unique-input counts without statistical testing or abundance "
        "weighting."
    ] = _COUNT_SEMANTICS
    unmatched_semantics: Literal[
        "Unmatched means that no selected BRITE hierarchy path was found for the supplied entity; "
        "it does not establish biological absence."
    ] = _UNMATCHED_SEMANTICS

    @model_validator(mode="after")
    def validate_summary(self) -> Self:
        if self.result.artifact_count != len(self.artifacts):
            raise ValueError("retained artifact count must match artifact metadata")
        if self.selected_brite_count != len(self.selected_brite_ids):
            raise ValueError("selected_brite_count must match selected_brite_ids")
        if self.resolved_brite_count != len(self.resolved_brite_ids):
            raise ValueError("resolved_brite_count must match resolved_brite_ids")
        for field_name, identifiers in (
            ("selected_brite_ids", self.selected_brite_ids),
            ("resolved_brite_ids", self.resolved_brite_ids),
            ("missing_brite_ids", self.missing_brite_ids),
        ):
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{field_name} must be unique")
        resolved = set(self.resolved_brite_ids)
        missing = set(self.missing_brite_ids)
        if resolved & missing or resolved | missing != set(self.selected_brite_ids):
            raise ValueError("resolved and missing BRITE identifiers must partition the selection")
        if self.path_count < len(self.path_preview):
            raise ValueError("path_count cannot be smaller than path_preview")
        if self.paths_truncated != (self.path_count > len(self.path_preview)):
            raise ValueError("paths_truncated must match path_preview")
        if self.classification_count < len(self.classification_preview):
            raise ValueError("classification_count cannot be smaller than classification_preview")
        if self.classifications_truncated != (
            self.classification_count > len(self.classification_preview)
        ):
            raise ValueError("classifications_truncated must match classification_preview")
        if not self.unmatched_included and self.unmatched_preview:
            raise ValueError("excluded unmatched entities must not be returned directly")
        if self.unmatched_count < len(self.unmatched_preview):
            raise ValueError("unmatched_count cannot be smaller than unmatched_preview")
        if self.unmatched_truncated != (self.unmatched_count > len(self.unmatched_preview)):
            raise ValueError("unmatched_truncated must match the direct preview")
        return self


def map_brite_hierarchy(
    request: MapBriteHierarchyRequest,
    *,
    client: KeggPrimitiveClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
) -> MapBriteHierarchyResult:
    """Map supplied entities through bounded BRITE LINK discovery and htext GET parsing."""
    detail = load_brite_hierarchy_detail(
        request,
        client=client,
        options=options,
    )
    paths = detail.paths
    classifications = detail.classifications
    retained_unmatched = detail.unmatched_entities
    relation_provenance = detail.relation_provenance
    hierarchy_provenance = detail.hierarchy_provenance
    classification_lookup = {
        (item.brite_id, tuple(_node_key(node) for node in item.path)): item.unique_input_count
        for item in classifications
    }
    unmatched_count = len(
        tuple(
            entity
            for entity in request.entity_ids
            if _entity_key(entity) not in {_entity_key(path.input_entity) for path in paths}
        )
    )
    detail_bytes = _json_bytes(detail.model_dump(mode="json"))
    table_bytes = _tsv_bytes(
        paths,
        retained_unmatched,
        classification_lookup,
    )
    _validate_artifact_bytes(detail_bytes, table_bytes)
    artifact_inputs = (
        ResultArtifactInput(
            section=BRITE_DETAIL_SECTION,
            mime_type=BRITE_DETAIL_MIME_TYPE,
            content=detail_bytes,
        ),
        ResultArtifactInput(
            section=BRITE_TABLE_SECTION,
            mime_type=BRITE_TABLE_MIME_TYPE,
            content=table_bytes,
        ),
    )
    with create_retained_result(result_store, scope_id, artifact_inputs) as stored:
        preview_limit = request.preview_limit
        path_preview = tuple(_path_preview(path) for path in paths[:preview_limit])
        classification_preview = tuple(
            _classification_preview(item) for item in classifications[:preview_limit]
        )
        unmatched_preview = (
            retained_unmatched[:MAX_BRITE_UNMATCHED_PREVIEW] if request.include_unmatched else ()
        )
        result = MapBriteHierarchyResult(
            result=stored,
            artifacts=(
                _artifact_metadata(
                    BRITE_DETAIL_SECTION,
                    BRITE_DETAIL_MIME_TYPE,
                    detail_bytes,
                ),
                _artifact_metadata(
                    BRITE_TABLE_SECTION,
                    BRITE_TABLE_MIME_TYPE,
                    table_bytes,
                ),
            ),
            entity_count=len(request.entity_ids),
            selected_brite_count=len(detail.selected_brite_ids),
            resolved_brite_count=len(detail.resolved_brite_ids),
            selected_brite_ids=detail.selected_brite_ids,
            resolved_brite_ids=detail.resolved_brite_ids,
            missing_brite_ids=detail.missing_brite_ids,
            path_count=len(paths),
            path_preview=path_preview,
            paths_truncated=len(path_preview) < len(paths),
            classification_count=len(classifications),
            classification_preview=classification_preview,
            classifications_truncated=(len(classification_preview) < len(classifications)),
            unmatched_count=unmatched_count,
            unmatched_preview=unmatched_preview,
            unmatched_truncated=unmatched_count > len(unmatched_preview),
            unmatched_included=request.include_unmatched,
            retrieval=summarize_query_retrieval((*relation_provenance, *hierarchy_provenance)),
        )
        require_bounded_query_direct_result(result)
        return result


def load_brite_hierarchy_detail(
    request: MapBriteHierarchyRequest,
    *,
    client: KeggPrimitiveClient,
    options: KeggRequestOptions | None = None,
) -> BriteHierarchyDetail:
    """Return one bounded parsed BRITE mapping without retaining transport artifacts."""
    options = effective_query_options(options)
    relation = BoundedRelationResult(rows=(), batches=())
    if request.brite_ids:
        selected_brite_ids = request.brite_ids
        entities_by_brite = {brite_id: request.entity_ids for brite_id in selected_brite_ids}
    else:
        relation = bounded_relation_batches(
            tuple(entity.identifier for entity in request.entity_ids),
            relationship=KeggLinkRelationship.KO_TO_BRITE,
            client=client,
            options=options,
            max_total_rows=MAX_BRITE_RELATION_ROWS,
            max_total_response_bytes=MAX_BRITE_RELATION_RESPONSE_BYTES,
        )
        selected_brite_ids, entities_by_brite = _discovered_brite_scope(
            request.entity_ids,
            relation,
        )

    documents: dict[str, KeggBriteHtextDocument] = {}
    missing_brite_ids: tuple[str, ...] = ()
    hierarchy_provenance: tuple[KeggBatchProvenance, ...] = ()
    if selected_brite_ids:
        loaded = load_brite_htext_documents(
            selected_brite_ids,
            client=client,
            options=options,
        )
        hierarchy_provenance = loaded.hierarchy_provenance
        documents = {document.identifier: document for document in loaded.documents}
        missing_brite_ids = loaded.missing_brite_ids
    resolved_brite_ids = tuple(brite_id for brite_id in selected_brite_ids if brite_id in documents)
    _validate_response_budget(relation.batches, hierarchy_provenance)

    paths = _extract_paths(
        resolved_brite_ids,
        documents,
        entities_by_brite,
        include_all_paths=request.include_all_paths,
    )
    classifications, _classification_lookup = _classification_counts(paths)
    matched_keys = {_entity_key(path.input_entity) for path in paths}
    unmatched = tuple(
        entity for entity in request.entity_ids if _entity_key(entity) not in matched_keys
    )
    retained_unmatched = unmatched if request.include_unmatched else ()
    detail = BriteHierarchyDetail(
        schema_version=BRITE_DETAIL_SCHEMA_VERSION,
        request=request,
        selected_brite_ids=selected_brite_ids,
        resolved_brite_ids=resolved_brite_ids,
        missing_brite_ids=missing_brite_ids,
        paths=paths,
        classifications=classifications,
        unmatched_entities=retained_unmatched,
        relation_provenance=relation.batches,
        hierarchy_provenance=hierarchy_provenance,
    )
    return detail


def load_brite_htext_documents(
    brite_ids: tuple[str, ...],
    *,
    client: KeggPrimitiveClient,
    options: KeggRequestOptions | None = None,
) -> LoadedBriteHtextDocuments:
    """Retrieve explicit BRITE hierarchy documents once for bounded local interpretation."""
    if not 1 <= len(brite_ids) <= MAX_BRITE_IDS:
        _limit_exceeded("brite_identifiers", len(brite_ids), MAX_BRITE_IDS)
    if len(brite_ids) != len(set(brite_ids)) or any(
        not is_kegg_brite_identifier(identifier) for identifier in brite_ids
    ):
        fail(
            ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
            "Explicit BRITE hierarchy identifiers must be unique and canonical.",
            suggested_action="Provide unique supported BRITE hierarchy identifiers.",
        )
    effective_options = effective_query_options(options)
    fetched_documents: list[object] = []
    fetched_batches: list[KeggBatchProvenance] = []
    for brite_id in brite_ids:
        fetched = client.get(
            GetRequest(
                entries=(
                    KeggEntryRef(
                        database=KeggGetDatabase.BRITE,
                        identifier=brite_id,
                        brite_kind=KeggBriteEntryKind.HIERARCHY,
                    ),
                )
            ),
            options=effective_options,
        )
        fetched_batches.extend(fetched.batches)
        _validate_response_budget((), tuple(fetched_batches))
        fetched_documents.extend(fetched.documents)
    documents_by_id = _brite_documents(fetched_documents)
    if not set(documents_by_id).issubset(brite_ids):
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "The BRITE hierarchy response included an unrequested document.",
            suggested_action="Refresh the exact typed BRITE hierarchy entries and retry.",
        )
    resolved = tuple(brite_id for brite_id in brite_ids if brite_id in documents_by_id)
    missing = tuple(brite_id for brite_id in brite_ids if brite_id not in documents_by_id)
    return LoadedBriteHtextDocuments(
        selected_brite_ids=brite_ids,
        resolved_brite_ids=resolved,
        missing_brite_ids=missing,
        documents=tuple(documents_by_id[brite_id] for brite_id in resolved),
        hierarchy_provenance=tuple(fetched_batches),
    )


def _node_preview(node: BriteHierarchyNode) -> BriteHierarchyNodePreview:
    name = node.name[:MAX_BRITE_PREVIEW_NODE_NAME_CHARACTERS]
    return BriteHierarchyNodePreview(
        depth=node.depth,
        level=node.level,
        node_id=node.node_id,
        name=name,
        name_truncated=len(node.name) > len(name),
        is_input_entity=node.is_input_entity,
    )


def _path_preview(path: BriteHierarchyPath) -> BriteHierarchyPathPreview:
    return BriteHierarchyPathPreview(
        input_entity=path.input_entity,
        brite_id=path.brite_id,
        nodes=tuple(_node_preview(node) for node in path.nodes),
    )


def _classification_preview(
    classification: BriteClassificationCount,
) -> BriteClassificationCountPreview:
    return BriteClassificationCountPreview(
        brite_id=classification.brite_id,
        path=tuple(_node_preview(node) for node in classification.path),
        unique_input_count=classification.unique_input_count,
    )


def _discovered_brite_scope(
    entities: tuple[KeggEntityRef, ...],
    relation: BoundedRelationResult,
) -> tuple[tuple[str, ...], dict[str, tuple[KeggEntityRef, ...]]]:
    entities_by_identifier = {entity.identifier: entity for entity in entities}
    brite_entities: dict[str, list[KeggEntityRef]] = {}
    for row in relation.rows:
        source_prefix, source_separator, source_identifier = row.source_id.partition(":")
        target_prefix, target_separator, brite_id = row.target_id.partition(":")
        if (
            source_separator != ":"
            or source_prefix != "ko"
            or source_identifier not in entities_by_identifier
            or target_separator != ":"
            or target_prefix not in {"br", "brite"}
            or not is_kegg_brite_identifier(brite_id)
        ):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The KO-to-BRITE relationship response was incompatible with the request.",
                suggested_action="Refresh the typed KEGG LINK response and retry.",
                safe_details=(
                    SafeDetail(name="reason", value="unexpected_relationship_identifier"),
                ),
            )
        selected = brite_entities.setdefault(brite_id, [])
        entity = entities_by_identifier[source_identifier]
        if entity not in selected:
            selected.append(entity)
        if len(brite_entities) > MAX_BRITE_IDS:
            _limit_exceeded(
                "discovered_brite_identifiers",
                len(brite_entities),
                MAX_BRITE_IDS,
            )
    brite_ids = tuple(brite_entities)
    return brite_ids, {brite_id: tuple(brite_entities[brite_id]) for brite_id in brite_ids}


def _validate_response_budget(
    relation_batches: tuple[KeggBatchProvenance, ...],
    hierarchy_batches: tuple[KeggBatchProvenance, ...],
) -> None:
    total_batches = len(relation_batches) + len(hierarchy_batches)
    if total_batches > MAX_QUERY_PROVENANCE_BATCHES:
        _limit_exceeded(
            "provenance_batches",
            total_batches,
            MAX_QUERY_PROVENANCE_BATCHES,
        )
    response_bytes = sum(batch.response_bytes for batch in (*relation_batches, *hierarchy_batches))
    if response_bytes > MAX_BRITE_TOTAL_RESPONSE_BYTES:
        _limit_exceeded(
            "total_response_bytes",
            response_bytes,
            MAX_BRITE_TOTAL_RESPONSE_BYTES,
        )


def _brite_documents(
    documents: Iterable[object],
) -> dict[str, KeggBriteHtextDocument]:
    selected: dict[str, KeggBriteHtextDocument] = {}
    line_count = 0
    character_count = 0
    for document in documents:
        if not isinstance(document, KeggBriteHtextDocument):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The BRITE hierarchy request returned an incompatible document.",
                suggested_action="Refresh the typed BRITE htext response and retry.",
            )
        if not document.lines:
            continue
        if document.identifier in selected:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The BRITE hierarchy response repeated one requested document.",
                suggested_action="Refresh the typed BRITE htext response and retry.",
            )
        line_count += len(document.lines)
        character_count += sum(len(line) for line in document.lines)
        if line_count > MAX_BRITE_SOURCE_LINES:
            _limit_exceeded(
                "brite_source_lines",
                line_count,
                MAX_BRITE_SOURCE_LINES,
            )
        if character_count > MAX_BRITE_SOURCE_CHARACTERS:
            _limit_exceeded(
                "brite_source_characters",
                character_count,
                MAX_BRITE_SOURCE_CHARACTERS,
            )
        selected[document.identifier] = document
    return selected


def _extract_paths(
    brite_ids: tuple[str, ...],
    documents: dict[str, KeggBriteHtextDocument],
    entities_by_brite: dict[str, tuple[KeggEntityRef, ...]],
    *,
    include_all_paths: bool,
) -> tuple[BriteHierarchyPath, ...]:
    paths: list[BriteHierarchyPath] = []
    path_keys: set[tuple[_EntityKey, str, tuple[_NodeKey, ...]]] = set()
    first_path_seen: set[tuple[_EntityKey, str]] = set()
    observed_matches = 0
    for brite_id in brite_ids:
        document = documents.get(brite_id)
        if document is None:
            continue
        entities = entities_by_brite.get(brite_id, ())
        if not entities:
            continue
        for nodes, candidate in iter_brite_htext_nodes(document):
            matched = tuple(
                entity for entity in entities if candidate in _entity_match_tokens(entity)
            )
            for entity in matched:
                observed_matches += 1
                if observed_matches > MAX_BRITE_PATHS:
                    _limit_exceeded(
                        "brite_hierarchy_paths",
                        observed_matches,
                        MAX_BRITE_PATHS,
                    )
                entity_key = _entity_key(entity)
                first_key = (entity_key, brite_id)
                if not include_all_paths and first_key in first_path_seen:
                    continue
                path_nodes = (
                    *nodes[:-1],
                    nodes[-1].model_copy(
                        update={
                            "node_id": nodes[-1].node_id or entity.identifier,
                            "is_input_entity": True,
                        }
                    ),
                )
                key = (
                    entity_key,
                    brite_id,
                    tuple(_node_key(node) for node in path_nodes),
                )
                if key in path_keys:
                    continue
                paths.append(
                    BriteHierarchyPath(
                        input_entity=entity,
                        brite_id=brite_id,
                        nodes=path_nodes,
                    )
                )
                path_keys.add(key)
                first_path_seen.add(first_key)
                if len(paths) > MAX_BRITE_PATHS:
                    _limit_exceeded(
                        "brite_hierarchy_paths",
                        len(paths),
                        MAX_BRITE_PATHS,
                    )
    return tuple(paths)


def iter_brite_htext_nodes(
    document: KeggBriteHtextDocument,
) -> Iterable[tuple[tuple[BriteHierarchyNode, ...], str]]:
    stack: list[BriteHierarchyNode] = []
    started = False
    for line in document.lines:
        match = _DATA_LINE.match(line)
        if match is None:
            continue
        level = match.group("level")
        if not started:
            if level != "A":
                continue
            started = True
        depth = ord(level) - ord("A")
        if depth >= MAX_BRITE_PATH_DEPTH:
            _limit_exceeded(
                "brite_path_depth",
                depth + 1,
                MAX_BRITE_PATH_DEPTH,
            )
        if depth > len(stack):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "The BRITE hierarchy contains a node without its required parent path.",
                suggested_action="Refresh the BRITE htext response and retry.",
                safe_details=(SafeDetail(name="reason", value="non_contiguous_hierarchy_level"),),
            )
        node, candidate = _parse_htext_node(level, match.group("body"))
        stack[depth:] = (node,)
        yield tuple(stack), candidate


def _parse_htext_node(level: str, body: str) -> tuple[BriteHierarchyNode, str]:
    text = body.strip()
    if not text:
        fail(
            ErrorCode.KEGG_PARSE_FAILED,
            "The BRITE hierarchy contains an empty data node.",
            suggested_action="Refresh the BRITE htext response and retry.",
            safe_details=(SafeDetail(name="reason", value="empty_hierarchy_node"),),
        )
    parts = text.split(maxsplit=1)
    candidate = parts[0]
    if _OFFICIAL_NODE_ID.fullmatch(candidate) is not None:
        node_id = candidate
        name = parts[1] if len(parts) == 2 else ""
    else:
        node_id = None
        name = text
    if len(name) > MAX_BRITE_NODE_NAME_CHARACTERS:
        _limit_exceeded(
            "brite_node_name_characters",
            len(name),
            MAX_BRITE_NODE_NAME_CHARACTERS,
        )
    return (
        BriteHierarchyNode(
            depth=ord(level) - ord("A"),
            level=level,
            node_id=node_id,
            name=name,
        ),
        candidate,
    )


def _entity_match_tokens(entity: KeggEntityRef) -> frozenset[str]:
    identifier = entity.identifier
    tokens = {identifier}
    if entity.kind in {
        KeggEntityKind.PATHWAY,
        KeggEntityKind.BRITE,
    }:
        tokens.add(identifier[-5:])
    elif entity.kind is KeggEntityKind.TAXONOMY:
        tokens.add(identifier.removeprefix("taxid:"))
    return frozenset(tokens)


def _classification_counts(
    paths: tuple[BriteHierarchyPath, ...],
) -> tuple[
    tuple[BriteClassificationCount, ...],
    dict[_ClassificationKey, int],
]:
    entities_by_path: dict[_ClassificationKey, set[_EntityKey]] = {}
    nodes_by_path: dict[_ClassificationKey, tuple[BriteHierarchyNode, ...]] = {}
    for path in paths:
        entity_key = _entity_key(path.input_entity)
        for length in range(1, len(path.nodes) + 1):
            prefix = path.nodes[:length]
            key = (
                path.brite_id,
                tuple(_node_key(node) for node in prefix),
            )
            nodes_by_path.setdefault(key, prefix)
            entities_by_path.setdefault(key, set()).add(entity_key)
            if len(entities_by_path) > MAX_BRITE_CLASSIFICATIONS:
                _limit_exceeded(
                    "brite_classifications",
                    len(entities_by_path),
                    MAX_BRITE_CLASSIFICATIONS,
                )
    classifications = tuple(
        BriteClassificationCount(
            brite_id=key[0],
            path=nodes_by_path[key],
            unique_input_count=len(entities_by_path[key]),
        )
        for key in entities_by_path
    )
    return classifications, {key: len(entities_by_path[key]) for key in entities_by_path}


def _tsv_bytes(
    paths: tuple[BriteHierarchyPath, ...],
    unmatched_entities: tuple[KeggEntityRef, ...],
    classification_lookup: dict[_ClassificationKey, int],
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(
        _safe_tsv_row(
            (
                "record_type",
                "input_kind",
                "input_identifier",
                "brite_id",
                "path_index",
                "depth",
                "level",
                "node_id",
                "node_name",
                "unique_input_count",
            )
        )
    )
    for path_index, path in enumerate(paths, start=1):
        for length, node in enumerate(path.nodes, start=1):
            prefix = path.nodes[:length]
            key = (
                path.brite_id,
                tuple(_node_key(item) for item in prefix),
            )
            writer.writerow(
                _safe_tsv_row(
                    (
                        "path_node",
                        path.input_entity.kind.value,
                        path.input_entity.identifier,
                        path.brite_id,
                        path_index,
                        node.depth,
                        node.level,
                        node.node_id or "",
                        node.name,
                        classification_lookup[key],
                    )
                )
            )
    for entity in unmatched_entities:
        writer.writerow(
            _safe_tsv_row(
                (
                    "unmatched",
                    entity.kind.value,
                    entity.identifier,
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                )
            )
        )
    return output.getvalue().encode("utf-8")


def _safe_tsv_row(values: Iterable[object]) -> tuple[str, ...]:
    return tuple(
        escape_spreadsheet_formula("" if value is None else str(value)) for value in values
    )


def _validate_artifact_bytes(detail: bytes, table: bytes) -> None:
    for name, content in (("brite_detail", detail), ("brite_table", table)):
        if len(content) > MAX_BRITE_ARTIFACT_BYTES:
            fail(
                ErrorCode.OUTPUT_LIMIT_EXCEEDED,
                "The BRITE hierarchy artifact exceeded its output bound.",
                suggested_action="Request fewer entities or BRITE hierarchies.",
                safe_details=(
                    SafeDetail(name="artifact", value=name),
                    SafeDetail(name="observed_bytes", value=str(len(content))),
                    SafeDetail(name="limit_bytes", value=str(MAX_BRITE_ARTIFACT_BYTES)),
                ),
            )


def _entity_key(entity: KeggEntityRef) -> _EntityKey:
    return entity.kind, entity.identifier


def _node_key(node: BriteHierarchyNode) -> _NodeKey:
    return node.level, node.node_id, node.name


def _limit_exceeded(name: str, observed: int, limit: int) -> None:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "The BRITE hierarchy request exceeded its aggregate service bound.",
        suggested_action="Request fewer entities, hierarchies, or hierarchy paths.",
        safe_details=(
            SafeDetail(name="limit_name", value=name),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit", value=str(limit)),
        ),
    )


__all__ = [
    "BRITE_DETAIL_MIME_TYPE",
    "BRITE_DETAIL_SCHEMA_VERSION",
    "BRITE_DETAIL_SECTION",
    "BRITE_TABLE_MIME_TYPE",
    "BRITE_TABLE_SECTION",
    "MAX_BRITE_ARTIFACT_BYTES",
    "MAX_BRITE_CLASSIFICATIONS",
    "MAX_BRITE_ENTITY_IDS",
    "MAX_BRITE_IDS",
    "MAX_BRITE_PATHS",
    "MAX_BRITE_PREVIEW_NODE_NAME_CHARACTERS",
    "MAX_BRITE_PREVIEW_PATHS",
    "MAX_BRITE_UNMATCHED_PREVIEW",
    "BriteClassificationCount",
    "BriteClassificationCountPreview",
    "BriteHierarchyDetail",
    "BriteHierarchyNode",
    "BriteHierarchyNodePreview",
    "BriteHierarchyPath",
    "BriteHierarchyPathPreview",
    "LoadedBriteHtextDocuments",
    "MapBriteHierarchyRequest",
    "MapBriteHierarchyResult",
    "iter_brite_htext_nodes",
    "load_brite_hierarchy_detail",
    "load_brite_htext_documents",
    "map_brite_hierarchy",
]
