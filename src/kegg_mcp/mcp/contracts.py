"""Explicit MCP tool and resource contracts."""

from __future__ import annotations

from typing import Annotated, Generic, Literal, Self, TypeVar, cast

from pydantic import Field, RootModel, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError

from kegg_mcp.analysis.pathway_coverage import PathwayReferenceNamespace
from kegg_mcp.analysis.pathway_ranking import PathwaySelection
from kegg_mcp.domain.annotations import AnalysisUnit, EvidenceMode, FrozenModel, ModuleId
from kegg_mcp.domain.errors import ErrorDetail
from kegg_mcp.importers import GenericColumnMapping, SourceProvenanceInput
from kegg_mcp.importers.contracts import MAX_ANNOTATION_DATE_CHARACTERS
from kegg_mcp.kegg import KeggEntryRef
from kegg_mcp.services.annotation_audit import (
    AnnotationMappingAuditResult,
    AnnotationMappingTarget,
    AnnotationQualityContext,
)
from kegg_mcp.services.brite_hierarchy import (
    MapBriteHierarchyRequest,
    MapBriteHierarchyResult,
)
from kegg_mcp.services.models import (
    DEFAULT_IMPORT_LIMITS,
    AnalyzeKoAnnotationsResult,
    AnalyzeModulesResult,
    AnalyzePathwaysResult,
    AnnotationInputFormat,
    CompareDatasetSource,
    CompareKoSetsResult,
    ConnectivityProbeResult,
    DatasetSource,
    GenericDecisionPolicy,
    KeggEntriesServiceResult,
    NormalizeAnnotationsRequest,
    NormalizeAnnotationsResult,
    ServerStatusResult,
)
from kegg_mcp.services.output_bundle import ManifestPathMode
from kegg_mcp.services.query_models import (
    ResolveKeggEntitiesRequest,
    ResolveKeggEntitiesResult,
    SearchKeggEntriesRequest,
    SearchKeggEntriesResult,
    TraceKeggRelationsRequest,
    TraceKeggRelationsResult,
)
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    canonicalize_pathway_specs,
)
from kegg_mcp.services.result_store import (
    RESULT_ID_FRAGMENT,
    RESULT_ID_SCHEMA_PATTERN,
    DeletedResult,
    ResultArtifactMetadata,
    ResultMetadata,
    ResultMetadataPage,
)

T = TypeVar("T")
ResultUri = Annotated[
    str,
    Field(pattern=rf"^ko-analysis://results/{RESULT_ID_FRAGMENT}$"),
]


class ToolPayload(FrozenModel, Generic[T]):
    """One typed successful payload and its optional retained-result resource."""

    data: T
    resource_uri: ResultUri | None = None


class ToolEnvelope(FrozenModel, Generic[T]):
    """One schema for both successful and repairable failed tool execution."""

    ok: bool
    result: ToolPayload[T] | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def validate_variant(self) -> Self:
        if self.ok and (self.result is None or self.error is not None):
            raise ValueError("successful tool envelopes require only result")
        if not self.ok and (self.error is None or self.result is not None):
            raise ValueError("failed tool envelopes require only error")
        return self


class NormalizeKoAnnotationsInput(FrozenModel):
    """User-facing normalization input without server tuning or cache controls."""

    text: str | None = Field(default=None, min_length=1, max_length=5_000_000)
    file_path: str | None = Field(default=None, min_length=1, max_length=4_096)
    output_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_096,
        description=(
            "New or empty allowed bundle directory. Omit to allocate a fresh directory beneath "
            "the deployment's default output root when file handoff is enabled."
        ),
    )
    manifest_path_mode: ManifestPathMode = ManifestPathMode.REDACTED
    input_format: AnnotationInputFormat = AnnotationInputFormat.PLAIN_KO
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN
    sample_id: str = Field(default="sample-1", min_length=1, max_length=256)
    taxon_id: int | None = Field(default=None, strict=True, gt=0)
    kegg_organism_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9]{1,7}$")
    source: SourceProvenanceInput | None = None
    column_mapping: GenericColumnMapping | None = None
    decision_policy: GenericDecisionPolicy | None = None

    @model_validator(mode="after")
    def validate_service_contract(self) -> Self:
        self.to_service_request()
        return self

    def to_service_request(self) -> NormalizeAnnotationsRequest:
        """Build the bounded internal request with deployment-owned limits."""
        return NormalizeAnnotationsRequest(
            text=self.text,
            file_path=self.file_path,
            output_directory=self.output_directory,
            manifest_path_mode=self.manifest_path_mode,
            input_format=self.input_format,
            analysis_unit=self.analysis_unit,
            sample_id=self.sample_id,
            taxon_id=self.taxon_id,
            kegg_organism_code=self.kegg_organism_code,
            source=self.source,
            column_mapping=self.column_mapping,
            decision_policy=self.decision_policy,
        )


