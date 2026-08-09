"""Tests for conservative, bounded KEGG MODULE evaluation."""

from kegg_mcp.analysis.contracts import (
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleEvaluationLimits,
    ModuleEvaluationResult,
    ModuleEvaluationStatus,
    ModuleWarningCode,
    OptionalComponentState,
    ResolvedModuleGraph,
)
from kegg_mcp.analysis.module_evaluation import evaluate_module
from kegg_mcp.analysis.module_resolution import resolve_module_definitions
from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS,
    AnnotationDataset,
    KoAnalysisView,
    NormalizedStatus,
    build_ko_analysis_view,
)
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


def _graph(
    *definitions: tuple[str, str],
    root_module_id: str = "M00001",
) -> ResolvedModuleGraph:
    collection = ModuleDefinitionCollection(
        root_module_id=root_module_id,
        definitions=tuple(
            ModuleDefinition.from_text(
                module_id=module_id,
                module_name="Test root" if module_id == root_module_id else None,
                definition=definition,
            )
            for module_id, definition in definitions
        ),
    )
    return resolve_module_definitions(collection)


def _normalized_dataset(
    *rows: tuple[str, str, str, int, int, int],
) -> AnnotationDataset:
    payload = "sequence,ko,decision,rank,start,end\n" + "".join(
        f"{sequence},{ko_id},{decision},{rank},{start},{end}\n"
        for sequence, ko_id, decision, rank, start, end in rows
    )
    return import_generic_table(
        payload,
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="decision",
            rank="rank",
            domain_start="start",
            domain_end="end",
        ),
        policy=CANONICAL_SOURCE_STATUS,
        limits=_IMPORT_LIMITS,
    )


def _dataset(
    *rows: tuple[str, str, str, int, int, int],
) -> KoAnalysisView:
    return build_ko_analysis_view(_normalized_dataset(*rows))


def _row(
    ko_id: str,
    decision: str,
    *,
    sequence: str = "protein-1",
    rank: int = 1,
    start: int = 1,
    end: int = 100,
) -> tuple[str, str, str, int, int, int]:
    return sequence, ko_id, decision, rank, start, end


def _warning_codes(result: ModuleEvaluationResult) -> set[ModuleWarningCode]:
    return {warning.code for warning in result.warnings}


def _diamond_definitions(
    depth: int,
    *,
    root_prefix: str = "",
) -> tuple[tuple[str, str], ...]:
    definitions: list[tuple[str, str]] = [
        ("M00001", f"{root_prefix}M00100+M00101"),
    ]
    for layer in range(depth):
        left = f"M{100 + layer * 2:05d}"
        right = f"M{101 + layer * 2:05d}"
        if layer == depth - 1:
            definition = "K00001"
        else:
            definition = f"M{102 + layer * 2:05d}+M{103 + layer * 2:05d}"
        definitions.extend(((left, definition), (right, definition)))
    return tuple(definitions)


def test_optional_terms_do_not_change_completion_or_block_denominator() -> None:
    graph = _graph(("M00001", "K00001-K00002 K00003"))
    dataset = _dataset(
        _row("K00001", "accepted"),
        _row("K00002", "rejected", sequence="protein-2"),
        _row("K00003", "accepted", sequence="protein-3"),
    )

    result = evaluate_module(graph, dataset)

    assert result.evaluation_status is ModuleEvaluationStatus.COMPLETE
    assert result.required_block_count == 2
    assert result.block_coverage == 1.0
    assert result.optional_components[0].state is OptionalComponentState.ABSENT


def test_nested_module_references_expand_with_complete_provenance() -> None:
    graph = _graph(
        ("M00001", "M00002 K00004"),
        ("M00002", "M00003+K00003"),
        ("M00003", "(K00001,K00002)"),
    )
    dataset = _dataset(
        _row("K00002", "accepted"),
        _row("K00003", "accepted", sequence="protein-2"),
        _row("K00004", "accepted", sequence="protein-3"),
    )

    result = evaluate_module(graph, dataset)

    assert result.evaluation_status is ModuleEvaluationStatus.COMPLETE
    assert result.present_blocks_preview == (1, 2)
    assert [item.module_id for item in result.provenance] == [
        "M00001",
        "M00002",
        "M00003",
    ]


def test_true_or_branch_proves_completion_despite_unknown_reference() -> None:
    graph = _graph(("M00001", "(M00002,K00001) K00003"))
    dataset = _dataset(
        _row("K00001", "accepted"),
        _row("K00003", "accepted", sequence="protein-2"),
    )

    result = evaluate_module(graph, dataset)

    assert result.evaluation_status is ModuleEvaluationStatus.COMPLETE
    assert result.is_complete is True
    assert result.block_coverage == 1.0
    assert len(result.unresolved_references) == 1
    assert ModuleWarningCode.UNRESOLVED_REFERENCE in _warning_codes(result)


