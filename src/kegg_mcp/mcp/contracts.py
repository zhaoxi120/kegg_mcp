"""Explicit MCP tool and resource contracts."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Generic, Literal, Self, TypeVar, cast

from pydantic import Field, field_validator, model_validator

from kegg_mcp.analysis.pathway_coverage import PathwayReferenceNamespace
from kegg_mcp.analysis.pathway_ranking import PathwaySelection
from kegg_mcp.domain.annotations import AnalysisUnit, EvidenceMode, FrozenModel
from kegg_mcp.domain.errors import ErrorDetail
from kegg_mcp.importers import GenericColumnMapping, SourceProvenanceInput
from kegg_mcp.importers.contracts import MAX_ANNOTATION_DATE_CHARACTERS
from kegg_mcp.kegg import KeggEntryRef
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
    KoMappingServiceResult,
    NormalizeAnnotationsRequest,
    NormalizeAnnotationsResult,
    ServerStatusResult,
)
from kegg_mcp.services.output_bundle import ManifestPathMode
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    canonicalize_pathway_specs,
)
from kegg_mcp.services.result_store import (
    DeletedResult,
    ResultArtifactMetadata,
    ResultMetadata,
    ResultMetadataPage,
)

T = TypeVar("T")
ModuleId = Annotated[str, Field(pattern=r"^M[0-9]{5}$")]
KoId = Annotated[str, Field(pattern=r"^K[0-9]{5}$")]
ResultUri = Annotated[str, Field(pattern=r"^ko-analysis://results/res_[A-Za-z0-9_-]{32}$")]


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
    output_directory: str | None = Field(default=None, min_length=1, max_length=4_096)
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
    annotations: NormalizeKoAnnotationsInput | None = None
    module_ids: Annotated[tuple[ModuleId, ...], Field(max_length=25)] = ()
    pathways: Annotated[tuple[PathwaySpec, ...], Field(max_length=25)] = ()
    pathway_selection: PathwaySelection | None = None
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN
    sample_id: str = Field(default="sample-1", min_length=1, max_length=256)
    pathway_evidence_mode: EvidenceMode = EvidenceMode.STRICT
    allow_global_or_overview: bool = False
    output_directory: str | None = Field(default=None, min_length=1, max_length=4_096)

    @field_validator("pathways")
    @classmethod
    def canonicalize_pathways(cls, value: tuple[PathwaySpec, ...]) -> tuple[PathwaySpec, ...]:
        return canonicalize_pathway_specs(value)

    @model_validator(mode="after")
    def validate_common_path(self) -> Self:
        if (self.ko_text is None) == (self.annotations is None):
            raise ValueError("provide exactly one of ko_text or annotations")
        if self.annotations is not None and (
            self.analysis_unit is not AnalysisUnit.UNKNOWN or self.sample_id != "sample-1"
        ):
            raise ValueError(
                "analysis_unit and sample_id must be set inside annotations when annotations "
                "is supplied"
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


class KoMappingTarget(StrEnum):
    """Approved KO relationship targets; unrestricted gene expansion is absent."""

    PATHWAY = "pathway"
    MODULE = "module"
    REACTION = "reaction"
    EC = "ec"
    BRITE = "brite"


class MapKoIdsInput(FrozenModel):
    """Bounded selected-KO mapping input."""

    ko_ids: Annotated[tuple[KoId, ...], Field(min_length=1, max_length=100)]
    target: KoMappingTarget

    @model_validator(mode="after")
    def require_unique_kos(self) -> Self:
        if len(self.ko_ids) != len(set(self.ko_ids)):
            raise ValueError("ko_ids must be unique")
        return self


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

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")


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
    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")
    section: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    mime_type: str
    total_bytes: int = Field(strict=True, ge=0)
    next_uri: str
    maximum_range_bytes: int = Field(strict=True, gt=0)


class ArtifactRangeEnvelope(FrozenModel):
    """One bounded binary-safe artifact page with deterministic continuation."""

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")
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
MappingToolEnvelope = ToolEnvelope[KoMappingServiceResult]
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
    "ArtifactRangeEnvelope",
    "CacheInfoResource",
    "CompareKoSetsInput",
    "CompareToolEnvelope",
    "ConnectivityToolEnvelope",
    "DeleteAnalysisResultInput",
    "DeleteToolEnvelope",
    "EntriesToolEnvelope",
    "GetKeggEntriesInput",
    "GetServerStatusInput",
    "KoMappingTarget",
    "ListAnalysisResultsInput",
    "ListResultsToolEnvelope",
    "MapKoIdsInput",
    "MappingToolEnvelope",
    "NormalizeAnnotationsRequest",
    "NormalizeKoAnnotationsInput",
    "NormalizeToolEnvelope",
    "OversizedArtifactNotice",
    "ProbeKeggConnectivityInput",
    "ResultResourceIndex",
    "StatusToolEnvelope",
    "ToolEnvelope",
    "ToolPayload",
    "constrain_mcp_input_schema",
    "constrain_mcp_output_schema",
]
