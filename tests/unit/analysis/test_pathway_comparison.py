"""Tests for shared-reference pathway comparison across annotation datasets."""

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from kegg_mcp.analysis.comparison import ComparisonDatasetInput
from kegg_mcp.analysis.functional_comparison import (
    FunctionalComparisonLimits,
    PathwayComparisonOrganismContext,
    PathwayComparisonResult,
    compare_pathway_references,
)
from kegg_mcp.analysis.pathway_coverage import (
    OrganismGeneContext,
    PathwayCoverageLimits,
    PathwayCoverageStatus,
    PathwayCoverageWarningCode,
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
)
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS, AnalysisUnit
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
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
    ResponseOrigin,
    RetrievalEndpointClass,
)

_NOW = datetime(2026, 7, 14, 5, 0, tzinfo=UTC)
_IMPORT_LIMITS = ImportLimits(
    max_bytes=100_000,
    max_rows=1_000,
    max_columns=20,
    max_field_length=1_000,
)


def _provenance(operation: KeggOperation) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.CACHE,
        cache_lookup_state=CacheLookupState.FRESH_HIT,
        retrieved_at=_NOW,
        served_at=_NOW + timedelta(hours=1),
        expires_at=_NOW + timedelta(days=1),
        response_bytes=100,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release 116.0+/07-14",
        attempt_count=0,
        is_stale=False,
    )


def _dataset(
    rows: tuple[tuple[str, str, str], ...],
    *,
    unit: AnalysisUnit = AnalysisUnit.ISOLATE_PROTEOME,
    organism_code: str | None = "hsa",
):
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
        policy=CANONICAL_SOURCE_STATUS,
        limits=_IMPORT_LIMITS,
        analysis_unit=unit,
        taxon_id=9606 if organism_code == "hsa" else None,
        kegg_organism_code=organism_code,
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


def _reference(
    *,
    pathway_id: str = "ko00010",
    namespace: PathwayReferenceNamespace = PathwayReferenceNamespace.KO,
    scope: PathwayReferenceScope = PathwayReferenceScope.STANDARD,
    organism_code: str | None = None,
    ko_ids: tuple[str, ...] = ("K00001", "K00002"),
) -> PathwayKoReference:
    pathway_class = (
        ("Metabolism; Global and overview maps",)
        if scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
        else ("Metabolism; Carbohydrate metabolism",)
    )
    return PathwayKoReference(
        reference_namespace=namespace,
        reference_scope=scope,
        pathway_id=pathway_id,
        pathway_name=f"Synthetic {pathway_id}",
        pathway_class=pathway_class,
        kegg_organism_code=organism_code,
        reference_kos=ko_ids,
        relationship_row_count=len(ko_ids),
        link_provenance=(_provenance(KeggOperation.LINK),),
        metadata_provenance=(_provenance(KeggOperation.GET),),
    )


def _organism_contexts() -> tuple[PathwayComparisonOrganismContext, ...]:
    return tuple(
        PathwayComparisonOrganismContext(
            input_index=index,
            label=item.label,
            gene_context=OrganismGeneContext(
                kegg_organism_code="hsa",
                qualified_gene_count=10,
            ),
        )
        for index, item in enumerate(_inputs())
    )


def _json_schema_property_names(node: object) -> set[str]:
    if isinstance(node, dict):
        mapping = cast(dict[str, object], node)
        properties = mapping.get("properties")
        names = set(cast(dict[str, object], properties)) if isinstance(properties, dict) else set()
        for value in mapping.values():
            names.update(_json_schema_property_names(value))
        return names
    if isinstance(node, list):
        names: set[str] = set()
        for value in cast(list[object], node):
            names.update(_json_schema_property_names(value))
        return names
    return set()


def test_pathway_comparison_recomputes_strict_and_lenient_under_one_reference() -> None:
    reference = _reference()
    result = compare_pathway_references(_inputs(), (reference,))
    target = result.targets[0]

    assert target.reference == reference
    assert target.reference.reference_kos
    assert [item.label for item in target.strict.outcomes] == [
        "complete",
        "uncertain",
        "incomplete",
    ]
    assert [item.detected_reference_ko_count for item in target.strict.outcomes] == [2, 1, 1]
    assert [item.coverage_ratio for item in target.strict.outcomes] == [1.0, 0.5, 0.5]
    assert target.strict.outcomes_differ is True
    assert [item.detected_reference_ko_count for item in target.lenient.outcomes] == [2, 2, 1]
    assert [item.coverage_ratio for item in target.lenient.outcomes] == [1.0, 1.0, 0.5]
    assert target.lenient.outcomes_differ is True
    assert all(item.reference_unique_ko_count == 2 for item in target.strict.outcomes)
    assert all(
        PathwayCoverageWarningCode.DESCRIPTIVE_RATIO in {warning.code for warning in item.warnings}
        for item in target.strict.outcomes
    )
    assert PathwayComparisonResult.model_validate_json(result.model_dump_json()) == result


