"""Tests for KEGG access and request contracts."""

import pytest
from pydantic import ValidationError

from kegg_mcp.kegg.contracts import (
    MIN_REQUESTS_PER_SECOND,
    PUBLIC_KEGG_ENDPOINT,
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    CachePolicy,
    ConvRequest,
    FindRequest,
    KeggBatchProvenance,
    KeggBriteEntryKind,
    KeggClientConfig,
    KeggClientLimits,
    KeggConvDatabase,
    KeggEntryRef,
    KeggFindDatabase,
    KeggFindMode,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    KeggTaxonomyRank,
    LicensedAccess,
    LinkRequest,
    OfflineCacheAccess,
    OrganismPathwayListRequest,
    PublicAcademicAccess,
    RateLimitPolicy,
    RetrievalEndpointClass,
    endpoint_fingerprint,
)


def test_client_defaults_to_confirmed_public_academic_access() -> None:
    config = KeggClientConfig()

    assert isinstance(config.access, PublicAcademicAccess)
    assert config.access.academic_use_confirmed is True


def test_request_options_default_to_network_refresh() -> None:
    options = KeggRequestOptions()

    assert options.refresh is True
    assert options.cache_only is False


def test_organism_pathway_list_requires_a_canonical_organism_code() -> None:
    assert OrganismPathwayListRequest(organism="hsa").organism == "hsa"

    for invalid in ("hs", "human", "HSA", "hsa/../ko"):
        with pytest.raises(ValidationError):
            OrganismPathwayListRequest(organism=invalid)


def test_offline_cache_access_defaults_to_the_public_endpoint_namespace() -> None:
    access = OfflineCacheAccess()

    assert access.mode == "offline_cache"
    assert access.endpoint_fingerprint == PUBLIC_KEGG_ENDPOINT_FINGERPRINT


def test_offline_licensed_cache_namespace_requires_matching_endpoint_fingerprint() -> None:
    endpoint = "https://licensed.example.test/kegg"

    access = OfflineCacheAccess(
        retrieval_endpoint_class=RetrievalEndpointClass.LICENSED,
        endpoint=endpoint,
        endpoint_fingerprint=endpoint_fingerprint(endpoint),
        endpoint_label="licensed-endpoint",
    )

    assert access.endpoint == endpoint
    with pytest.raises(ValidationError):
        OfflineCacheAccess(
            retrieval_endpoint_class=RetrievalEndpointClass.LICENSED,
            endpoint=endpoint,
            endpoint_fingerprint=endpoint_fingerprint("https://other.example.test/kegg"),
            endpoint_label="licensed-endpoint",
        )


def test_client_limits_reject_rates_below_the_persisted_cross_process_minimum() -> None:
    with pytest.raises(ValidationError):
        KeggClientConfig.model_validate(
            {"limits": {"requests_per_second": MIN_REQUESTS_PER_SECOND / 2.0}}
        )


def test_cache_only_requests_reject_network_refresh() -> None:
    with pytest.raises(ValidationError):
        KeggRequestOptions(cache_only=True)


@pytest.mark.parametrize(
    ("database", "wire_database"),
    [
        (KeggFindDatabase.KO, "ko"),
        (KeggFindDatabase.PATHWAY, "pathway"),
        (KeggFindDatabase.MODULE, "module"),
        (KeggFindDatabase.REACTION, "reaction"),
        (KeggFindDatabase.ENZYME, "enzyme"),
        (KeggFindDatabase.COMPOUND, "compound"),
        (KeggFindDatabase.GENOME, "genome"),
        (KeggFindDatabase.ORGANISM, "genome"),
        (KeggFindDatabase.GENES, "genes"),
    ],
)
def test_find_keyword_database_allowlist_has_an_explicit_wire_database(
    database: KeggFindDatabase,
    wire_database: str,
) -> None:
    request = FindRequest(database=database, query="glucose metabolism")

    assert request.mode is KeggFindMode.KEYWORD
    assert request.database.wire_database == wire_database
    assert "max_results" not in FindRequest.model_fields


def test_find_keyword_query_accepts_unicode_spaces_plus_and_percent() -> None:
    request = FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query="β-D-glucose + water \u6c34 100%",
    )

    assert request.query == "β-D-glucose + water \u6c34 100%"


def test_gene_find_accepts_one_canonical_organism_scope() -> None:
    request = FindRequest(
        database=KeggFindDatabase.GENES,
        query="TP53",
        organism="hsa",
    )

    assert request.organism == "hsa"


