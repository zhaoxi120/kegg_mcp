"""Tests for pure, bounded KEGG MODULE reference resolution."""

from collections import Counter

import pytest

from kegg_mcp.analysis import module_resolution
from kegg_mcp.analysis.contracts import (
    MODULE_RESOLVER_VERSION,
    ModuleAnalysisLimits,
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleDiagnosticCode,
    ModuleParseLimits,
    ModuleParseResult,
    ModuleReferenceIssueKind,
    ModuleResolutionLimits,
)
from kegg_mcp.analysis.module_resolution import resolve_module_definitions


def _collection(
    *definitions: tuple[str, str],
    root_module_id: str = "M00001",
) -> ModuleDefinitionCollection:
    return ModuleDefinitionCollection(
        root_module_id=root_module_id,
        definitions=tuple(
            ModuleDefinition.from_text(module_id=module_id, definition=definition)
            for module_id, definition in definitions
        ),
    )


def test_missing_reference_retains_edge_span_path_and_root_provenance() -> None:
    root = ModuleDefinition.from_text(
        module_id="M00001",
        module_name="Local root",
        definition="K00001 M00002",
    )
    collection = ModuleDefinitionCollection(root_module_id="M00001", definitions=(root,))

    graph = resolve_module_definitions(collection)

    assert graph.resolver_version == MODULE_RESOLVER_VERSION
    assert [item.definition.module_id for item in graph.modules] == ["M00001"]
    assert graph.modules[0].definition == root
    assert [(edge.source_module_id, edge.target_module_id) for edge in graph.edges] == [
        ("M00001", "M00002")
    ]
    assert graph.edges[0].source_span.start_offset == 7
    assert graph.edges[0].source_span.end_offset == 13
    assert len(graph.issues) == 1
    issue = graph.issues[0]
    assert issue.kind is ModuleReferenceIssueKind.UNRESOLVED
    assert issue.path == ("M00001", "M00002")
    assert issue.source_span == graph.edges[0].source_span
    assert graph.total_ast_nodes == graph.modules[0].parse_result.ast_node_count


def test_cycle_reports_the_complete_active_traversal_path() -> None:
    collection = _collection(
        ("M00001", "M00002"),
        ("M00002", "M00003"),
        ("M00003", "M00002"),
    )

    graph = resolve_module_definitions(collection)

    assert [item.definition.module_id for item in graph.modules] == [
        "M00001",
        "M00002",
        "M00003",
    ]
    assert [(edge.source_module_id, edge.target_module_id) for edge in graph.edges] == [
        ("M00001", "M00002"),
        ("M00002", "M00003"),
        ("M00003", "M00002"),
    ]
    assert len(graph.issues) == 1
    assert graph.issues[0].kind is ModuleReferenceIssueKind.CYCLE
    assert graph.issues[0].path == ("M00001", "M00002", "M00003", "M00002")


def test_deep_cycle_keeps_the_full_path_without_exceeding_the_bounded_message() -> None:
    module_count = 101
    definitions = tuple(
        (
            f"M{index:05d}",
            f"M{index + 1:05d}" if index < module_count else "M00001",
        )
        for index in range(1, module_count + 1)
    )
    collection = _collection(*definitions)
    limits = ModuleResolutionLimits(
        max_reference_depth=128,
        max_modules=128,
        max_references=128,
        max_total_ast_nodes=4_096,
    )

    graph = resolve_module_definitions(collection, limits)

    assert len(graph.modules) == module_count
    assert len(graph.edges) == module_count
    assert len(graph.issues) == 1
    issue = graph.issues[0]
    assert issue.kind is ModuleReferenceIssueKind.CYCLE
    assert issue.path == tuple(
        [*(f"M{index:05d}" for index in range(1, module_count + 1)), "M00001"]
    )
    assert issue.message == (
        "Module reference cycle detected; inspect path for the complete traversal."
    )
    assert len(issue.message) <= 1_000


