"""Tests for deterministic bounded KEGG flat-file entry cards."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg import KeggClientConfig, KeggRequestOptions, PublicAcademicAccess
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
    KeggOperation,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.parsers import parse_flat_file_response
from kegg_mcp.services.entry_cards import (
    ENTRY_CARD_DATABASES,
    ENTRY_CARD_PARSER_VERSION,
    ENTRY_CARD_SCHEMA_VERSION,
    CompoundEntryCard,
    EnzymeEntryCard,
    GeneEntryCard,
    GenomeEntryCard,
    GlycanEntryCard,
    KeggEntryCardEntity,
    KeggEntryCardKind,
    KeggEntryCardSnapshot,
    KoEntryCard,
    ModuleEntryCard,
    PathwayEntryCard,
    ReactionEntryCard,
    build_entry_cards,
    entry_card_previews,
)
from kegg_mcp.services.kegg_entries import retrieve_kegg_entries
from kegg_mcp.services.models import KeggEntryProjection
from kegg_mcp.services.reference_budget import KeggPrimitiveClient
from kegg_mcp.services.result_store import SQLiteResultStore

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)


class _UnexpectedGetClient:
    def __init__(self) -> None:
        self._config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))
        self.get_call_count = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del request, options
        self.get_call_count += 1
        raise AssertionError("unsupported card projection must fail before KEGG access")


class _StaticGetClient:
    def __init__(self, result: GetResult) -> None:
        self._config = KeggClientConfig(access=PublicAcademicAccess(academic_use_confirmed=True))
        self.result = result
        self.get_call_count = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del options
        assert request == self.result.request
        self.get_call_count += 1
        return self.result


def _provenance() -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=KeggOperation.GET,
        request_key="get:synthetic-entry-card",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        response_bytes=100,
        parser_name="flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release synthetic",
        attempt_count=1,
        is_stale=False,
    )


def _result(
    database: KeggGetDatabase,
    identifier: str,
    body: bytes,
) -> GetResult:
    request = GetRequest(entries=(KeggEntryRef(database=database, identifier=identifier),))
    return GetResult(
        request=request,
        documents=(parse_flat_file_response(body),),
        missing_entries=(),
        batches=(_provenance(),),
    )


def test_card_service_rejects_unsupported_database_before_kegg_access(
    tmp_path: Path,
) -> None:
    client = _UnexpectedGetClient()
    store = SQLiteResultStore(tmp_path / "unsupported-card.sqlite3")

    with pytest.raises(KeggMcpError) as caught:
        retrieve_kegg_entries(
            GetRequest(
                entries=(
                    KeggEntryRef(
                        database=KeggGetDatabase.DRUG,
                        identifier="D00109",
                    ),
                )
            ),
            client=cast(KeggPrimitiveClient, client),
            result_store=store,
            scope_id="unsupported-card-scope",
            projection=KeggEntryProjection.CARD,
        )

    assert caught.value.detail.code is ErrorCode.ANALYSIS_CONFIGURATION_INVALID
    assert client.get_call_count == 0
    assert store.list_results("unsupported-card-scope").total_items == 0


def test_card_service_rejects_duplicate_genome_aliases_without_retention(
    tmp_path: Path,
) -> None:
    request = GetRequest(
        entries=(
            KeggEntryRef(database=KeggGetDatabase.GENOME, identifier="hsa"),
            KeggEntryRef(database=KeggGetDatabase.GENOME, identifier="T01001"),
        )
    )
    client = _StaticGetClient(
        GetResult(
            request=request,
            documents=(
                parse_flat_file_response(
                    b"ENTRY       T01001            Complete  Genome\n"
                    b"NAME        Homo sapiens\n"
                    b"ORG_CODE    hsa\n"
                    b"///\n"
                ),
            ),
            missing_entries=(),
            batches=(_provenance(),),
        )
    )
    store = SQLiteResultStore(tmp_path / "duplicate-genome-alias.sqlite3")

    with pytest.raises(KeggMcpError) as caught:
        retrieve_kegg_entries(
            request,
            client=cast(KeggPrimitiveClient, client),
            result_store=store,
            scope_id="duplicate-genome-alias",
            projection=KeggEntryProjection.CARD,
        )

    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert client.get_call_count == 1
    assert store.list_results("duplicate-genome-alias").total_items == 0


def test_ko_card_parses_common_fields_and_keeps_unknown_fields_visible() -> None:
    snapshot = build_entry_cards(
        _result(
            KeggGetDatabase.KO,
            "K00001",
            (
                b"ENTRY       K00001                      KO\n"
                b"NAME        primary name; alternate name\n"
                b"DEFINITION  Synthetic enzyme [EC:1.1.1.1 2.2.2.-]\n"
                b"CLASS       Metabolism; Synthetic class\n"
                b"MODULE      M00001  Synthetic module\n"
                b"PATHWAY     map00010  Synthetic pathway\n"
                b"DBLINKS     UniProt: P00001 P00002\n"
                b"REFERENCE   PMID:123456\n"
                b"  AUTHORS   Database Author\n"
                b"AASEQ       3\n"
                b"            AAA\n"
                b"///\n"
            ),
        )
    )

    assert snapshot.schema_version == ENTRY_CARD_SCHEMA_VERSION
    assert snapshot.parser_version == ENTRY_CARD_PARSER_VERSION
    assert snapshot.response_parser_version == PARSER_VERSION
    assert len(snapshot.provenance) == 1
    card = snapshot.entries[0]
    assert isinstance(card, KoEntryCard)
    assert card.names == ("primary name", "alternate name")
    assert card.definition == "Synthetic enzyme [EC:1.1.1.1 2.2.2.-]"
    assert card.classes == ("Metabolism; Synthetic class",)
    assert card.ec_numbers == ("1.1.1.1", "2.2.2.-")
    assert tuple(item.identifier for item in card.modules) == ("M00001",)
    assert tuple(item.identifier for item in card.pathways) == ("map00010",)
    assert card.dblinks[0].database == "UniProt"
    assert card.dblinks[0].identifiers == ("P00001", "P00002")
    assert card.pubmed_ids == ("123456",)
    assert card.unparsed_field_names == ("AASEQ",)
    assert "AAA" not in card.model_dump_json()


def test_module_and_pathway_cards_preserve_logic_and_ko_denominator() -> None:
    module = build_entry_cards(
        _result(
            KeggGetDatabase.MODULE,
            "M00001",
            (
                b"ENTRY       M00001            Module\n"
                b"NAME        Synthetic module\n"
                b"DEFINITION  K00001+K00002-K00003 M00002\n"
                b"PATHWAY     map00010  Synthetic pathway\n"
                b"REACTION    R00001  Synthetic reaction\n"
                b"///\n"
            ),
        )
    ).entries[0]
    pathway = build_entry_cards(
        _result(
            KeggGetDatabase.PATHWAY,
            "ko00010",
            (
                b"ENTRY       ko00010                    Pathway\n"
                b"NAME        Synthetic pathway\n"
                b"ORTHOLOGY   K00001  First KO\n"
                b"            K00002  Second KO\n"
                b"MODULE      M00001  Synthetic module\n"
                b"REACTION    R00001  Synthetic reaction\n"
                b"COMPOUND    C00031  Synthetic compound\n"
                b"GLYCAN      G00001  Synthetic glycan\n"
                b"///\n"
            ),
        )
    ).entries[0]

    assert isinstance(module, ModuleEntryCard)
    assert module.module_definition is not None
    assert module.module_definition.is_valid is True
    assert module.module_definition.raw_definition == "K00001+K00002-K00003 M00002"
    assert module.module_definition.required_blocks == (
        "K00001+K00002-K00003",
        "M00002",
    )
    assert module.module_definition.optional_components == ("-K00003",)
    assert module.module_definition.referenced_modules == ("M00002",)
    assert module.module_definition.ko_components == (
        "K00001",
        "K00002",
        "K00003",
    )
    assert isinstance(pathway, PathwayEntryCard)
    assert pathway.ko_identifiers == ("K00001", "K00002")
    assert tuple(item.identifier for item in pathway.modules) == ("M00001",)
    assert tuple(item.identifier for item in pathway.compounds) == ("C00031",)
    assert tuple(item.identifier for item in pathway.glycans) == ("G00001",)


def test_module_card_preserves_unsupported_syntax_without_inventing_semantics() -> None:
    card = build_entry_cards(
        _result(
            KeggGetDatabase.MODULE,
            "M00001",
            (
                b"ENTRY       M00001            Module\n"
                b"NAME        Synthetic module\n"
                b"DEFINITION  K00001 ? K00002\n"
                b"///\n"
            ),
        )
    ).entries[0]

    assert isinstance(card, ModuleEntryCard)
    assert card.module_definition is not None
    assert card.module_definition.raw_definition == "K00001 ? K00002"
    assert "UNSUPPORTED_TOKEN" in card.module_definition.diagnostic_codes
    assert card.module_definition.ko_components == ("K00001", "K00002")


@pytest.mark.parametrize(
    ("database", "identifier", "body", "expected_type", "expected"),
    [
        (
            KeggGetDatabase.REACTION,
            "R00001",
            (
                b"ENTRY       R00001                      Reaction\n"
                b"NAME        Synthetic reaction\n"
                b"EQUATION    C00001 + G00001 <=> C00002\n"
                b"ENZYME      1.1.1.1 2.2.2.-\n"
                b"ORTHOLOGY   K00001  Synthetic KO\n"
                b"RCLASS      RC00001  C00001_C00002\n"
                b"///\n"
            ),
            ReactionEntryCard,
            {
                "enzyme_ids": ("1.1.1.1", "2.2.2.-"),
                "ko_identifiers": ("K00001",),
                "rclass_ids": ("RC00001",),
                "compound_ids": ("C00001", "C00002"),
                "glycan_ids": ("G00001",),
            },
        ),
        (
            KeggGetDatabase.ENZYME,
            "1.1.1.1",
            (
                b"ENTRY       EC 1.1.1.1\n"
                b"NAME        Synthetic enzyme\n"
                b"ALL_REAC    R00001; R00002\n"
                b"ORTHOLOGY   K00001  Synthetic KO\n"
                b"///\n"
            ),
            EnzymeEntryCard,
            {
                "reaction_ids": ("R00001", "R00002"),
                "ko_identifiers": ("K00001",),
            },
        ),
        (
            KeggGetDatabase.COMPOUND,
            "C00031",
            (
                b"ENTRY       C00031                      Compound\n"
                b"NAME        Synthetic compound\n"
                b"FORMULA     C6H12O6\n"
                b"EXACT_MASS  180.0634\n"
                b"MOL_WEIGHT  180.16\n"
                b"REACTION    R00001 R00002\n"
                b"PATHWAY     map00010  Synthetic pathway\n"
                b"///\n"
            ),
            CompoundEntryCard,
            {
                "formula": "C6H12O6",
                "exact_mass": "180.0634",
                "molecular_weight": "180.16",
            },
        ),
        (
            KeggGetDatabase.GLYCAN,
            "G00001",
            (
                b"ENTRY       G00001                      Glycan\n"
                b"NAME        Synthetic glycan\n"
                b"COMPOSITION Hex(1) HexNAc(1)\n"
                b"MASS        383.1\n"
                b"REACTION    R00001  Synthetic reaction\n"
                b"PATHWAY     map00010  Synthetic pathway\n"
                b"///\n"
            ),
            GlycanEntryCard,
            {"composition": "Hex(1) HexNAc(1)", "mass": "383.1"},
        ),
    ],
)
def test_reaction_centered_cards_are_deterministic(
    database: KeggGetDatabase,
    identifier: str,
    body: bytes,
    expected_type: type[object],
    expected: dict[str, object],
) -> None:
    card = build_entry_cards(_result(database, identifier, body)).entries[0]

    assert isinstance(card, expected_type)
    for field, value in expected.items():
        assert getattr(card, field) == value
    if isinstance(card, ReactionEntryCard):
        assert card.equation == "C00001 + G00001 <=> C00002"
        assert "direction" not in card.model_dump()
    if isinstance(card, (CompoundEntryCard, GlycanEntryCard)):
        reaction_ids = tuple(item.identifier for item in card.reactions)
        expected_reactions = (
            ("R00001", "R00002") if isinstance(card, CompoundEntryCard) else ("R00001",)
        )
        assert reaction_ids == expected_reactions


def test_gene_and_genome_cards_preserve_scoped_identity_and_taxonomy() -> None:
    gene = build_entry_cards(
        _result(
            KeggGetDatabase.GENE,
            "hsa:10458",
            (
                b"ENTRY       10458             CDS       T01001\n"
                b"NAME        Synthetic gene\n"
                b"ORGANISM    hsa  Homo sapiens (human)\n"
                b"POSITION    1:1..100\n"
                b"ORTHOLOGY   K00001  Synthetic KO\n"
                b"PATHWAY     hsa00010  Synthetic pathway\n"
                b"///\n"
            ),
        )
    ).entries[0]
    genome = build_entry_cards(
        _result(
            KeggGetDatabase.GENOME,
            "T01001",
            (
                b"ENTRY       T01001            Complete  Genome\n"
                b"NAME        Homo sapiens\n"
                b"ORG_CODE    hsa\n"
                b"TAXONOMY    TAX:9606\n"
                b"LINEAGE     Eukaryota; Metazoa; Chordata\n"
                b"///\n"
            ),
        )
    ).entries[0]

    assert isinstance(gene, GeneEntryCard)
    assert gene.entity.identifier == "hsa:10458"
    assert gene.organism_code == "hsa"
    assert gene.organism_name == "Homo sapiens (human)"
    assert gene.position == "1:1..100"
    assert isinstance(genome, GenomeEntryCard)
    assert genome.organism_code == "hsa"
    assert genome.taxonomy_id == "taxid:9606"
    assert genome.lineage == ("Eukaryota", "Metazoa", "Chordata")


def test_missing_fields_are_explicitly_empty_and_direct_preview_is_bounded() -> None:
    long_name = "n" * 300
    long_definition = "d" * 300
    snapshot = build_entry_cards(
        _result(
            KeggGetDatabase.KO,
            "K00001",
            (
                f"ENTRY       K00001                      KO\n"
                f"NAME        {long_name}\n"
                f"DEFINITION  {long_definition}\n"
                "///\n"
            ).encode(),
        )
    )
    card = snapshot.entries[0]
    assert isinstance(card, KoEntryCard)
    assert card.classes == ()
    assert card.dblinks == ()
    assert card.pubmed_ids == ()

    direct = entry_card_previews(snapshot)
    assert direct.entry_count == 1
    assert direct.previews_truncated is False
    preview = direct.previews[0]
    assert preview.primary_name_truncated is True
    assert preview.definition_truncated is True
    assert len(preview.primary_name or "") == 256
    assert len(preview.definition_preview or "") == 256


def test_complete_card_fails_closed_instead_of_silently_truncating() -> None:
    oversized_name = "n" * 65_537

    with pytest.raises(KeggMcpError) as captured:
        build_entry_cards(
            _result(
                KeggGetDatabase.KO,
                "K00001",
                (
                    f"ENTRY       K00001                      KO\n"
                    f"NAME        {oversized_name}\n"
                    "///\n"
                ).encode(),
            )
        )

    assert captured.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED


def test_snapshot_contract_rejects_mismatched_or_duplicate_accounting() -> None:
    bad_card = KoEntryCard(
        entity=KeggEntryCardEntity(
            database=KeggEntryCardKind.PATHWAY,
            identifier="ko00010",
        )
    )
    with pytest.raises(ValidationError, match="kind must match"):
        KeggEntryCardSnapshot(
            requested_entries=(bad_card.entity,),
            entries=(bad_card,),
            provenance=(_provenance(),),
        )

    missing = KeggEntryCardEntity(
        database=KeggEntryCardKind.KO,
        identifier="K00001",
    )
    with pytest.raises(ValidationError, match="must be unique"):
        KeggEntryCardSnapshot(
            requested_entries=(missing,),
            entries=(),
            missing_entries=(missing, missing),
            provenance=(_provenance(),),
        )
    with pytest.raises(ValidationError, match="preserve requested database counts"):
        KeggEntryCardSnapshot(
            requested_entries=(missing,),
            entries=(
                GenomeEntryCard(
                    entity=KeggEntryCardEntity(
                        database=KeggEntryCardKind.GENOME,
                        identifier="T01001",
                    ),
                ),
            ),
            provenance=(_provenance(),),
        )


def test_entry_card_database_allowlist_excludes_unimplemented_flat_types() -> None:
    assert KeggGetDatabase.KO in ENTRY_CARD_DATABASES
    assert KeggGetDatabase.GLYCAN in ENTRY_CARD_DATABASES
    assert KeggGetDatabase.BRITE not in ENTRY_CARD_DATABASES
    assert KeggGetDatabase.DRUG not in ENTRY_CARD_DATABASES
    assert KeggGetDatabase.RCLASS not in ENTRY_CARD_DATABASES
