"""Tests for bounded KEGG request preparation."""

import pytest

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg.contracts import (
    ConvRequest,
    FindRequest,
    GetRequest,
    InfoRequest,
    KeggBriteEntryKind,
    KeggClientLimits,
    KeggConvDatabase,
    KeggEntryRef,
    KeggFindDatabase,
    KeggFindMode,
    KeggGetDatabase,
    KeggInfoDatabase,
    KeggLinkRelationship,
    KeggTaxonomyRank,
    LinkRequest,
    OrganismPathwayListRequest,
)
from kegg_mcp.kegg.operations import (
    PairTargetDatabase,
    ResponseParser,
    get_entry_matches,
    pair_target_matches,
    prepare_conv,
    prepare_find,
    prepare_get,
    prepare_info,
    prepare_link,
    prepare_organism_pathway_list,
)
from kegg_mcp.kegg.parsers import parse_flat_file_response


def _ko(number: int) -> KeggEntryRef:
    return KeggEntryRef(database=KeggGetDatabase.KO, identifier=f"K{number:05d}")


def test_info_preparation_uses_only_the_typed_database() -> None:
    batches = prepare_info(InfoRequest(database=KeggInfoDatabase.KO), KeggClientLimits())

    assert len(batches) == 1
    assert batches[0].path == "/info/ko"
    assert batches[0].parser is ResponseParser.INFO


def test_organism_pathway_list_preparation_has_one_fixed_typed_route() -> None:
    prepared = prepare_organism_pathway_list(
        OrganismPathwayListRequest(organism="hsa"),
        KeggClientLimits(),
    )

    assert prepared.operation.value == "list"
    assert prepared.path == "/list/pathway/hsa"
    assert prepared.normalized_request_key == prepared.path
    assert prepared.parser is ResponseParser.ORGANISM_PATHWAY_LIST
    assert prepared.list_organism == "hsa"


def test_find_preparation_percent_encodes_the_query_as_one_path_segment() -> None:
    request = FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query="β-D-glucose + water \u6c34 100%",
    )

    prepared = prepare_find(request, KeggClientLimits())[0]

    assert prepared.operation.value == "find"
    assert prepared.path == ("/find/compound/%CE%B2-D-glucose%20%2B%20water%20%E6%B0%B4%20100%25")
    assert prepared.normalized_request_key == prepared.path
    assert prepared.parser is ResponseParser.FIND_TABLE
    assert prepared.find_database is KeggFindDatabase.COMPOUND


def test_find_preparation_maps_the_organism_alias_to_genome_on_the_wire() -> None:
    prepared = prepare_find(
        FindRequest(database=KeggFindDatabase.ORGANISM, query="Escherichia coli"),
        KeggClientLimits(),
    )[0]

    assert prepared.path == "/find/genome/Escherichia%20coli"
    assert prepared.find_database is KeggFindDatabase.ORGANISM


def test_gene_find_preparation_uses_the_typed_organism_as_the_wire_database() -> None:
    prepared = prepare_find(
        FindRequest(
            database=KeggFindDatabase.GENES,
            query="tumor protein p53",
            organism="hsa",
        ),
        KeggClientLimits(),
    )[0]

    assert prepared.path == "/find/hsa/tumor%20protein%20p53"
    assert prepared.find_database is KeggFindDatabase.GENES
    assert prepared.find_organism == "hsa"


@pytest.mark.parametrize(
    ("mode", "suffix"),
    [
        (KeggFindMode.FORMULA, "formula"),
        (KeggFindMode.EXACT_MASS, "exact_mass"),
        (KeggFindMode.MOL_WEIGHT, "mol_weight"),
    ],
)
def test_compound_find_preparation_uses_only_allowlisted_mode_suffixes(
    mode: KeggFindMode,
    suffix: str,
) -> None:
    query = "C7H10O5" if mode is KeggFindMode.FORMULA else "174.05"
    prepared = prepare_find(
        FindRequest(database=KeggFindDatabase.COMPOUND, query=query, mode=mode),
        KeggClientLimits(),
    )[0]

    assert prepared.path == f"/find/compound/{query}/{suffix}"


