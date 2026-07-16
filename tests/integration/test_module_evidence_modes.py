"""Integration tests from immutable annotation evidence to paired MODULE evaluation."""

from kegg_mcp.analysis.contracts import (
    ModuleBlockState,
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleEvaluationStatus,
    ModuleWarningCode,
    OptionalComponentState,
    PairedModuleEvaluation,
)
from kegg_mcp.analysis.module_evaluation import evaluate_module_pair
from kegg_mcp.analysis.module_resolution import resolve_module_definitions
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS, AnalysisUnit, AnnotationDataset
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    TableDialect,
    import_generic_table,
)


def _annotation_dataset() -> AnnotationDataset:
    payload = (
        "protein,ko,status\n"
        "accepted-root,K00001,accepted\n"
        "uncertain-one,K00002,uncertain\n"
        "uncertain-two,K00002,uncertain\n"
        "rejected-only,K00003,rejected\n"
        "accepted-reference,K00005,accepted\n"
        "accepted-or-guard,K00008,accepted\n"
    )
    return import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="protein",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=CANONICAL_SOURCE_STATUS,
        limits=ImportLimits(
            max_bytes=10_000,
            max_rows=100,
            max_columns=10,
            max_field_length=1_000,
        ),
        analysis_unit=AnalysisUnit.ISOLATE_PROTEOME,
    )


def test_paired_evaluation_preserves_evidence_policy_references_and_unknown_or_branch() -> None:
    definitions = ModuleDefinitionCollection(
        root_module_id="M00001",
        definitions=(
            ModuleDefinition.from_text(
                module_id="M00001",
                module_name="Synthetic integration module",
                definition="(K00001,K00009) K00002-K00004 M00002 (K00008,M00003)",
            ),
            ModuleDefinition.from_text(
                module_id="M00002",
                module_name="Synthetic referenced alternative",
                definition="K00005,K00006+K00007",
            ),
        ),
    )
    graph = resolve_module_definitions(definitions)

    pair = evaluate_module_pair(graph, _annotation_dataset())

    assert pair.strict.evaluation_status is ModuleEvaluationStatus.INCOMPLETE
    assert pair.strict.is_complete is False
    assert pair.strict.completed_required_blocks == 3
    assert pair.strict.evaluable_required_blocks == 4
    assert pair.strict.required_block_count == 4
    assert pair.strict.block_coverage == 0.75
    assert pair.strict.present_blocks_preview == (1, 3, 4)
    assert len(pair.strict.missing_blocks_preview) == 1
    strict_missing = pair.strict.missing_blocks_preview[0]
    assert strict_missing.block_index == 2
    assert strict_missing.state is ModuleBlockState.INCOMPLETE
    assert strict_missing.missing is not None
    assert [item.ko_ids for item in strict_missing.missing.alternatives] == [("K00002",)]

    assert pair.lenient.evaluation_status is ModuleEvaluationStatus.COMPLETE
    assert pair.lenient.is_complete is True
    assert pair.lenient.completed_required_blocks == 4
    assert pair.lenient.block_coverage == 1.0
    assert pair.strict_to_lenient_changed
    assert pair.newly_completed_block_indexes == (2,)
    assert not pair.newly_completed_blocks_truncated

    assert len(pair.lenient.uncertain_support) == 1
    support = pair.lenient.uncertain_support[0]
    assert support.ko_id == "K00002"
    assert support.record_ids == ("record-000002", "record-000003")
    assert support.required_block_indexes == (2,)
    assert not support.record_ids_truncated
    assert not support.required_block_indexes_truncated

    assert len(pair.strict.optional_components) == 1
    optional = pair.strict.optional_components[0]
    assert optional.source_module_id == "M00001"
    assert optional.state is OptionalComponentState.ABSENT
    assert optional.matched_ko_ids == ()

    assert [issue.target_module_id for issue in pair.strict.unresolved_references] == ["M00003"]
    assert ModuleWarningCode.UNRESOLVED_REFERENCE in {
        warning.code for warning in pair.strict.warnings
    }
    assert [item.module_id for item in pair.strict.provenance] == ["M00001", "M00002"]
    assert pair.strict.decision_policy == CANONICAL_SOURCE_STATUS.reference

    round_trip = PairedModuleEvaluation.model_validate_json(pair.model_dump_json())
    assert round_trip == pair