@pytest.mark.parametrize("organism", ["HSA", "ko", "abcde", "T01001", "../hsa"])
def test_gene_find_rejects_a_noncanonical_organism_scope(organism: str) -> None:
    with pytest.raises(ValidationError):
        FindRequest(
            database=KeggFindDatabase.GENES,
            query="TP53",
            organism=organism,
        )


def test_non_gene_find_rejects_an_organism_scope() -> None:
    with pytest.raises(ValidationError):
        FindRequest(
            database=KeggFindDatabase.KO,
            query="TP53",
            organism="hsa",
        )


@pytest.mark.parametrize("query", ["", " glucose", "glucose ", "glucose\twater", "bad\x7ftext"])
def test_find_query_rejects_blank_outer_whitespace_or_control_characters(query: str) -> None:
    with pytest.raises(ValidationError):
        FindRequest(database=KeggFindDatabase.KO, query=query)


@pytest.mark.parametrize("query", ["a/b", "a\\b", "a?b", "a#b", ".", ".."])
def test_find_query_rejects_url_structural_path_characters(query: str) -> None:
    with pytest.raises(ValidationError):
        FindRequest(database=KeggFindDatabase.KO, query=query)


def test_find_query_rejects_text_that_cannot_be_encoded_as_utf8() -> None:
    with pytest.raises(ValidationError):
        FindRequest(database=KeggFindDatabase.KO, query="\ud800")


@pytest.mark.parametrize("query", ["C7H10O5", "O5C7", "NaCl", "CH4"])
def test_compound_formula_find_accepts_bounded_molecular_formula_syntax(query: str) -> None:
    request = FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query=query,
        mode=KeggFindMode.FORMULA,
    )

    assert request.query == query


@pytest.mark.parametrize("query", ["C0", "c6H12O6", "C6 H12 O6", "(CH2)n", "C6/../H12"])
def test_compound_formula_find_rejects_non_formula_syntax(query: str) -> None:
    with pytest.raises(ValidationError):
        FindRequest(
            database=KeggFindDatabase.COMPOUND,
            query=query,
            mode=KeggFindMode.FORMULA,
        )


@pytest.mark.parametrize("query", ["174.05", "300-310", "0.1", "300.0-310.000"])
@pytest.mark.parametrize("mode", [KeggFindMode.EXACT_MASS, KeggFindMode.MOL_WEIGHT])
def test_compound_mass_find_accepts_one_positive_value_or_ordered_range(
    query: str,
    mode: KeggFindMode,
) -> None:
    request = FindRequest(
        database=KeggFindDatabase.COMPOUND,
        query=query,
        mode=mode,
    )

    assert request.query == query


@pytest.mark.parametrize("query", ["0", "-1", "310-300", "1e3", ".5", "1.", "01"])
def test_compound_mass_find_rejects_ambiguous_or_non_positive_syntax(query: str) -> None:
    with pytest.raises(ValidationError):
        FindRequest(
            database=KeggFindDatabase.COMPOUND,
            query=query,
            mode=KeggFindMode.EXACT_MASS,
        )


@pytest.mark.parametrize(
    "mode",
    [KeggFindMode.FORMULA, KeggFindMode.EXACT_MASS, KeggFindMode.MOL_WEIGHT],
)
def test_chemical_find_modes_are_restricted_to_compound(mode: KeggFindMode) -> None:
    with pytest.raises(ValidationError):
        FindRequest(database=KeggFindDatabase.KO, query="174.05", mode=mode)


def test_request_key_bound_matches_the_hard_url_bound() -> None:
    provenance_schema = KeggBatchProvenance.model_json_schema()
    limits_schema = KeggClientLimits.model_json_schema()

    assert provenance_schema["properties"]["request_key"]["maxLength"] == 65_536
    assert limits_schema["properties"]["max_url_bytes"]["maximum"] == 65_536


@pytest.mark.parametrize("state_root", ["relative", "../rate-limit"])
def test_rate_limit_state_root_must_be_absolute_and_traversal_free(state_root: str) -> None:
    with pytest.raises(ValidationError):
        RateLimitPolicy(state_root=state_root)


def test_public_access_requires_literal_academic_confirmation() -> None:
    with pytest.raises(ValidationError):
        PublicAcademicAccess.model_validate(
            {
                "mode": "public_academic",
                "academic_use_confirmed": False,
                "endpoint": PUBLIC_KEGG_ENDPOINT,
            }
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://licensed.example.test",
        "https://user:secret@licensed.example.test",
        "https://licensed.example.test?token=secret",
        "https://licensed.example.test/#fragment",
        "https://licensed.example.test/%2e%2e/private",
        "https://licensed.example.test/../private",
        PUBLIC_KEGG_ENDPOINT,
        "https://REST.KEGG.JP",
        "https://rest.kegg.jp.",
        "https://rest.kegg.jp:443",
    ],
)
def test_licensed_access_rejects_unsafe_or_mislabeled_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        LicensedAccess(
            authorized_use_confirmed=True,
            endpoint=endpoint,
            endpoint_label="institutional-service",
        )