@pytest.mark.parametrize(
    ("database", "query", "mode", "expected_path"),
    [
        (
            KeggFindDatabase.DRUG,
            "174.05",
            KeggFindMode.EXACT_MASS,
            "/find/drug/174.05/exact_mass",
        ),
        (
            KeggFindDatabase.GLYCAN,
            "mannose",
            KeggFindMode.KEYWORD,
            "/find/glycan/mannose",
        ),
        (
            KeggFindDatabase.RCLASS,
            "phosphotransfer",
            KeggFindMode.KEYWORD,
            "/find/rclass/phosphotransfer",
        ),
    ],
)
def test_extended_find_scopes_use_fixed_database_routes(
    database: KeggFindDatabase,
    query: str,
    mode: KeggFindMode,
    expected_path: str,
) -> None:
    prepared = prepare_find(
        FindRequest(database=database, query=query, mode=mode),
        KeggClientLimits(),
    )[0]

    assert prepared.path == expected_path


def test_find_preparation_accounts_for_the_endpoint_in_the_url_bound() -> None:
    request = FindRequest(database=KeggFindDatabase.KO, query="a" * 240)

    with pytest.raises(KeggMcpError) as caught:
        prepare_find(
            request,
            KeggClientLimits(max_url_bytes=256),
            url_prefix_bytes=len("https://rest.kegg.jp"),
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_find_request_key_can_exceed_the_old_provenance_limit_within_the_url_bound() -> None:
    request = FindRequest(database=KeggFindDatabase.KO, query="a" * 4_096)
    limits = KeggClientLimits(max_url_bytes=8_192)

    prepared = prepare_find(request, limits)[0]

    assert len(prepared.normalized_request_key) > 4_096
    assert len(prepared.normalized_request_key.encode("ascii")) <= limits.max_url_bytes


def test_get_never_batches_more_than_ten_entries() -> None:
    batches = prepare_get(
        GetRequest(entries=tuple(_ko(index) for index in range(1, 24))), KeggClientLimits()
    )

    assert [len(batch.requested_entries) for batch in batches] == [10, 10, 3]
    assert all(batch.path.count("+") <= 9 for batch in batches)


def test_get_isolates_brite_htext_from_flat_file_batches() -> None:
    request = GetRequest(
        entries=(
            _ko(1),
            KeggEntryRef(
                database=KeggGetDatabase.BRITE,
                identifier="br08901",
                brite_kind=KeggBriteEntryKind.HIERARCHY,
            ),
            _ko(2),
        )
    )

    batches = prepare_get(request, KeggClientLimits())

    assert [batch.parser for batch in batches] == [
        ResponseParser.FLAT_FILE,
        ResponseParser.BRITE_HTEXT,
        ResponseParser.FLAT_FILE,
    ]
    assert batches[1].path == "/get/br:br08901"


def test_extended_substance_and_rclass_get_entries_remain_text_only() -> None:
    prepared = prepare_get(
        GetRequest(
            entries=(
                KeggEntryRef(database=KeggGetDatabase.GLYCAN, identifier="G00001"),
                KeggEntryRef(database=KeggGetDatabase.DRUG, identifier="D00109"),
                KeggEntryRef(database=KeggGetDatabase.RCLASS, identifier="RC00002"),
            )
        ),
        KeggClientLimits(),
    )

    assert len(prepared) == 1
    assert prepared[0].path == "/get/G00001+D00109+RC00002"
    assert prepared[0].parser is ResponseParser.FLAT_FILE


def test_get_preparation_uses_selected_gene_and_qualified_genome_wire_identifiers() -> None:
    request = GetRequest(
        entries=(
            KeggEntryRef(database=KeggGetDatabase.GENE, identifier="hsa:10458"),
            KeggEntryRef(database=KeggGetDatabase.GENOME, identifier="hsa"),
            KeggEntryRef(database=KeggGetDatabase.GENOME, identifier="T01001"),
        )
    )

    prepared = prepare_get(request, KeggClientLimits())

    assert [batch.path for batch in prepared] == [
        "/get/hsa:10458+gn:hsa",
        "/get/gn:T01001",
    ]


def test_get_separates_gene_aliases_with_the_same_suffix() -> None:
    request = GetRequest(
        entries=(
            KeggEntryRef(database=KeggGetDatabase.GENE, identifier="hsa:10458"),
            KeggEntryRef(database=KeggGetDatabase.GENE, identifier="T01001:10458"),
        )
    )

    prepared = prepare_get(request, KeggClientLimits())

    assert [batch.path for batch in prepared] == [
        "/get/hsa:10458",
        "/get/T01001:10458",
    ]


@pytest.mark.parametrize(
    ("requested", "body"),
    [
        (
            KeggEntryRef(database=KeggGetDatabase.GENE, identifier="hsa:10458"),
            b"ENTRY       10458             CDS       T01001\nORGANISM    hsa  Homo sapiens\n///\n",
        ),
        (
            KeggEntryRef(database=KeggGetDatabase.GENE, identifier="T01001:10458"),
            b"ENTRY       10458             CDS       T01001\nORGANISM    hsa  Homo sapiens\n///\n",
        ),
        (
            KeggEntryRef(database=KeggGetDatabase.GENOME, identifier="hsa"),
            b"ENTRY       T01001            Complete  Genome\nORG_CODE    hsa\n///\n",
        ),
        (
            KeggEntryRef(database=KeggGetDatabase.GENOME, identifier="T01001"),
            b"ENTRY       T01001            Complete  Genome\nORG_CODE    hsa\n///\n",
        ),
    ],
)
def test_get_entry_matching_reconciles_gene_and_genome_flat_file_identifiers(
    requested: KeggEntryRef,
    body: bytes,
) -> None:
    returned = parse_flat_file_response(body).entries[0]

    assert get_entry_matches(requested, returned)


def test_link_uses_bounded_selected_identifier_batches() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_MODULE,
        source_identifiers=tuple(f"K{index:05d}" for index in range(1, 6)),
    )

    batches = prepare_link(request, KeggClientLimits(link_batch_size=2))

    assert [batch.requested_identifiers for batch in batches] == [
        ("K00001", "K00002"),
        ("K00003", "K00004"),
        ("K00005",),
    ]
    assert batches[0].path == "/link/module/K00001+K00002"
    assert batches[0].expected_pair_source_ids == frozenset({"ko:K00001", "ko:K00002"})
    assert batches[0].pair_target_database is PairTargetDatabase.MODULE


