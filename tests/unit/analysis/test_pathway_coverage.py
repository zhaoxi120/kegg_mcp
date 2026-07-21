"""Tests for pure and bounded descriptive KEGG pathway KO coverage."""

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis.pathway_coverage import (
    OrganismGeneContext,
    PathwayCoverageLimits,
    PathwayCoverageParameters,
    PathwayCoverageResult,
    PathwayCoverageStatus,
    PathwayCoverageWarningCode,
    PathwayInputContext,
    PathwayInputKind,
    PathwayKoReference,
    PathwayReferenceExclusion,
    PathwayReferenceExclusionReason,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    build_pathway_reference,
    evaluate_pathway_coverage,
)
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS, AnalysisUnit, EvidenceMode
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    SourceProvenanceInput,
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

_NOW = datetime(2026, 7, 14, 3, 0, tzinfo=UTC)
_IMPORT_LIMITS = ImportLimits(
    max_bytes=100_000,
    max_rows=1_000,
    max_columns=20,
    max_field_length=1_000,
)


def _provenance(
    operation: KeggOperation,
    *,
    stale: bool = False,
) -> KeggBatchProvenance:
    expires_at = _NOW + timedelta(days=1)
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.CACHE if stale else ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.STALE_HIT if stale else CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=expires_at + timedelta(hours=1) if stale else _NOW,
        expires_at=expires_at,
        response_bytes=100,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release 116.0+/07-14",
        attempt_count=0 if stale else 1,
        is_stale=stale,
    )


def _dataset(
    *,
    analysis_unit: AnalysisUnit = AnalysisUnit.UNKNOWN,
    taxon_id: int | None = None,
    organism_code: str | None = None,
):
    return import_generic_table(
        (
            "sequence,ko,decision\n"
            "accepted-pathway,K00001,accepted\n"
            "accepted-outside,K00004,accepted\n"
            "uncertain-pathway,K00002,uncertain\n"
            "rejected-pathway,K00003,rejected\n"
        ),
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(
            sequence_id="sequence",
            ko_id="ko",
            raw_decision="decision",
        ),
        policy=CANONICAL_SOURCE_STATUS,
        limits=_IMPORT_LIMITS,
        analysis_unit=analysis_unit,
        taxon_id=taxon_id,
        kegg_organism_code=organism_code,
        source=SourceProvenanceInput(
            source_name="test_annotations",
            source_version="1",
        ),
    )


def _reference(
    *,
    namespace: PathwayReferenceNamespace = PathwayReferenceNamespace.KO,
    scope: PathwayReferenceScope = PathwayReferenceScope.STANDARD,
    pathway_id: str = "ko00010",
    organism_code: str | None = None,
    ko_ids: tuple[str, ...] = ("K00001", "K00002", "K00003"),
    exclusions: tuple[PathwayReferenceExclusion, ...] = (),
    duplicate_count: int = 0,
    stale: bool = False,
    pathway_class: tuple[str, ...] | None = None,
) -> PathwayKoReference:
    if pathway_class is None:
        pathway_class = (
            ("Metabolism; Global and overview maps",)
            if scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
            else ("Metabolism; Carbohydrate metabolism",)
        )
    return PathwayKoReference(
        reference_namespace=namespace,
        reference_scope=scope,
        pathway_id=pathway_id,
        pathway_name="Synthetic glycolysis reference",
        pathway_class=pathway_class,
        kegg_organism_code=organism_code,
        reference_kos=ko_ids,
        exclusions=exclusions,
        relationship_row_count=len(ko_ids) + len(exclusions) + duplicate_count,
        duplicate_relationship_count=duplicate_count,
        link_provenance=(_provenance(KeggOperation.LINK, stale=stale),),
        metadata_provenance=(_provenance(KeggOperation.GET),),
    )