def test_licensed_access_normalizes_one_trailing_slash() -> None:
    access = LicensedAccess(
        authorized_use_confirmed=True,
        endpoint="https://licensed.example.test/api/",
        endpoint_label="institutional-service",
    )

    assert access.endpoint == "https://licensed.example.test/api"


def test_licensed_access_canonicalizes_equivalent_authority_forms() -> None:
    access = LicensedAccess(
        authorized_use_confirmed=True,
        endpoint="HTTPS://Licensed.Example.Test.:443/api/",
        endpoint_label="institutional-service",
    )

    assert access.endpoint == "https://licensed.example.test/api"


def test_licensed_access_preserves_a_valid_nondefault_port() -> None:
    access = LicensedAccess(
        authorized_use_confirmed=True,
        endpoint="https://licensed.example.test:8443/api",
        endpoint_label="institutional-service",
    )

    assert access.endpoint == "https://licensed.example.test:8443/api"


@pytest.mark.parametrize(
    "endpoint",
    ["https://licensed.example.test:0", "https://licensed.example.test:70000"],
)
def test_licensed_access_rejects_invalid_ports(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        LicensedAccess(
            authorized_use_confirmed=True,
            endpoint=endpoint,
            endpoint_label="institutional-service",
        )


def test_sensitive_configuration_values_are_hidden_in_validation_errors() -> None:
    secret = "credential-that-must-not-appear"

    with pytest.raises(ValidationError) as endpoint_error:
        LicensedAccess(
            authorized_use_confirmed=True,
            endpoint=f"https://user:{secret}@licensed.example.test",
            endpoint_label="institutional-service",
        )
    with pytest.raises(ValidationError) as cache_error:
        CachePolicy(path=f"/private/{secret}\x00/cache.sqlite3")

    assert secret not in str(endpoint_error.value)
    assert secret not in str(cache_error.value)


def test_endpoint_fingerprint_is_stable_opaque_and_endpoint_specific() -> None:
    first = endpoint_fingerprint("https://licensed.example.test/api")
    equivalent = endpoint_fingerprint("https://licensed.example.test/api")
    second = endpoint_fingerprint("https://other.example.test/api")

    assert first == equivalent
    assert first != second
    assert len(first) == 64
    assert "licensed.example.test" not in first
    assert endpoint_fingerprint(PUBLIC_KEGG_ENDPOINT) == PUBLIC_KEGG_ENDPOINT_FINGERPRINT


@pytest.mark.parametrize("label", ["has space", " leading", "trailing ", "bad/label"])
def test_endpoint_label_uses_one_shared_strict_logical_pattern(label: str) -> None:
    with pytest.raises(ValidationError):
        LicensedAccess(
            authorized_use_confirmed=True,
            endpoint="https://licensed.example.test/api",
            endpoint_label=label,
        )


@pytest.mark.parametrize(
    ("database", "identifier"),
    [
        (KeggGetDatabase.KO, "M00001"),
        (KeggGetDatabase.MODULE, "K00001"),
        (KeggGetDatabase.PATHWAY, "../map00010"),
        (KeggGetDatabase.ENZYME, "ec:1.1.1.1"),
        (KeggGetDatabase.BRITE, "br:br08901"),
    ],
)
def test_entry_reference_rejects_cross_database_or_wire_form_identifiers(
    database: KeggGetDatabase, identifier: str
) -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(database=database, identifier=identifier)


@pytest.mark.parametrize(
    "identifier",
    ["1.1.1.1", "7.6.2.4", "1.1.1.-", "1.1.-.-", "1.-.-.-"],
)
def test_enzyme_entry_accepts_complete_and_trailing_partial_ec_numbers(
    identifier: str,
) -> None:
    entry = KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier=identifier)

    assert entry.wire_identifier == f"ec:{identifier}"


@pytest.mark.parametrize(
    "identifier",
    [
        "0.1.1.1",
        "8.1.1.1",
        "1.--.1.1",
        "1.1-2.3.4",
        "1.---.---.---",
        "1.1.-.1",
        "1.-.-.1",
    ],
)
def test_enzyme_entry_rejects_malformed_or_non_trailing_partial_ec_numbers(
    identifier: str,
) -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(database=KeggGetDatabase.ENZYME, identifier=identifier)


