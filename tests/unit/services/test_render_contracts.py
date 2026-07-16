"""Tests for the complete bounded renderer handoff contract."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis import (
    ModuleAnalysisLimits,
    ModuleBlockState,
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleEvaluationLimits,
    PairedModuleEvaluation,
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    PathwayCoverageResult,
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    ResolvedModuleGraph,
    evaluate_module_pair,
    evaluate_pathway_coverage,
    resolve_module_definitions,
)
from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS_V1,
    AnnotationDataset,
    EvidenceMode,
    NormalizedStatus,
)
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.execution import (
    AnalysisExecutionProvenance,
    AnalysisServiceLimits,
    PathwayExecutionParameters,
)
from kegg_mcp.importers import (
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
    RENDER_INPUT_MIME_TYPE,
    RenderabilityStatus,
    RenderInputLimits,
    RenderInputV2,
    build_render_input,
    parse_render_input_json,
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
        "uncertain,K00002,uncertain\n"
        "rejected,K00003,rejected\n"
        "unclassified,K00004,other\n"
        "invalid,not-a-ko,accepted\n",
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=CANONICAL_SOURCE_STATUS_V1,
        limits=_IMPORT_LIMITS,
    )


def _module_values(
    dataset: AnnotationDataset,
) -> tuple[ResolvedModuleGraph, PairedModuleEvaluation]:
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
    return graph, evaluate_module_pair(graph, dataset, _MODULE_LIMITS)


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
    dataset: AnnotationDataset,
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
        dataset,
        PathwayCoverageParameters(evidence_mode=EvidenceMode.LENIENT),
        _PATHWAY_LIMITS,
    )
    assert result.detected_preview_truncated is True
    return reference, result


def _execution() -> AnalysisExecutionProvenance:
    return AnalysisExecutionProvenance(
        import_limits=_IMPORT_LIMITS,
        kegg_request_options=KeggRequestOptions(),
        reference_loading_limits=ReferenceLoadingLimits(),
        module_analysis_limits=_MODULE_LIMITS,
        pathway_parameters=PathwayExecutionParameters(evidence_mode=EvidenceMode.LENIENT),
        pathway_coverage_limits=_PATHWAY_LIMITS,
        direct_result_limits=AnalysisServiceLimits(),
    )


def _render_input(*, limits: RenderInputLimits | None = None) -> RenderInputV2:
    dataset = _dataset()
    graph, pair = _module_values(dataset)
    reference, coverage = _pathway_values(dataset)
    return build_render_input(
        dataset,
        (graph,),
        (pair,),
        (reference,),
        (coverage,),
        _execution(),
        limits=limits,
    )


def test_version_2_schema_and_canonical_json_round_trip() -> None:
    value = _render_input()
    serialized = serialize_render_input(value)
    schema = RenderInputV2.model_json_schema()

    assert schema["$id"] == "urn:kegg-mcp:schema:render-input:2"
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert "workflow_digest" not in json.dumps(schema)
    assert value.schema_version == "2"
    assert parse_render_input_json(serialized) == value
    assert serialize_render_input(parse_render_input_json(serialized)) == serialized
    assert RENDER_INPUT_MIME_TYPE.endswith("version=2")

    payload = json.loads(serialized)
    payload["schema_version"] = "1"
    with pytest.raises(ValidationError):
        RenderInputV2.model_validate(payload)


def test_evidence_classes_and_complete_renderer_targets_exclude_other_statuses() -> None:
    value = _render_input()

    assert value.evidence.accepted_ko_ids == ("K00001",)
    assert value.evidence.uncertain_ko_ids == ("K00002",)
    assert tuple(item.status for item in value.evidence.status_counts) == tuple(NormalizedStatus)
    assert value.evidence.status_counts[2].count == 1
    assert value.evidence.status_counts[3].count == 1
    assert value.evidence.status_counts[4].count == 1

    module = value.modules[0]
    assert module.renderability is RenderabilityStatus.RENDERABLE
    assert module.required_block_states_complete is True
    assert tuple(item.block_index for item in module.required_block_states) == (1, 2, 3)
    assert tuple(item.strict_state for item in module.required_block_states) == (
        ModuleBlockState.COMPLETE,
        ModuleBlockState.INCOMPLETE,
        ModuleBlockState.INCOMPLETE,
    )
    assert tuple(item.lenient_state for item in module.required_block_states) == (
        ModuleBlockState.COMPLETE,
        ModuleBlockState.COMPLETE,
        ModuleBlockState.INCOMPLETE,
    )
    assert module.required_block_states[1].uncertain_support_ko_ids == ("K00002",)
    assert module.strict.block_coverage == 1 / 3
    assert module.lenient.block_coverage == 2 / 3
    assert module.optional_component_states_complete is True
    assert len(module.optional_component_states) == 1

    pathway = value.pathways[0]
    assert pathway.renderability is RenderabilityStatus.RENDERABLE
    assert pathway.coverage_numerator == 2
    assert pathway.coverage_denominator == 3
    assert pathway.detected_ko_ids_complete is True
    assert pathway.detected_ko_ids == ("K00001", "K00002")
    assert "K00003" not in pathway.detected_ko_ids


def test_oversized_targets_are_explicit_and_never_retain_partial_vectors() -> None:
    value = _render_input(
        limits=RenderInputLimits(
            max_module_required_blocks_per_target=2,
            max_pathway_detected_ko_ids_per_target=1,
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
    assert pathway.coverage_numerator == 2


def test_identity_mismatch_and_serialized_byte_limit_fail_with_dedicated_errors() -> None:
    dataset = _dataset()
    graph, pair = _module_values(dataset)
    reference, coverage = _pathway_values(dataset)
    mismatched = reference.model_copy(update={"pathway_name": "Different pathway"})

    with pytest.raises(KeggMcpError) as identity_error:
        build_render_input(
            dataset,
            (graph,),
            (pair,),
            (mismatched,),
            (coverage,),
            _execution(),
        )
    assert identity_error.value.detail.code is ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE

    with pytest.raises(KeggMcpError) as limit_error:
        _render_input(limits=RenderInputLimits(max_serialized_bytes=1_000))
    assert limit_error.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert {item.name for item in limit_error.value.detail.safe_details} == {
        "metric",
        "observed",
        "limit_name",
        "limit",
    }


def test_execution_provenance_must_match_analysis_limits_and_parameters() -> None:
    dataset = _dataset()
    graph, pair = _module_values(dataset)
    reference, coverage = _pathway_values(dataset)

    mismatched_execution = _execution().model_copy(
        update={
            "module_analysis_limits": ModuleAnalysisLimits(),
            "pathway_parameters": PathwayExecutionParameters(evidence_mode=EvidenceMode.STRICT),
        }
    )
    with pytest.raises(KeggMcpError) as raised:
        build_render_input(
            dataset,
            (graph,),
            (pair,),
            (reference,),
            (coverage,),
            mismatched_execution,
        )

    assert raised.value.detail.code is ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE


def test_visualization_evidence_classes_must_be_disjoint() -> None:
    payload = _render_input().model_dump(mode="json")
    payload["evidence"]["uncertain_ko_ids"] = ["K00001", "K00002"]
    payload["evidence"]["uncertain_count"] += 1

    with pytest.raises(ValidationError, match="must be disjoint"):
        RenderInputV2.model_validate_json(json.dumps(payload), strict=True)


def test_non_ko_reference_pathway_is_explicitly_summary_only() -> None:
    dataset = _dataset()
    reference, _ = _pathway_values(dataset)
    map_reference = reference.model_copy(
        update={
            "reference_namespace": PathwayReferenceNamespace.MAP,
            "pathway_id": "map00010",
        }
    )
    coverage = evaluate_pathway_coverage(
        map_reference,
        dataset,
        PathwayCoverageParameters(
            reference_namespace=PathwayReferenceNamespace.MAP,
            evidence_mode=EvidenceMode.LENIENT,
        ),
        _PATHWAY_LIMITS,
    )
    value = build_render_input(
        dataset,
        (),
        (),
        (map_reference,),
        (coverage,),
        _execution(),
    )

    assert value.pathways[0].renderability is RenderabilityStatus.SUMMARY_ONLY
    assert value.pathways[0].not_renderable_reason == ("pathway_reference_namespace_unsupported")


def test_same_count_different_module_and_pathway_content_fails_identity_alignment() -> None:
    dataset = _dataset()
    graph, pair = _module_values(dataset)
    reference, coverage = _pathway_values(dataset)
    mismatched_graph = resolve_module_definitions(
        ModuleDefinitionCollection(
            root_module_id="M00001",
            definitions=(
                ModuleDefinition.from_text(
                    module_id="M00001",
                    module_name="Synthetic module",
                    definition="K00001-K00004 K00002 K00004",
                ),
            ),
        ),
        pair.strict.limits,
    )
    mismatched_reference = reference.model_copy(
        update={"reference_kos": ("K00001", "K00002", "K00004")}
    )

    with pytest.raises(KeggMcpError) as module_error:
        build_render_input(
            dataset,
            (mismatched_graph,),
            (pair,),
            (reference,),
            (coverage,),
            _execution(),
        )
    assert module_error.value.detail.code is ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE

    with pytest.raises(KeggMcpError) as pathway_error:
        build_render_input(
            dataset,
            (graph,),
            (pair,),
            (mismatched_reference,),
            (coverage,),
            _execution(),
        )
    assert pathway_error.value.detail.code is ErrorCode.INCOMPATIBLE_ANALYSIS_PROVENANCE


def test_analysis_bundle_limit_failure_writes_no_partial_directory(tmp_path: Path) -> None:
    dataset = _dataset()
    graph, pair = _module_values(dataset)
    reference, coverage = _pathway_values(dataset)
    output_directory = tmp_path / "bounded-analysis"

    with pytest.raises(KeggMcpError) as error:
        write_analysis_bundle(
            dataset,
            (graph,),
            (pair,),
            (reference,),
            (coverage,),
            execution=_execution(),
            analysis_report="# Synthetic analysis\n",
            output_directory=output_directory,
            render_limits=RenderInputLimits(max_serialized_bytes=1_000),
        )

    assert error.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert not output_directory.exists()