def _link_result(
    pathway_id: str,
    targets: tuple[str, ...],
    *,
    source_id: str | None = None,
    relationship: KeggLinkRelationship = KeggLinkRelationship.PATHWAY_TO_KO,
) -> LinkResult:
    return LinkResult(
        request=LinkRequest(
            relationship=relationship,
            source_identifiers=(
                pathway_id if relationship is KeggLinkRelationship.PATHWAY_TO_KO else "K00001",
            ),
        ),
        rows=tuple(
            KeggPairRow(
                line_number=index,
                source_id=source_id or f"path:{pathway_id}",
                target_id=target,
            )
            for index, target in enumerate(targets, start=1)
        ),
        batches=(_provenance(KeggOperation.LINK),),
    )


def _get_result(
    pathway_id: str,
    *,
    pathway_name: str = "Glycolysis / Gluconeogenesis",
    pathway_class: str = "Metabolism; Carbohydrate metabolism",
    entry_type: str = "Pathway",
    include_class: bool = True,
    missing: bool = False,
) -> GetResult:
    entry = KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier=pathway_id)
    body = (
        f"ENTRY       {pathway_id}                    {entry_type}\n"
        f"NAME        {pathway_name}\n"
        + (f"CLASS       {pathway_class}\n" if include_class else "")
        + "///\n"
    ).encode()
    return GetResult(
        request=GetRequest(entries=(entry,)),
        documents=() if missing else (parse_flat_file_response(body),),
        missing_entries=(entry,) if missing else (),
        batches=(_provenance(KeggOperation.GET),),
    )


def _warning_codes(result: PathwayCoverageResult) -> set[PathwayCoverageWarningCode]:
    return {warning.code for warning in result.warnings}


def _assert_error_code(caught: pytest.ExceptionInfo[KeggMcpError], code: ErrorCode) -> None:
    assert caught.value.detail.code is code


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


def test_strict_ko_reference_uses_dataset_evidence_and_preserves_provenance() -> None:
    dataset = _dataset(
        analysis_unit=AnalysisUnit.ISOLATE_GENOME,
        taxon_id=9606,
        organism_code="hsa",
    )
    result = evaluate_pathway_coverage(_reference(), dataset)

    assert result.reference_namespace is PathwayReferenceNamespace.KO
    assert result.pathway_id == "ko00010"
    assert result.evidence_mode is EvidenceMode.STRICT
    assert result.evaluation_status is PathwayCoverageStatus.EVALUATED
    assert result.input_record_count == len(dataset.records)
    assert result.input_unique_ko_count == 2
    assert result.detected_unique_ko_count == 1
    assert result.missing_unique_ko_count == 2
    assert result.reference_unique_ko_count == 3
    assert result.coverage_ratio == 1 / 3
    assert result.detected_kos_preview == ("K00001",)
    assert result.missing_kos_preview == ("K00002", "K00003")
    assert result.dataset_id == dataset.dataset_id
    assert result.decision_policy == CANONICAL_SOURCE_STATUS.reference
    assert result.analysis_unit is AnalysisUnit.ISOLATE_GENOME
    assert result.taxon_id == 9606
    assert result.kegg_organism_code == "hsa"
    assert result.sources == dataset.sources
    assert result.reference_link_provenance[0].origin is ResponseOrigin.NETWORK
    assert result.reference_metadata_provenance[0].database_release == ("Release 116.0+/07-14")
    assert _warning_codes(result) == {
        PathwayCoverageWarningCode.DESCRIPTIVE_RATIO,
        PathwayCoverageWarningCode.ISOLATE_GENOME_CONTEXT,
    }
    descriptive = next(
        warning.message
        for warning in result.warnings
        if warning.code is PathwayCoverageWarningCode.DESCRIPTIVE_RATIO
    )
    for unsupported_claim in (
        "presence",
        "completeness",
        "expression",
        "activity",
        "flux",
        "phenotype",
    ):
        assert unsupported_claim in descriptive

    serialized = result.model_dump(mode="json")
    assert "pathway_present" not in serialized
    assert "is_complete" not in serialized
    assert PathwayCoverageResult.model_validate_json(result.model_dump_json()) == result