@pytest.mark.parametrize(
    ("relationship", "source", "path", "expected_source", "target_database"),
    [
        (
            KeggLinkRelationship.KO_TO_PATHWAY,
            "K00001",
            "/link/pathway/K00001",
            "ko:K00001",
            PairTargetDatabase.PATHWAY,
        ),
        (
            KeggLinkRelationship.KO_TO_MODULE,
            "K00001",
            "/link/module/K00001",
            "ko:K00001",
            PairTargetDatabase.MODULE,
        ),
        (
            KeggLinkRelationship.KO_TO_REACTION,
            "K00001",
            "/link/reaction/K00001",
            "ko:K00001",
            PairTargetDatabase.REACTION,
        ),
        (
            KeggLinkRelationship.KO_TO_ENZYME,
            "K00001",
            "/link/enzyme/K00001",
            "ko:K00001",
            PairTargetDatabase.ENZYME,
        ),
        (
            KeggLinkRelationship.KO_TO_BRITE,
            "K00001",
            "/link/brite/K00001",
            "ko:K00001",
            PairTargetDatabase.BRITE,
        ),
        (
            KeggLinkRelationship.PATHWAY_TO_KO,
            "map00010",
            "/link/ko/map00010",
            "path:map00010",
            PairTargetDatabase.KO,
        ),
        (
            KeggLinkRelationship.GENE_TO_KO,
            "hsa:10458",
            "/link/ko/hsa:10458",
            "hsa:10458",
            PairTargetDatabase.KO,
        ),
        (
            KeggLinkRelationship.GENE_TO_PATHWAY,
            "hsa:10458",
            "/link/pathway/hsa:10458",
            "hsa:10458",
            PairTargetDatabase.PATHWAY,
        ),
        (
            KeggLinkRelationship.ENZYME_TO_REACTION,
            "1.1.1.1",
            "/link/reaction/ec:1.1.1.1",
            "ec:1.1.1.1",
            PairTargetDatabase.REACTION,
        ),
        (
            KeggLinkRelationship.REACTION_TO_ENZYME,
            "R00001",
            "/link/enzyme/R00001",
            "rn:R00001",
            PairTargetDatabase.ENZYME,
        ),
        (
            KeggLinkRelationship.REACTION_TO_KO,
            "R00001",
            "/link/ko/R00001",
            "rn:R00001",
            PairTargetDatabase.KO,
        ),
        (
            KeggLinkRelationship.REACTION_TO_COMPOUND,
            "R00001",
            "/link/compound/R00001",
            "rn:R00001",
            PairTargetDatabase.COMPOUND,
        ),
        (
            KeggLinkRelationship.REACTION_TO_PATHWAY,
            "R00001",
            "/link/pathway/R00001",
            "rn:R00001",
            PairTargetDatabase.PATHWAY,
        ),
        (
            KeggLinkRelationship.COMPOUND_TO_REACTION,
            "C00031",
            "/link/reaction/C00031",
            "cpd:C00031",
            PairTargetDatabase.REACTION,
        ),
        (
            KeggLinkRelationship.COMPOUND_TO_PATHWAY,
            "C00031",
            "/link/pathway/C00031",
            "cpd:C00031",
            PairTargetDatabase.PATHWAY,
        ),
        (
            KeggLinkRelationship.PATHWAY_TO_REACTION,
            "map00010",
            "/link/reaction/map00010",
            "path:map00010",
            PairTargetDatabase.REACTION,
        ),
        (
            KeggLinkRelationship.PATHWAY_TO_COMPOUND,
            "map00010",
            "/link/compound/map00010",
            "path:map00010",
            PairTargetDatabase.COMPOUND,
        ),
        (
            KeggLinkRelationship.GENOME_TO_TAXONOMY,
            "T01001",
            "/link/taxonomy/gn:T01001",
            "gn:T01001",
            PairTargetDatabase.TAXONOMY,
        ),
        (
            KeggLinkRelationship.GENOME_TO_TAXONOMY,
            "hsa",
            "/link/taxonomy/gn:hsa",
            "gn:hsa",
            PairTargetDatabase.TAXONOMY,
        ),
        (
            KeggLinkRelationship.TAXONOMY_TO_GENOME,
            "taxid:9606",
            "/link/genome/taxid:9606",
            "taxid:9606",
            PairTargetDatabase.GENOME,
        ),
    ],
)
def test_link_preparation_uses_the_authoritative_relation_contract(
    relationship: KeggLinkRelationship,
    source: str,
    path: str,
    expected_source: str,
    target_database: PairTargetDatabase,
) -> None:
    prepared = prepare_link(
        LinkRequest(relationship=relationship, source_identifiers=(source,)),
        KeggClientLimits(),
    )[0]

    assert prepared.path == path
    assert prepared.expected_pair_source_ids == frozenset({expected_source})
    assert prepared.pair_target_database is target_database


