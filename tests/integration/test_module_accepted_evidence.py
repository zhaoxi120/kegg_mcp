"""Integration tests from immutable annotation evidence to accepted-only MODULE evaluation."""

from kegg_mcp.analysis.contracts import (
    ModuleBlockState,
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleEvaluationResult,
    ModuleEvaluationStatus,
    ModuleWarningCode,
    OptionalComponentState,
)
from kegg_mcp.analysis.module_evaluation import evaluate_module
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
        "unclassified-one,K00002,unclassified\n"
        "unclassified-two,K00002,unclassified\n"
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


def test_accepted_only_evaluation_preserves_policy_references_and_unknown_or_branch() -> None:
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

    result = evaluate_module(graph, _annotation_dataset())

    assert result.evaluation_status is ModuleEvaluationStatus.INCOMPLETE
    assert result.is_complete is False
    assert result.completed_required_blocks == 3
    assert result.evaluable_required_blocks == 4
    assert result.required_block_count == 4
    assert result.block_coverage == 0.75
    assert result.present_blocks_preview == (1, 3, 4)
    assert len(result.missing_blocks_preview) == 1
    missing = result.missing_blocks_preview[0]
    assert missing.block_index == 2
    assert missing.state is ModuleBlockState.INCOMPLETE
    assert missing.missing is not None
    assert [item.ko_ids for item in missing.missing.alternatives] == [("K00002",)]

    assert len(result.optional_components) == 1
    optional = result.optional_components[0]
    assert optional.source_module_id == "M00001"
    assert optional.state is OptionalComponentState.ABSENT
    assert optional.matched_ko_ids == ()

    assert [issue.target_module_id for issue in result.unresolved_references] == ["M00003"]
    assert ModuleWarningCode.UNRESOLVED_REFERENCE in {
        warning.code for warning in result.warnings
    }
    assert [item.module_id for item in result.provenance] == ["M00001", "M00002"]
    assert result.decision_policy == CANONICAL_SOURCE_STATUS.reference

    round_trip = ModuleEvaluationResult.model_validate_json(result.model_dump_json())
    assert round_trip == result
