"""Tests for the complete bounded renderer handoff contract."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis import (
    MODULE_RANKING_METHOD,
    MODULE_RANKING_VERSION,
    PATHWAY_RANKING_METHOD,
    PATHWAY_RANKING_VERSION,
    ModuleAnalysisLimits,
    ModuleBlockState,
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleEvaluationLimits,
    ModuleEvaluationResult,
    ModuleSelection,
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    PathwayCoverageResult,
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    PathwaySelection,
    ResolvedModuleGraph,
    evaluate_module,
    evaluate_pathway_coverage,
    resolve_module_definitions,
)
from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS,
    AnnotationDataset,
    KoAnalysisView,
    NormalizedStatus,
    build_ko_analysis_view,
)
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.execution import (
    ANALYSIS_SERVICE_NAME,
    ANALYSIS_SERVICE_VERSION,
    AnalysisExecutionProvenance,
    AnalysisServiceLimits,
    ModuleRankingExecution,
    PathwayExecutionParameters,
    PathwayRankingExecution,
)
from kegg_mcp.importers import (
    AnalysisViewImportLimits,
    GenericColumnMapping,
    ImportLimits,
    TableDialect,
    import_generic_table,
)
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggOperation,
    KeggRequestOptions,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services.output_bundle import write_analysis_bundle
from kegg_mcp.services.reference_loading import ReferenceLoadingLimits
from kegg_mcp.services.render_contracts import (
    RENDER_INPUT_BUILDER_VERSION,
    RENDER_INPUT_MIME_TYPE,
    RENDER_INPUT_SCHEMA_VERSION,
    ModuleRenderTarget,
    RenderabilityStatus,
    RenderInput,
    RenderInputLimits,
    build_render_input,
    serialize_render_input,
)

_NOW = datetime(2026, 7, 16, 9, 0, tzinfo=UTC)
_IMPORT_LIMITS = ImportLimits(
    max_bytes=100_000,
    max_rows=1_000,
    max_columns=20,
    max_field_length=1_000,
)
_MODULE_LIMITS = ModuleAnalysisLimits(evaluation=ModuleEvaluationLimits(max_block_previews=1))
_PATHWAY_LIMITS = PathwayCoverageLimits(max_detected_preview=1)


def _dataset() -> AnnotationDataset:
    return import_generic_table(
        "sequence,ko,status\n"
        "accepted,K00001,accepted\n"
        "unclassified-source,K00002,unclassified\n"
        "rejected,K00003,rejected\n"
        "unclassified,K00004,other\n"
        "invalid,not-a-ko,accepted\n",
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=CANONICAL_SOURCE_STATUS,
        limits=_IMPORT_LIMITS,
    )


def _module_values(
    view: KoAnalysisView,
) -> tuple[ResolvedModuleGraph, ModuleEvaluationResult]:
    graph = resolve_module_definitions(
        ModuleDefinitionCollection(
            root_module_id="M00001",
            definitions=(
                ModuleDefinition.from_text(
                    module_id="M00001",
                    module_name="Synthetic module",
                    definition="K00001-K00004 K00002 K00003",
                ),
            ),
        ),
        _MODULE_LIMITS,
    )
    return graph, evaluate_module(graph, view, _MODULE_LIMITS)


def _view(dataset: AnnotationDataset) -> KoAnalysisView:
    return build_ko_analysis_view(dataset, input_bytes=500)


def _provenance(operation: KeggOperation) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.CACHE,
        cache_lookup_state=CacheLookupState.FRESH_HIT,
        retrieved_at=_NOW,
        served_at=_NOW + timedelta(minutes=1),
        expires_at=_NOW + timedelta(days=1),
        response_bytes=123,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release synthetic-2026-07-16",
        attempt_count=0,
        is_stale=False,
    )


def _pathway_values(
    view: KoAnalysisView,
) -> tuple[PathwayKoReference, PathwayCoverageResult]:
    reference = PathwayKoReference(
        reference_namespace=PathwayReferenceNamespace.KO,
        reference_scope=PathwayReferenceScope.STANDARD,
        pathway_id="ko00010",
        pathway_name="Synthetic glycolysis",
        pathway_class=("Metabolism; Carbohydrate metabolism",),
        reference_kos=("K00001", "K00002", "K00003"),
        relationship_row_count=3,
        link_provenance=(_provenance(KeggOperation.LINK),),
        metadata_provenance=(_provenance(KeggOperation.GET),),
    )
    result = evaluate_pathway_coverage(
        reference,
        view,
        PathwayCoverageParameters(),
        _PATHWAY_LIMITS,
    )
    assert result.detected_preview_truncated is False
    return reference, result


def _execution(
    *,
    allow_global_or_overview: bool = False,
    ranking_view: KoAnalysisView | None = None,
    stream_intake: bool = False,
) -> AnalysisExecutionProvenance:
    module_ranking: ModuleRankingExecution | None = None
    pathway_ranking: PathwayRankingExecution | None = None
    if ranking_view is not None:
        decision_policy = ranking_view.decision_policy
        module_ranking = ModuleRankingExecution(
            method=MODULE_RANKING_METHOD,
            method_version=MODULE_RANKING_VERSION,
            selection=ModuleSelection(top_n=1),
            decision_policy=decision_policy,
            selected_unique_ko_count=1,
            candidate_module_count=1,
            selected_module_ids=("M00001",),
            mapping_request_count=1,
            mapping_network_request_count=0,
            mapping_cache_hit_count=1,
            mapping_response_bytes=123,
        )
        pathway_ranking = PathwayRankingExecution(
            method=PATHWAY_RANKING_METHOD,
            method_version=PATHWAY_RANKING_VERSION,
            selection=PathwaySelection(top_n=1),
            decision_policy=decision_policy,
            selected_unique_ko_count=1,
            candidate_pathway_count=1,
            selected_pathway_ids=("ko00010",),
            mapping_request_count=1,
            mapping_network_request_count=0,
            mapping_cache_hit_count=1,
            mapping_response_bytes=123,
        )
    return AnalysisExecutionProvenance(
        service_name=ANALYSIS_SERVICE_NAME,
        service_version=ANALYSIS_SERVICE_VERSION,
        import_limits=None if stream_intake else _IMPORT_LIMITS,
        stream_import_limits=AnalysisViewImportLimits() if stream_intake else None,
        kegg_request_options=KeggRequestOptions(),
        reference_loading_limits=ReferenceLoadingLimits(),
        module_analysis_limits=_MODULE_LIMITS,
        module_ranking=module_ranking,
        pathway_parameters=PathwayExecutionParameters(
            allow_global_or_overview=allow_global_or_overview,
            ranking=pathway_ranking,
        ),
        pathway_coverage_limits=_PATHWAY_LIMITS,
        direct_result_limits=AnalysisServiceLimits(),
    )


def test_execution_provenance_requires_the_current_service_identity() -> None:
    execution = _execution()
    payload = execution.model_dump(mode="json")
    payload["service_name"] = "unsupported_analysis_service"

    assert execution.service_name == "kegg_mcp_annotation_analysis"
    with pytest.raises(ValidationError):
        AnalysisExecutionProvenance.model_validate(payload)


def test_execution_provenance_requires_exactly_one_intake_limit_contract() -> None:
    bounded = _execution()
    assert bounded.import_limits == _IMPORT_LIMITS
    assert bounded.stream_import_limits is None

    streamed = _execution(stream_intake=True)
    assert streamed.import_limits is None
    assert streamed.stream_import_limits == AnalysisViewImportLimits()

    mismatched = bounded.model_dump(mode="json")
    mismatched["stream_import_limits"] = AnalysisViewImportLimits().model_dump(mode="json")
    with pytest.raises(ValidationError, match="exactly one intake-limit contract"):
        AnalysisExecutionProvenance.model_validate(mismatched)


def _render_input(
    *,
    limits: RenderInputLimits | None = None,
    include_rankings: bool = False,
) -> RenderInput:
    dataset = _dataset()
    view = _view(dataset)
    graph, _ = _module_values(view)
    reference, _ = _pathway_values(view)
    return build_render_input(
        view,
        (graph,),
        (reference,),
        _execution(ranking_view=view if include_rankings else None),
        limits=limits,
    )


def test_version_6_schema_and_canonical_json_round_trip() -> None:
    value = _render_input()
    serialized = serialize_render_input(value)
    schema = RenderInput.model_json_schema()

    assert schema["$id"] == "urn:kegg-mcp:schema:render-input:6"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$defs"]["PathwayRenderTarget"]["$id"] == (
        "urn:kegg-mcp:schema:pathway-render-target:4"
    )
    assert "workflow_digest" not in json.dumps(schema)
    assert value.schema_version == RENDER_INPUT_SCHEMA_VERSION == "6"
    assert value.execution.handoff_builder_version == RENDER_INPUT_BUILDER_VERSION == "5"
    assert RenderInput.model_validate_json(serialized, strict=True) == value
    assert (
        serialize_render_input(RenderInput.model_validate_json(serialized, strict=True))
        == serialized
    )
    assert RENDER_INPUT_MIME_TYPE.endswith("version=6")

    assert "schema_version" in schema["required"]
    assert "name" in schema["$defs"]["RenderProducer"]["required"]
    execution_schema = schema["$defs"]["RenderExecutionProvenance"]
    assert {"handoff_builder_name", "handoff_builder_version"}.issubset(
        execution_schema["required"]
    )


def test_version_6_handoff_accepts_the_compact_analysis_view() -> None:
    dataset = _dataset()
    view = _view(dataset)
    graph, _ = _module_values(view)
    reference, _ = _pathway_values(view)
    value = build_render_input(
        view,
        (graph,),
        (reference,),
        _execution(),
    )

    assert value.evidence.accepted_ko_ids == ("K00001",)
    assert value.modules[0].completion.block_coverage == 1 / 3
    assert value.pathways[0].detected_ko_ids == ("K00001",)


def test_current_render_input_rejects_version_5_or_missing_wire_versions() -> None:
    payload = json.loads(serialize_render_input(_render_input()))
    payload["schema_version"] = "5"
    with pytest.raises(ValidationError):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    del payload["schema_version"]
    with pytest.raises(ValidationError):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)


def test_current_render_input_requires_builder_identity_fields() -> None:
    payload = json.loads(serialize_render_input(_render_input()))
    del payload["producer"]["name"]
    with pytest.raises(ValidationError):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    del payload["execution"]["handoff_builder_name"]
    with pytest.raises(ValidationError):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    del payload["execution"]["handoff_builder_version"]
    with pytest.raises(ValidationError):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    payload["execution"]["handoff_builder_version"] = "2"
    with pytest.raises(ValidationError):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)


def test_current_render_targets_reject_fabricated_analysis_identity() -> None:
    mutations = (
        ("pathways", 0, "calculation_method", "fabricated_method"),
        ("pathways", 0, "calculation_version", "999"),
        ("modules", 0, "parser_name", "fabricated_parser"),
        ("modules", 0, "parser_version", "999"),
        ("modules", 0, "resolver_version", "999"),
    )
    for collection, index, field, replacement in mutations:
        payload = json.loads(serialize_render_input(_render_input()))
        payload[collection][index][field] = replacement
        with pytest.raises(ValidationError):
            RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    payload["pathways"][0]["reference_link_provenance"][0]["operation"] = "get"
    payload["pathways"][0]["reference_metadata_provenance"][0]["operation"] = "link"
    with pytest.raises(ValidationError, match="only LINK provenance"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    payload["modules"][0]["definitions"][0]["parse_result"]["parser_name"] = "fabricated_parser"
    with pytest.raises(ValidationError, match="definition parser identity"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    payload["modules"][0]["completion"]["calculation_method"]["name"] = "fabricated_method"
    with pytest.raises(ValidationError, match="calculation identity"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload = json.loads(serialize_render_input(_render_input()))
    payload["modules"][0]["reference_retrieval_provenance"] = [
        _provenance(KeggOperation.LINK).model_dump(mode="json")
    ]
    with pytest.raises(ValidationError, match="only GET retrieval provenance"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)


def test_current_render_input_requires_analysis_and_ranking_identity_fields() -> None:
    value = _render_input(include_rankings=True)
    schema = RenderInput.model_json_schema()
    definitions = schema["$defs"]
    assert {"service_name", "service_version"}.issubset(
        definitions["AnalysisExecutionProvenance"]["required"]
    )
    for definition_name in ("ModuleRankingExecution", "PathwayRankingExecution"):
        assert {"method", "method_version"}.issubset(definitions[definition_name]["required"])

    missing_paths = (
        ("execution", "analysis", "service_name"),
        ("execution", "analysis", "service_version"),
        ("execution", "analysis", "module_ranking", "method"),
        ("execution", "analysis", "module_ranking", "method_version"),
        ("execution", "analysis", "pathway_parameters", "ranking", "method"),
        (
            "execution",
            "analysis",
            "pathway_parameters",
            "ranking",
            "method_version",
        ),
    )
    for path in missing_paths:
        payload = json.loads(serialize_render_input(value))
        parent = payload
        for component in path[:-1]:
            parent = parent[component]
        del parent[path[-1]]
        with pytest.raises(ValidationError):
            RenderInput.model_validate_json(json.dumps(payload), strict=True)


def test_opted_in_global_or_overview_ko_pathway_is_renderable() -> None:
    dataset = _dataset()
    view = _view(dataset)
    reference = PathwayKoReference(
        reference_namespace=PathwayReferenceNamespace.KO,
        reference_scope=PathwayReferenceScope.GLOBAL_OR_OVERVIEW,
        pathway_id="ko01100",
        pathway_name="Synthetic metabolic pathways",
        pathway_class=("ENTRY: Global Pathway",),
        reference_kos=("K00001", "K00002", "K00003"),
        relationship_row_count=3,
        link_provenance=(_provenance(KeggOperation.LINK),),
        metadata_provenance=(_provenance(KeggOperation.GET),),
    )
    value = build_render_input(
        view,
        (),
        (reference,),
        _execution(allow_global_or_overview=True),
    )

    target = value.pathways[0]
    assert target.reference_namespace is PathwayReferenceNamespace.KO
    assert target.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
    assert target.renderability is RenderabilityStatus.RENDERABLE
    assert target.not_renderable_reason is None
    assert target.detected_ko_ids_complete is True
    assert target.detected_ko_ids == ("K00001",)

    payload = value.model_dump(mode="json")
    payload["execution"]["analysis"]["pathway_parameters"]["allow_global_or_overview"] = False
    with pytest.raises(ValidationError, match="explicit execution opt-in"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)

    payload["pathways"][0]["reference_scope"] = PathwayReferenceScope.STANDARD
    with pytest.raises(ValidationError, match="conflicts with retained PATHWAY classification"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)


def test_unevaluable_global_or_overview_ko_pathway_remains_summary_only() -> None:
    dataset = _dataset()
    view = _view(dataset)
    reference = PathwayKoReference(
        reference_namespace=PathwayReferenceNamespace.KO,
        reference_scope=PathwayReferenceScope.GLOBAL_OR_OVERVIEW,
        pathway_id="ko01100",
        pathway_name="Synthetic metabolic pathways",
        pathway_class=("Metabolism; Global and overview maps",),
        reference_kos=(),
        relationship_row_count=0,
        link_provenance=(_provenance(KeggOperation.LINK),),
        metadata_provenance=(_provenance(KeggOperation.GET),),
    )
    value = build_render_input(
        view,
        (),
        (reference,),
        _execution(allow_global_or_overview=True),
    )

    target = value.pathways[0]
    assert target.renderability is RenderabilityStatus.SUMMARY_ONLY
    assert target.not_renderable_reason == "pathway_not_evaluable"


def test_accepted_evidence_and_complete_renderer_targets_exclude_other_statuses() -> None:
    value = _render_input()

    assert value.evidence.accepted_ko_ids == ("K00001",)
    assert tuple(item.status for item in value.evidence.status_counts) == tuple(NormalizedStatus)
    assert value.evidence.status_counts[1].count == 1
    assert value.evidence.status_counts[2].count == 2
    assert value.evidence.status_counts[3].count == 1

    module = value.modules[0]
    assert module.renderability is RenderabilityStatus.RENDERABLE
    assert module.required_block_states_complete is True
    assert tuple(item.block_index for item in module.required_block_states) == (1, 2, 3)
    assert tuple(item.state for item in module.required_block_states) == (
        ModuleBlockState.COMPLETE,
        ModuleBlockState.INCOMPLETE,
        ModuleBlockState.INCOMPLETE,
    )
    assert module.completion.block_coverage == 1 / 3
    assert module.optional_component_states_complete is True
    assert len(module.optional_component_states) == 1

    pathway = value.pathways[0]
    assert pathway.renderability is RenderabilityStatus.RENDERABLE
    assert pathway.coverage_numerator == 1
    assert pathway.coverage_denominator == 3
    assert pathway.detected_ko_ids_complete is True
    assert pathway.detected_ko_ids == ("K00001",)
    assert "K00003" not in pathway.detected_ko_ids


def test_oversized_targets_are_explicit_and_never_retain_partial_vectors() -> None:
    value = _render_input(
        limits=RenderInputLimits(
            max_module_required_blocks_per_target=2,
            max_pathway_detected_ko_ids_per_target=0,
        )
    )

    module = value.modules[0]
    assert module.renderability is RenderabilityStatus.NOT_RENDERABLE
    assert module.not_renderable_reason == "module_required_block_limit_exceeded"
    assert module.required_block_states_complete is False
    assert module.required_block_states == ()

    pathway = value.pathways[0]
    assert pathway.renderability is RenderabilityStatus.NOT_RENDERABLE
    assert pathway.not_renderable_reason == "pathway_detected_ko_limit_exceeded"
    assert pathway.detected_ko_ids_complete is False
    assert pathway.detected_ko_ids == ()
    assert pathway.coverage_numerator == 1


def test_module_renderer_layout_bound_is_authoritative_in_builder_and_schema() -> None:
    dataset = _dataset()
    view = _view(dataset)
    graph = resolve_module_definitions(
        ModuleDefinitionCollection(
            root_module_id="M00001",
            definitions=(
                ModuleDefinition.from_text(
                    module_id="M00001",
                    module_name="Oversized renderer layout",
                    definition="+".join("K00001" for _ in range(400)),
                ),
            ),
        ),
        _MODULE_LIMITS,
    )
    value = build_render_input(
        view,
        (graph,),
        (),
        _execution(),
    )

    oversized = value.modules[0]
    assert oversized.renderability is RenderabilityStatus.NOT_RENDERABLE
    assert oversized.not_renderable_reason == "module_renderer_layout_limit_exceeded"
    assert oversized.required_block_states == ()
    assert oversized.optional_component_states == ()

    payload = _render_input().modules[0].model_dump(mode="json")
    payload["definitions"] = [item.model_dump(mode="json") for item in graph.modules]
    with pytest.raises(ValidationError, match="normative renderer layout"):
        ModuleRenderTarget.model_validate_json(json.dumps(payload), strict=True)


def test_serialized_byte_limit_fails_with_dedicated_error() -> None:
    with pytest.raises(KeggMcpError) as limit_error:
        _render_input(limits=RenderInputLimits(max_serialized_bytes=1_000))
    assert limit_error.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert {item.name for item in limit_error.value.detail.safe_details} == {
        "metric",
        "observed",
        "limit_name",
        "limit",
    }


def test_execution_provenance_must_match_module_graph_limits() -> None:
    dataset = _dataset()
    view = _view(dataset)
    graph, _ = _module_values(view)
    mismatched_graph = graph.model_copy(
        update={
            "limits": graph.limits.model_copy(update={"max_modules": graph.limits.max_modules + 1})
        }
    )
    with pytest.raises(KeggMcpError) as raised:
        build_render_input(
            view,
            (mismatched_graph,),
            (),
            _execution(),
        )

    assert raised.value.detail.code is ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE


def test_visualization_accepted_evidence_must_be_sorted_and_unique() -> None:
    payload = _render_input().model_dump(mode="json")
    payload["evidence"]["accepted_ko_ids"] = ["K00001", "K00001"]
    with pytest.raises(ValidationError, match="sorted and unique"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)


def test_visualization_unique_accepted_kos_cannot_exceed_accepted_assignments() -> None:
    payload = _render_input().model_dump(mode="json")
    payload["evidence"]["accepted_ko_ids"] = ["K00001", "K00002"]
    with pytest.raises(ValidationError, match="cannot exceed accepted assignments"):
        RenderInput.model_validate_json(json.dumps(payload), strict=True)


def test_non_ko_reference_pathway_is_explicitly_summary_only() -> None:
    dataset = _dataset()
    view = _view(dataset)
    reference, _ = _pathway_values(view)
    map_reference = reference.model_copy(
        update={
            "reference_namespace": PathwayReferenceNamespace.MAP,
            "pathway_id": "map00010",
        }
    )
    value = build_render_input(
        view,
        (),
        (map_reference,),
        _execution(),
    )

    assert value.pathways[0].renderability is RenderabilityStatus.SUMMARY_ONLY
    assert value.pathways[0].not_renderable_reason == ("pathway_reference_namespace_unsupported")


def test_analysis_bundle_limit_failure_writes_no_partial_directory(tmp_path: Path) -> None:
    dataset = _dataset()
    view = _view(dataset)
    graph, evaluation = _module_values(view)
    reference, coverage = _pathway_values(view)
    output_directory = tmp_path / "bounded-analysis"

    with pytest.raises(KeggMcpError) as error:
        write_analysis_bundle(
            view,
            (graph,),
            (evaluation,),
            (reference,),
            (coverage,),
            execution=_execution(),
            analysis_report="# Synthetic analysis\n",
            output_directory=output_directory,
            render_limits=RenderInputLimits(max_serialized_bytes=1_000),
        )

    assert error.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert not output_directory.exists()