def test_unresolved_required_blocks_distinguish_partial_and_not_evaluable() -> None:
    dataset = _dataset(_row("K00001", "accepted"))

    partial = evaluate_module(
        _graph(("M00001", "K00001 M00002")),
        dataset,
    )
    not_evaluable = evaluate_module(
        _graph(("M00001", "M00002")),
        dataset,
    )

    assert partial.evaluation_status is ModuleEvaluationStatus.PARTIALLY_EVALUABLE
    assert partial.completed_required_blocks == 1
    assert partial.evaluable_required_blocks == 1
    assert partial.required_block_count == 2
    assert partial.block_coverage is None
    assert partial.not_evaluable_blocks_preview[0].block_index == 2
    assert not_evaluable.evaluation_status is ModuleEvaluationStatus.NOT_EVALUABLE
    assert not_evaluable.evaluable_required_blocks == 0
    assert not_evaluable.block_coverage is None


def test_invalid_root_definition_returns_zero_block_not_evaluable_result() -> None:
    result = evaluate_module(
        _graph(("M00001", "K00001+")),
        _dataset(_row("K00001", "accepted")),
    )

    assert result.evaluation_status is ModuleEvaluationStatus.NOT_EVALUABLE
    assert result.required_block_count == 0
    assert result.block_coverage is None
    assert ModuleWarningCode.UNSUPPORTED_CONTENT in _warning_codes(result)


def test_unsupported_content_fails_closed_even_when_an_or_branch_is_true() -> None:
    result = evaluate_module(
        _graph(("M00001", "(K00001,R00001)")),
        _dataset(_row("K00001", "accepted")),
    )

    assert result.evaluation_status is ModuleEvaluationStatus.NOT_EVALUABLE
    assert result.not_evaluable_blocks_preview[0].block_index == 1
    assert ModuleWarningCode.UNSUPPORTED_CONTENT in _warning_codes(result)


def test_unclassified_records_do_not_contribute_to_completion() -> None:
    graph = _graph(("M00001", "K00001 K00002"))
    normalized = _normalized_dataset(
        _row("K00001", "accepted"),
        _row(
            "K00002",
            "unclassified",
            sequence="multi-domain-1",
            rank=2,
            start=51,
            end=100,
        ),
        _row(
            "K00002",
            "unclassified",
            sequence="multi-domain-2",
            rank=1,
            start=1,
            end=50,
        ),
    )

    result = evaluate_module(graph, build_ko_analysis_view(normalized))

    assert result.evaluation_status is ModuleEvaluationStatus.INCOMPLETE
    assert result.evidence_ko_count == 1
    assert result.missing_blocks_preview[0].block_index == 2
    assert all(
        record.normalized_status is NormalizedStatus.UNCLASSIFIED
        for record in normalized.records[1:]
    )


def test_rejected_and_unclassified_predictions_never_enter_analysis_evidence() -> None:
    graph = _graph(("M00001", "K00003"))
    dataset = _dataset(
        _row("K00003", "rejected"),
        _row("K00002", "unclassified", sequence="protein-2"),
    )

    result = evaluate_module(graph, dataset)

    assert result.evaluation_status is ModuleEvaluationStatus.INCOMPLETE
    assert result.evidence_ko_count == 0


def test_top_k_multi_domain_records_remain_distinct_and_can_satisfy_one_block() -> None:
    graph = _graph(("M00001", "K00001+K00002"))
    normalized = _normalized_dataset(
        _row("K00001", "accepted", rank=1, start=1, end=50),
        _row("K00002", "accepted", rank=2, start=51, end=100),
    )

    result = evaluate_module(graph, build_ko_analysis_view(normalized))

    assert len(normalized.records) == 2
    assert [record.rank for record in normalized.records] == [1, 2]
    assert result.evaluation_status is ModuleEvaluationStatus.COMPLETE


def test_missing_alternatives_are_an_ordered_bounded_antichain() -> None:
    graph = _graph(("M00001", "(K00001,K00002)+(K00003,K00004)"))
    limits = ModuleEvaluationLimits(max_missing_alternatives=2)

    result = evaluate_module(
        graph,
        _dataset(_row("K99999", "rejected")),
        limits,
    )

    missing = result.missing_blocks_preview[0].missing
    assert missing is not None
    assert [item.ko_ids for item in missing.alternatives] == [
        ("K00001", "K00003"),
        ("K00001", "K00004"),
    ]
    assert missing.truncated is True
    assert missing.combination_expansions <= limits.max_combination_expansions
    assert ModuleWarningCode.MISSING_ALTERNATIVES_TRUNCATED in _warning_codes(result)