@pytest.mark.parametrize(
    ("relationship", "source", "path", "expected_source", "target_database"),
    [
        (
            KeggLinkRelationship.MODULE_TO_KO,
            "M00001",
            "/link/ko/M00001",
            "md:M00001",
            PairTargetDatabase.KO,
        ),
        (
            KeggLinkRelationship.MODULE_TO_PATHWAY,
            "M00001",
            "/link/pathway/M00001",
            "md:M00001",
            PairTargetDatabase.PATHWAY,
        ),
        (
            KeggLinkRelationship.MODULE_TO_REACTION,
            "M00001",
            "/link/reaction/M00001",
            "md:M00001",
            PairTargetDatabase.REACTION,
        ),
        (
            KeggLinkRelationship.PATHWAY_TO_MODULE,
            "map00010",
            "/link/module/map00010",
            "path:map00010",
            PairTargetDatabase.MODULE,
        ),
        (
            KeggLinkRelationship.GLYCAN_TO_REACTION,
            "G00001",
            "/link/reaction/G00001",
            "gl:G00001",
            PairTargetDatabase.REACTION,
        ),
        (
            KeggLinkRelationship.REACTION_TO_GLYCAN,
            "R05969",
            "/link/glycan/R05969",
            "rn:R05969",
            PairTargetDatabase.GLYCAN,
        ),
        (
            KeggLinkRelationship.PATHWAY_TO_GLYCAN,
            "map00510",
            "/link/glycan/map00510",
            "path:map00510",
            PairTargetDatabase.GLYCAN,
        ),
        (
            KeggLinkRelationship.DRUG_TO_PATHWAY,
            "D00109",
            "/link/pathway/D00109",
            "dr:D00109",
            PairTargetDatabase.PATHWAY,
        ),
    ],
)
def test_extended_relation_contracts_remain_selected_entry_only(
    relationship: KeggLinkRelationship,
    source: str,
    path: str,
    expected_source: str,
    target_database: PairTargetDatabase,
) -> None:
    prepared = prepare_link(
        LinkRequest(relationship=relationship, source_identifiers=(source,)),
        KeggClientLimits(),
    )[0]

    assert prepared.path == path
    assert prepared.expected_pair_source_ids == frozenset({expected_source})
    assert prepared.pair_target_database is target_database