@pytest.mark.parametrize(
    "identifier",
    [
        "map00010",
        "ko00010",
        "ec00010",
        "rn00010",
        "hsa00010",
        "ddi00230",
        "vg00541",
        "vx00541",
    ],
)
def test_pathway_entry_accepts_supported_reference_and_organism_prefixes(
    identifier: str,
) -> None:
    entry = KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier=identifier)

    assert entry.identifier == identifier


@pytest.mark.parametrize("identifier", ["module00010", "path00010", "abcde00010", "hs100010"])
def test_pathway_entry_rejects_non_pathway_prefixes(identifier: str) -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(database=KeggGetDatabase.PATHWAY, identifier=identifier)


def test_pathway_link_rejects_a_non_pathway_source_identifier() -> None:
    with pytest.raises(ValidationError):
        LinkRequest(
            relationship=KeggLinkRelationship.PATHWAY_TO_KO,
            source_identifiers=("module00010",),
        )


def test_pathway_link_accepts_an_organism_code_that_matches_an_api_operation() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.PATHWAY_TO_KO,
        source_identifiers=("ddi00230",),
    )

    assert request.source_identifiers == ("ddi00230",)


def test_brite_get_requires_an_explicit_supported_content_kind() -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(database=KeggGetDatabase.BRITE, identifier="br08901")

    entry = KeggEntryRef(
        database=KeggGetDatabase.BRITE,
        identifier="br08901",
        brite_kind=KeggBriteEntryKind.HIERARCHY,
    )
    assert entry.wire_identifier == "br:br08901"

    organism_entry = KeggEntryRef(
        database=KeggGetDatabase.BRITE,
        identifier="hsa00001",
        brite_kind=KeggBriteEntryKind.HIERARCHY,
    )
    assert organism_entry.wire_identifier == "br:hsa00001"

    ddi_entry = KeggEntryRef(
        database=KeggGetDatabase.BRITE,
        identifier="ddi00001",
        brite_kind=KeggBriteEntryKind.HIERARCHY,
    )
    assert ddi_entry.wire_identifier == "br:ddi00001"


@pytest.mark.parametrize("identifier", ["module00010", "map00010", "abcde00001", "hs100001"])
def test_brite_get_rejects_non_brite_prefixes(identifier: str) -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(
            database=KeggGetDatabase.BRITE,
            identifier=identifier,
            brite_kind=KeggBriteEntryKind.HIERARCHY,
        )


def test_non_brite_get_rejects_a_brite_content_kind() -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(
            database=KeggGetDatabase.KO,
            identifier="K00001",
            brite_kind=KeggBriteEntryKind.HIERARCHY,
        )


@pytest.mark.parametrize(
    "identifier",
    ["hsa:10458", "T01001:10458", "ag:ENTRY1", "vg:ENTRY1", "vp:ENTRY1"],
)
def test_gene_get_accepts_canonical_database_qualified_identifiers(identifier: str) -> None:
    entry = KeggEntryRef(database=KeggGetDatabase.GENE, identifier=identifier)

    assert entry.wire_identifier == identifier


@pytest.mark.parametrize("identifier", ["P12345", "ko:K00001", "hsa:bad/value"])
def test_gene_get_rejects_unqualified_or_non_gene_identifiers(identifier: str) -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(database=KeggGetDatabase.GENE, identifier=identifier)


@pytest.mark.parametrize("identifier", ["T01001", "hsa", "ddi"])
def test_genome_get_accepts_t_numbers_and_canonical_organism_codes(identifier: str) -> None:
    entry = KeggEntryRef(database=KeggGetDatabase.GENOME, identifier=identifier)

    assert entry.wire_identifier == f"gn:{identifier}"


@pytest.mark.parametrize("identifier", ["gn:T01001", "T1001", "HSA", "ko"])
def test_genome_get_rejects_wire_forms_or_non_genome_identifiers(identifier: str) -> None:
    with pytest.raises(ValidationError):
        KeggEntryRef(database=KeggGetDatabase.GENOME, identifier=identifier)


