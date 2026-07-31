"""Tests for deterministic, statistics-free enrichment handoff preparation."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg import (
    ConvRequest,
    ConvResult,
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggClientConfig,
    KeggConvDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
)
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBriteHtextDocument,
    KeggOperation,
    KeggPairRow,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services import enrichment_handoff
from kegg_mcp.services.enrichment_handoff import (
    EnrichmentGeneSetType,
    EnrichmentHandoffRequest,
    EnrichmentIdentifierNamespace,
    EnrichmentIdentifierSet,
    EnrichmentMappingStatus,
    build_enrichment_handoff,
    prepare_enrichment_handoff,
)
from kegg_mcp.services.reference_budget import KeggQueryClient

_NOW = datetime(2026, 7, 31, 8, 0, tzinfo=UTC)


def _provenance(operation: KeggOperation, marker: int) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        request_key=f"synthetic:{operation.value}:{marker}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.NETWORK,
        cache_lookup_state=CacheLookupState.MISS,
        retrieved_at=_NOW,
        served_at=_NOW,
        expires_at=_NOW + timedelta(days=1),
        response_bytes=100,
        parser_name="brite_htext" if operation is KeggOperation.GET else "pair_table",
        parser_version=PARSER_VERSION,
        database_release="Release synthetic",
        attempt_count=1,
        is_stale=False,
    )


class _Client:
    def __init__(self) -> None:
        self._config = KeggClientConfig.model_validate({})
        self.conv_rows: tuple[tuple[str, str], ...] = ()
        self.link_rows: dict[
            KeggLinkRelationship,
            tuple[tuple[str, str], ...],
        ] = {}
        self.brite_documents: dict[str, KeggBriteHtextDocument] = {}
        self.conv_requests: list[ConvRequest] = []
        self.link_requests: list[LinkRequest] = []
        self.get_requests: list[GetRequest] = []
        self.options: list[KeggRequestOptions | None] = []
        self._calls = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def _batch(self, operation: KeggOperation) -> KeggBatchProvenance:
        self._calls += 1
        return _provenance(operation, self._calls)

    def conv(
        self,
        request: ConvRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> ConvResult:
        self.conv_requests.append(request)
        self.options.append(options)
        requested = set(request.source_identifiers)
        rows = tuple(
            KeggPairRow(line_number=index, source_id=source, target_id=target)
            for index, (source, target) in enumerate(self.conv_rows, start=1)
            if source in requested
            or (source.startswith("up:") and f"uniprot:{source.removeprefix('up:')}" in requested)
        )
        return ConvResult(
            request=request,
            rows=rows,
            batches=(self._batch(KeggOperation.CONV),),
        )

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        self.link_requests.append(request)
        self.options.append(options)
        requested = set(request.source_identifiers)
        rows = tuple(
            KeggPairRow(line_number=index, source_id=source, target_id=target)
            for index, (source, target) in enumerate(
                self.link_rows.get(request.relationship, ()),
                start=1,
            )
            if source in requested or source.partition(":")[2] in requested
        )
        return LinkResult(
            request=request,
            rows=rows,
            batches=(self._batch(KeggOperation.LINK),),
        )

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        self.get_requests.append(request)
        self.options.append(options)
        documents = tuple(
            self.brite_documents[entry.identifier]
            for entry in request.entries
            if entry.identifier in self.brite_documents
        )
        returned = {document.identifier for document in documents}
        return GetResult(
            request=request,
            documents=documents,
            missing_entries=tuple(
                entry for entry in request.entries if entry.identifier not in returned
            ),
            batches=(self._batch(KeggOperation.GET),),
        )

    def find(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("enrichment handoff must not call FIND")

    def list_organism_pathways(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("enrichment handoff must not list organism pathways")


def _identifiers(
    namespace: EnrichmentIdentifierNamespace,
    *values: str,
) -> EnrichmentIdentifierSet:
    return EnrichmentIdentifierSet(namespace=namespace, identifiers=values)


def _ko_request(
    *,
    foreground: tuple[str, ...] = ("K00001",),
    universe: tuple[str, ...] = ("K00001", "K00002", "K00003"),
    gene_sets: tuple[EnrichmentGeneSetType, ...] = (
        EnrichmentGeneSetType.PATHWAY,
        EnrichmentGeneSetType.MODULE,
    ),
    brite_ids: tuple[str, ...] = (),
) -> EnrichmentHandoffRequest:
    return EnrichmentHandoffRequest(
        target="enrichment",
        foreground=_identifiers(EnrichmentIdentifierNamespace.KO, *foreground),
        universe=_identifiers(EnrichmentIdentifierNamespace.KO, *universe),
        gene_sets=gene_sets,
        brite_ids=brite_ids,
    )


def _read_tsv(path: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(Path(path).read_text()), delimiter="\t"))


def test_request_requires_explicit_compatible_universe_and_gene_context() -> None:
    with pytest.raises(ValidationError, match="target"):
        EnrichmentHandoffRequest.model_validate(
            {
                "foreground": {"namespace": "ko", "identifiers": ["K00001"]},
                "universe": {"namespace": "ko", "identifiers": ["K00001"]},
            }
        )
    with pytest.raises(ValidationError, match="explicit subset"):
        EnrichmentHandoffRequest(
            target="enrichment",
            foreground=_identifiers(EnrichmentIdentifierNamespace.KO, "K00002"),
            universe=_identifiers(EnrichmentIdentifierNamespace.KO, "K00001"),
        )
    with pytest.raises(ValidationError, match="same identifier namespace"):
        EnrichmentHandoffRequest(
            target="enrichment",
            foreground=_identifiers(EnrichmentIdentifierNamespace.KO, "K00001"),
            universe=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P12345"),
        )
    with pytest.raises(ValidationError, match="requires organism"):
        EnrichmentHandoffRequest(
            target="enrichment",
            foreground=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P12345"),
            universe=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P12345"),
        )
    with pytest.raises(ValidationError, match="not used for direct KO"):
        EnrichmentHandoffRequest(
            target="enrichment",
            foreground=_identifiers(EnrichmentIdentifierNamespace.KO, "K00001"),
            universe=_identifiers(EnrichmentIdentifierNamespace.KO, "K00001"),
            organism="hsa",
        )


def test_request_rejects_gene_symbols_and_unscoped_brite_categories() -> None:
    with pytest.raises(ValidationError):
        EnrichmentIdentifierSet.model_validate(
            {"namespace": "gene_symbol", "identifiers": ["TP53"]}
        )
    with pytest.raises(ValidationError, match="requires explicit brite_ids"):
        _ko_request(gene_sets=(EnrichmentGeneSetType.BRITE,))
    with pytest.raises(ValidationError, match="valid only when BRITE"):
        _ko_request(brite_ids=("ko00001",))


def test_request_canonicalizes_unique_gene_set_order() -> None:
    request = _ko_request(
        gene_sets=(
            EnrichmentGeneSetType.MODULE,
            EnrichmentGeneSetType.PATHWAY,
        )
    )

    assert request.gene_sets == (
        EnrichmentGeneSetType.PATHWAY,
        EnrichmentGeneSetType.MODULE,
    )


def test_ko_handoff_writes_six_committed_files_without_statistics(tmp_path: Path) -> None:
    client = _Client()
    client.link_rows = {
        KeggLinkRelationship.KO_TO_PATHWAY: (
            ("ko:K00001", "path:map00010"),
            ("ko:K00002", "path:map00010"),
            ("ko:K00003", "path:map00020"),
        ),
        KeggLinkRelationship.KO_TO_MODULE: (
            ("ko:K00001", "md:M00001"),
            ("ko:K00003", "md:M00002"),
        ),
    }
    result = prepare_enrichment_handoff(
        _ko_request(),
        client=cast(KeggQueryClient, client),
        output_directory=tmp_path / "handoff",
    )

    assert len(result.bundle.artifacts) == 6
    assert all(Path(artifact.path).is_file() for artifact in result.bundle.artifacts)
    assert Path(result.bundle.manifest).name == "handoff_manifest.json"
    assert result.foreground.mapping_yield == 1.0
    assert result.universe.mapping_yield == 1.0
    assert result.retrieval.batch_count == 2
    assert "p-values" in result.interpretation_caveat

    foreground = _read_tsv(result.bundle.mapped_foreground)
    assert foreground == [
        ["input_identifier", "kegg_gene", "ko_id", "ambiguous"],
        ["K00001", "", "K00001", "false"],
    ]
    universe = _read_tsv(result.bundle.mapped_universe)
    assert len(universe) == 4
    assert _read_tsv(result.bundle.unmapped) == [
        [
            "role",
            "input_identifier",
            "mapping_status",
            "organism_mismatch_count",
            "organism_mismatch_gene_candidates",
        ]
    ]
    gmt = _read_tsv(result.bundle.gene_sets)
    assert gmt == [
        ["map00010", "na", "K00001", "K00002"],
        ["map00020", "na", "K00003"],
        ["M00001", "na", "K00001"],
        ["M00002", "na", "K00003"],
    ]

    audit = json.loads(Path(result.bundle.mapping_audit).read_text())
    assert audit["statistical_tests_performed"] is False
    assert audit["foreground"]["input_count"] == 1
    assert audit["universe"]["input_count"] == 3
    assert len(audit["expanded_mappings"]) == 3
    manifest = json.loads(Path(result.bundle.manifest).read_text())
    assert manifest["bundle_kind"] == "kegg_enrichment_input"
    assert manifest["target"] == "enrichment"
    assert manifest["statistical_tests_performed"] is False
    assert set(manifest["files"]) == {
        "mapped_foreground.tsv",
        "mapped_universe.tsv",
        "unmapped_identifiers.tsv",
        "gene_sets.gmt",
        "mapping_audit.json",
    }
    for artifact in result.bundle.artifacts:
        assert artifact.byte_size == Path(artifact.path).stat().st_size


def test_external_gene_resolution_preserves_ambiguity_mismatch_and_unmapped(
    tmp_path: Path,
) -> None:
    client = _Client()
    client.conv_rows = (
        ("up:P11111", "hsa:1"),
        ("up:P11111", "hsa:2"),
        ("up:P22222", "mmu:3"),
    )
    client.link_rows = {
        KeggLinkRelationship.GENE_TO_KO: (
            ("hsa:1", "ko:K00001"),
            ("hsa:2", "ko:K00002"),
        ),
        KeggLinkRelationship.KO_TO_PATHWAY: (
            ("ko:K00001", "path:hsa00010"),
            ("ko:K00002", "path:hsa00010"),
        ),
    }
    request = EnrichmentHandoffRequest(
        target="enrichment",
        foreground=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P11111"),
        universe=_identifiers(
            EnrichmentIdentifierNamespace.UNIPROT,
            "P11111",
            "P22222",
            "P33333",
        ),
        organism="hsa",
    )
    result = prepare_enrichment_handoff(
        request,
        client=cast(KeggQueryClient, client),
        output_directory=tmp_path / "external",
    )

    assert result.foreground.mapped_input_count == 1
    assert result.foreground.ambiguous_input_count == 1
    assert result.universe.mapped_input_count == 1
    assert result.universe.organism_mismatch_input_count == 1
    assert result.universe.unmapped_input_count == 1
    assert result.universe.mapping_yield == pytest.approx(1 / 3)
    assert [request.source_database.value for request in client.conv_requests] == ["uniprot"]
    assert all(option == KeggRequestOptions(refresh=False) for option in client.options)

    mapped = _read_tsv(result.bundle.mapped_universe)
    assert mapped == [
        ["input_identifier", "kegg_gene", "ko_id", "ambiguous"],
        ["P11111", "hsa:1", "K00001", "true"],
        ["P11111", "hsa:2", "K00002", "true"],
    ]
    unmapped = _read_tsv(result.bundle.unmapped)
    assert unmapped[1:] == [
        ["universe", "P22222", "organism_mismatch", "1", "mmu:3"],
        ["universe", "P33333", "unmapped", "0", ""],
    ]
    audit = json.loads(Path(result.bundle.mapping_audit).read_text())
    assert result.target == "enrichment"
    assert audit["request"]["target"] == "enrichment"
    assert audit["mappings"][1]["status"] == EnrichmentMappingStatus.ORGANISM_MISMATCH
    assert audit["mappings"][1]["organism_mismatch_genes"] == ["mmu:3"]
    assert audit["mappings"][2]["status"] == EnrichmentMappingStatus.UNMAPPED
    assert audit["expanded_mappings"] == [
        {
            "input_identifier": "P11111",
            "kegg_gene": "hsa:1",
            "ko_id": "K00001",
        },
        {
            "input_identifier": "P11111",
            "kegg_gene": "hsa:2",
            "ko_id": "K00002",
        },
    ]


@pytest.mark.parametrize(
    ("namespace", "identifier", "wire_source", "source_database"),
    [
        (
            EnrichmentIdentifierNamespace.NCBI_GENEID,
            "7157",
            "ncbi-geneid:7157",
            KeggConvDatabase.NCBI_GENEID,
        ),
        (
            EnrichmentIdentifierNamespace.NCBI_PROTEINID,
            "NP_000537.3",
            "ncbi-proteinid:NP_000537.3",
            KeggConvDatabase.NCBI_PROTEINID,
        ),
        (
            EnrichmentIdentifierNamespace.UNIPROT,
            "P04637",
            "up:P04637",
            KeggConvDatabase.UNIPROT,
        ),
    ],
)
def test_external_gene_namespaces_use_typed_conversion_then_gene_to_ko(
    namespace: EnrichmentIdentifierNamespace,
    identifier: str,
    wire_source: str,
    source_database: KeggConvDatabase,
) -> None:
    client = _Client()
    client.conv_rows = ((wire_source, "hsa:7157"),)
    client.link_rows = {
        KeggLinkRelationship.GENE_TO_KO: (("hsa:7157", "ko:K04451"),),
        KeggLinkRelationship.KO_TO_PATHWAY: (("ko:K04451", "path:hsa04115"),),
    }
    request = EnrichmentHandoffRequest(
        target="enrichment",
        foreground=_identifiers(namespace, identifier),
        universe=_identifiers(namespace, identifier),
        organism="hsa",
    )

    detail = build_enrichment_handoff(request, client=cast(KeggQueryClient, client))

    assert detail.audit.universe.mapped_input_count == 1
    assert detail.audit.mappings[0].kegg_genes == ("hsa:7157",)
    assert detail.audit.mappings[0].ko_ids == ("K04451",)
    assert [item.source_database for item in client.conv_requests] == [source_database]
    assert [item.relationship for item in client.link_requests] == [
        KeggLinkRelationship.GENE_TO_KO,
        KeggLinkRelationship.KO_TO_PATHWAY,
    ]


def test_direct_kegg_gene_namespace_skips_conversion() -> None:
    client = _Client()
    client.link_rows = {
        KeggLinkRelationship.GENE_TO_KO: (("hsa:7157", "ko:K04451"),),
        KeggLinkRelationship.KO_TO_PATHWAY: (("ko:K04451", "path:hsa04115"),),
    }
    request = EnrichmentHandoffRequest(
        target="enrichment",
        foreground=_identifiers(EnrichmentIdentifierNamespace.KEGG_GENE, "hsa:7157"),
        universe=_identifiers(EnrichmentIdentifierNamespace.KEGG_GENE, "hsa:7157"),
        organism="hsa",
    )

    detail = build_enrichment_handoff(request, client=cast(KeggQueryClient, client))

    assert client.conv_requests == []
    assert detail.audit.mappings[0].ko_ids == ("K04451",)


def test_same_organism_gene_without_ko_is_unmapped_even_with_mismatch_candidates() -> None:
    client = _Client()
    client.conv_rows = (
        ("up:P04637", "hsa:7157"),
        ("up:P04637", "mmu:22059"),
    )
    request = EnrichmentHandoffRequest(
        target="enrichment",
        foreground=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P04637"),
        universe=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P04637"),
        organism="hsa",
    )

    detail = build_enrichment_handoff(request, client=cast(KeggQueryClient, client))

    mapping = detail.audit.mappings[0]
    assert mapping.status is EnrichmentMappingStatus.UNMAPPED
    assert mapping.kegg_genes == ("hsa:7157",)
    assert mapping.organism_mismatch_genes == ("mmu:22059",)


def test_brite_handoff_uses_explicit_hierarchy_category_paths(tmp_path: Path) -> None:
    client = _Client()
    client.brite_documents["ko00001"] = KeggBriteHtextDocument(
        identifier="ko00001",
        lines=(
            "+D\tKO hierarchy",
            "!",
            "A=Formula-like root",
            "B  Carbohydrate metabolism",
            "C    00010 Glycolysis",
            "D      K00001 first enzyme",
            "D      K00002 second enzyme",
        ),
    )
    result = prepare_enrichment_handoff(
        _ko_request(
            foreground=("K00001",),
            universe=("K00001", "K00002"),
            gene_sets=(EnrichmentGeneSetType.BRITE,),
            brite_ids=("ko00001",),
        ),
        client=cast(KeggQueryClient, client),
        output_directory=tmp_path / "brite",
    )

    assert len(client.get_requests) == 1
    assert client.link_requests == []
    assert result.gene_sets[0].term_count == 3
    assert result.gene_sets[0].membership_count == 6
    gmt = _read_tsv(result.bundle.gene_sets)
    assert len(gmt) == 3
    assert all(row[0].startswith("brite:ko00001:") for row in gmt)
    assert all("K00001" not in row[1] for row in gmt)
    formula_row = next(row for row in gmt if row[1].startswith("'=Formula-like"))
    assert formula_row[1] == "'=Formula-like root"
    assert formula_row[2:] == ["K00001", "K00002"]
    audit = json.loads(Path(result.bundle.mapping_audit).read_text())
    assert audit["brite_resolved_ids"] == ["ko00001"]
    assert audit["brite_missing_ids"] == []


def test_brite_handoff_supports_more_than_p0_entity_limit_and_audits_unmatched() -> None:
    matched = tuple(f"K{index:05d}" for index in range(1, 102))
    unmatched = "K00999"
    client = _Client()
    client.brite_documents["ko00001"] = KeggBriteHtextDocument(
        identifier="ko00001",
        lines=(
            "A Root",
            "B  Synthetic category",
            *(f"C    {ko_id} synthetic function" for ko_id in matched),
        ),
    )
    detail = build_enrichment_handoff(
        _ko_request(
            foreground=(matched[0],),
            universe=(*matched, unmatched),
            gene_sets=(EnrichmentGeneSetType.BRITE,),
            brite_ids=("ko00001",),
        ),
        client=cast(KeggQueryClient, client),
    )

    assert len(client.get_requests) == 1
    assert detail.audit.brite_unmatched_ko_ids == (unmatched,)
    assert {item.term_id for item in detail.gene_sets}
    assert max(len(item.ko_ids) for item in detail.gene_sets) == len(matched)


def test_known_request_plan_exceeding_budget_fails_before_any_kegg_call() -> None:
    direct_client = _Client()
    direct_kos = tuple(f"K{index:05d}" for index in range(5_000))
    with pytest.raises(KeggMcpError) as direct_error:
        build_enrichment_handoff(
            _ko_request(
                foreground=(direct_kos[0],),
                universe=direct_kos,
                gene_sets=(
                    EnrichmentGeneSetType.PATHWAY,
                    EnrichmentGeneSetType.MODULE,
                    EnrichmentGeneSetType.BRITE,
                ),
                brite_ids=("ko00001",),
            ),
            client=cast(KeggQueryClient, direct_client),
        )
    assert direct_error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert direct_client.link_requests == []
    assert direct_client.get_requests == []

    external_client = _Client()
    external_ids = tuple(f"P{index:05d}" for index in range(1_000))
    with pytest.raises(KeggMcpError) as external_error:
        build_enrichment_handoff(
            EnrichmentHandoffRequest(
                target="enrichment",
                foreground=_identifiers(
                    EnrichmentIdentifierNamespace.UNIPROT,
                    external_ids[0],
                ),
                universe=_identifiers(
                    EnrichmentIdentifierNamespace.UNIPROT,
                    *external_ids,
                ),
                organism="hsa",
            ),
            client=cast(KeggQueryClient, external_client),
        )
    assert external_error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert external_client.conv_requests == []
    assert external_client.link_requests == []


def test_query_limit_failure_does_not_create_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(enrichment_handoff, "MAX_ENRICHMENT_RELATIONSHIP_ROWS", 1)
    client = _Client()
    client.link_rows[KeggLinkRelationship.KO_TO_PATHWAY] = (
        ("ko:K00001", "path:map00010"),
        ("ko:K00002", "path:map00020"),
    )
    output = tmp_path / "not-written"
    with pytest.raises(KeggMcpError) as captured:
        prepare_enrichment_handoff(
            _ko_request(
                foreground=("K00001",),
                universe=("K00001", "K00002"),
                gene_sets=(EnrichmentGeneSetType.PATHWAY,),
            ),
            client=cast(KeggQueryClient, client),
            output_directory=output,
        )
    assert captured.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert not output.exists()


def test_all_unmapped_gene_universe_retains_complete_evidence_without_rows() -> None:
    request = EnrichmentHandoffRequest(
        target="enrichment",
        foreground=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P11111"),
        universe=_identifiers(EnrichmentIdentifierNamespace.UNIPROT, "P11111"),
        organism="hsa",
    )
    detail = build_enrichment_handoff(
        request,
        client=cast(KeggQueryClient, _Client()),
    )
    assert detail.audit.foreground.mapping_yield == 0.0
    assert detail.audit.universe.unmapped_input_count == 1
    assert detail.audit.expanded_mappings == ()
    assert detail.gene_sets == ()


def test_nonempty_output_is_never_replaced(tmp_path: Path) -> None:
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "sentinel.txt"
    sentinel.write_text("keep")
    with pytest.raises(KeggMcpError) as captured:
        prepare_enrichment_handoff(
            _ko_request(
                foreground=("K00001",),
                universe=("K00001",),
                gene_sets=(EnrichmentGeneSetType.PATHWAY,),
            ),
            client=cast(KeggQueryClient, _Client()),
            output_directory=output,
        )
    assert captured.value.detail.code is ErrorCode.OUTPUT_ALREADY_EXISTS
    assert sentinel.read_text() == "keep"


def test_build_is_deterministic_for_same_ko_reference_rows() -> None:
    request = _ko_request()
    first = _Client()
    second = _Client()
    rows = {
        KeggLinkRelationship.KO_TO_PATHWAY: (
            ("ko:K00002", "path:map00010"),
            ("ko:K00001", "path:map00010"),
        ),
        KeggLinkRelationship.KO_TO_MODULE: (),
    }
    first.link_rows = rows
    second.link_rows = rows
    first_detail = build_enrichment_handoff(
        request,
        client=cast(KeggQueryClient, first),
    )
    second_detail = build_enrichment_handoff(
        request,
        client=cast(KeggQueryClient, second),
    )
    assert first_detail.model_dump(mode="json") == second_detail.model_dump(mode="json")