def test_result_json_schema_excludes_biological_and_statistical_claim_fields() -> None:
    property_names = _json_schema_property_names(PathwayCoverageResult.model_json_schema())

    assert property_names.isdisjoint(
        {
            "pathway_present",
            "is_complete",
            "activity",
            "flux",
            "phenotype",
            "p_value",
            "fold_change",
            "enrichment",
        }
    )


def test_lenient_mode_adds_only_policy_defined_uncertain_kos() -> None:
    result = evaluate_pathway_coverage(
        _reference(),
        _dataset(),
        PathwayCoverageParameters(evidence_mode=EvidenceMode.LENIENT),
    )

    assert result.input_unique_ko_count == 3
    assert result.detected_unique_ko_count == 2
    assert result.missing_unique_ko_count == 1
    assert result.coverage_ratio == 2 / 3
    assert result.detected_kos_preview == ("K00001", "K00002")
    assert result.missing_kos_preview == ("K00003",)


def test_input_context_cannot_override_dataset_analysis_unit() -> None:
    with pytest.raises(ValidationError):
        PathwayInputContext.model_validate(
            {"kind": "ko_only", "analysis_unit": "metagenomic_community"}
        )

    result = evaluate_pathway_coverage(
        _reference(),
        _dataset(analysis_unit=AnalysisUnit.MAG),
    )
    assert result.analysis_unit is AnalysisUnit.MAG
    assert PathwayCoverageWarningCode.MAG_CONTEXT in _warning_codes(result)


def test_namespace_mismatch_uses_stable_domain_error() -> None:
    reference = _reference(
        namespace=PathwayReferenceNamespace.MAP,
        pathway_id="map00010",
    )

    with pytest.raises(KeggMcpError) as caught:
        evaluate_pathway_coverage(reference, _dataset())

    _assert_error_code(caught, ErrorCode.PATHWAY_NAMESPACE_MISMATCH)


@pytest.mark.parametrize(
    "analysis_unit",
    [AnalysisUnit.ISOLATE_GENOME, AnalysisUnit.ISOLATE_PROTEOME],
)
def test_organism_reference_requires_exact_compatible_isolate_dataset(
    analysis_unit: AnalysisUnit,
) -> None:
    reference = _reference(
        namespace=PathwayReferenceNamespace.ORGANISM,
        pathway_id="hsa00010",
        organism_code="hsa",
    )
    dataset = _dataset(analysis_unit=analysis_unit, taxon_id=9606, organism_code="hsa")
    parameters = PathwayCoverageParameters(
        reference_namespace=PathwayReferenceNamespace.ORGANISM,
        input_context=PathwayInputContext(
            kind=PathwayInputKind.ORGANISM_GENE_CONTEXT,
            organism_gene_context=OrganismGeneContext(
                kegg_organism_code="hsa",
                qualified_gene_count=10,
            ),
        ),
    )

    result = evaluate_pathway_coverage(reference, dataset, parameters)

    assert result.reference_kegg_organism_code == "hsa"
    assert result.kegg_organism_code == "hsa"
    assert result.analysis_unit is analysis_unit


@pytest.mark.parametrize(
    "analysis_unit",
    [
        AnalysisUnit.MAG,
        AnalysisUnit.PANGENOME,
        AnalysisUnit.METAGENOMIC_COMMUNITY,
        AnalysisUnit.MIXED,
        AnalysisUnit.UNKNOWN,
    ],
)
def test_organism_reference_fails_closed_for_non_isolate_units(
    analysis_unit: AnalysisUnit,
) -> None:
    reference = _reference(
        namespace=PathwayReferenceNamespace.ORGANISM,
        pathway_id="hsa00010",
        organism_code="hsa",
    )
    parameters = PathwayCoverageParameters(
        reference_namespace=PathwayReferenceNamespace.ORGANISM,
        input_context=PathwayInputContext(
            kind=PathwayInputKind.ORGANISM_GENE_CONTEXT,
            organism_gene_context=OrganismGeneContext(
                kegg_organism_code="hsa",
                qualified_gene_count=10,
            ),
        ),
    )

    with pytest.raises(KeggMcpError) as caught:
        evaluate_pathway_coverage(
            reference,
            _dataset(analysis_unit=analysis_unit, organism_code="hsa"),
            parameters,
        )

    _assert_error_code(caught, ErrorCode.ANALYSIS_CONFIGURATION_INVALID)