def test_depth_limit_allows_exact_limit_and_rejects_the_next_reference() -> None:
    collection = _collection(
        ("M00001", "M00002"),
        ("M00002", "M00003"),
        ("M00003", "K00001"),
    )
    limits = ModuleResolutionLimits(max_reference_depth=1)

    graph = resolve_module_definitions(collection, limits)

    assert [item.definition.module_id for item in graph.modules] == ["M00001", "M00002"]
    assert [(edge.source_module_id, edge.target_module_id) for edge in graph.edges] == [
        ("M00001", "M00002"),
        ("M00002", "M00003"),
    ]
    assert len(graph.issues) == 1
    assert graph.issues[0].kind is ModuleReferenceIssueKind.DEPTH_LIMIT
    assert graph.issues[0].path == ("M00001", "M00002", "M00003")


def test_shared_reference_is_parsed_once_and_modules_follow_dfs_source_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _collection(
        ("M00001", "M00002,M00003"),
        ("M00002", "M00004+K00002"),
        ("M00003", "M00004+K00003"),
        ("M00004", "K00001"),
    )
    original_parse = module_resolution.parse_module_definition
    calls: Counter[str] = Counter()

    def counting_parse(
        definition: str,
        *,
        limits: ModuleParseLimits | None = None,
    ) -> ModuleParseResult:
        calls[definition] += 1
        return original_parse(definition, limits=limits)

    monkeypatch.setattr(module_resolution, "parse_module_definition", counting_parse)

    graph = resolve_module_definitions(collection)

    assert [item.definition.module_id for item in graph.modules] == [
        "M00001",
        "M00002",
        "M00004",
        "M00003",
    ]
    assert [(edge.source_module_id, edge.target_module_id) for edge in graph.edges] == [
        ("M00001", "M00002"),
        ("M00002", "M00004"),
        ("M00001", "M00003"),
        ("M00003", "M00004"),
    ]
    assert calls == Counter(item.definition for item in collection.definitions)
    assert not graph.issues


def test_invalid_referenced_definition_is_retained_and_reported() -> None:
    collection = _collection(
        ("M00001", "M00002"),
        ("M00002", "K00001+"),
    )

    graph = resolve_module_definitions(collection)

    assert [item.definition.module_id for item in graph.modules] == ["M00001", "M00002"]
    assert graph.modules[1].parse_result.is_valid is False
    assert len(graph.issues) == 1
    assert graph.issues[0].kind is ModuleReferenceIssueKind.INVALID_DEFINITION
    assert graph.issues[0].path == ("M00001", "M00002")


def test_unsupported_but_syntactically_valid_definition_is_not_mislabeled_invalid() -> None:
    collection = _collection(
        ("M00001", "M00002"),
        ("M00002", "R00001"),
    )

    graph = resolve_module_definitions(collection)

    assert graph.modules[1].parse_result.is_valid is True
    assert not graph.issues


def test_module_limit_keeps_edges_but_does_not_append_excess_definitions() -> None:
    collection = _collection(
        ("M00001", "M00002,M00003"),
        ("M00002", "K00001"),
        ("M00003", "K00002"),
    )

    graph = resolve_module_definitions(collection, ModuleResolutionLimits(max_modules=2))

    assert [item.definition.module_id for item in graph.modules] == ["M00001", "M00002"]
    assert [(edge.source_module_id, edge.target_module_id) for edge in graph.edges] == [
        ("M00001", "M00002"),
        ("M00001", "M00003"),
    ]
    assert len(graph.issues) == 1
    assert graph.issues[0].kind is ModuleReferenceIssueKind.MODULE_LIMIT
    assert graph.issues[0].target_module_id == "M00003"


def test_reference_limit_stops_traversal_at_a_bounded_edge_count() -> None:
    collection = _collection(
        ("M00001", "M00002,M00003"),
        ("M00002", "K00001"),
        ("M00003", "K00002"),
    )

    graph = resolve_module_definitions(collection, ModuleResolutionLimits(max_references=1))

    assert [(edge.source_module_id, edge.target_module_id) for edge in graph.edges] == [
        ("M00001", "M00002")
    ]
    assert [item.definition.module_id for item in graph.modules] == ["M00001", "M00002"]
    assert len(graph.issues) == 1
    assert graph.issues[0].kind is ModuleReferenceIssueKind.REFERENCE_LIMIT
    assert graph.issues[0].target_module_id == "M00003"