class AnalyzeKoAnnotationsInput(FrozenModel):
    """Simple common-path KO analysis input."""

    ko_text: str | None = Field(default=None, min_length=1, max_length=5_000_000)
    annotations: NormalizeKoAnnotationsInput | SkipJsonSchema[None] = Field(
        default=None,
        description=(
            "Nested annotation-table import. Put analysis_unit and sample_id inside this "
            "object; do not combine it with ko_text."
        ),
    )
    module_ids: Annotated[tuple[ModuleId, ...], Field(max_length=25)] = ()
    pathways: Annotated[
        tuple[PathwaySpec, ...],
        Field(
            max_length=25,
            description=(
                'Explicit KO-reference pathway objects such as [{"pathway_id": "ko00010"}]. '
                "This KO-only tool accepts koNNNNN identifiers, not organism pathway IDs. Do not "
                "combine this field with pathway_selection."
            ),
        ),
    ] = ()
    pathway_selection: PathwaySelection | SkipJsonSchema[None] = Field(
        default=None,
        description="Automatic ranked pathway selection; mutually exclusive with pathways.",
    )
    analysis_unit: AnalysisUnit = Field(
        default=AnalysisUnit.UNKNOWN,
        description=(
            "Biological unit for ko_text. When annotations is supplied, set this field inside "
            "the annotations object instead."
        ),
    )
    sample_id: str = Field(default="sample-1", min_length=1, max_length=256)
    pathway_evidence_mode: EvidenceMode = EvidenceMode.STRICT
    allow_global_or_overview: bool = False
    output_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_096,
        description=(
            "New or empty allowed analysis directory. Omit to allocate a fresh directory beneath "
            "the deployment's default output root when file handoff is enabled."
        ),
    )

    @field_validator("pathways")
    @classmethod
    def canonicalize_pathways(cls, value: tuple[PathwaySpec, ...]) -> tuple[PathwaySpec, ...]:
        return canonicalize_pathway_specs(value)

    @model_validator(mode="after")
    def validate_common_path(self) -> Self:
        if (self.ko_text is None) == (self.annotations is None):
            raise ValueError("provide exactly one of ko_text or annotations")
        if self.annotations is not None:
            conflict_fields: list[str] = []
            if "analysis_unit" in self.model_fields_set:
                conflict_fields.extend(("analysis_unit", "annotations.analysis_unit"))
            if "sample_id" in self.model_fields_set:
                conflict_fields.extend(("sample_id", "annotations.sample_id"))
            if conflict_fields:
                raise PydanticCustomError(
                    "nested_annotation_context_conflict",
                    "top-level context conflicts with nested annotation context",
                    {"conflict_fields": ",".join(conflict_fields)},
                )
        if (
            self.annotations is not None
            and self.annotations.output_directory is not None
            and self.output_directory is not None
            and self.annotations.output_directory != self.output_directory
        ):
            raise ValueError("conflicting output_directory values were supplied")
        _reject_organism_pathways(self.pathways)
        if self.pathway_selection is not None and self.pathways:
            raise ValueError("automatic pathway selection cannot include explicit pathways")
        return self


class GetKeggEntriesInput(FrozenModel):
    """Bounded allowlisted GET request; never an arbitrary URL."""

    entries: Annotated[tuple[KeggEntryRef, ...], Field(min_length=1, max_length=50)]


SearchKeggEntriesInput = SearchKeggEntriesRequest