@pytest.mark.parametrize(
    ("relationship", "source", "organism", "path", "expected_source"),
    [
        (
            KeggLinkRelationship.KO_TO_GENE,
            "K01810",
            "eco",
            "/link/eco/K01810",
            "ko:K01810",
        ),
        (
            KeggLinkRelationship.PATHWAY_TO_GENE,
            "hsa00010",
            "hsa",
            "/link/hsa/hsa00010",
            "path:hsa00010",
        ),
    ],
)
def test_organism_scoped_gene_link_uses_a_validated_dynamic_target(
    relationship: KeggLinkRelationship,
    source: str,
    organism: str,
    path: str,
    expected_source: str,
) -> None:
    prepared = prepare_link(
        LinkRequest(
            relationship=relationship,
            organism_scope=organism,
            source_identifiers=(source,),
        ),
        KeggClientLimits(),
    )[0]

    assert prepared.path == path
    assert prepared.expected_pair_source_ids == frozenset({expected_source})
    assert prepared.pair_target_database is PairTargetDatabase.GENES


def test_taxonomy_species_link_appends_the_typed_rank_suffix() -> None:
    prepared = prepare_link(
        LinkRequest(
            relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
            taxonomy_rank=KeggTaxonomyRank.SPECIES,
            source_identifiers=("taxid:562",),
        ),
        KeggClientLimits(),
    )[0]

    assert prepared.path == "/link/genome/taxid:562/species"
    assert prepared.normalized_request_key == prepared.path
    assert prepared.expected_pair_source_ids == frozenset({"taxid:562"})
    assert prepared.pair_target_database is PairTargetDatabase.GENOME


def test_taxonomy_species_suffix_is_included_in_the_url_size_bound() -> None:
    taxid = f"taxid:{'1' * 230}"
    limits = KeggClientLimits(max_url_bytes=256)

    exact = prepare_link(
        LinkRequest(
            relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
            source_identifiers=(taxid,),
        ),
        limits,
    )[0]

    assert len(exact.path.encode("ascii")) <= limits.max_url_bytes
    with pytest.raises(KeggMcpError) as caught:
        prepare_link(
            LinkRequest(
                relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
                taxonomy_rank=KeggTaxonomyRank.SPECIES,
                source_identifiers=(taxid,),
            ),
            limits,
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_link_greedily_packs_seventy_three_kos_into_one_default_request() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=tuple(f"K{index:05d}" for index in range(1, 74)),
    )

    batches = prepare_link(request, KeggClientLimits())

    assert len(batches) == 1
    assert len(batches[0].requested_identifiers) == 73
    assert batches[0].normalized_request_key.startswith("/link/pathway/")