def test_intermediate_output_cap_cannot_create_nonminimal_missing_set() -> None:
    graph = _graph(
        ("M00001", "((K00001+K00002),K00003)+(K00001+K00002)"),
    )

    result = evaluate_module(
        graph,
        _dataset(_row("K99999", "rejected")),
        ModuleEvaluationLimits(max_missing_alternatives=1),
    )

    missing = result.missing_blocks_preview[0].missing
    assert missing is not None
    assert [item.ko_ids for item in missing.alternatives] == [
        ("K00001", "K00002"),
    ]


def test_combination_budget_is_global_and_fails_closed_before_explosion() -> None:
    graph = _graph(("M00001", "(K00001,K00002) (K00003,K00004)"))
    limits = ModuleEvaluationLimits(max_combination_expansions=3)

    result = evaluate_module(
        graph,
        _dataset(_row("K99999", "rejected")),
        limits,
    )

    first, second = result.missing_blocks_preview
    assert first.missing is not None
    assert second.missing is not None
    assert first.missing.alternatives
    assert second.missing.alternatives == ()
    assert second.missing.truncated is True
    assert second.missing.combination_expansions == 3


def test_false_and_unknown_does_not_claim_a_complete_missing_enumeration() -> None:
    graph = _graph(("M00001", "K00001+M00002"))

    result = evaluate_module(
        graph,
        _dataset(_row("K99999", "rejected")),
    )

    missing = result.missing_blocks_preview[0].missing
    assert missing is not None
    assert missing.alternatives == ()
    assert missing.truncated is True
    assert ModuleWarningCode.UNRESOLVED_REFERENCE in _warning_codes(result)


def test_matched_ko_block_and_optional_previews_are_independently_bounded() -> None:
    matched = evaluate_module(
        _graph(("M00001", "K00001+K00002+K00003+K00004")),
        _dataset(
            _row("K00001", "accepted"),
            _row("K00002", "accepted", sequence="protein-2"),
            _row("K00003", "accepted", sequence="protein-3"),
        ),
        ModuleEvaluationLimits(max_matched_ko_ids=2),
    )
    block = matched.missing_blocks_preview[0]

    optional = evaluate_module(
        _graph(("M00001", "K00001-(K00002+K00003+K00004)")),
        _dataset(
            _row("K00001", "accepted"),
            _row("K00002", "accepted", sequence="protein-2"),
            _row("K00003", "accepted", sequence="protein-3"),
            _row("K00004", "accepted", sequence="protein-4"),
        ),
        ModuleEvaluationLimits(max_matched_ko_ids=2),
    )

    assert block.matched_ko_ids == ("K00001", "K00002")
    assert block.matched_ko_ids_truncated is True
    assert ModuleWarningCode.OUTPUT_PREVIEW_TRUNCATED in _warning_codes(matched)
    assert optional.optional_components[0].matched_ko_ids == ("K00002", "K00003")
    assert optional.optional_components[0].matched_ko_ids_truncated is True
    assert ModuleWarningCode.OUTPUT_PREVIEW_TRUNCATED in _warning_codes(optional)


def test_shared_reference_dag_is_memoized_for_the_accepted_ko_set() -> None:
    depth = 18
    limits = ModuleEvaluationLimits(max_combination_expansions=depth * 4)
    graph = _graph(*_diamond_definitions(depth))
    result = evaluate_module(
        graph,
        _dataset(_row("K99999", "rejected")),
        limits,
    )

    assert result.evaluation_status is ModuleEvaluationStatus.INCOMPLETE
    assert len(result.provenance) == 1 + depth * 2
    missing = result.missing_blocks_preview[0].missing
    assert missing is not None
    assert [item.ko_ids for item in missing.alternatives] == [("K00001",)]
    assert missing.truncated is False


def test_unsupported_shared_dag_fails_closed_without_recursive_counting() -> None:
    graph = _graph(*_diamond_definitions(20, root_prefix="R00001+"))

    result = evaluate_module(
        graph,
        _dataset(_row("K00001", "accepted")),
    )

    assert result.evaluation_status is ModuleEvaluationStatus.NOT_EVALUABLE
    assert result.required_block_count == 1
    assert ModuleWarningCode.UNSUPPORTED_CONTENT in _warning_codes(result)


def test_large_block_result_lists_are_sliced_before_public_preview_output() -> None:
    definition = " ".join(f"K{index:05d}" for index in range(1, 201))

    result = evaluate_module(
        _graph(("M00001", definition)),
        _dataset(_row("K99999", "rejected")),
        ModuleEvaluationLimits(max_block_previews=1),
    )

    assert result.required_block_count == 200
    assert result.evaluable_required_blocks == 200
    assert len(result.missing_blocks_preview) == 1
    assert result.missing_blocks_preview[0].block_index == 1
    assert ModuleWarningCode.OUTPUT_PREVIEW_TRUNCATED in _warning_codes(result)
