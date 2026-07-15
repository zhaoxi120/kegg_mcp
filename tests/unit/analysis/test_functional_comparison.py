"""Tests for shared-definition MODULE comparison across annotation datasets."""

import json

import pytest

from kegg_mcp.analysis.comparison import ComparisonDatasetInput, ComparisonLimits
from kegg_mcp.analysis.contracts import (
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleEvaluationStatus,
)
from kegg_mcp.analysis.functional_comparison import (
    FunctionalComparisonLimits,
    ModuleComparisonResult,
    compare_module_graphs,
)
from kegg_mcp.analysis.module_resolution import resolve_module_definitions
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS_V1, AnalysisUnit
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    TableDialect,
    import_generic_table,
)

_IMPORT_LIMITS = ImportLimits(
    max_bytes=100_000,
    max_rows=1_000,
    max_columns=20,
    max_field_length=1_000,
)


def _dataset(rows: tuple[tuple[str, str, str], ...]):
    payload = "sequence,ko,status\n" + "".join(
        f"{sequence},{ko_id},{status}\n" for sequence, ko_id, status in rows
    )
    return import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="status",
        ),
        policy=CANONICAL_SOURCE_STATUS_V1,
        limits=_IMPORT_LIMITS,
        analysis_unit=AnalysisUnit.ISOLATE_PROTEOME,
    )


def _inputs() -> tuple[ComparisonDatasetInput, ...]:
    complete = _dataset(
        (
            ("complete-one", "K00001", "accepted"),
            ("complete-two", "K00002", "accepted"),
        )
    )
    uncertain = _dataset(
        (
            ("uncertain-one", "K00001", "accepted"),
            ("uncertain-two", "K00002", "uncertain"),
        )
    )
    incomplete = _dataset((("incomplete-one", "K00001", "accepted"),))
    return (
        ComparisonDatasetInput(label="complete", dataset=complete),
        ComparisonDatasetInput(label="uncertain", dataset=uncertain),
        ComparisonDatasetInput(label="incomplete", dataset=incomplete),
    )


def _graph(module_id: str, definition: str):
    return resolve_module_definitions(
        ModuleDefinitionCollection(
            root_module_id=module_id,
            definitions=(
                ModuleDefinition.from_text(
                    module_id=module_id,
                    module_name=f"Synthetic {module_id}",
                    definition=definition,
                ),
            ),
        )
    )


def test_module_comparison_recomputes_strict_and_lenient_under_one_graph() -> None:
    result = compare_module_graphs(_inputs(), (_graph("M00001", "K00001 K00002"),))
    target = result.targets[0]

    assert target.module_id == "M00001"
    assert [item.label for item in target.strict.outcomes] == [
        "complete",
        "uncertain",
        "incomplete",
    ]
    assert [item.evaluation_status for item in target.strict.outcomes] == [
        ModuleEvaluationStatus.COMPLETE,
        ModuleEvaluationStatus.INCOMPLETE,
        ModuleEvaluationStatus.INCOMPLETE,
    ]
    assert target.strict.complete_in_set_indexes == (0,)
    assert target.strict.incomplete_in_set_indexes == (1, 2)
    assert target.strict.outcomes_differ is True
    assert [item.evaluation_status for item in target.lenient.outcomes] == [
        ModuleEvaluationStatus.COMPLETE,
        ModuleEvaluationStatus.COMPLETE,
        ModuleEvaluationStatus.INCOMPLETE,
    ]
    assert target.lenient.complete_in_set_indexes == (0, 1)
    assert target.lenient.incomplete_in_set_indexes == (2,)
    assert target.lenient.outcomes_differ is True
    assert all(item.required_block_count == 2 for item in target.strict.outcomes)
    assert target.definition_provenance[0].module_id == target.module_id
    assert ModuleComparisonResult.model_validate_json(result.model_dump_json()) == result


def test_multiple_module_targets_preserve_caller_order_and_shared_context() -> None:
    first = _graph("M00001", "K00001 K00002")
    second = _graph("M00002", "K00001,K00003")

    result = compare_module_graphs(_inputs(), (second, first))

    assert [target.module_id for target in result.targets] == ["M00002", "M00001"]
    assert [item.label for item in result.datasets] == [
        "complete",
        "uncertain",
        "incomplete",
    ]
    assert result.targets[0].strict.outcomes_differ is False
    assert all(
        outcome.evaluation_status is ModuleEvaluationStatus.COMPLETE
        for outcome in result.targets[0].strict.outcomes
    )


def test_not_evaluable_module_outcomes_never_claim_coverage() -> None:
    result = compare_module_graphs(_inputs(), (_graph("M00003", "K00001 / K00002"),))
    target = result.targets[0]

    assert target.strict.not_evaluable_in_set_indexes == (0, 1, 2)
    assert target.strict.outcomes_differ is False
    assert all(outcome.block_coverage is None for outcome in target.strict.outcomes)


def test_shared_unresolved_module_references_remain_explainable() -> None:
    result = compare_module_graphs(_inputs(), (_graph("M00004", "M99999"),))
    target = result.targets[0]

    assert target.strict.not_evaluable_in_set_indexes == (0, 1, 2)
    assert [issue.target_module_id for issue in target.unresolved_references] == ["M99999"]


def test_duplicate_and_excess_module_targets_fail_before_result_construction() -> None:
    graph = _graph("M00001", "K00001")
    other = _graph("M00002", "K00002")

    with pytest.raises(KeggMcpError) as caught:
        compare_module_graphs(_inputs(), (graph, graph))
    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID

    with pytest.raises(KeggMcpError) as caught:
        compare_module_graphs(
            _inputs(),
            (graph, other),
            functional_limits=FunctionalComparisonLimits(max_modules=1),
        )
    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED

    with pytest.raises(KeggMcpError) as caught:
        compare_module_graphs(
            _inputs(),
            (graph,),
            comparison_limits=ComparisonLimits(max_total_membership_entries=1),
        )
    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_empty_module_target_list_is_a_valid_bounded_comparison() -> None:
    result = compare_module_graphs(_inputs(), ())

    assert result.targets == ()
    assert len(result.datasets) == 3


def test_schema_uses_outcome_language_not_gain_loss_or_statistics() -> None:
    schema_text = json.dumps(ModuleComparisonResult.model_json_schema()).lower()

    for forbidden in ("gain", "loss", "p_value", "fold_change", "enrichment"):
        assert f'"{forbidden}"' not in schema_text