def test_total_node_limit_parses_once_but_excludes_definition_that_would_exceed_it() -> None:
    collection = _collection(
        ("M00001", "M00002"),
        ("M00002", "K00001"),
    )
    root_only = resolve_module_definitions(
        collection,
        ModuleResolutionLimits(max_modules=1),
    )
    root_node_count = root_only.modules[0].parse_result.ast_node_count

    graph = resolve_module_definitions(
        collection,
        ModuleResolutionLimits(max_total_ast_nodes=root_node_count),
        parse_limits=ModuleParseLimits(max_ast_nodes=root_node_count),
    )

    assert [item.definition.module_id for item in graph.modules] == ["M00001"]
    assert graph.total_ast_nodes == root_node_count
    assert graph.total_ast_nodes <= graph.limits.max_total_ast_nodes
    assert len(graph.edges) == 1
    assert len(graph.issues) == 1
    assert graph.issues[0].kind is ModuleReferenceIssueKind.TOTAL_NODE_LIMIT
    assert graph.issues[0].target_module_id == "M00002"


def test_incompatible_node_limits_are_rejected_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = _collection(("M00001", "K00001"))

    def should_not_parse(
        definition: str,
        *,
        limits: ModuleParseLimits | None = None,
    ) -> ModuleParseResult:
        del definition, limits
        pytest.fail("the resolver parsed a definition before validating its limits")

    monkeypatch.setattr(module_resolution, "parse_module_definition", should_not_parse)

    with pytest.raises(ValueError, match=r"max_ast_nodes.*max_total_ast_nodes"):
        resolve_module_definitions(
            collection,
            ModuleResolutionLimits(max_total_ast_nodes=1),
        )


@pytest.mark.parametrize(
    ("definition", "node_limit", "expected_valid"),
    [
        ("K00001", 1, True),
        ("K00001+K00002", 2, False),
    ],
)
def test_root_and_graph_never_exceed_the_total_node_limit(
    definition: str,
    node_limit: int,
    expected_valid: bool,
) -> None:
    collection = _collection(("M00001", definition))

    graph = resolve_module_definitions(
        collection,
        ModuleResolutionLimits(max_total_ast_nodes=node_limit),
        parse_limits=ModuleParseLimits(max_ast_nodes=node_limit),
    )

    assert graph.modules[0].parse_result.is_valid is expected_valid
    assert graph.total_ast_nodes <= graph.limits.max_total_ast_nodes
    assert not graph.issues


def test_malformed_root_is_expressed_by_parse_diagnostics_not_reference_issues() -> None:
    collection = _collection(("M00001", "K00001+"))

    graph = resolve_module_definitions(collection)

    root_parse = graph.modules[0].parse_result
    assert root_parse.is_valid is False
    assert ModuleDiagnosticCode.MISSING_OPERAND in {
        diagnostic.code for diagnostic in root_parse.diagnostics
    }
    assert graph.total_ast_nodes == 0
    assert graph.issues == ()


def test_analysis_limits_supply_parser_and_resolution_bounds() -> None:
    collection = _collection(
        ("M00001", "M00002"),
        ("M00002", "K00001"),
    )
    limits = ModuleAnalysisLimits(
        parsing=ModuleParseLimits(max_definition_bytes=6),
        resolution=ModuleResolutionLimits(max_reference_depth=1),
    )

    graph = resolve_module_definitions(collection, limits)

    assert graph.limits == limits.resolution
    assert all(item.parse_result.limits == limits.parsing for item in graph.modules)


def test_resolution_is_deterministic_and_total_node_count_matches_modules() -> None:
    collection = _collection(
        ("M00001", "M00002,M00003"),
        ("M00002", "K00001"),
        ("M00003", "M00004"),
        ("M00004", "K00002+K00003"),
    )

    first = resolve_module_definitions(collection)
    second = resolve_module_definitions(collection)

    assert first == second
    assert first.total_ast_nodes == sum(item.parse_result.ast_node_count for item in first.modules)