@pytest.mark.parametrize(
    ("dataset_code", "context_code"),
    [(None, "hsa"), ("mmu", "hsa"), ("hsa", "mmu")],
)
def test_organism_reference_requires_exact_code_in_all_three_contracts(
    dataset_code: str | None,
    context_code: str,
) -> None:
    reference = _reference(
        namespace=PathwayReferenceNamespace.ORGANISM,
        pathway_id="hsa00010",
        organism_code="hsa",
    )
    parameters = PathwayCoverageParameters(
        reference_namespace=PathwayReferenceNamespace.ORGANISM,
        input_context=PathwayInputContext(
            kind=PathwayInputKind.ORGANISM_GENE_CONTEXT,
            organism_gene_context=OrganismGeneContext(
                kegg_organism_code=context_code,
                qualified_gene_count=10,
            ),
        ),
    )

    with pytest.raises(KeggMcpError) as caught:
        evaluate_pathway_coverage(
            reference,
            _dataset(
                analysis_unit=AnalysisUnit.ISOLATE_GENOME,
                organism_code=dataset_code,
            ),
            parameters,
        )

    _assert_error_code(caught, ErrorCode.ANALYSIS_CONFIGURATION_INVALID)


def test_empty_denominator_is_not_evaluable_instead_of_zero_percent() -> None:
    result = evaluate_pathway_coverage(_reference(ko_ids=()), _dataset())

    assert result.evaluation_status is PathwayCoverageStatus.NOT_EVALUABLE
    assert result.reference_unique_ko_count == 0
    assert result.detected_unique_ko_count == 0
    assert result.missing_unique_ko_count == 0
    assert result.coverage_ratio is None
    assert PathwayCoverageWarningCode.NO_REFERENCE_KOS in _warning_codes(result)


def test_scope_is_explicit_and_never_guessed_from_pathway_number() -> None:
    numbered_like_global = _reference(
        namespace=PathwayReferenceNamespace.MAP,
        pathway_id="map01100",
        scope=PathwayReferenceScope.STANDARD,
    )
    map_parameters = PathwayCoverageParameters(reference_namespace=PathwayReferenceNamespace.MAP)
    standard = evaluate_pathway_coverage(numbered_like_global, _dataset(), map_parameters)
    assert standard.reference_scope is PathwayReferenceScope.STANDARD

    explicitly_broad = _reference(
        pathway_id="ko00010",
        scope=PathwayReferenceScope.GLOBAL_OR_OVERVIEW,
    )
    with pytest.raises(KeggMcpError) as caught:
        evaluate_pathway_coverage(explicitly_broad, _dataset())
    _assert_error_code(caught, ErrorCode.ANALYSIS_CONFIGURATION_INVALID)

    broad = evaluate_pathway_coverage(
        explicitly_broad,
        _dataset(),
        PathwayCoverageParameters(allow_global_or_overview=True),
    )
    assert PathwayCoverageWarningCode.GLOBAL_OR_OVERVIEW_REFERENCE in _warning_codes(broad)


def test_direct_reference_requires_name_and_class_scope_evidence() -> None:
    valid = _reference()

    with pytest.raises(ValidationError):
        PathwayKoReference.model_validate(valid.model_dump(mode="python") | {"pathway_class": ()})
    with pytest.raises(ValidationError):
        PathwayKoReference.model_validate(valid.model_dump(mode="python") | {"pathway_name": None})
    with pytest.raises(ValidationError, match="conflicts"):
        PathwayKoReference.model_validate(
            valid.model_dump(mode="python")
            | {
                "reference_scope": PathwayReferenceScope.STANDARD,
                "pathway_class": ("Metabolism; Global and overview maps",),
            }
        )