def test_link_greedy_batches_are_canonical_and_respect_the_url_limit() -> None:
    identifiers = tuple(f"K{index:05d}" for index in range(1, 101))
    forward = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=identifiers,
    )
    reversed_request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=tuple(reversed(identifiers)),
    )
    limits = KeggClientLimits(max_url_bytes=256)
    endpoint_bytes = len("https://rest.kegg.jp".encode("ascii"))

    batches = prepare_link(forward, limits, url_prefix_bytes=endpoint_bytes)
    reversed_batches = prepare_link(
        reversed_request,
        limits,
        url_prefix_bytes=endpoint_bytes,
    )

    assert len(batches) > 1
    assert [batch.normalized_request_key for batch in batches] == [
        batch.normalized_request_key for batch in reversed_batches
    ]
    assert all(
        endpoint_bytes + len(batch.path.encode("ascii")) <= limits.max_url_bytes
        for batch in batches
    )
    assert (
        tuple(identifier for batch in batches for identifier in batch.requested_identifiers)
        == identifiers
    )


def test_conv_never_prepares_a_whole_database_conversion() -> None:
    request = ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.UNIPROT,
        source_identifiers=("uniprot:P12345", "uniprot:Q00001"),
    )

    batches = prepare_conv(request, KeggClientLimits(relation_batch_size=1))

    assert [batch.path for batch in batches] == [
        "/conv/genes/uniprot:P12345",
        "/conv/genes/uniprot:Q00001",
    ]
    assert batches[0].expected_pair_source_ids == frozenset({"uniprot:P12345", "up:P12345"})
    assert batches[0].pair_target_database is PairTargetDatabase.GENES


@pytest.mark.parametrize(
    ("target", "source", "identifier", "path", "expected_source", "pair_target"),
    [
        (
            KeggConvDatabase.COMPOUND,
            KeggConvDatabase.CHEBI,
            "chebi:4167",
            "/conv/compound/chebi:4167",
            "chebi:4167",
            PairTargetDatabase.COMPOUND,
        ),
        (
            KeggConvDatabase.DRUG,
            KeggConvDatabase.PUBCHEM,
            "pubchem:7847177",
            "/conv/drug/pubchem:7847177",
            "pubchem:7847177",
            PairTargetDatabase.DRUG,
        ),
        (
            KeggConvDatabase.PUBCHEM,
            KeggConvDatabase.COMPOUND,
            "C00031",
            "/conv/pubchem/C00031",
            "cpd:C00031",
            PairTargetDatabase.PUBCHEM,
        ),
    ],
)
def test_selected_substance_conv_uses_explicit_sid_and_chebi_namespaces(
    target: KeggConvDatabase,
    source: KeggConvDatabase,
    identifier: str,
    path: str,
    expected_source: str,
    pair_target: PairTargetDatabase,
) -> None:
    prepared = prepare_conv(
        ConvRequest(
            target_database=target,
            source_database=source,
            source_identifiers=(identifier,),
        ),
        KeggClientLimits(),
    )[0]

    assert prepared.path == path
    assert prepared.expected_pair_source_ids == frozenset({expected_source})
    assert prepared.pair_target_database is pair_target