class ResolveKeggEntitiesInput(RootModel[ResolveKeggEntitiesRequest]):
    """Direct discriminated gene-or-organism resolution input."""


TraceKeggRelationsInput = TraceKeggRelationsRequest
MapBriteHierarchyInput = MapBriteHierarchyRequest


class AuditAnnotationMappingInput(FrozenModel):
    """Audit one inline or retained normalized annotation dataset."""

    source: DatasetSource
    quality_context: AnnotationQualityContext | None = None
    mapping_targets: Annotated[
        tuple[AnnotationMappingTarget, ...],
        Field(max_length=len(AnnotationMappingTarget)),
    ] = tuple(AnnotationMappingTarget)

    @field_validator("mapping_targets")
    @classmethod
    def canonicalize_mapping_targets(
        cls,
        value: tuple[AnnotationMappingTarget, ...],
    ) -> tuple[AnnotationMappingTarget, ...]:
        if len(value) != len(set(value)):
            raise ValueError("mapping_targets must be unique")
        return tuple(target for target in AnnotationMappingTarget if target in value)


class AnalyzeModulesInput(FrozenModel):
    """MODULE targets evaluated against inline or retained evidence."""

    source: DatasetSource
    module_ids: Annotated[tuple[ModuleId, ...], Field(min_length=1, max_length=25)]

    @model_validator(mode="after")
    def require_unique_modules(self) -> Self:
        if len(self.module_ids) != len(set(self.module_ids)):
            raise ValueError("module_ids must be unique")
        return self


class AnalyzePathwaysInput(FrozenModel):
    """Descriptive pathway coverage targets with explicit namespaces."""

    source: DatasetSource
    pathways: Annotated[tuple[PathwaySpec, ...], Field(min_length=1, max_length=25)]
    evidence_mode: EvidenceMode = EvidenceMode.STRICT
    allow_global_or_overview: bool = False

    @field_validator("pathways")
    @classmethod
    def canonicalize_pathways(cls, value: tuple[PathwaySpec, ...]) -> tuple[PathwaySpec, ...]:
        return canonicalize_pathway_specs(value)

    @model_validator(mode="after")
    def reject_unsupported_pathways(self) -> Self:
        _reject_organism_pathways(self.pathways)
        return self


class CompareKoSetsInput(FrozenModel):
    """Two or more labelled inline or retained datasets."""

    inputs: Annotated[tuple[CompareDatasetSource, ...], Field(min_length=2, max_length=10)]
    module_ids: Annotated[tuple[ModuleId, ...], Field(max_length=25)] = ()
    pathways: Annotated[tuple[PathwaySpec, ...], Field(max_length=25)] = ()
    allow_global_or_overview: bool = False

    @field_validator("pathways")
    @classmethod
    def canonicalize_pathways(cls, value: tuple[PathwaySpec, ...]) -> tuple[PathwaySpec, ...]:
        return canonicalize_pathway_specs(value)

    @model_validator(mode="after")
    def reject_unsupported_organism_context(self) -> Self:
        _reject_organism_pathways(self.pathways)
        return self


class GetServerStatusInput(FrozenModel):
    """No-argument status request with unknown fields rejected."""


class ProbeKeggConnectivityInput(FrozenModel):
    """Explicit no-argument request that authorizes one low-cost network probe."""


class DeleteAnalysisResultInput(FrozenModel):
    """One opaque current-session retained result selected for immediate deletion."""

    result_id: str = Field(pattern=RESULT_ID_SCHEMA_PATTERN)


class ListAnalysisResultsInput(FrozenModel):
    """Bounded current-session retained-result page."""

    offset: int = Field(default=0, strict=True, ge=0, le=1_000_000)
    limit: int = Field(default=50, strict=True, ge=1, le=100)


class ResultResourceIndex(FrozenModel):
    """Scoped retained-result metadata and validated section links."""

    result: ResultMetadata
    artifacts: tuple[ResultArtifactMetadata, ...]
    section_uris: tuple[str, ...]


