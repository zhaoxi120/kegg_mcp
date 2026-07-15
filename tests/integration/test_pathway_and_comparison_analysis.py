"""Offline integration tests for shared-reference pathway and comparison analysis."""

from datetime import UTC, datetime, timedelta

from kegg_mcp.analysis import (
    ComparedKoClass,
    ComparisonDatasetInput,
    ModuleDefinition,
    ModuleDefinitionCollection,
    ModuleEvaluationStatus,
    PathwayComparisonResult,
    PathwayCoverageParameters,
    PathwayCoverageStatus,
    PathwayReferenceNamespace,
    build_pathway_reference,
    compare_ko_datasets,
    compare_module_graphs,
    compare_pathway_references,
    evaluate_pathway_coverage,
    resolve_module_definitions,
)
from kegg_mcp.domain import (
    CANONICAL_SOURCE_STATUS_V1,
    AnalysisUnit,
    AnnotationDataset,
    EvidenceMode,
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
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggEntryRef,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggOperation,
    KeggPairRow,
    LinkRequest,
    LinkResult,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.parsers import parse_flat_file_response

_RETRIEVED_AT = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
_IMPORT_LIMITS = ImportLimits(
    max_bytes=10_000,
    max_rows=100,
    max_columns=10,
    max_field_length=1_000,
)


def _offline_provenance(operation: KeggOperation) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.OFFLINE_CACHE,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.CACHE,
        cache_lookup_state=CacheLookupState.FRESH_HIT,
        retrieved_at=_RETRIEVED_AT,
        served_at=_RETRIEVED_AT + timedelta(hours=1),
        expires_at=_RETRIEVED_AT + timedelta(days=1),
        response_bytes=256,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release 116.0+/07-14",
        attempt_count=0,
        is_stale=False,
    )


def _dataset(rows: tuple[tuple[str, str, str], ...]) -> AnnotationDataset:
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


def _datasets() -> tuple[AnnotationDataset, AnnotationDataset, AnnotationDataset]:
    complete = _dataset(
        (
            ("complete-one", "K00001", "accepted"),
            ("complete-two", "K00002", "accepted"),
            ("complete-specific", "K00005", "accepted"),
        )
    )
    uncertain = _dataset(
        (
            ("uncertain-one", "K00001", "accepted"),
            ("uncertain-two", "K00002", "uncertain"),
            ("uncertain-specific", "K00003", "accepted"),
        )
    )
    incomplete = _dataset(
        (
            ("incomplete-one", "K00001", "accepted"),
            ("incomplete-two", "K00002", "rejected"),
            ("incomplete-specific", "K00004", "accepted"),
        )
    )
    return complete, uncertain, incomplete


def _comparison_inputs(
    datasets: tuple[AnnotationDataset, AnnotationDataset, AnnotationDataset],
) -> tuple[ComparisonDatasetInput, ...]:
    return tuple(
        ComparisonDatasetInput(label=label, dataset=dataset)
        for label, dataset in zip(
            ("complete", "uncertain", "incomplete"),
            datasets,
            strict=True,
        )
    )


def _pathway_reference():
    pathway_id = "ko00010"
    link_result = LinkResult(
        request=LinkRequest(
            relationship=KeggLinkRelationship.PATHWAY_TO_KO,
            source_identifiers=(pathway_id,),
        ),
        rows=(
            KeggPairRow(
                line_number=1,
                source_id=f"path:{pathway_id}",
                target_id="ko:K00001",
            ),
            KeggPairRow(
                line_number=2,
                source_id=f"path:{pathway_id}",
                target_id="ko:K00002",
            ),
            KeggPairRow(
                line_number=3,
                source_id=f"path:{pathway_id}",
                target_id="ko:K00002",
            ),
            KeggPairRow(
                line_number=4,
                source_id=f"path:{pathway_id}",
                target_id="cpd:C00001",
            ),
        ),
        batches=(_offline_provenance(KeggOperation.LINK),),
    )
    pathway_entry = KeggEntryRef(
        database=KeggGetDatabase.PATHWAY,
        identifier=pathway_id,
    )
    document = parse_flat_file_response(
        b"ENTRY       ko00010                    Pathway\n"
        b"NAME        Synthetic carbohydrate pathway\n"
        b"CLASS       Metabolism; Carbohydrate metabolism\n"
        b"///\n"
    )
    get_result = GetResult(
        request=GetRequest(entries=(pathway_entry,)),
        documents=(document,),
        missing_entries=(),
        batches=(_offline_provenance(KeggOperation.GET),),
    )
    return build_pathway_reference(
        link_result,
        get_result,
        PathwayReferenceNamespace.KO,
    )


def _module_graph():
    return resolve_module_definitions(
        ModuleDefinitionCollection(
            root_module_id="M00001",
            definitions=(
                ModuleDefinition.from_text(
                    module_id="M00001",
                    module_name="Synthetic shared-reference module",
                    definition="K00001 K00002",
                ),
            ),
        )
    )