@pytest.mark.parametrize(
    ("database", "identifier"),
    [
        (PairTargetDatabase.PATHWAY, "path:map00010"),
        (PairTargetDatabase.PATHWAY, "path:hsa00010"),
        (PairTargetDatabase.PATHWAY, "path:ddi00230"),
        (PairTargetDatabase.PATHWAY, "path:vg00541"),
        (PairTargetDatabase.PATHWAY, "path:vx00541"),
        (PairTargetDatabase.MODULE, "md:M00001"),
        (PairTargetDatabase.REACTION, "rn:R00001"),
        (PairTargetDatabase.ENZYME, "ec:1.1.1.1"),
        (PairTargetDatabase.ENZYME, "ec:1.1.1.-"),
        (PairTargetDatabase.ENZYME, "enzyme:1.-.-.-"),
        (PairTargetDatabase.BRITE, "br:ko00001"),
        (PairTargetDatabase.BRITE, "br:hsa00001"),
        (PairTargetDatabase.BRITE, "br:ddi00001"),
        (PairTargetDatabase.KO, "ko:K00001"),
        (PairTargetDatabase.GENES, "T01001:10458"),
        (PairTargetDatabase.GENES, "hsa:10458"),
        (PairTargetDatabase.GENES, "ddi:DDB_G0291764"),
        (PairTargetDatabase.GENES, "ag:ENTRY1"),
        (PairTargetDatabase.NCBI_GENEID, "ncbi-geneid:948364"),
        (PairTargetDatabase.UNIPROT, "up:P12345"),
        (PairTargetDatabase.COMPOUND, "cpd:C00031"),
        (PairTargetDatabase.GENOME, "gn:hsa"),
        (PairTargetDatabase.GENOME, "gn:T01001"),
        (PairTargetDatabase.TAXONOMY, "taxid:9606"),
    ],
)
def test_pair_target_contract_accepts_expected_namespaces(
    database: PairTargetDatabase, identifier: str
) -> None:
    assert pair_target_matches(database, identifier)


def test_pair_target_contract_rejects_a_wrong_relationship_namespace() -> None:
    assert not pair_target_matches(PairTargetDatabase.MODULE, "path:map00010")


@pytest.mark.parametrize(
    ("database", "identifier"),
    [
        (PairTargetDatabase.PATHWAY, "path:module00010"),
        (PairTargetDatabase.PATHWAY, "path:path00010"),
        (PairTargetDatabase.BRITE, "br:module00010"),
        (PairTargetDatabase.BRITE, "br:map00010"),
        (PairTargetDatabase.ENZYME, "ec:0.1.1.1"),
        (PairTargetDatabase.ENZYME, "ec:8.1.1.1"),
        (PairTargetDatabase.ENZYME, "ec:1.--.1.1"),
        (PairTargetDatabase.ENZYME, "ec:1.1-2.3.4"),
        (PairTargetDatabase.ENZYME, "ec:1.1.-.1"),
        (PairTargetDatabase.GENES, "uniprot:P12345"),
        (PairTargetDatabase.GENES, "ko:K00001"),
        (PairTargetDatabase.GENES, "vtax:1234"),
        (PairTargetDatabase.NCBI_GENEID, "ncbi-geneid:not-a-number"),
        (PairTargetDatabase.NCBI_GENEID, "ncbi-geneid:0"),
        (PairTargetDatabase.COMPOUND, "cpd:R00001"),
        (PairTargetDatabase.GENOME, "gn:ko"),
        (PairTargetDatabase.GENOME, "gn:T1001"),
        (PairTargetDatabase.TAXONOMY, "taxid:0"),
        (PairTargetDatabase.TAXONOMY, "taxonomy:not-a-number"),
    ],
)
def test_pair_target_contract_rejects_invalid_internal_namespaces(
    database: PairTargetDatabase, identifier: str
) -> None:
    assert not pair_target_matches(database, identifier)


def test_service_limit_is_enforced_before_batching() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.KO_TO_PATHWAY,
        source_identifiers=("K00001", "K00002"),
    )

    with pytest.raises(KeggMcpError) as caught:
        prepare_link(request, KeggClientLimits(max_identifiers=1))

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED


def test_preparation_rejects_a_url_above_the_configured_bound() -> None:
    request = ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.UNIPROT,
        source_identifiers=("uniprot:" + "A" * 240,),
    )

    with pytest.raises(KeggMcpError) as caught:
        prepare_conv(request, KeggClientLimits(max_url_bytes=256))

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