class OversizedArtifactNotice(FrozenModel):
    """Safe response for a section too large to return in one resource read."""

    kind: Literal["artifact_requires_pagination"] = "artifact_requires_pagination"
    result_id: str = Field(pattern=RESULT_ID_SCHEMA_PATTERN)
    section: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    mime_type: str
    total_bytes: int = Field(strict=True, ge=0)
    next_uri: str
    maximum_range_bytes: int = Field(strict=True, gt=0)


class ArtifactRangeEnvelope(FrozenModel):
    """One bounded binary-safe artifact page with deterministic continuation."""

    result_id: str = Field(pattern=RESULT_ID_SCHEMA_PATTERN)
    section: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    mime_type: str
    total_bytes: int = Field(strict=True, ge=0)
    offset: int = Field(strict=True, ge=0)
    returned_bytes: int = Field(strict=True, ge=0)
    content_base64: str
    next_uri: str | None


class CacheInfoResource(FrozenModel):
    """Redacted cache facts without paths, endpoint URLs, or credentials."""

    access_mode: str
    cache_endpoint_class: Literal["public_academic", "licensed"]
    network_enabled: bool
    cache_configured: bool = True
    inspection_status: Literal["not_probed"] = "not_probed"
    entry_count: None = None
    stored_payload_bytes: None = None
    newest_entry_age_seconds: None = None
    location: Literal["user-local"] = "user-local"
    raw_payloads_exposed: bool = False


NormalizeToolEnvelope = ToolEnvelope[NormalizeAnnotationsResult]
EntriesToolEnvelope = ToolEnvelope[KeggEntriesServiceResult]
SearchEntriesToolEnvelope = ToolEnvelope[SearchKeggEntriesResult]
ResolveEntitiesToolEnvelope = ToolEnvelope[ResolveKeggEntitiesResult]
TraceRelationsToolEnvelope = ToolEnvelope[TraceKeggRelationsResult]
BriteHierarchyToolEnvelope = ToolEnvelope[MapBriteHierarchyResult]
AnnotationAuditToolEnvelope = ToolEnvelope[AnnotationMappingAuditResult]
AnalyzeKoAnnotationsToolEnvelope = ToolEnvelope[AnalyzeKoAnnotationsResult]
AnalyzeModulesToolEnvelope = ToolEnvelope[AnalyzeModulesResult]
AnalyzePathwaysToolEnvelope = ToolEnvelope[AnalyzePathwaysResult]
CompareToolEnvelope = ToolEnvelope[CompareKoSetsResult]
StatusToolEnvelope = ToolEnvelope[ServerStatusResult]
ConnectivityToolEnvelope = ToolEnvelope[ConnectivityProbeResult]
DeleteToolEnvelope = ToolEnvelope[DeletedResult]
ListResultsToolEnvelope = ToolEnvelope[ResultMetadataPage]


def constrain_mcp_input_schema(schema: dict[str, object]) -> None:
    """Add string bounds that Pydantic cannot express on datetime or scalar unions."""
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return
    definition_map = cast(dict[str, object], definitions)
    source_definition = definition_map.get("SourceProvenanceInput")
    if isinstance(source_definition, dict):
        properties = cast(dict[str, object], source_definition).get("properties")
        if isinstance(properties, dict):
            annotation_date = cast(dict[str, object], properties).get("annotation_date")
            if isinstance(annotation_date, dict):
                alternatives = cast(dict[str, object], annotation_date).get("anyOf")
                if isinstance(alternatives, list):
                    for alternative in cast(list[object], alternatives):
                        if isinstance(alternative, dict):
                            alternative_map = cast(dict[str, object], alternative)
                            if alternative_map.get("format") == "date-time":
                                alternative_map["maxLength"] = MAX_ANNOTATION_DATE_CHARACTERS
    evidence_definition = definition_map.get("EvidenceField")
    if isinstance(evidence_definition, dict):
        properties = cast(dict[str, object], evidence_definition).get("properties")
        if isinstance(properties, dict):
            value_schema = cast(dict[str, object], properties).get("value")
            if isinstance(value_schema, dict):
                alternatives = cast(dict[str, object], value_schema).get("anyOf")
                if isinstance(alternatives, list):
                    for alternative in cast(list[object], alternatives):
                        if isinstance(alternative, dict):
                            alternative_map = cast(dict[str, object], alternative)
                            if alternative_map.get("type") == "string":
                                alternative_map["maxLength"] = (
                                    DEFAULT_IMPORT_LIMITS.max_field_length
                                )