def test_offline_pathway_ko_and_module_analyses_share_ordered_annotation_evidence() -> None:
    datasets = _datasets()
    inputs = _comparison_inputs(datasets)
    reference = _pathway_reference()

    strict_results = tuple(evaluate_pathway_coverage(reference, dataset) for dataset in datasets)
    lenient_parameters = PathwayCoverageParameters(evidence_mode=EvidenceMode.LENIENT)
    lenient_results = tuple(
        evaluate_pathway_coverage(reference, dataset, lenient_parameters) for dataset in datasets
    )

    assert reference.reference_kos == ("K00001", "K00002")
    assert reference.relationship_row_count == 4
    assert reference.duplicate_relationship_count == 1
    assert len(reference.exclusions) == 1
    assert all(item.origin is ResponseOrigin.CACHE for item in reference.link_provenance)
    assert all(item.origin is ResponseOrigin.CACHE for item in reference.metadata_provenance)
    assert [result.evaluation_status for result in strict_results] == [
        PathwayCoverageStatus.EVALUATED,
        PathwayCoverageStatus.EVALUATED,
        PathwayCoverageStatus.EVALUATED,
    ]
    assert [result.coverage_ratio for result in strict_results] == [1.0, 0.5, 0.5]
    assert [result.coverage_ratio for result in lenient_results] == [1.0, 1.0, 0.5]
    assert [result.dataset_id for result in strict_results] == [
        dataset.dataset_id for dataset in datasets
    ]
    assert all(
        result.reference_unique_ko_count == len(reference.reference_kos)
        for result in (*strict_results, *lenient_results)
    )
    assert all(
        result.reference_link_provenance == reference.link_provenance
        and result.reference_metadata_provenance == reference.metadata_provenance
        for result in (*strict_results, *lenient_results)
    )

    pathway_comparison = compare_pathway_references(inputs, (reference,))
    pathway_target = pathway_comparison.targets[0]
    assert [item.label for item in pathway_comparison.datasets] == [
        "complete",
        "uncertain",
        "incomplete",
    ]
    assert pathway_target.reference == reference
    assert pathway_target.reference.reference_kos == reference.reference_kos
    assert pathway_target.reference.link_provenance == reference.link_provenance
    assert pathway_target.reference.metadata_provenance == reference.metadata_provenance
    assert [outcome.label for outcome in pathway_target.strict.outcomes] == [
        "complete",
        "uncertain",
        "incomplete",
    ]
    assert [outcome.coverage_ratio for outcome in pathway_target.strict.outcomes] == [
        1.0,
        0.5,
        0.5,
    ]
    assert [outcome.coverage_ratio for outcome in pathway_target.lenient.outcomes] == [
        1.0,
        1.0,
        0.5,
    ]
    assert [outcome.detected_reference_ko_count for outcome in pathway_target.strict.outcomes] == [
        2,
        1,
        1,
    ]
    assert [outcome.detected_reference_ko_count for outcome in pathway_target.lenient.outcomes] == [
        2,
        2,
        1,
    ]
    assert all(
        outcome.reference_unique_ko_count == 2
        for outcome in (*pathway_target.strict.outcomes, *pathway_target.lenient.outcomes)
    )
    assert pathway_target.strict.evaluated_in_set_indexes == (0, 1, 2)
    assert pathway_target.lenient.evaluated_in_set_indexes == (0, 1, 2)
    assert pathway_target.strict.outcomes_differ is True
    assert pathway_target.lenient.outcomes_differ is True
    assert (
        PathwayComparisonResult.model_validate_json(pathway_comparison.model_dump_json())
        == pathway_comparison
    )

    ko_comparison = compare_ko_datasets(inputs)
    accepted = next(
        partition
        for partition in ko_comparison.partitions
        if partition.ko_class is ComparedKoClass.ACCEPTED
    )
    lenient = next(
        partition
        for partition in ko_comparison.partitions
        if partition.ko_class is ComparedKoClass.LENIENT
    )
    assert [item.label for item in ko_comparison.datasets] == [
        "complete",
        "uncertain",
        "incomplete",
    ]
    assert accepted.shared_by_all == ("K00001",)
    assert [item.ko_ids for item in accepted.set_specific] == [
        ("K00002", "K00005"),
        ("K00003",),
        ("K00004",),
    ]
    assert lenient.shared_by_all == ("K00001",)
    assert lenient.partially_shared[0].member_set_indexes == (0, 1)
    assert lenient.partially_shared[0].ko_ids == ("K00002",)

    module_comparison = compare_module_graphs(inputs, (_module_graph(),))
    module_target = module_comparison.targets[0]
    assert [outcome.label for outcome in module_target.strict.outcomes] == [
        "complete",
        "uncertain",
        "incomplete",
    ]
    assert [outcome.evaluation_status for outcome in module_target.strict.outcomes] == [
        ModuleEvaluationStatus.COMPLETE,
        ModuleEvaluationStatus.INCOMPLETE,
        ModuleEvaluationStatus.INCOMPLETE,
    ]
    assert [outcome.evaluation_status for outcome in module_target.lenient.outcomes] == [
        ModuleEvaluationStatus.COMPLETE,
        ModuleEvaluationStatus.COMPLETE,
        ModuleEvaluationStatus.INCOMPLETE,
    ]
    assert module_target.strict.complete_in_set_indexes == (0,)
    assert module_target.lenient.complete_in_set_indexes == (0, 1)