@pytest.mark.parametrize(
    ("relationship", "source_identifier"),
    [
        (KeggLinkRelationship.GENE_TO_KO, "K00001"),
        (KeggLinkRelationship.ENZYME_TO_REACTION, "ec:1.1.1.1"),
        (KeggLinkRelationship.REACTION_TO_KO, "C00031"),
        (KeggLinkRelationship.COMPOUND_TO_REACTION, "R00001"),
        (KeggLinkRelationship.PATHWAY_TO_COMPOUND, "module00010"),
        (KeggLinkRelationship.GENOME_TO_TAXONOMY, "gn:T01001"),
        (KeggLinkRelationship.TAXONOMY_TO_GENOME, "9606"),
    ],
)
def test_link_relationships_reject_cross_kind_or_wire_form_sources(
    relationship: KeggLinkRelationship,
    source_identifier: str,
) -> None:
    with pytest.raises(ValidationError):
        LinkRequest(
            relationship=relationship,
            source_identifiers=(source_identifier,),
        )


def test_taxonomy_to_genome_link_defaults_to_exact_taxonomy_rank() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
        source_identifiers=("taxid:562",),
    )

    assert request.taxonomy_rank is KeggTaxonomyRank.EXACT


def test_taxonomy_to_genome_link_accepts_the_typed_species_rank() -> None:
    request = LinkRequest(
        relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
        taxonomy_rank=KeggTaxonomyRank.SPECIES,
        source_identifiers=("taxid:562",),
    )

    assert request.taxonomy_rank is KeggTaxonomyRank.SPECIES


def test_non_taxonomy_link_rejects_a_non_default_taxonomy_rank() -> None:
    with pytest.raises(ValidationError, match="taxonomy_rank"):
        LinkRequest(
            relationship=KeggLinkRelationship.KO_TO_PATHWAY,
            taxonomy_rank=KeggTaxonomyRank.SPECIES,
            source_identifiers=("K00001",),
        )


def test_link_request_rejects_an_untyped_taxonomy_rank() -> None:
    with pytest.raises(ValidationError):
        LinkRequest(
            relationship=KeggLinkRelationship.TAXONOMY_TO_GENOME,
            taxonomy_rank="species",  # type: ignore[arg-type]
            source_identifiers=("taxid:562",),
        )


def test_conversion_requires_selected_external_to_gene_direction() -> None:
    with pytest.raises(ValidationError):
        ConvRequest(
            target_database=KeggConvDatabase.UNIPROT,
            source_database=KeggConvDatabase.NCBI_GENEID,
            source_identifiers=("ncbi-geneid:1",),
        )


def test_conversion_requires_database_qualified_source_identifiers() -> None:
    with pytest.raises(ValidationError):
        ConvRequest(
            target_database=KeggConvDatabase.GENES,
            source_database=KeggConvDatabase.UNIPROT,
            source_identifiers=("P12345",),
        )


@pytest.mark.parametrize(
    "identifier",
    [
        "hsa:10458",
        "ddi:DDB_G0291764",
        "T01001:10458",
        "ag:ENTRY1",
        "vg:ENTRY1",
        "vp:ENTRY1",
    ],
)
def test_conversion_accepts_supported_kegg_gene_identifiers(identifier: str) -> None:
    request = ConvRequest(
        target_database=KeggConvDatabase.UNIPROT,
        source_database=KeggConvDatabase.GENES,
        source_identifiers=(identifier,),
    )

    assert request.source_identifiers == (identifier,)


@pytest.mark.parametrize(
    "identifier",
    [
        "uniprot:P12345",
        "ko:K00001",
        "module:M00001",
        "pathway:map00010",
        "vtax:1234",
    ],
)
def test_conversion_rejects_non_gene_namespaces_as_kegg_genes(identifier: str) -> None:
    with pytest.raises(ValidationError):
        ConvRequest(
            target_database=KeggConvDatabase.UNIPROT,
            source_database=KeggConvDatabase.GENES,
            source_identifiers=(identifier,),
        )


def test_conversion_accepts_a_positive_numeric_ncbi_gene_id() -> None:
    request = ConvRequest(
        target_database=KeggConvDatabase.GENES,
        source_database=KeggConvDatabase.NCBI_GENEID,
        source_identifiers=("ncbi-geneid:948364",),
    )

    assert request.source_identifiers == ("ncbi-geneid:948364",)


@pytest.mark.parametrize(
    "identifier",
    ["ncbi-geneid:not-a-number", "ncbi-geneid:0", "ncbi-geneid:-1"],
)
def test_conversion_rejects_non_positive_ncbi_gene_ids(identifier: str) -> None:
    with pytest.raises(ValidationError):
        ConvRequest(
            target_database=KeggConvDatabase.GENES,
            source_database=KeggConvDatabase.NCBI_GENEID,
            source_identifiers=(identifier,),
        )
