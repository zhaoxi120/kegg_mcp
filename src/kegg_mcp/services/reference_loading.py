"""Typed, bounded loading of KEGG references for service orchestration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, NoReturn, Protocol, Self, cast

from pydantic import ConfigDict, Field, ValidationError, model_validator

from kegg_mcp.analysis.contracts import (
    ModuleAnalysisLimits,
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleDefinitionOrigin,
    ModuleDefinitionProvenance,
    ModuleReferenceIssueKind,
    ResolvedModuleGraph,
)
from kegg_mcp.analysis.module_resolution import resolve_module_definitions
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageLimits,
    PathwayKoReference,
    PathwayReferenceNamespace,
    build_pathway_reference,
)
from kegg_mcp.domain.annotations import JSON_SCHEMA_DIALECT, FrozenModel
from kegg_mcp.domain.errors import ErrorCode, SafeDetail, fail
from kegg_mcp.execution import ReferenceLoadingLimits
from kegg_mcp.kegg.contracts import (
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggEntryRef,
    KeggFlatFileDocument,
    KeggFlatFileEntry,
    KeggFlatFileField,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggOperation,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    ResponseOrigin,
    is_kegg_organism_code,
    is_kegg_pathway_identifier,
)

ModuleId = Annotated[str, Field(pattern=r"^M[0-9]{5}$")]
_MAX_MODULE_GET_ENTRIES = 10


class KeggReferenceClient(Protocol):
    """Minimal injected client surface needed to load analysis references."""

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        """Return one typed GET result."""
        ...

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        """Return one typed LINK result."""
        ...


class PathwaySpec(FrozenModel):
    """One KEGG pathway with namespace inferred from a single identifier."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-reference-spec:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    pathway_id: str = Field(min_length=7, max_length=9)
    reference_namespace: PathwayReferenceNamespace = PathwayReferenceNamespace.KO

    @model_validator(mode="before")
    @classmethod
    def infer_namespace(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        supplied = dict(cast(Mapping[str, object], value))
        pathway_id = supplied.get("pathway_id")
        if not isinstance(pathway_id, str) or "reference_namespace" in supplied:
            return supplied
        prefix = pathway_id[:-5]
        if prefix in {"ko", "map"}:
            supplied["pathway_id"] = f"ko{pathway_id[-5:]}"
            supplied["reference_namespace"] = PathwayReferenceNamespace.KO
        elif is_kegg_organism_code(prefix):
            supplied["reference_namespace"] = PathwayReferenceNamespace.ORGANISM
        return supplied

    @model_validator(mode="after")
    def require_matching_namespace(self) -> Self:
        if not is_kegg_pathway_identifier(self.pathway_id):
            raise ValueError("pathway_id must use supported KEGG pathway syntax")
        prefix = self.pathway_id[:-5]
        if self.reference_namespace is PathwayReferenceNamespace.KO:
            matches = prefix == "ko"
        elif self.reference_namespace is PathwayReferenceNamespace.MAP:
            matches = prefix == "map"
        else:
            matches = is_kegg_organism_code(prefix)
        if not matches:
            raise ValueError("pathway_id is incompatible with reference_namespace")
        return self

    @property
    def pathway_number(self) -> str:
        """Return the shared five-digit pathway number used for deduplication."""
        return self.pathway_id[-5:]

    @property
    def namespace(self) -> str:
        """Return the inferred namespace as a stable serialized string."""
        return self.reference_namespace.value

    @property
    def paired_reference_id(self) -> str | None:
        """Return the paired ko/map view when the reference has one."""
        if self.reference_namespace is PathwayReferenceNamespace.KO:
            return f"map{self.pathway_number}"
        if self.reference_namespace is PathwayReferenceNamespace.MAP:
            return f"ko{self.pathway_number}"
        return None


def load_module_graphs(
    client: KeggReferenceClient,
    module_ids: tuple[ModuleId, ...],
    *,
    options: KeggRequestOptions,
    limits: ReferenceLoadingLimits | None = None,
    analysis_limits: ModuleAnalysisLimits | None = None,
) -> tuple[ResolvedModuleGraph, ...]:
    """Load roots and reachable MODULE references, then resolve ordered graphs."""
    bounds = limits or ReferenceLoadingLimits()
    resolver_bounds = analysis_limits or ModuleAnalysisLimits()
    roots = _validate_module_roots(module_ids, bounds)

    definitions: dict[str, ModuleDefinition] = {}
    attempted: set[str] = set()
    pending = roots
    rounds = 0
    total_response_bytes = 0
    total_requests = 0
    retrieval_batches: list[KeggBatchProvenance] = []

    while pending:
        if rounds >= bounds.max_module_rounds:
            _fail_limit(
                "module_retrieval_rounds",
                rounds + 1,
                "max_module_rounds",
                bounds.max_module_rounds,
            )
        projected_entries = len(attempted) + len(pending)
        if projected_entries > bounds.max_module_entries:
            _fail_limit(
                "module_entries",
                projected_entries,
                "max_module_entries",
                bounds.max_module_entries,
            )

        loaded: dict[str, ModuleDefinition] = {}
        for start in range(0, len(pending), _MAX_MODULE_GET_ENTRIES):
            chunk = pending[start : start + _MAX_MODULE_GET_ENTRIES]
            request = GetRequest(
                entries=tuple(
                    KeggEntryRef(database=KeggGetDatabase.MODULE, identifier=module_id)
                    for module_id in chunk
                )
            )
            total_requests += 1
            _require_aggregate_within_limit(
                "total_kegg_requests",
                total_requests,
                "max_total_kegg_requests",
                bounds.max_total_kegg_requests,
            )
            result = client.get(request, options=options)
            retrieval_batches.extend(result.batches)
            total_response_bytes += _response_bytes(result.batches)
            _require_aggregate_within_limit(
                "total_response_bytes",
                total_response_bytes,
                "max_total_response_bytes",
                bounds.max_total_response_bytes,
            )
            loaded.update(_module_definitions_from_result(result, request))
        attempted.update(pending)
        for module_id in pending:
            definition = loaded.get(module_id)
            if definition is not None:
                definitions[module_id] = definition
        rounds += 1

        missing_roots = tuple(module_id for module_id in roots if module_id not in definitions)
        if missing_roots:
            fail(
                ErrorCode.KEGG_ENTRY_NOT_FOUND,
                "One or more requested root MODULE entries are missing.",
                suggested_action="Verify the root MODULE identifiers or refresh KEGG data.",
                safe_details=(
                    SafeDetail(name="first_missing_module_id", value=missing_roots[0]),
                    SafeDetail(name="missing_root_count", value=str(len(missing_roots))),
                ),
            )

        graphs = _resolve_graphs(roots, definitions, resolver_bounds)
        reference_keys = {
            (
                edge.source_module_id,
                edge.target_module_id,
                edge.source_span.start_offset,
                edge.source_span.end_offset,
            )
            for graph in graphs
            for edge in graph.edges
        }
        if len(reference_keys) > bounds.max_module_reference_occurrences:
            _fail_limit(
                "module_reference_occurrences",
                len(reference_keys),
                "max_module_reference_occurrences",
                bounds.max_module_reference_occurrences,
            )

        next_pending: list[str] = []
        seen_pending: set[str] = set()
        for graph in graphs:
            for issue in graph.issues:
                target_id = issue.target_module_id
                if (
                    issue.kind is ModuleReferenceIssueKind.UNRESOLVED
                    and target_id not in attempted
                    and target_id not in seen_pending
                ):
                    next_pending.append(target_id)
                    seen_pending.add(target_id)
        pending = tuple(next_pending)

    return tuple(
        _attach_module_retrieval_provenance(graph, tuple(retrieval_batches))
        for graph in _resolve_graphs(roots, definitions, resolver_bounds)
    )


def load_pathway_references(
    client: KeggReferenceClient,
    specs: tuple[PathwaySpec, ...],
    *,
    options: KeggRequestOptions,
    limits: ReferenceLoadingLimits | None = None,
    pathway_limits: PathwayCoverageLimits | None = None,
) -> tuple[PathwayKoReference, ...]:
    """Load ordered PATHWAY_TO_KO and metadata pairs through typed operations."""
    bounds = limits or ReferenceLoadingLimits()
    reference_bounds = pathway_limits or PathwayCoverageLimits()
    _validate_pathway_specs(specs, bounds)

    references: list[PathwayKoReference] = []
    total_response_bytes = 0
    total_relationship_rows = 0
    total_reference_kos = 0
    total_reference_exclusions = 0
    total_requests = 0
    for spec in specs:
        link_request = LinkRequest(
            relationship=KeggLinkRelationship.PATHWAY_TO_KO,
            source_identifiers=(spec.pathway_id,),
        )
        total_requests += 1
        _require_aggregate_within_limit(
            "total_kegg_requests",
            total_requests,
            "max_total_kegg_requests",
            bounds.max_total_kegg_requests,
        )
        link_result = client.link(link_request, options=options)
        total_response_bytes += _response_bytes(link_result.batches)
        _require_aggregate_within_limit(
            "total_response_bytes",
            total_response_bytes,
            "max_total_response_bytes",
            bounds.max_total_response_bytes,
        )
        _require_aggregate_within_limit(
            "pathway_relationship_rows",
            len(link_result.rows),
            "pathway_limits.max_relationship_rows",
            reference_bounds.max_relationship_rows,
        )
        total_relationship_rows += len(link_result.rows)
        _require_aggregate_within_limit(
            "total_pathway_relationship_rows",
            total_relationship_rows,
            "max_total_pathway_relationship_rows",
            bounds.max_total_pathway_relationship_rows,
        )
        get_request = GetRequest(
            entries=(
                KeggEntryRef(
                    database=KeggGetDatabase.PATHWAY,
                    identifier=spec.pathway_id,
                ),
            )
        )
        total_requests += 1
        _require_aggregate_within_limit(
            "total_kegg_requests",
            total_requests,
            "max_total_kegg_requests",
            bounds.max_total_kegg_requests,
        )
        get_result = client.get(get_request, options=options)
        total_response_bytes += _response_bytes(get_result.batches)
        _require_aggregate_within_limit(
            "total_response_bytes",
            total_response_bytes,
            "max_total_response_bytes",
            bounds.max_total_response_bytes,
        )
        reference = build_pathway_reference(
            link_result,
            get_result,
            spec.reference_namespace,
            limits=reference_bounds,
        )
        total_reference_kos += len(reference.reference_kos)
        _require_aggregate_within_limit(
            "total_pathway_reference_kos",
            total_reference_kos,
            "max_total_pathway_reference_kos",
            bounds.max_total_pathway_reference_kos,
        )
        total_reference_exclusions += len(reference.exclusions)
        _require_aggregate_within_limit(
            "total_pathway_reference_exclusions",
            total_reference_exclusions,
            "max_total_pathway_reference_exclusions",
            bounds.max_total_pathway_reference_exclusions,
        )
        references.append(reference)
    return tuple(references)


def _validate_module_roots(
    module_ids: tuple[ModuleId, ...],
    limits: ReferenceLoadingLimits,
) -> tuple[str, ...]:
    if not module_ids:
        _fail_configuration(
            "At least one root MODULE identifier is required.",
            "Supply a non-empty tuple of unique M-number identifiers.",
        )
    if len(module_ids) > limits.max_module_roots:
        _fail_limit(
            "module_root_count",
            len(module_ids),
            "max_module_roots",
            limits.max_module_roots,
        )
    if len(module_ids) != len(set(module_ids)):
        _fail_configuration(
            "Root MODULE identifiers must be unique.",
            "Remove duplicate root identifiers while preserving caller order.",
        )
    try:
        entries = tuple(
            KeggEntryRef(database=KeggGetDatabase.MODULE, identifier=module_id)
            for module_id in module_ids
        )
    except ValidationError:
        _fail_configuration(
            "A root MODULE identifier is invalid.",
            "Use M followed by exactly five ASCII digits.",
        )
    return tuple(entry.identifier for entry in entries)


def _validate_pathway_specs(
    specs: tuple[PathwaySpec, ...],
    limits: ReferenceLoadingLimits,
) -> None:
    if not specs:
        _fail_configuration(
            "At least one pathway reference specification is required.",
            "Supply a non-empty tuple of unique PathwaySpec values.",
        )
    if len(specs) > limits.max_pathway_specs:
        _fail_limit(
            "pathway_spec_count",
            len(specs),
            "max_pathway_specs",
            limits.max_pathway_specs,
        )
    pathway_numbers = tuple(spec.pathway_number for spec in specs)
    if len(pathway_numbers) != len(set(pathway_numbers)):
        _fail_configuration(
            "Pathway reference specifications must use unique pathway numbers.",
            "Remove duplicate ko/map views while preserving caller order.",
        )


def _module_definitions_from_result(
    result: GetResult,
    request: GetRequest,
) -> dict[str, ModuleDefinition]:
    if result.request != request:
        _fail_parse("The MODULE GET result does not match the typed request.")
    if len(result.documents) != len(result.batches):
        _fail_parse("MODULE GET documents and provenance batches do not align.")
    if any(batch.operation is not KeggOperation.GET for batch in result.batches):
        _fail_parse("MODULE GET results contain non-GET provenance.")

    requested_ids = tuple(entry.identifier for entry in request.entries)
    requested_set = set(requested_ids)
    returned: dict[str, ModuleDefinition] = {}
    for document, batch in zip(result.documents, result.batches, strict=True):
        if not isinstance(document, KeggFlatFileDocument):
            _fail_parse("MODULE GET must return flat-file documents.")
        for entry in document.entries:
            if entry.identifier not in requested_set or entry.identifier in returned:
                _fail_parse("MODULE GET returned an unexpected or duplicate entry.")
            returned[entry.identifier] = _module_definition_from_entry(entry, batch)

    missing_ids: list[str] = []
    for entry in result.missing_entries:
        if entry.database is not KeggGetDatabase.MODULE or entry.identifier not in requested_set:
            _fail_parse("MODULE GET reported an unexpected missing entry.")
        if entry.identifier in missing_ids:
            _fail_parse("MODULE GET reported a duplicate missing entry.")
        missing_ids.append(entry.identifier)
    if set(returned).intersection(missing_ids):
        _fail_parse("MODULE GET reported one entry as both returned and missing.")
    if set(returned).union(missing_ids) != requested_set:
        _fail_parse("MODULE GET did not account for every requested entry.")
    return returned


def _module_definition_from_entry(
    entry: KeggFlatFileEntry,
    batch: KeggBatchProvenance,
) -> ModuleDefinition:
    module_id = entry.identifier
    entry_text = _required_module_field(entry, "ENTRY", module_id, single_line=True)
    if entry_text.split()[0] != module_id:
        _fail_parse("The MODULE ENTRY field does not match its parsed identifier.", module_id)
    module_name = _required_module_field(entry, "NAME", module_id)
    definition = _required_module_field(entry, "DEFINITION", module_id)
    provenance = ModuleDefinitionProvenance(
        origin=(
            ModuleDefinitionOrigin.KEGG_NETWORK
            if batch.origin is ResponseOrigin.NETWORK
            else ModuleDefinitionOrigin.KEGG_CACHE
        ),
        retrieval=batch,
    )
    try:
        return ModuleDefinition.from_text(
            module_id=module_id,
            module_name=module_name,
            definition=definition,
            provenance=provenance,
        )
    except ValidationError:
        _fail_parse("The MODULE fields exceed the supported typed contract.", module_id)


def _required_module_field(
    entry: KeggFlatFileEntry,
    field_name: str,
    module_id: str,
    *,
    single_line: bool = False,
) -> str:
    fields: Sequence[KeggFlatFileField] = tuple(
        field for field in entry.fields if field.indent_columns == 0 and field.name == field_name
    )
    if len(fields) != 1:
        _fail_parse(
            f"The MODULE flat file must contain exactly one top-level {field_name} field.",
            module_id,
        )
    lines = tuple(line.strip() for line in fields[0].value_lines)
    if not lines or any(not line for line in lines) or (single_line and len(lines) != 1):
        _fail_parse(
            f"The MODULE top-level {field_name} field is malformed.",
            module_id,
        )
    return " ".join(lines)


def _resolve_graphs(
    roots: tuple[str, ...],
    definitions: dict[str, ModuleDefinition],
    limits: ModuleAnalysisLimits,
) -> tuple[ResolvedModuleGraph, ...]:
    supplied = tuple(definitions.values())
    return tuple(
        resolve_module_definitions(
            ModuleDefinitionCollection(
                root_module_id=root_module_id,
                definitions=supplied,
            ),
            limits,
        )
        for root_module_id in roots
    )


def _attach_module_retrieval_provenance(
    graph: ResolvedModuleGraph,
    batches: tuple[KeggBatchProvenance, ...],
) -> ResolvedModuleGraph:
    return ResolvedModuleGraph(
        root_module_id=graph.root_module_id,
        modules=graph.modules,
        edges=graph.edges,
        issues=graph.issues,
        retrieval_provenance=batches,
        total_ast_nodes=graph.total_ast_nodes,
        resolver_version=graph.resolver_version,
        limits=graph.limits,
    )


def _response_bytes(batches: tuple[KeggBatchProvenance, ...]) -> int:
    return sum(batch.response_bytes for batch in batches)


def _require_aggregate_within_limit(
    metric: str,
    observed: int,
    limit_name: str,
    limit: int,
) -> None:
    if observed > limit:
        _fail_limit(metric, observed, limit_name, limit)


def _fail_configuration(message: str, suggested_action: str) -> NoReturn:
    fail(
        ErrorCode.ANALYSIS_CONFIGURATION_INVALID,
        message,
        suggested_action=suggested_action,
    )


def _fail_limit(metric: str, observed: int, limit_name: str, limit: int) -> NoReturn:
    fail(
        ErrorCode.INPUT_LIMIT_EXCEEDED,
        "Reference loading exceeded a configured hard limit.",
        suggested_action="Reduce the request or raise the relevant bounded service limit.",
        safe_details=(
            SafeDetail(name="metric", value=metric),
            SafeDetail(name="observed", value=str(observed)),
            SafeDetail(name="limit_name", value=limit_name),
            SafeDetail(name="limit", value=str(limit)),
        ),
    )


def _fail_parse(message: str, module_id: str | None = None) -> NoReturn:
    details = () if module_id is None else (SafeDetail(name="module_id", value=module_id),)
    fail(
        ErrorCode.KEGG_PARSE_FAILED,
        message,
        suggested_action="Refresh the exact typed KEGG response and retry.",
        safe_details=details,
    )


__all__ = [
    "KeggReferenceClient",
    "PathwaySpec",
    "ReferenceLoadingLimits",
    "load_module_graphs",
    "load_pathway_references",
]