def test_empty_shared_denominator_is_not_evaluable_without_a_ratio() -> None:
    result = compare_pathway_references(_inputs(), (_reference(ko_ids=()),))
    target = result.targets[0]

    for mode in (target.strict, target.lenient):
        assert mode.evaluated_in_set_indexes == ()
        assert mode.not_evaluable_in_set_indexes == (0, 1, 2)
        assert mode.outcomes_differ is False
        assert all(
            item.evaluation_status is PathwayCoverageStatus.NOT_EVALUABLE
            and item.reference_unique_ko_count == 0
            and item.detected_reference_ko_count == 0
            and item.coverage_ratio is None
            for item in mode.outcomes
        )


def test_multiple_pathway_targets_preserve_reference_and_caller_order() -> None:
    first = _reference()
    second = _reference(
        pathway_id="map00020",
        namespace=PathwayReferenceNamespace.MAP,
        ko_ids=("K00001",),
    )
    result = compare_pathway_references(_inputs(), (second, first))

    assert [item.reference.pathway_id for item in result.targets] == ["map00020", "ko00010"]
    assert result.targets[0].strict.outcomes_differ is False
    assert all(item.coverage_ratio == 1.0 for item in result.targets[0].strict.outcomes)
    assert result.targets[0].reference.link_provenance == second.link_provenance
    assert result.targets[1].reference.metadata_provenance == first.metadata_provenance


def test_global_or_overview_reference_requires_comparison_wide_opt_in() -> None:
    broad = _reference(scope=PathwayReferenceScope.GLOBAL_OR_OVERVIEW)

    with pytest.raises(KeggMcpError) as caught:
        compare_pathway_references(_inputs(), (broad,))
    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID

    result = compare_pathway_references(
        _inputs(),
        (broad,),
        allow_global_or_overview=True,
    )
    assert result.allow_global_or_overview is True
    assert result.targets[0].allow_global_or_overview is True
    assert all(
        PathwayCoverageWarningCode.GLOBAL_OR_OVERVIEW_REFERENCE
        in {warning.code for warning in item.warnings}
        for item in result.targets[0].strict.outcomes
    )


def test_organism_reference_requires_exact_input_aligned_gene_contexts() -> None:
    organism = _reference(
        pathway_id="hsa00010",
        namespace=PathwayReferenceNamespace.ORGANISM,
        organism_code="hsa",
    )
    inputs = _inputs()

    with pytest.raises(KeggMcpError) as missing:
        compare_pathway_references(inputs, (organism,))
    assert missing.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID

    contexts = _organism_contexts()
    result = compare_pathway_references(
        inputs,
        (organism,),
        organism_contexts=contexts,
    )
    assert result.organism_contexts == contexts
    assert result.targets[0].reference.kegg_organism_code == "hsa"

    misaligned = (contexts[1], contexts[0], contexts[2])
    with pytest.raises(KeggMcpError) as caught:
        compare_pathway_references(inputs, (organism,), organism_contexts=misaligned)
    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID

    with pytest.raises(KeggMcpError) as unnecessary:
        compare_pathway_references(inputs, (_reference(),), organism_contexts=contexts)
    assert unnecessary.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID


def test_duplicate_and_excess_pathway_targets_fail_before_result_construction() -> None:
    first = _reference()
    second = _reference(
        pathway_id="map00020",
        namespace=PathwayReferenceNamespace.MAP,
    )

    with pytest.raises(KeggMcpError) as duplicate:
        compare_pathway_references(_inputs(), (first, first))
    assert duplicate.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID

    with pytest.raises(KeggMcpError) as target_limit:
        compare_pathway_references(
            _inputs(),
            (first, second),
            functional_limits=FunctionalComparisonLimits(max_pathways=1),
        )
    assert target_limit.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED

    with pytest.raises(KeggMcpError) as aggregate_limit:
        compare_pathway_references(
            _inputs(),
            (first,),
            functional_limits=FunctionalComparisonLimits(max_total_pathway_reference_kos=1),
        )
    assert aggregate_limit.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED

    with pytest.raises(KeggMcpError) as coverage_limit:
        compare_pathway_references(
            _inputs(),
            (first,),
            coverage_limits=PathwayCoverageLimits(max_reference_kos=1),
        )
    assert coverage_limit.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_pathway_comparison_schema_uses_descriptive_non_statistical_fields() -> None:
    property_names = {
        name.lower()
        for name in _json_schema_property_names(PathwayComparisonResult.model_json_schema())
    }
    forbidden = {
        "activity",
        "enrichment",
        "flux",
        "fold_change",
        "gain",
        "is_complete",
        "loss",
        "p_value",
        "pathway_present",
        "phenotype",
    }

    assert forbidden.isdisjoint(property_names), json.dumps(sorted(property_names))