def test_builder_uses_exact_link_rows_and_get_class_metadata() -> None:
    link = _link_result(
        "map01100",
        (
            "ko:K00002",
            "ko:K00001",
            "ko:K00001",
            "cpd:C00001",
            "ko:k00003",
        ),
    )
    get = _get_result(
        "map01100",
        pathway_name="Metabolic pathways",
        pathway_class="Metabolism; Global and overview maps",
    )

    reference = build_pathway_reference(link, get, PathwayReferenceNamespace.MAP)

    assert reference.pathway_id == "map01100"
    assert reference.pathway_name == "Metabolic pathways"
    assert reference.pathway_class == ("Metabolism; Global and overview maps",)
    assert reference.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
    assert reference.reference_kos == ("K00001", "K00002")
    assert reference.relationship_row_count == 5
    assert reference.duplicate_relationship_count == 1
    assert reference.exclusions == (
        PathwayReferenceExclusion(
            entry="cpd:C00001",
            reason=PathwayReferenceExclusionReason.NON_KO_IDENTIFIER,
        ),
        PathwayReferenceExclusion(
            entry="ko:k00003",
            reason=PathwayReferenceExclusionReason.INVALID_IDENTIFIER,
        ),
    )
    assert reference.link_provenance == link.batches
    assert reference.metadata_provenance == get.batches


@pytest.mark.parametrize("entry_type", ["Global Pathway", "Overview Pathway"])
def test_builder_accepts_current_broad_entry_metadata_without_class(
    entry_type: str,
) -> None:
    reference = build_pathway_reference(
        _link_result("ko01100", ("ko:K00001",)),
        _get_result(
            "ko01100",
            pathway_name="Metabolic pathways",
            entry_type=entry_type,
            include_class=False,
        ),
        PathwayReferenceNamespace.KO,
    )

    assert reference.reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
    assert reference.pathway_class == ("Metabolism; Global and overview maps",)


def test_builder_rejects_conflicting_entry_and_class_scope_metadata() -> None:
    with pytest.raises(KeggMcpError) as caught:
        build_pathway_reference(
            _link_result("ko01100", ("ko:K00001",)),
            _get_result(
                "ko01100",
                entry_type="Global Pathway",
                pathway_class="Metabolism; Carbohydrate metabolism",
            ),
            PathwayReferenceNamespace.KO,
        )

    _assert_error_code(caught, ErrorCode.KEGG_PARSE_FAILED)


def test_builder_rejects_unknown_entry_qualifier_even_with_standard_class() -> None:
    with pytest.raises(KeggMcpError) as caught:
        build_pathway_reference(
            _link_result("ko00010", ("ko:K00001",)),
            _get_result(
                "ko00010",
                entry_type="Weird Pathway",
                pathway_class="Metabolism; Carbohydrate metabolism",
            ),
            PathwayReferenceNamespace.KO,
        )

    _assert_error_code(caught, ErrorCode.KEGG_PARSE_FAILED)


def test_builder_accepts_an_empty_link_result_without_fabricating_coverage() -> None:
    reference = build_pathway_reference(
        _link_result("ko00010", ()),
        _get_result("ko00010"),
        PathwayReferenceNamespace.KO,
    )

    assert reference.reference_kos == ()
    assert reference.relationship_row_count == 0
    result = evaluate_pathway_coverage(reference, _dataset())
    assert result.evaluation_status is PathwayCoverageStatus.NOT_EVALUABLE
    assert result.coverage_ratio is None


def test_builder_rejects_a_link_source_that_is_not_the_exact_pathway() -> None:
    link = _link_result("ko00010", ("ko:K00001",), source_id="path:ko00020")

    with pytest.raises(KeggMcpError) as caught:
        build_pathway_reference(
            link,
            _get_result("ko00010"),
            PathwayReferenceNamespace.KO,
        )

    _assert_error_code(caught, ErrorCode.KEGG_PARSE_FAILED)


def test_builder_rejects_non_pathway_link_relationship() -> None:
    link = _link_result(
        "ko00010",
        ("path:ko00010",),
        source_id="ko:K00001",
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
    )

    with pytest.raises(KeggMcpError) as caught:
        build_pathway_reference(
            link,
            _get_result("ko00010"),
            PathwayReferenceNamespace.KO,
        )

    _assert_error_code(caught, ErrorCode.ANALYSIS_CONFIGURATION_INVALID)


