"""Tests for bounded KEGG search, entity resolution, and relation tracing."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NoReturn, cast

import pytest
from pydantic import TypeAdapter, ValidationError

from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg import (
    ConvRequest,
    ConvResult,
    FindRequest,
    FindResult,
    GetRequest,
    GetResult,
    KeggClientConfig,
    KeggEntryRef,
    KeggFindDatabase,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggRequestOptions,
    KeggTaxonomyRank,
    LinkRequest,
    LinkResult,
    OrganismPathwayListRequest,
    OrganismPathwayListResult,
)
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggFindDocument,
    KeggFindRow,
    KeggFlatFileDocument,
    KeggFlatFileEntry,
    KeggFlatFileField,
    KeggOperation,
    KeggOrganismPathwayDocument,
    KeggOrganismPathwayRow,
    KeggPairRow,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.services import (
    kegg_search,
    query_support,
    relation_tracing,
    resolution_support,
)
from kegg_mcp.services.entity_resolution import resolve_kegg_entities
from kegg_mcp.services.kegg_search import search_kegg_entries
from kegg_mcp.services.query_models import (
    GeneIdentifierNamespace,
    GeneResolutionRequest,
    GeneResolutionTarget,
    KeggEntityKind,
    KeggEntityRef,
    KeggRelationEdge,
    KeggRelationType,
    KeggSearchDatabase,
    KeggSearchMode,
    MappingStatus,
    OrganismIdentifierNamespace,
    OrganismResolutionRequest,
    ResolutionOperation,
    ResolveKeggEntitiesRequest,
    SearchKeggEntriesRequest,
    TaxonomyResolutionRank,
    TraceKeggRelationsRequest,
)
from kegg_mcp.services.reference_budget import KeggQueryClient
from kegg_mcp.services.relation_tracing import trace_kegg_relations
from kegg_mcp.services.result_store import SQLiteResultStore

_NOW = datetime(2026, 7, 30, 8, 0, tzinfo=UTC)


def _provenance(
    operation: KeggOperation,
    marker: int,
    *,
    response_bytes: int = 100,
) -> KeggBatchProvenance:
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
        response_bytes=response_bytes,
        parser_name=f"synthetic_{operation.value}",
        parser_version=PARSER_VERSION,
        database_release="Release synthetic",
        attempt_count=1,
        is_stale=False,
    )


class _QueryClient:
    def __init__(self, config: KeggClientConfig | None = None) -> None:
        self._config = config or KeggClientConfig.model_validate({})
        self.find_rows: dict[
            tuple[KeggFindDatabase, str, str | None],
            tuple[tuple[str, str], ...],
        ] = {}
        self.conv_rows: tuple[tuple[str, str], ...] = ()
        self.link_rows: dict[
            KeggLinkRelationship,
            tuple[tuple[str, str], ...],
        ] = {}
        self.link_rows_by_rank: dict[
            tuple[KeggLinkRelationship, KeggTaxonomyRank],
            tuple[tuple[str, str], ...],
        ] = {}
        self.link_response_bytes: dict[KeggLinkRelationship, int] = {}
        self.genomes: dict[str, tuple[str, str, str, tuple[str, ...]]] = {}
        self.missing_genes: set[str] = set()
        self.organism_pathways: dict[
            str,
            tuple[tuple[str, str], ...],
        ] = {}
        self.organism_pathway_response_bytes: dict[str, int] = {}
        self.find_requests: list[FindRequest] = []
        self.conv_requests: list[ConvRequest] = []
        self.link_requests: list[LinkRequest] = []
        self.get_requests: list[GetRequest] = []
        self.organism_pathway_requests: list[OrganismPathwayListRequest] = []
        self._call_count = 0

    @property
    def config(self) -> KeggClientConfig:
        return self._config

    def _next_provenance(
        self,
        operation: KeggOperation,
        *,
        response_bytes: int = 100,
    ) -> KeggBatchProvenance:
        self._call_count += 1
        return _provenance(
            operation,
            self._call_count,
            response_bytes=response_bytes,
        )

    def find(
        self,
        request: FindRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> FindResult:
        del options
        self.find_requests.append(request)
        rows = self.find_rows.get(
            (request.database, request.query, request.organism),
            (),
        )
        return FindResult(
            request=request,
            document=KeggFindDocument(
                rows=tuple(
                    KeggFindRow(
                        line_number=index,
                        identifier=identifier,
                        matched_text=matched_text,
                    )
                    for index, (identifier, matched_text) in enumerate(
                        rows,
                        start=1,
                    )
                )
            ),
            batch=self._next_provenance(KeggOperation.FIND),
        )

    def conv(
        self,
        request: ConvRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> ConvResult:
        del options
        self.conv_requests.append(request)
        rows = tuple(
            KeggPairRow(
                line_number=index,
                source_id=source,
                target_id=target,
            )
            for index, (source, target) in enumerate(self.conv_rows, start=1)
            if _requested_source(source, request.source_identifiers)
        )
        return ConvResult(
            request=request,
            rows=rows,
            batches=(self._next_provenance(KeggOperation.CONV),),
        )

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        del options
        self.link_requests.append(request)
        configured_rows = self.link_rows_by_rank.get(
            (request.relationship, request.taxonomy_rank),
            self.link_rows.get(request.relationship, ()),
        )
        rows = tuple(
            KeggPairRow(
                line_number=index,
                source_id=source,
                target_id=target,
            )
            for index, (source, target) in enumerate(
                configured_rows,
                start=1,
            )
            if _requested_source(source, request.source_identifiers)
        )
        return LinkResult(
            request=request,
            rows=rows,
            batches=(
                self._next_provenance(
                    KeggOperation.LINK,
                    response_bytes=self.link_response_bytes.get(
                        request.relationship,
                        100,
                    ),
                ),
            ),
        )

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        del options
        self.get_requests.append(request)
        missing: list[KeggEntryRef] = []
        if request.entries[0].database is KeggGetDatabase.GENE:
            gene_entries: list[KeggFlatFileEntry] = []
            for item in request.entries:
                if item.identifier in self.missing_genes:
                    missing.append(item)
                else:
                    gene_entries.append(_gene_entry(item.identifier))
            documents = (KeggFlatFileDocument(entries=tuple(gene_entries)),)
        else:
            records: dict[str, tuple[str, str, str, tuple[str, ...]]] = {}
            for item in request.entries:
                record = self.genomes.get(item.identifier)
                if record is None:
                    missing.append(item)
                else:
                    records.setdefault(record[0], record)
            documents = (
                KeggFlatFileDocument(
                    entries=tuple(_genome_entry(record) for record in records.values())
                ),
            )
        return GetResult(
            request=request,
            documents=documents,
            missing_entries=tuple(missing),
            batches=(self._next_provenance(KeggOperation.GET),),
        )

    def list_organism_pathways(
        self,
        request: OrganismPathwayListRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> OrganismPathwayListResult:
        del options
        self.organism_pathway_requests.append(request)
        rows = self.organism_pathways.get(request.organism, ())
        return OrganismPathwayListResult(
            request=request,
            document=KeggOrganismPathwayDocument(
                organism=request.organism,
                rows=tuple(
                    KeggOrganismPathwayRow(
                        line_number=index,
                        pathway_id=pathway_id,
                        name=name,
                    )
                    for index, (pathway_id, name) in enumerate(rows, start=1)
                ),
            ),
            batch=self._next_provenance(
                KeggOperation.LIST,
                response_bytes=self.organism_pathway_response_bytes.get(
                    request.organism,
                    100,
                ),
            ),
        )


def _requested_source(source: str, requested: tuple[str, ...]) -> bool:
    aliases = {source}
    if source.startswith("up:"):
        aliases.add(f"uniprot:{source.removeprefix('up:')}")
    return any(
        alias == identifier or alias.endswith(f":{identifier}")
        for alias in aliases
        for identifier in requested
    )


def _genome_entry(
    record: tuple[str, str, str, tuple[str, ...]],
) -> KeggFlatFileEntry:
    t_number, code, name, lineage = record
    fields = (
        KeggFlatFileField(
            name="ORG_CODE",
            value_lines=(code,),
            start_line=1,
            end_line=1,
        ),
        KeggFlatFileField(
            name="NAME",
            value_lines=(name,),
            start_line=2,
            end_line=2,
        ),
        KeggFlatFileField(
            name="LINEAGE",
            value_lines=("; ".join(lineage),),
            start_line=3,
            end_line=3,
        ),
    )
    return KeggFlatFileEntry(
        identifier=t_number,
        fields=fields,
        start_line=1,
        end_line=3,
    )


def _gene_entry(identifier: str) -> KeggFlatFileEntry:
    field = KeggFlatFileField(
        name="ENTRY",
        value_lines=(identifier,),
        start_line=1,
        end_line=1,
    )
    return KeggFlatFileEntry(
        identifier=identifier,
        fields=(field,),
        start_line=1,
        end_line=1,
    )


def _add_genome(
    client: _QueryClient,
    *,
    t_number: str,
    code: str,
    name: str,
    lineage: tuple[str, ...],
) -> None:
    record = (t_number, code, name, lineage)
    client.genomes[t_number] = record
    client.genomes[code] = record


def _artifact(
    store: SQLiteResultStore,
    result_id: str,
) -> dict[str, object]:
    content = store.read_artifact(
        "scope",
        result_id,
        "detail",
        offset=0,
        limit=store.limits.max_range_bytes,
    ).content
    parsed = json.loads(content)
    assert isinstance(parsed, dict)
    return cast(dict[str, object], parsed)


def _entity(kind: KeggEntityKind, identifier: str) -> KeggEntityRef:
    return KeggEntityRef(kind=kind, identifier=identifier)


def test_search_request_is_bounded_and_public_scope_excludes_brite() -> None:
    assert "brite" not in {database.value for database in KeggSearchDatabase}
    with pytest.raises(ValidationError):
        SearchKeggEntriesRequest(
            database=KeggSearchDatabase.KO,
            query="hexokinase",
            max_results=0,
        )
    with pytest.raises(ValidationError):
        SearchKeggEntriesRequest(
            database=KeggSearchDatabase.KO,
            query="hexokinase",
            max_results=101,
        )


@pytest.mark.parametrize(
    ("mode", "query"),
    [
        (KeggSearchMode.FORMULA, "not-a-formula"),
        (KeggSearchMode.EXACT_MASS, "-1"),
        (KeggSearchMode.EXACT_MASS, "3-2"),
        (KeggSearchMode.EXACT_MASS, "nan"),
        (KeggSearchMode.EXACT_MASS, "1.2.3"),
        (KeggSearchMode.MOLECULAR_WEIGHT, "-1"),
    ],
)
def test_search_rejects_invalid_chemical_queries_at_the_public_boundary(
    mode: KeggSearchMode,
    query: str,
) -> None:
    with pytest.raises(ValidationError):
        SearchKeggEntriesRequest(
            database=KeggSearchDatabase.COMPOUND,
            query=query,
            mode=mode,
        )


def test_search_preserves_raw_candidates_without_scores_and_retains_all_rows(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    client.find_rows[(KeggFindDatabase.KO, "hexokinase", None)] = (
        ("K00844", "hexokinase [EC:2.7.1.1]"),
        ("K12407", "glucokinase"),
        ("K00844", "hexokinase [EC:2.7.1.1]"),
    )
    store = SQLiteResultStore(tmp_path / "results.sqlite3")

    result = search_kegg_entries(
        SearchKeggEntriesRequest(
            database=KeggSearchDatabase.KO,
            query="hexokinase",
            max_results=2,
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    assert result.observed_count == 3
    assert result.returned_count == 2
    assert result.truncated is True
    assert result.candidates[0].raw_match == "hexokinase [EC:2.7.1.1]"
    assert result.candidates[0].name == result.candidates[0].raw_match
    assert "score" not in result.candidates[0].model_dump()
    assert "not relevance-ranked" in result.interpretation_caveats[0]
    assert "not compound identifications" in result.interpretation_caveats[1]
    retained = _artifact(store, result.result.result_id)
    find_result = cast(dict[str, object], retained["find_result"])
    document = cast(dict[str, object], find_result["document"])
    assert len(cast(list[object], document["rows"])) == 3


def test_resolution_discriminator_is_required_in_schema_and_at_runtime() -> None:
    adapter: TypeAdapter[ResolveKeggEntitiesRequest] = TypeAdapter(ResolveKeggEntitiesRequest)
    schema = adapter.json_schema()
    definitions = cast(dict[str, dict[str, object]], schema["$defs"])
    branches = cast(list[dict[str, str]], schema["oneOf"])

    for branch in branches:
        definition_name = branch["$ref"].rsplit("/", maxsplit=1)[-1]
        required = cast(list[str], definitions[definition_name]["required"])
        assert "kind" in required

    with pytest.raises(ValidationError, match="union_tag_not_found"):
        adapter.validate_python(
            {
                "source_namespace": "gene_symbol",
                "identifiers": ["TP53"],
                "organism": "hsa",
            }
        )


def test_public_models_reject_leading_zero_numeric_identifiers() -> None:
    with pytest.raises(ValidationError):
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.NCBI_GENEID,
            identifiers=("0001",),
        )
    with pytest.raises(ValidationError):
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.TAXONOMY,
            identifiers=("taxid:00562",),
        )
    with pytest.raises(ValidationError):
        KeggEntityRef(
            kind=KeggEntityKind.TAXONOMY,
            identifier="taxid:00562",
        )


@pytest.mark.parametrize(
    ("namespace", "valid_identifier", "oversized_identifier"),
    [
        (
            GeneIdentifierNamespace.NCBI_GENEID,
            "1" * 244,
            "1" * 245,
        ),
        (
            GeneIdentifierNamespace.NCBI_PROTEINID,
            "A" * 241,
            "A" * 242,
        ),
        (
            GeneIdentifierNamespace.UNIPROT,
            "A" * 248,
            "A" * 249,
        ),
    ],
)
def test_external_gene_identifiers_fit_the_qualified_conv_bound(
    namespace: GeneIdentifierNamespace,
    valid_identifier: str,
    oversized_identifier: str,
) -> None:
    request = GeneResolutionRequest(
        kind="gene",
        source_namespace=namespace,
        identifiers=(valid_identifier,),
    )
    assert len(request.conversion_identifier(valid_identifier)) == 256

    with pytest.raises(ValidationError):
        GeneResolutionRequest(
            kind="gene",
            source_namespace=namespace,
            identifiers=(oversized_identifier,),
        )


def test_direct_gene_and_taxonomy_identifiers_fit_downstream_bounds() -> None:
    GeneResolutionRequest(
        kind="gene",
        source_namespace=GeneIdentifierNamespace.KEGG_GENE,
        identifiers=(f"hsa:{'A' * 96}",),
    )
    with pytest.raises(ValidationError):
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.KEGG_GENE,
            identifiers=(f"hsa:{'A' * 97}",),
        )

    OrganismResolutionRequest(
        kind="organism",
        source_namespace=OrganismIdentifierNamespace.TAXONOMY,
        identifiers=("1" * 250,),
    )
    with pytest.raises(ValidationError):
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.TAXONOMY,
            identifiers=("1" * 251,),
        )


@pytest.mark.parametrize(
    ("namespace", "identifier", "conv_source", "find_key", "expected_operations"),
    [
        (
            GeneIdentifierNamespace.KEGG_GENE,
            "hsa:1",
            None,
            None,
            (ResolutionOperation.DIRECT, ResolutionOperation.GET),
        ),
        (
            GeneIdentifierNamespace.NCBI_GENEID,
            "1",
            "ncbi-geneid:1",
            None,
            (ResolutionOperation.CONV,),
        ),
        (
            GeneIdentifierNamespace.NCBI_PROTEINID,
            "NP_000001.1",
            "ncbi-proteinid:NP_000001.1",
            None,
            (ResolutionOperation.CONV,),
        ),
        (
            GeneIdentifierNamespace.UNIPROT,
            "P12345",
            "up:P12345",
            None,
            (ResolutionOperation.CONV,),
        ),
        (
            GeneIdentifierNamespace.GENE_SYMBOL,
            "TP53",
            None,
            (KeggFindDatabase.GENES, "TP53", "hsa"),
            (ResolutionOperation.FIND, ResolutionOperation.GET),
        ),
    ],
)
def test_gene_resolver_supports_each_namespace(
    tmp_path: Path,
    namespace: GeneIdentifierNamespace,
    identifier: str,
    conv_source: str | None,
    find_key: tuple[KeggFindDatabase, str, str | None] | None,
    expected_operations: tuple[ResolutionOperation, ...],
) -> None:
    client = _QueryClient()
    if namespace is GeneIdentifierNamespace.GENE_SYMBOL:
        _add_genome(
            client,
            t_number="T01001",
            code="hsa",
            name="Homo sapiens",
            lineage=("Eukaryotes",),
        )
    if conv_source is not None:
        client.conv_rows = ((conv_source, "hsa:1"),)
    if find_key is not None:
        client.find_rows[find_key] = (("hsa:1", "TP53; tumor protein p53"),)
    store = SQLiteResultStore(tmp_path / f"{namespace.value}.sqlite3")

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=namespace,
            identifiers=(identifier,),
            organism="hsa" if namespace is GeneIdentifierNamespace.GENE_SYMBOL else None,
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    resolution = result.resolutions[0]
    assert resolution.status is MappingStatus.ONE_TO_ONE
    assert resolution.operations_used == expected_operations
    assert resolution.candidates[0].canonical_entity == _entity(
        KeggEntityKind.GENE,
        "hsa:1",
    )
    assert resolution.candidates[0].entities == (_entity(KeggEntityKind.GENE, "hsa:1"),)


def test_direct_gene_missing_from_kegg_is_unmapped(tmp_path: Path) -> None:
    client = _QueryClient()
    client.missing_genes.add("hsa:999999")

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.KEGG_GENE,
            identifiers=("hsa:999999",),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "missing-gene.sqlite3"),
        scope_id="scope",
    )

    assert result.resolutions[0].status is MappingStatus.UNMAPPED
    assert result.resolutions[0].candidates == ()
    assert result.resolutions[0].operations_used == (
        ResolutionOperation.DIRECT,
        ResolutionOperation.GET,
    )
    assert len(client.get_requests) == 1


def test_gene_resolver_reports_many_to_one_and_organism_mismatch(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01001",
        code="hsa",
        name="Homo sapiens",
        lineage=("Eukaryotes",),
    )
    client.conv_rows = (
        ("up:P1", "hsa:1"),
        ("up:P2", "hsa:1"),
        ("up:P3", "mmu:2"),
    )
    store = SQLiteResultStore(tmp_path / "results.sqlite3")

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.UNIPROT,
            identifiers=("P1", "P2", "P3"),
            organism="hsa",
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    assert [resolution.status for resolution in result.resolutions] == [
        MappingStatus.MANY_TO_ONE,
        MappingStatus.MANY_TO_ONE,
        MappingStatus.ORGANISM_MISMATCH,
    ]
    assert result.mapping_yield == pytest.approx(2 / 3)
    assert result.many_to_one_input_count == 2
    assert result.mismatch_input_count == 1
    assert result.resolutions[2].discarded_organism_mismatch_count == 1
    assert "not evidence" in result.interpretation_caveats[0]
    assert "without automatic selection" in result.interpretation_caveats[1]
    retained = _artifact(store, result.result.result_id)
    assert "mmu:2" in json.dumps(retained)


def test_gene_organism_filter_accepts_the_matching_t_number_and_rejects_other_species(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01001",
        code="hsa",
        name="Homo sapiens",
        lineage=("Eukaryotes",),
    )

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.KEGG_GENE,
            identifiers=("T01001:10458", "T01002:10458"),
            organism="hsa",
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "t-number-filter.sqlite3"),
        scope_id="scope",
    )

    assert [resolution.status for resolution in result.resolutions] == [
        MappingStatus.ONE_TO_ONE,
        MappingStatus.ORGANISM_MISMATCH,
    ]
    assert result.resolutions[0].candidates[0].canonical_entity == _entity(
        KeggEntityKind.GENE,
        "T01001:10458",
    )
    assert [
        tuple(entry.identifier for entry in request.entries) for request in client.get_requests
    ] == [
        ("hsa",),
        ("T01001:10458",),
    ]
    assert len(result.provenance) == 2


def test_gene_conversion_accepts_a_t_number_for_the_requested_organism(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01001",
        code="hsa",
        name="Homo sapiens",
        lineage=("Eukaryotes",),
    )
    client.conv_rows = (("up:P12345", "T01001:10458"),)

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.UNIPROT,
            identifiers=("P12345",),
            organism="hsa",
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "t-number-conv.sqlite3"),
        scope_id="scope",
    )

    assert result.resolutions[0].status is MappingStatus.ONE_TO_ONE
    assert result.resolutions[0].operations_used == (
        ResolutionOperation.CONV,
        ResolutionOperation.GET,
    )
    assert result.resolutions[0].candidates[0].canonical_entity == _entity(
        KeggEntityKind.GENE,
        "T01001:10458",
    )
    assert len(result.provenance) == 2


def test_many_to_one_counts_candidates_inside_overlapping_one_to_many_groups(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    client.conv_rows = (
        ("up:P1", "hsa:1"),
        ("up:P1", "hsa:2"),
        ("up:P2", "hsa:1"),
    )

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.UNIPROT,
            identifiers=("P1", "P2"),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "overlap.sqlite3"),
        scope_id="scope",
    )

    assert [resolution.status for resolution in result.resolutions] == [
        MappingStatus.ONE_TO_MANY,
        MappingStatus.MANY_TO_ONE,
    ]
    assert result.ambiguous_input_count == 1
    assert result.many_to_one_input_count == 1


def test_gene_conversion_is_split_into_single_low_level_batches(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    identifiers = tuple(f"P{index:05d}" for index in range(21))
    client.conv_rows = tuple(
        (f"up:{identifier}", f"hsa:{index}")
        for index, identifier in enumerate(identifiers, start=1)
    )
    store = SQLiteResultStore(tmp_path / "conv-batches.sqlite3")

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.UNIPROT,
            identifiers=identifiers,
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    assert [len(request.source_identifiers) for request in client.conv_requests] == [
        10,
        10,
        1,
    ]
    assert result.mapped_input_count == 21
    retained = _artifact(store, result.result.result_id)
    budget = cast(dict[str, object], retained["budget"])
    assert budget["kegg_requests"] == 3


def test_gene_get_and_conversion_respect_lower_max_identifiers(
    tmp_path: Path,
) -> None:
    config = KeggClientConfig.model_validate(
        {"limits": {"max_identifiers": 3, "relation_batch_size": 10}}
    )
    identifiers = tuple(f"hsa:{index}" for index in range(1, 8))
    direct_client = _QueryClient(config)

    direct = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.KEGG_GENE,
            identifiers=identifiers,
        ),
        client=cast(KeggQueryClient, direct_client),
        result_store=SQLiteResultStore(tmp_path / "low-get.sqlite3"),
        scope_id="scope",
    )

    assert direct.mapped_input_count == 7
    assert [len(request.entries) for request in direct_client.get_requests] == [3, 3, 1]

    converted_client = _QueryClient(config)
    accessions = tuple(f"P{index:05d}" for index in range(7))
    converted_client.conv_rows = tuple(
        (f"up:{accession}", f"hsa:{index}") for index, accession in enumerate(accessions, start=1)
    )
    converted = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.UNIPROT,
            identifiers=accessions,
        ),
        client=cast(KeggQueryClient, converted_client),
        result_store=SQLiteResultStore(tmp_path / "low-conv.sqlite3"),
        scope_id="scope",
    )

    assert converted.mapped_input_count == 7
    assert [len(request.source_identifiers) for request in converted_client.conv_requests] == [
        3,
        3,
        1,
    ]


def test_gene_targets_preserve_direct_organism_pathway_context(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    client.link_rows = {
        KeggLinkRelationship.GENE_TO_PATHWAY: (("hsa:1", "path:hsa00010"),),
        KeggLinkRelationship.GENE_TO_KO: (("hsa:1", "ko:K00001"),),
        KeggLinkRelationship.KO_TO_MODULE: (("ko:K00001", "md:M00001"),),
        KeggLinkRelationship.KO_TO_REACTION: (("ko:K00001", "rn:R00001"),),
        KeggLinkRelationship.KO_TO_ENZYME: (("ko:K00001", "ec:1.1.1.1"),),
    }
    store = SQLiteResultStore(tmp_path / "results.sqlite3")

    result = resolve_kegg_entities(
        GeneResolutionRequest(
            kind="gene",
            source_namespace=GeneIdentifierNamespace.KEGG_GENE,
            identifiers=("hsa:1",),
            targets=tuple(GeneResolutionTarget),
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    entities = result.resolutions[0].candidates[0].entities
    assert _entity(KeggEntityKind.PATHWAY, "hsa00010") in entities
    assert _entity(KeggEntityKind.KO, "K00001") in entities
    assert _entity(KeggEntityKind.MODULE, "M00001") in entities
    assert _entity(KeggEntityKind.REACTION, "R00001") in entities
    assert _entity(KeggEntityKind.ENZYME, "1.1.1.1") in entities
    relationships = [request.relationship for request in client.link_requests]
    assert KeggLinkRelationship.GENE_TO_PATHWAY in relationships
    assert KeggLinkRelationship.KO_TO_PATHWAY not in relationships
    assert result.resolutions[0].operations_used == (
        ResolutionOperation.DIRECT,
        ResolutionOperation.GET,
        ResolutionOperation.LINK,
    )


def test_organism_resolver_returns_code_t_number_taxonomy_name_and_lineage(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01001",
        code="hsa",
        name="Homo sapiens (human)",
        lineage=("Eukaryotes", "Animals", "Vertebrates"),
    )
    _add_genome(
        client,
        t_number="T00007",
        code="eco",
        name="Escherichia coli K-12 MG1655",
        lineage=("Bacteria", "Gammaproteobacteria"),
    )
    _add_genome(
        client,
        t_number="T00068",
        code="ece",
        name="Escherichia coli O157:H7",
        lineage=("Bacteria", "Gammaproteobacteria"),
    )
    client.find_rows = {
        (KeggFindDatabase.ORGANISM, "hsa", None): (("gn:T01001", "hsa Homo sapiens (human)"),),
        (KeggFindDatabase.ORGANISM, "Escherichia", None): (
            ("gn:T00007", "eco Escherichia coli K-12 MG1655"),
            ("gn:T00068", "ece Escherichia coli O157:H7"),
        ),
    }
    client.link_rows = {
        KeggLinkRelationship.GENOME_TO_TAXONOMY: (
            ("gn:hsa", "taxid:9606"),
            ("gn:eco", "taxid:562"),
            ("gn:ece", "taxid:562"),
        ),
        KeggLinkRelationship.TAXONOMY_TO_GENOME: (("taxid:9606", "gn:hsa"),),
    }

    requests = (
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.CODE,
            identifiers=("hsa",),
        ),
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.GENOME,
            identifiers=("T01001",),
        ),
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.TAXONOMY,
            identifiers=("9606",),
        ),
    )
    for index, request in enumerate(requests):
        result = resolve_kegg_entities(
            request,
            client=cast(KeggQueryClient, client),
            result_store=SQLiteResultStore(tmp_path / f"organism-{index}.sqlite3"),
            scope_id="scope",
        )
        candidate = result.resolutions[0].candidates[0]
        assert candidate.canonical_entity == _entity(KeggEntityKind.ORGANISM, "hsa")
        assert {(entity.kind, entity.identifier) for entity in candidate.entities} == {
            (KeggEntityKind.ORGANISM, "hsa"),
            (KeggEntityKind.GENOME, "T01001"),
            (KeggEntityKind.TAXONOMY, "taxid:9606"),
        }
        assert candidate.name == "Homo sapiens (human)"
        assert candidate.taxonomy_lineage == (
            "Eukaryotes",
            "Animals",
            "Vertebrates",
        )
        assert candidate.organism_pathways is not None
        assert candidate.organism_pathways.total_count == 0
        assert candidate.organism_pathways.preview == ()
        assert candidate.organism_pathways.truncated is False

    ambiguous = resolve_kegg_entities(
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.NAME,
            identifiers=("Escherichia",),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "ambiguous.sqlite3"),
        scope_id="scope",
    )
    assert ambiguous.resolutions[0].status is MappingStatus.ONE_TO_MANY
    assert {
        candidate.canonical_entity.identifier for candidate in ambiguous.resolutions[0].candidates
    } == {"eco", "ece"}
    assert {candidate.name for candidate in ambiguous.resolutions[0].candidates} == {
        "Escherichia coli K-12 MG1655",
        "Escherichia coli O157:H7",
    }


@pytest.mark.parametrize(
    ("namespace", "identifier"),
    [
        (OrganismIdentifierNamespace.CODE, "zzz"),
        (OrganismIdentifierNamespace.GENOME, "T99999"),
    ],
)
def test_missing_organism_code_or_genome_is_unmapped(
    tmp_path: Path,
    namespace: OrganismIdentifierNamespace,
    identifier: str,
) -> None:
    result = resolve_kegg_entities(
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=namespace,
            identifiers=(identifier,),
        ),
        client=cast(KeggQueryClient, _QueryClient()),
        result_store=SQLiteResultStore(tmp_path / f"missing-{namespace.value}.sqlite3"),
        scope_id="scope",
    )

    assert result.resolutions[0].status is MappingStatus.UNMAPPED
    assert result.resolutions[0].candidates == ()
    assert ResolutionOperation.GET in result.resolutions[0].operations_used


def test_organism_pathway_summary_is_bounded_and_retains_complete_list(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01001",
        code="hsa",
        name="Homo sapiens (human)",
        lineage=("Eukaryotes", "Animals"),
    )
    client.organism_pathways["hsa"] = tuple(
        (f"path:hsa{index:05d}", f"Synthetic pathway {index}") for index in range(1, 26)
    )
    store = SQLiteResultStore(tmp_path / "pathway-summary.sqlite3")

    result = resolve_kegg_entities(
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.CODE,
            identifiers=("hsa",),
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    resolution = result.resolutions[0]
    assert resolution.status is MappingStatus.ONE_TO_ONE
    assert client.find_requests == []
    assert resolution.operations_used == (
        ResolutionOperation.DIRECT,
        ResolutionOperation.GET,
        ResolutionOperation.LIST,
        ResolutionOperation.LINK,
    )
    assert ResolutionOperation.LIST in resolution.operations_used
    summary = resolution.candidates[0].organism_pathways
    assert summary is not None
    assert summary.total_count == 25
    assert len(summary.preview) == 20
    assert summary.preview[0].pathway == _entity(
        KeggEntityKind.PATHWAY,
        "hsa00001",
    )
    assert summary.preview[0].name == "Synthetic pathway 1"
    assert summary.truncated is True

    retained = _artifact(store, result.result.result_id)
    list_steps = [
        step
        for step in cast(list[dict[str, object]], retained["steps"])
        if step["operation"] == ResolutionOperation.LIST.value
    ]
    assert len(list_steps) == 1
    list_result = cast(dict[str, object], list_steps[0]["result"])
    document = cast(dict[str, object], list_result["document"])
    assert len(cast(list[object], document["rows"])) == 25


def test_organism_name_resolution_rejects_a_find_get_code_mismatch(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01002",
        code="mmu",
        name="Mus musculus",
        lineage=("Eukaryotes",),
    )
    client.find_rows[(KeggFindDatabase.ORGANISM, "human", None)] = (("T01002", "hsa Homo sapiens"),)
    store = SQLiteResultStore(tmp_path / "organism-mismatch.sqlite3")

    with pytest.raises(KeggMcpError) as caught:
        resolve_kegg_entities(
            OrganismResolutionRequest(
                kind="organism",
                source_namespace=OrganismIdentifierNamespace.NAME,
                identifiers=("human",),
            ),
            client=cast(KeggQueryClient, client),
            result_store=store,
            scope_id="scope",
        )

    assert caught.value.detail.code is ErrorCode.KEGG_PARSE_FAILED
    assert {detail.name: detail.value for detail in caught.value.detail.safe_details}[
        "reason"
    ] == "organism_code_mismatch"
    assert store.list_results("scope").total_items == 0


def test_organism_pathway_list_counts_toward_resolver_budget(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01001",
        code="hsa",
        name="Homo sapiens (human)",
        lineage=("Eukaryotes", "Animals"),
    )
    client.organism_pathways["hsa"] = tuple(
        (f"path:hsa{index:05d}", f"Synthetic pathway {index}") for index in range(10_000)
    )
    store = SQLiteResultStore(tmp_path / "pathway-budget.sqlite3")

    with pytest.raises(KeggMcpError) as caught:
        resolve_kegg_entities(
            OrganismResolutionRequest(
                kind="organism",
                source_namespace=OrganismIdentifierNamespace.CODE,
                identifiers=("hsa",),
            ),
            client=cast(KeggQueryClient, client),
            result_store=store,
            scope_id="scope",
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert len(client.organism_pathway_requests) == 1
    assert client.link_requests == []
    assert store.list_results("scope").total_items == 0


def test_taxonomy_alias_duplicates_are_rejected_after_normalization() -> None:
    with pytest.raises(ValidationError, match="unique after namespace normalization"):
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.TAXONOMY,
            identifiers=("562", "taxid:562"),
        )


def test_taxonomy_rank_preserves_exact_empty_species_candidates_and_t_targets(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T00007",
        code="eco",
        name="Escherichia coli K-12 MG1655",
        lineage=("Bacteria", "Gammaproteobacteria"),
    )
    _add_genome(
        client,
        t_number="T00068",
        code="ece",
        name="Escherichia coli O157:H7",
        lineage=("Bacteria", "Gammaproteobacteria"),
    )
    client.link_rows_by_rank = {
        (
            KeggLinkRelationship.TAXONOMY_TO_GENOME,
            KeggTaxonomyRank.EXACT,
        ): (),
        (
            KeggLinkRelationship.TAXONOMY_TO_GENOME,
            KeggTaxonomyRank.SPECIES,
        ): (
            ("taxid:562", "gn:eco"),
            ("taxid:562", "gn:T00068"),
        ),
    }
    client.link_rows[KeggLinkRelationship.GENOME_TO_TAXONOMY] = (
        ("gn:eco", "taxid:562"),
        ("gn:ece", "taxid:562"),
    )

    exact = resolve_kegg_entities(
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.TAXONOMY,
            identifiers=("562",),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "exact.sqlite3"),
        scope_id="scope",
    )
    assert exact.resolutions[0].status is MappingStatus.UNMAPPED

    species = resolve_kegg_entities(
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.TAXONOMY,
            identifiers=("562",),
            taxonomy_rank=TaxonomyResolutionRank.SPECIES,
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "species.sqlite3"),
        scope_id="scope",
    )
    assert species.resolutions[0].status is MappingStatus.ONE_TO_MANY
    assert {
        candidate.canonical_entity.identifier for candidate in species.resolutions[0].candidates
    } == {"eco", "ece"}
    assert client.link_requests[-2].taxonomy_rank is KeggTaxonomyRank.SPECIES
    with pytest.raises(ValidationError):
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.NAME,
            identifiers=("Escherichia coli",),
            taxonomy_rank=TaxonomyResolutionRank.SPECIES,
        )


def test_organism_genome_get_is_chunked_and_recorded_per_low_level_batch(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    find_rows: list[tuple[str, str]] = []
    for index in range(11):
        code = f"a{chr(97 + index)}z"
        t_number = f"T{index + 1:05d}"
        _add_genome(
            client,
            t_number=t_number,
            code=code,
            name=f"Synthetic organism {index}",
            lineage=("Bacteria",),
        )
        find_rows.append((t_number, f"{code} Synthetic organism {index}"))
    client.find_rows[(KeggFindDatabase.ORGANISM, "Synthetic", None)] = tuple(find_rows)
    store = SQLiteResultStore(tmp_path / "chunked.sqlite3")

    result = resolve_kegg_entities(
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.NAME,
            identifiers=("Synthetic",),
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    assert [len(request.entries) for request in client.get_requests] == [10, 1]
    retained = _artifact(store, result.result.result_id)
    budget = cast(dict[str, object], retained["budget"])
    assert budget["kegg_requests"] == 15
    assert result.resolutions[0].status is MappingStatus.ONE_TO_MANY


def test_organism_get_respects_lower_max_identifiers(tmp_path: Path) -> None:
    config = KeggClientConfig.model_validate({"limits": {"max_identifiers": 2}})
    client = _QueryClient(config)
    identifiers = tuple(f"T{index:05d}" for index in range(1, 6))
    for index, identifier in enumerate(identifiers):
        _add_genome(
            client,
            t_number=identifier,
            code=f"a{chr(97 + index)}z",
            name=f"Synthetic organism {index}",
            lineage=("Bacteria",),
        )

    result = resolve_kegg_entities(
        OrganismResolutionRequest(
            kind="organism",
            source_namespace=OrganismIdentifierNamespace.GENOME,
            identifiers=identifiers,
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "low-genome-get.sqlite3"),
        scope_id="scope",
    )

    assert result.mapped_input_count == 5
    assert [len(request.entries) for request in client.get_requests] == [2, 2, 1]


def test_resolver_aggregate_budget_fails_before_retention(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    client.find_rows[(KeggFindDatabase.GENES, "TP53", "hsa")] = tuple(
        ("hsa:1", "TP53 repeated candidate") for _ in range(10_001)
    )
    store = SQLiteResultStore(tmp_path / "budget.sqlite3")

    with pytest.raises(KeggMcpError) as caught:
        resolve_kegg_entities(
            GeneResolutionRequest(
                kind="gene",
                source_namespace=GeneIdentifierNamespace.GENE_SYMBOL,
                identifiers=("TP53",),
                organism="hsa",
            ),
            client=cast(KeggQueryClient, client),
            result_store=store,
            scope_id="scope",
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert store.list_results("scope").total_items == 0


def test_trace_is_typed_retained_and_supports_depth_two(tmp_path: Path) -> None:
    client = _QueryClient()
    client.link_rows = {
        KeggLinkRelationship.KO_TO_REACTION: (("ko:K00001", "rn:R00001"),),
        KeggLinkRelationship.REACTION_TO_COMPOUND: (("rn:R00001", "cpd:C00001"),),
    }
    store = SQLiteResultStore(tmp_path / "results.sqlite3")

    result = trace_kegg_relations(
        TraceKeggRelationsRequest(
            seeds=(_entity(KeggEntityKind.KO, "K00001"),),
            edge_types=(
                KeggRelationType.KO_TO_REACTION,
                KeggRelationType.REACTION_TO_COMPOUND,
            ),
            max_depth=2,
        ),
        client=cast(KeggQueryClient, client),
        result_store=store,
        scope_id="scope",
    )

    assert [edge.depth for edge in result.edges] == [1, 2]
    assert [edge.provenance_batch_indexes for edge in result.edges] == [(0,), (1,)]
    assert result.edges[1].target == _entity(KeggEntityKind.COMPOUND, "C00001")
    assert "not evidence of regulation" in result.interpretation_caveats[0]
    assert "does not establish activity" in result.interpretation_caveats[1]
    retained = _artifact(store, result.result.result_id)
    assert len(cast(list[object], retained["steps"])) == 2
    budget = cast(dict[str, object], retained["budget"])
    assert budget["kegg_requests"] == 2


def test_trace_rejects_edge_sets_that_cannot_start_from_any_seed() -> None:
    with pytest.raises(ValidationError, match="traversable"):
        TraceKeggRelationsRequest(
            seeds=(_entity(KeggEntityKind.COMPOUND, "C00031"),),
            edge_types=(KeggRelationType.KO_TO_PATHWAY,),
        )


def test_trace_edge_contract_rejects_incompatible_endpoint_kinds() -> None:
    with pytest.raises(ValidationError, match="endpoint kinds"):
        KeggRelationEdge(
            relationship=KeggRelationType.KO_TO_PATHWAY,
            source=_entity(KeggEntityKind.COMPOUND, "C00031"),
            target=_entity(KeggEntityKind.MODULE, "M00001"),
            depth=1,
            provenance_batch_indexes=(0,),
        )


def test_trace_genome_taxonomy_bridge_preserves_public_t_number_nodes(
    tmp_path: Path,
) -> None:
    client = _QueryClient()
    _add_genome(
        client,
        t_number="T01001",
        code="hsa",
        name="Homo sapiens (human)",
        lineage=("Eukaryotes", "Animals"),
    )
    client.link_rows = {
        KeggLinkRelationship.GENOME_TO_TAXONOMY: (("gn:hsa", "taxid:9606"),),
        KeggLinkRelationship.TAXONOMY_TO_GENOME: (("taxid:9606", "gn:hsa"),),
    }

    forward = trace_kegg_relations(
        TraceKeggRelationsRequest(
            seeds=(_entity(KeggEntityKind.GENOME, "T01001"),),
            edge_types=(KeggRelationType.GENOME_TO_TAXONOMY,),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "forward.sqlite3"),
        scope_id="scope",
    )
    assert forward.edges[0].source == _entity(KeggEntityKind.GENOME, "T01001")
    assert forward.edges[0].target == _entity(
        KeggEntityKind.TAXONOMY,
        "taxid:9606",
    )
    assert forward.edges[0].provenance_batch_indexes == (0, 1)
    assert len(forward.provenance) == 2
    assert client.link_requests[-1].source_identifiers == ("hsa",)

    reverse = trace_kegg_relations(
        TraceKeggRelationsRequest(
            seeds=(_entity(KeggEntityKind.TAXONOMY, "taxid:9606"),),
            edge_types=(KeggRelationType.TAXONOMY_TO_GENOME,),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "reverse.sqlite3"),
        scope_id="scope",
    )
    assert reverse.edges[0].source == _entity(
        KeggEntityKind.TAXONOMY,
        "taxid:9606",
    )
    assert reverse.edges[0].target == _entity(KeggEntityKind.GENOME, "T01001")
    assert reverse.edges[0].provenance_batch_indexes == (0, 1)
    assert len(reverse.provenance) == 2
    assert client.get_requests[-1].entries[0].identifier == "hsa"


def test_trace_genome_taxonomy_edges_reference_only_their_own_get_batch(
    tmp_path: Path,
) -> None:
    client = _QueryClient(KeggClientConfig.model_validate({"limits": {"max_identifiers": 1}}))
    _add_genome(
        client,
        t_number="T00007",
        code="eco",
        name="Escherichia coli K-12",
        lineage=("Bacteria",),
    )
    _add_genome(
        client,
        t_number="T01002",
        code="mmu",
        name="Mus musculus",
        lineage=("Eukaryotes",),
    )
    client.link_rows = {
        KeggLinkRelationship.GENOME_TO_TAXONOMY: (
            ("gn:eco", "taxid:562"),
            ("gn:mmu", "taxid:10090"),
        ),
        KeggLinkRelationship.TAXONOMY_TO_GENOME: (
            ("taxid:562", "gn:eco"),
            ("taxid:10090", "gn:mmu"),
        ),
    }

    forward = trace_kegg_relations(
        TraceKeggRelationsRequest(
            seeds=(
                _entity(KeggEntityKind.GENOME, "T00007"),
                _entity(KeggEntityKind.GENOME, "T01002"),
            ),
            edge_types=(KeggRelationType.GENOME_TO_TAXONOMY,),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "forward-exact.sqlite3"),
        scope_id="scope",
    )
    assert [edge.provenance_batch_indexes for edge in forward.edges] == [
        (0, 2),
        (1, 3),
    ]

    reverse = trace_kegg_relations(
        TraceKeggRelationsRequest(
            seeds=(
                _entity(KeggEntityKind.TAXONOMY, "taxid:562"),
                _entity(KeggEntityKind.TAXONOMY, "taxid:10090"),
            ),
            edge_types=(KeggRelationType.TAXONOMY_TO_GENOME,),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "reverse-exact.sqlite3"),
        scope_id="scope",
    )
    assert [edge.provenance_batch_indexes for edge in reverse.edges] == [
        (0, 2),
        (1, 3),
    ]


def test_trace_edges_reference_their_exact_link_batches(tmp_path: Path) -> None:
    client = _QueryClient(KeggClientConfig.model_validate({"limits": {"max_identifiers": 1}}))
    client.link_rows = {
        KeggLinkRelationship.KO_TO_PATHWAY: (
            ("ko:K00001", "path:ko00010"),
            ("ko:K00002", "path:ko00020"),
        ),
    }

    result = trace_kegg_relations(
        TraceKeggRelationsRequest(
            seeds=(
                _entity(KeggEntityKind.KO, "K00001"),
                _entity(KeggEntityKind.KO, "K00002"),
            ),
            edge_types=(KeggRelationType.KO_TO_PATHWAY,),
        ),
        client=cast(KeggQueryClient, client),
        result_store=SQLiteResultStore(tmp_path / "edge-provenance.sqlite3"),
        scope_id="scope",
    )

    assert len(result.provenance) == 2
    assert [edge.provenance_batch_indexes for edge in result.edges] == [(0,), (1,)]


def test_trace_enforces_global_raw_row_and_response_byte_budgets(
    tmp_path: Path,
) -> None:
    row_client = _QueryClient()
    row_client.link_rows = {
        KeggLinkRelationship.KO_TO_PATHWAY: tuple(("ko:K00001", "path:ko00010") for _ in range(3)),
        KeggLinkRelationship.KO_TO_MODULE: tuple(("ko:K00001", "md:M00001") for _ in range(6)),
    }
    row_store = SQLiteResultStore(tmp_path / "row-budget.sqlite3")
    with pytest.raises(KeggMcpError) as row_error:
        trace_kegg_relations(
            TraceKeggRelationsRequest(
                seeds=(_entity(KeggEntityKind.KO, "K00001"),),
                edge_types=(
                    KeggRelationType.KO_TO_PATHWAY,
                    KeggRelationType.KO_TO_MODULE,
                ),
                max_edges=2,
            ),
            client=cast(KeggQueryClient, row_client),
            result_store=row_store,
            scope_id="scope",
        )
    assert row_error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert row_store.list_results("scope").total_items == 0

    byte_client = _QueryClient()
    byte_client.link_rows = {
        KeggLinkRelationship.KO_TO_PATHWAY: (("ko:K00001", "path:ko00010"),),
        KeggLinkRelationship.KO_TO_MODULE: (("ko:K00001", "md:M00001"),),
    }
    byte_client.link_response_bytes = {
        KeggLinkRelationship.KO_TO_PATHWAY: 3_000_000,
        KeggLinkRelationship.KO_TO_MODULE: 3_000_000,
    }
    byte_store = SQLiteResultStore(tmp_path / "byte-budget.sqlite3")
    with pytest.raises(KeggMcpError) as byte_error:
        trace_kegg_relations(
            TraceKeggRelationsRequest(
                seeds=(_entity(KeggEntityKind.KO, "K00001"),),
                edge_types=(
                    KeggRelationType.KO_TO_PATHWAY,
                    KeggRelationType.KO_TO_MODULE,
                ),
                max_edges=2,
            ),
            client=cast(KeggQueryClient, byte_client),
            result_store=byte_store,
            scope_id="scope",
        )
    assert byte_error.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert byte_store.list_results("scope").total_items == 0


def test_trace_enforces_global_request_budget_across_relationships(
    tmp_path: Path,
) -> None:
    config = KeggClientConfig.model_validate({"limits": {"max_identifiers": 1}})
    client = _QueryClient(config)
    seeds = tuple(_entity(KeggEntityKind.KO, f"K{index:05d}") for index in range(1, 51))
    store = SQLiteResultStore(tmp_path / "request-budget.sqlite3")

    with pytest.raises(KeggMcpError) as caught:
        trace_kegg_relations(
            TraceKeggRelationsRequest(
                seeds=seeds,
                edge_types=(
                    KeggRelationType.KO_TO_PATHWAY,
                    KeggRelationType.KO_TO_MODULE,
                    KeggRelationType.KO_TO_REACTION,
                ),
            ),
            client=cast(KeggQueryClient, client),
            result_store=store,
            scope_id="scope",
        )

    assert caught.value.detail.code is ErrorCode.INPUT_LIMIT_EXCEEDED
    assert len(client.link_requests) == relation_tracing.MAX_TRACE_KEGG_REQUESTS
    assert store.list_results("scope").total_items == 0


def test_query_artifact_cap_applies_before_retention_for_all_services(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(query_support, "MAX_QUERY_ARTIFACT_BYTES", 1)

    search_store = SQLiteResultStore(tmp_path / "search-cap.sqlite3")
    with pytest.raises(KeggMcpError) as search_error:
        search_kegg_entries(
            SearchKeggEntriesRequest(
                database=KeggSearchDatabase.KO,
                query="hexokinase",
            ),
            client=cast(KeggQueryClient, _QueryClient()),
            result_store=search_store,
            scope_id="scope",
        )
    assert search_error.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert search_store.list_results("scope").total_items == 0

    resolve_store = SQLiteResultStore(tmp_path / "resolve-cap.sqlite3")
    with pytest.raises(KeggMcpError) as resolve_error:
        resolve_kegg_entities(
            GeneResolutionRequest(
                kind="gene",
                source_namespace=GeneIdentifierNamespace.KEGG_GENE,
                identifiers=("hsa:1",),
            ),
            client=cast(KeggQueryClient, _QueryClient()),
            result_store=resolve_store,
            scope_id="scope",
        )
    assert resolve_error.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert resolve_store.list_results("scope").total_items == 0

    trace_store = SQLiteResultStore(tmp_path / "trace-cap.sqlite3")
    with pytest.raises(KeggMcpError) as trace_error:
        trace_kegg_relations(
            TraceKeggRelationsRequest(
                seeds=(_entity(KeggEntityKind.KO, "K00001"),),
                edge_types=(KeggRelationType.KO_TO_PATHWAY,),
            ),
            client=cast(KeggQueryClient, _QueryClient()),
            result_store=trace_store,
            scope_id="scope",
        )
    assert trace_error.value.detail.code is ErrorCode.OUTPUT_LIMIT_EXCEEDED
    assert trace_store.list_results("scope").total_items == 0


def test_retained_result_model_failures_are_compensated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_result(**values: object) -> NoReturn:
        del values
        raise RuntimeError("synthetic result failure")

    search_client = _QueryClient()
    search_client.find_rows[(KeggFindDatabase.KO, "hexokinase", None)] = (("K00844", "hexokinase"),)
    search_store = SQLiteResultStore(tmp_path / "search.sqlite3")
    with monkeypatch.context() as patch:
        patch.setattr(kegg_search, "SearchKeggEntriesResult", reject_result)
        with pytest.raises(RuntimeError, match="synthetic result failure"):
            search_kegg_entries(
                SearchKeggEntriesRequest(
                    database=KeggSearchDatabase.KO,
                    query="hexokinase",
                ),
                client=cast(KeggQueryClient, search_client),
                result_store=search_store,
                scope_id="scope",
            )
    assert search_store.list_results("scope").total_items == 0

    resolve_store = SQLiteResultStore(tmp_path / "resolve.sqlite3")
    with monkeypatch.context() as patch:
        patch.setattr(
            resolution_support,
            "ResolveKeggEntitiesResult",
            reject_result,
        )
        with pytest.raises(RuntimeError, match="synthetic result failure"):
            resolve_kegg_entities(
                GeneResolutionRequest(
                    kind="gene",
                    source_namespace=GeneIdentifierNamespace.KEGG_GENE,
                    identifiers=("hsa:1",),
                ),
                client=cast(KeggQueryClient, _QueryClient()),
                result_store=resolve_store,
                scope_id="scope",
            )
    assert resolve_store.list_results("scope").total_items == 0

    trace_client = _QueryClient()
    trace_client.link_rows = {
        KeggLinkRelationship.KO_TO_PATHWAY: (("ko:K00001", "path:ko00010"),),
    }
    trace_store = SQLiteResultStore(tmp_path / "trace.sqlite3")
    with monkeypatch.context() as patch:
        patch.setattr(
            relation_tracing,
            "TraceKeggRelationsResult",
            reject_result,
        )
        with pytest.raises(RuntimeError, match="synthetic result failure"):
            trace_kegg_relations(
                TraceKeggRelationsRequest(
                    seeds=(_entity(KeggEntityKind.KO, "K00001"),),
                    edge_types=(KeggRelationType.KO_TO_PATHWAY,),
                ),
                client=cast(KeggQueryClient, trace_client),
                result_store=trace_store,
                scope_id="scope",
            )
    assert trace_store.list_results("scope").total_items == 0