def constrain_mcp_output_schema(schema: dict[str, object]) -> None:
    """Expose MCP-known maxima for reused comparison preview definitions."""
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return
    definition_map = cast(dict[str, object], definitions)
    result_page = definition_map.get("ResultMetadataPage")
    if isinstance(result_page, dict):
        properties = cast(dict[str, object], result_page).get("properties")
        if isinstance(properties, dict):
            item_schema = cast(dict[str, object], properties).get("items")
            if isinstance(item_schema, dict):
                cast(dict[str, object], item_schema)["maxItems"] = 100
            limit_schema = cast(dict[str, object], properties).get("limit")
            if isinstance(limit_schema, dict):
                cast(dict[str, object], limit_schema)["maximum"] = 100
    maxima = {
        "KoPreview": {"ko_ids": 100},
        "KoClassComparisonSummary": {
            "set_specific": 10,
            "partially_shared_patterns_preview": 256,
        },
        "KoMembershipPatternPreview": {
            "member_set_indexes": 10,
            "member_labels": 10,
        },
        "ComparisonWarning": {"affected_input_indexes": 10},
    }
    for model_name, fields in maxima.items():
        definition = definition_map.get(model_name)
        if not isinstance(definition, dict):
            continue
        properties = cast(dict[str, object], definition).get("properties")
        if not isinstance(properties, dict):
            continue
        property_map = cast(dict[str, object], properties)
        for field_name, maximum in fields.items():
            property_schema = property_map.get(field_name)
            if isinstance(property_schema, dict):
                cast(dict[str, object], property_schema)["maxItems"] = maximum
    membership_definition = definition_map.get("KoMembershipPatternPreview")
    if isinstance(membership_definition, dict):
        properties = cast(dict[str, object], membership_definition).get("properties")
        if isinstance(properties, dict):
            member_labels = cast(dict[str, object], properties).get("member_labels")
            if isinstance(member_labels, dict):
                items = cast(dict[str, object], member_labels).get("items")
                if isinstance(items, dict):
                    cast(dict[str, object], items)["maxLength"] = 128


def _reject_organism_pathways(pathways: tuple[PathwaySpec, ...]) -> None:
    if any(item.reference_namespace is PathwayReferenceNamespace.ORGANISM for item in pathways):
        raise ValueError(
            "organism pathway references require gene-level context and are not accepted by "
            "KO-only MCP inputs"
        )


__all__ = [
    "AnalyzeKoAnnotationsInput",
    "AnalyzeKoAnnotationsToolEnvelope",
    "AnalyzeModulesInput",
    "AnalyzeModulesToolEnvelope",
    "AnalyzePathwaysInput",
    "AnalyzePathwaysToolEnvelope",
    "AnnotationAuditToolEnvelope",
    "ArtifactRangeEnvelope",
    "AuditAnnotationMappingInput",
    "BriteHierarchyToolEnvelope",
    "CacheInfoResource",
    "CompareKoSetsInput",
    "CompareToolEnvelope",
    "ConnectivityToolEnvelope",
    "DeleteAnalysisResultInput",
    "DeleteToolEnvelope",
    "EntriesToolEnvelope",
    "GetKeggEntriesInput",
    "GetServerStatusInput",
    "ListAnalysisResultsInput",
    "ListResultsToolEnvelope",
    "MapBriteHierarchyInput",
    "NormalizeAnnotationsRequest",
    "NormalizeKoAnnotationsInput",
    "NormalizeToolEnvelope",
    "OversizedArtifactNotice",
    "ProbeKeggConnectivityInput",
    "ResolveEntitiesToolEnvelope",
    "ResolveKeggEntitiesInput",
    "ResultResourceIndex",
    "SearchEntriesToolEnvelope",
    "SearchKeggEntriesInput",
    "StatusToolEnvelope",
    "ToolEnvelope",
    "ToolPayload",
    "TraceKeggRelationsInput",
    "TraceRelationsToolEnvelope",
    "constrain_mcp_input_schema",
    "constrain_mcp_output_schema",
]
