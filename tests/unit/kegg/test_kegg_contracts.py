"""Tests for KEGG access and request contracts."""

import pytest
from pydantic import ValidationError

from kegg_mcp.kegg.contracts import (
    MIN_REQUESTS_PER_SECOND,
    PUBLIC_KEGG_ENDPOINT,
    PUBLIC_KEGG_ENDPOINT_FINGERPRINT,
    CachePolicy,
    ConvRequest,
    KeggBriteEntryKind,
    KeggClientConfig,
    KeggConvDatabase,
    KeggEntryRef,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    LicensedAccess,
    LinkRequest,
    OfflineCacheAccess,
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