def test_builder_namespace_mismatch_uses_stable_domain_error() -> None:
    with pytest.raises(KeggMcpError) as caught:
        build_pathway_reference(
            _link_result("map00010", ("ko:K00001",)),
            _get_result("map00010"),
            PathwayReferenceNamespace.KO,
        )

    _assert_error_code(caught, ErrorCode.PATHWAY_NAMESPACE_MISMATCH)


def test_builder_fails_closed_for_missing_or_ambiguous_get_metadata() -> None:
    with pytest.raises(KeggMcpError) as missing_entry:
        build_pathway_reference(
            _link_result("ko00010", ()),
            _get_result("ko00010", missing=True),
            PathwayReferenceNamespace.KO,
        )
    _assert_error_code(missing_entry, ErrorCode.KEGG_ENTRY_NOT_FOUND)

    with pytest.raises(KeggMcpError) as missing_class:
        build_pathway_reference(
            _link_result("ko00010", ()),
            _get_result("ko00010", include_class=False),
            PathwayReferenceNamespace.KO,
        )
    _assert_error_code(missing_class, ErrorCode.KEGG_PARSE_FAILED)


@pytest.mark.parametrize(
    ("analysis_unit", "expected_warning"),
    [
        (AnalysisUnit.ISOLATE_GENOME, PathwayCoverageWarningCode.ISOLATE_GENOME_CONTEXT),
        (
            AnalysisUnit.ISOLATE_PROTEOME,
            PathwayCoverageWarningCode.ISOLATE_PROTEOME_CONTEXT,
        ),
        (AnalysisUnit.MAG, PathwayCoverageWarningCode.MAG_CONTEXT),
        (AnalysisUnit.PANGENOME, PathwayCoverageWarningCode.PANGENOME_CONTEXT),
        (
            AnalysisUnit.METAGENOMIC_COMMUNITY,
            PathwayCoverageWarningCode.METAGENOMIC_COMMUNITY_CONTEXT,
        ),
        (AnalysisUnit.MIXED, PathwayCoverageWarningCode.MIXED_CONTEXT),
        (AnalysisUnit.UNKNOWN, PathwayCoverageWarningCode.UNKNOWN_CONTEXT),
    ],
)
def test_every_analysis_unit_has_an_explicit_interpretation_warning(
    analysis_unit: AnalysisUnit,
    expected_warning: PathwayCoverageWarningCode,
) -> None:
    result = evaluate_pathway_coverage(
        _reference(),
        _dataset(analysis_unit=analysis_unit),
    )

    unit_warnings = _warning_codes(result).intersection(
        {
            PathwayCoverageWarningCode.ISOLATE_GENOME_CONTEXT,
            PathwayCoverageWarningCode.ISOLATE_PROTEOME_CONTEXT,
            PathwayCoverageWarningCode.MAG_CONTEXT,
            PathwayCoverageWarningCode.PANGENOME_CONTEXT,
            PathwayCoverageWarningCode.METAGENOMIC_COMMUNITY_CONTEXT,
            PathwayCoverageWarningCode.MIXED_CONTEXT,
            PathwayCoverageWarningCode.UNKNOWN_CONTEXT,
        }
    )
    assert unit_warnings == {expected_warning}


