"""Explicit MCP tool and resource contracts."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Generic, Literal, Self, TypeVar, cast

from pydantic import BaseModel, Field, model_validator

from kegg_mcp.analysis import (
    ComparisonLimits,
    ComparisonPreviewLimits,
    FunctionalComparisonLimits,
    ModuleAnalysisLimits,
)
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageLimits,
    PathwayReferenceNamespace,
)
from kegg_mcp.domain.annotations import AnalysisUnit, EvidenceMode, FrozenModel
from kegg_mcp.domain.errors import ErrorDetail
from kegg_mcp.importers.contracts import MAX_ANNOTATION_DATE_CHARACTERS
from kegg_mcp.kegg import KeggEntryRef, KeggRequestOptions
from kegg_mcp.services import (
    DEFAULT_IMPORT_LIMITS,
    CompareDatasetSource,
    CompareKoSetsResult,
    DatasetSource,
    KeggEntriesServiceResult,
    KoMappingServiceResult,
    NormalizeAnnotationsRequest,
    NormalizeAnnotationsResult,
    PathwaySpec,
    PrimitiveAnalysisResult,
    ReferenceLoadingLimits,
    ResultArtifactMetadata,
    ResultMetadata,
    ServerStatusResult,
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


class AnalyzeKoAnnotationsInput(FrozenModel):
    """Simple common-path KO analysis input."""

    ko_text: str | None = Field(default=None, min_length=1, max_length=5_000_000)
    annotations: NormalizeAnnotationsRequest | None = None
    module_ids: Annotated[tuple[ModuleId, ...], Field(max_length=25)] = ()
    pathways: Annotated[tuple[PathwaySpec, ...], Field(max_length=25)] = ()
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN
    sample_id: str = Field(default="sample-1", min_length=1, max_length=256)
    pathway_evidence_mode: EvidenceMode = EvidenceMode.STRICT
    allow_global_or_overview: bool = False
    refresh: bool = False
    allow_stale: bool = False

    @model_validator(mode="after")
    def require_targets(self) -> Self:
        if (self.ko_text is None) == (self.annotations is None):
            raise ValueError("provide exactly one of ko_text or annotations")
        if self.annotations is not None and (
            self.analysis_unit is not AnalysisUnit.UNKNOWN or self.sample_id != "sample-1"
        ):
            raise ValueError(
                "analysis_unit and sample_id must be set inside annotations when annotations "
                "is supplied"
            )
        if self.annotations is not None and (
            self.annotations.preview_limit != 20 or self.annotations.diagnostic_preview_limit != 20
        ):
            raise ValueError(
                "preview_limit and diagnostic_preview_limit are not used by the high-level "
                "analysis tool"
            )
        if not self.module_ids and not self.pathways:
            raise ValueError("at least one MODULE or pathway target is required")
        _reject_organism_pathways(self.pathways)
        return self


class GetKeggEntriesInput(FrozenModel):
    """Bounded allowlisted GET request; never an arbitrary URL."""

    entries: Annotated[tuple[KeggEntryRef, ...], Field(min_length=1, max_length=50)]
    refresh: bool = False
    allow_stale: bool = False


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
    refresh: bool = False
    allow_stale: bool = False
    preview_limit: int = Field(default=100, strict=True, ge=0, le=200)

    @model_validator(mode="after")
    def require_unique_kos(self) -> Self:
        if len(self.ko_ids) != len(set(self.ko_ids)):
            raise ValueError("ko_ids must be unique")
        return self


class AnalyzeModulesInput(FrozenModel):
    """MODULE targets evaluated against inline or retained evidence."""

    source: DatasetSource
    module_ids: Annotated[tuple[ModuleId, ...], Field(min_length=1, max_length=25)]
    refresh: bool = False
    allow_stale: bool = False
    reference_limits: ReferenceLoadingLimits = Field(default_factory=ReferenceLoadingLimits)
    analysis_limits: ModuleAnalysisLimits = Field(default_factory=ModuleAnalysisLimits)

    @model_validator(mode="after")
    def require_unique_modules(self) -> Self:
        if len(self.module_ids) != len(set(self.module_ids)):
            raise ValueError("module_ids must be unique")
        _require_default_bounded(
            self.reference_limits, ReferenceLoadingLimits(), "reference_limits"
        )
        _require_default_bounded(self.analysis_limits, ModuleAnalysisLimits(), "analysis_limits")
        return self


class AnalyzePathwaysInput(FrozenModel):
    """Descriptive pathway coverage targets with explicit namespaces."""

    source: DatasetSource
    pathways: Annotated[tuple[PathwaySpec, ...], Field(min_length=1, max_length=25)]
    evidence_mode: EvidenceMode = EvidenceMode.STRICT
    allow_global_or_overview: bool = False
    refresh: bool = False
    allow_stale: bool = False
    reference_limits: ReferenceLoadingLimits = Field(default_factory=ReferenceLoadingLimits)
    pathway_limits: PathwayCoverageLimits = Field(default_factory=PathwayCoverageLimits)

    @model_validator(mode="after")
    def require_unique_pathways(self) -> Self:
        identifiers = tuple(item.pathway_id for item in self.pathways)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("pathways must be unique")
        _reject_organism_pathways(self.pathways)
        _require_default_bounded(
            self.reference_limits, ReferenceLoadingLimits(), "reference_limits"
        )
        _require_default_bounded(self.pathway_limits, PathwayCoverageLimits(), "pathway_limits")
        return self


class CompareKoSetsInput(FrozenModel):
    """Two or more labelled inline or retained datasets."""

    inputs: Annotated[tuple[CompareDatasetSource, ...], Field(min_length=2, max_length=10)]
    module_ids: Annotated[tuple[ModuleId, ...], Field(max_length=25)] = ()
    pathways: Annotated[tuple[PathwaySpec, ...], Field(max_length=25)] = ()
    refresh: bool = False
    allow_stale: bool = False
    allow_global_or_overview: bool = False
    reference_limits: ReferenceLoadingLimits = Field(default_factory=ReferenceLoadingLimits)
    module_limits: ModuleAnalysisLimits = Field(default_factory=ModuleAnalysisLimits)
    pathway_limits: PathwayCoverageLimits = Field(default_factory=PathwayCoverageLimits)
    functional_limits: FunctionalComparisonLimits = Field(
        default_factory=FunctionalComparisonLimits
    )
    limits: ComparisonLimits = Field(default_factory=ComparisonLimits)
    preview_limits: ComparisonPreviewLimits = Field(default_factory=ComparisonPreviewLimits)

    @model_validator(mode="after")
    def reject_unsupported_organism_context(self) -> Self:
        _reject_organism_pathways(self.pathways)
        for value, default, name in (
            (self.reference_limits, ReferenceLoadingLimits(), "reference_limits"),
            (self.module_limits, ModuleAnalysisLimits(), "module_limits"),
            (self.pathway_limits, PathwayCoverageLimits(), "pathway_limits"),
            (self.functional_limits, FunctionalComparisonLimits(), "functional_limits"),
            (self.limits, ComparisonLimits(), "limits"),
            (self.preview_limits, ComparisonPreviewLimits(), "preview_limits"),
        ):
            _require_default_bounded(value, default, name)
        return self


class GetServerStatusInput(FrozenModel):
    """No-argument status request with unknown fields rejected."""


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
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    next_uri: str
    maximum_range_bytes: int = Field(strict=True, gt=0)


class ArtifactRangeEnvelope(FrozenModel):
    """One bounded binary-safe artifact page with deterministic continuation."""

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9_-]{32}$")
    section: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    mime_type: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
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
PrimitiveAnalysisToolEnvelope = ToolEnvelope[PrimitiveAnalysisResult]
CompareToolEnvelope = ToolEnvelope[CompareKoSetsResult]
StatusToolEnvelope = ToolEnvelope[ServerStatusResult]


def options(refresh: bool, allow_stale: bool) -> KeggRequestOptions:
    """Build the typed cache policy shared by transport adapters."""
    return KeggRequestOptions(refresh=refresh, allow_stale=allow_stale)


def constrain_mcp_input_schema(schema: dict[str, object]) -> None:
    """Advertise the same hard maxima enforced by the MCP request validators."""
    module_limits = ModuleAnalysisLimits()
    bounded_models = (
        DEFAULT_IMPORT_LIMITS,
        ReferenceLoadingLimits(),
        module_limits.parsing,
        module_limits.resolution,
        module_limits.evaluation,
        PathwayCoverageLimits(),
        FunctionalComparisonLimits(),
        ComparisonLimits(),
        ComparisonPreviewLimits(),
    )
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):
        return
    definition_map = cast(dict[str, object], definitions)
    for model in bounded_models:
        definition = definition_map.get(type(model).__name__)
        if not isinstance(definition, dict):
            continue
        properties = cast(dict[str, object], definition).get("properties")
        if not isinstance(properties, dict):
            continue
        property_map = cast(dict[str, object], properties)
        for field_name, maximum in model.model_dump(mode="python").items():
            if not isinstance(maximum, (int, float)) or isinstance(maximum, bool):
                continue
            property_schema = property_map.get(field_name)
            if not isinstance(property_schema, dict):
                continue
            cast(dict[str, object], property_schema)["maximum"] = maximum
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


def _require_default_bounded(value: BaseModel, default: BaseModel, prefix: str) -> None:
    _compare_limit_values(
        value.model_dump(mode="python"),
        default.model_dump(mode="python"),
        prefix,
    )


def _compare_limit_values(value: object, default: object, path: str) -> None:
    if isinstance(value, Mapping) and isinstance(default, Mapping):
        value_map = cast(Mapping[str, object], value)
        default_map = cast(Mapping[str, object], default)
        for key, nested in value_map.items():
            _compare_limit_values(nested, default_map[key], f"{path}.{key}")
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isinstance(default, (int, float))
        and not isinstance(default, bool)
        and value > default
    ):
        raise ValueError(f"{path} exceeds the MCP server-approved default bound")


__all__ = [
    "AnalyzeKoAnnotationsInput",
    "AnalyzeModulesInput",
    "AnalyzePathwaysInput",
    "ArtifactRangeEnvelope",
    "CacheInfoResource",
    "CompareKoSetsInput",
    "CompareToolEnvelope",
    "EntriesToolEnvelope",
    "GetKeggEntriesInput",
    "GetServerStatusInput",
    "KoMappingTarget",
    "MapKoIdsInput",
    "MappingToolEnvelope",
    "NormalizeAnnotationsRequest",
    "NormalizeToolEnvelope",
    "OversizedArtifactNotice",
    "PrimitiveAnalysisToolEnvelope",
    "ResultResourceIndex",
    "StatusToolEnvelope",
    "ToolEnvelope",
    "ToolPayload",
    "constrain_mcp_input_schema",
    "constrain_mcp_output_schema",
    "options",
]