def test_stale_excluded_community_result_has_bounded_previews_and_warnings() -> None:
    exclusions = tuple(
        PathwayReferenceExclusion(
            entry=f"bad-{index}",
            reason=PathwayReferenceExclusionReason.INVALID_IDENTIFIER,
        )
        for index in range(1, 4)
    )
    reference = _reference(
        ko_ids=("K00001", "K00002", "K00003", "K00004", "K00005"),
        exclusions=exclusions,
        duplicate_count=2,
        stale=True,
    )
    limits = PathwayCoverageLimits(
        max_detected_preview=1,
        max_missing_preview=1,
        max_exclusion_preview=1,
    )
    parameters = PathwayCoverageParameters(evidence_mode=EvidenceMode.LENIENT)

    result = evaluate_pathway_coverage(
        reference,
        _dataset(analysis_unit=AnalysisUnit.METAGENOMIC_COMMUNITY),
        parameters,
        limits,
    )

    assert result.detected_unique_ko_count == 3
    assert result.missing_unique_ko_count == 2
    assert result.detected_kos_preview == ("K00001",)
    assert result.missing_kos_preview == ("K00003",)
    assert result.exclusions_preview == (exclusions[0],)
    assert result.reference_link_provenance[0].origin is ResponseOrigin.CACHE
    assert _warning_codes(result) == {
        PathwayCoverageWarningCode.DESCRIPTIVE_RATIO,
        PathwayCoverageWarningCode.STALE_REFERENCE,
        PathwayCoverageWarningCode.REFERENCE_EXCLUSIONS,
        PathwayCoverageWarningCode.DUPLICATE_RELATIONSHIPS,
        PathwayCoverageWarningCode.METAGENOMIC_COMMUNITY_CONTEXT,
        PathwayCoverageWarningCode.DETECTED_PREVIEW_TRUNCATED,
        PathwayCoverageWarningCode.MISSING_PREVIEW_TRUNCATED,
        PathwayCoverageWarningCode.EXCLUSION_PREVIEW_TRUNCATED,
    }


def test_input_reference_and_exclusion_limits_use_input_limit_error() -> None:
    exclusion = PathwayReferenceExclusion(
        entry="bad-entry",
        reason=PathwayReferenceExclusionReason.UNSUPPORTED_ENTRY,
    )
    cases = (
        (_reference(), PathwayCoverageLimits(max_input_kos=1)),
        (_reference(), PathwayCoverageLimits(max_reference_kos=2)),
        (
            _reference(exclusions=(exclusion,)),
            PathwayCoverageLimits(max_reference_exclusions=0),
        ),
    )

    for reference, limits in cases:
        with pytest.raises(KeggMcpError) as caught:
            evaluate_pathway_coverage(reference, _dataset(), limits=limits)
        _assert_error_code(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)


def test_builder_relationship_limit_uses_input_limit_error() -> None:
    with pytest.raises(KeggMcpError) as caught:
        build_pathway_reference(
            _link_result("ko00010", ("ko:K00001", "ko:K00002")),
            _get_result("ko00010"),
            PathwayReferenceNamespace.KO,
            limits=PathwayCoverageLimits(max_relationship_rows=1),
        )

    _assert_error_code(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)


@pytest.mark.parametrize(
    ("targets", "limits"),
    [
        (
            ("ko:K00001", "ko:K00002"),
            PathwayCoverageLimits(max_reference_kos=1),
        ),
        (
            ("cpd:C00001",),
            PathwayCoverageLimits(max_reference_exclusions=0),
        ),
    ],
)
def test_builder_unique_reference_limits_use_input_limit_error(
    targets: tuple[str, ...],
    limits: PathwayCoverageLimits,
) -> None:
    with pytest.raises(KeggMcpError) as caught:
        build_pathway_reference(
            _link_result("ko00010", targets),
            _get_result("ko00010"),
            PathwayReferenceNamespace.KO,
            limits=limits,
        )

    _assert_error_code(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)


def test_input_record_limit_fails_before_evidence_view_construction() -> None:
    with pytest.raises(KeggMcpError) as caught:
        evaluate_pathway_coverage(
            _reference(),
            _dataset(),
            limits=PathwayCoverageLimits(max_input_records=1),
        )

    _assert_error_code(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)


def test_zero_preview_limits_return_empty_previews_with_explicit_truncation() -> None:
    result = evaluate_pathway_coverage(
        _reference(),
        _dataset(),
        limits=PathwayCoverageLimits(
            max_detected_preview=0,
            max_missing_preview=0,
            max_exclusion_preview=0,
        ),
    )

    assert result.detected_kos_preview == ()
    assert result.missing_kos_preview == ()
    assert result.detected_preview_truncated is True
    assert result.missing_preview_truncated is True
