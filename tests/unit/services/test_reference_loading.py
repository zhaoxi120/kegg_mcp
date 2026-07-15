"""Tests for typed and bounded KEGG reference loading services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from kegg_mcp.analysis.contracts import (
    ModuleDefinitionOrigin,
    ModuleReferenceIssueKind,
)
from kegg_mcp.analysis.pathway_coverage import (
    PathwayCoverageLimits,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
)
from kegg_mcp.domain.errors import ErrorCode, KeggMcpError
from kegg_mcp.kegg.contracts import (
    PARSER_VERSION,
    PUBLIC_KEGG_ENDPOINT_LABEL,
    AccessMode,
    CacheLookupState,
    GetRequest,
    GetResult,
    KeggBatchProvenance,
    KeggFlatFileDocument,
    KeggGetDatabase,
    KeggLinkRelationship,
    KeggOperation,
    KeggPairRow,
    KeggRequestOptions,
    LinkRequest,
    LinkResult,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.parsers import parse_flat_file_response
from kegg_mcp.services.reference_loading import (
    PathwaySpec,
    ReferenceLoadingLimits,
    load_module_graphs,
    load_pathway_references,
)

_NOW = datetime(2026, 7, 14, 5, 0, tzinfo=UTC)


def _provenance(
    operation: KeggOperation,
    *,
    marker: int = 1,
    cached: bool = False,
    stale: bool = False,
) -> KeggBatchProvenance:
    if stale and not cached:
        raise AssertionError("stale test provenance must originate from cache")
    retrieved_at = _NOW + timedelta(minutes=marker)
    expires_at = retrieved_at + timedelta(days=1)
    return KeggBatchProvenance(
        operation=operation,
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label=PUBLIC_KEGG_ENDPOINT_LABEL,
        origin=ResponseOrigin.CACHE if cached else ResponseOrigin.NETWORK,
        cache_lookup_state=(
            CacheLookupState.STALE_HIT
            if stale
            else CacheLookupState.FRESH_HIT
            if cached
            else CacheLookupState.MISS
        ),
        retrieved_at=retrieved_at,
        served_at=expires_at + timedelta(hours=1) if stale else retrieved_at,
        expires_at=expires_at,
        response_bytes=100,
        parser_name="pair_table" if operation is KeggOperation.LINK else "flat_file",
        parser_version=PARSER_VERSION,
        database_release="Release 116.0+/07-14",
        attempt_count=0 if cached else 1,
        is_stale=stale,
    )


def _module_text(
    module_id: str,
    definition: str,
    *,
    name_lines: tuple[str, ...] = ("Synthetic module",),
    definition_lines: tuple[str, ...] | None = None,
    include_name: bool = True,
    duplicate_definition: bool = False,
) -> str:
    lines = [f"ENTRY       {module_id}            Module"]
    if include_name:
        lines.append(f"NAME        {name_lines[0]}")
        lines.extend(f"            {line}" for line in name_lines[1:])
    parts = definition_lines or (definition,)
    lines.append(f"DEFINITION  {parts[0]}")
    lines.extend(f"            {line}" for line in parts[1:])
    if duplicate_definition:
        lines.append("DEFINITION  K99999")
    lines.append("///")
    return "\n".join(lines) + "\n"


class _FakeClient:
    def __init__(
        self,
        *,
        modules: dict[str, str] | None = None,
        pathways: dict[str, tuple[str, str, str]] | None = None,
        module_batch_size: int = 10,
        cached_module_ids: frozenset[str] = frozenset(),
    ) -> None:
        self.modules = modules or {}
        self.pathways = pathways or {}
        self.module_batch_size = module_batch_size
        self.cached_module_ids = cached_module_ids
        self.get_requests: list[GetRequest] = []
        self.link_requests: list[LinkRequest] = []
        self.options: list[KeggRequestOptions | None] = []
        self.call_log: list[tuple[str, str]] = []

    def get(
        self,
        request: GetRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> GetResult:
        self.get_requests.append(request)
        self.options.append(options)
        first = request.entries[0]
        self.call_log.append(("get", first.identifier))
        if first.database is KeggGetDatabase.PATHWAY:
            return self._pathway_get(request)
        if first.database is not KeggGetDatabase.MODULE:
            raise AssertionError("the reference loader used an unexpected GET database")
        return self._module_get(request)

    def link(
        self,
        request: LinkRequest,
        *,
        options: KeggRequestOptions | None = None,
    ) -> LinkResult:
        self.link_requests.append(request)
        self.options.append(options)
        pathway_id = request.source_identifiers[0]
        self.call_log.append(("link", pathway_id))
        targets, _, _ = self.pathways[pathway_id]
        return LinkResult(
            request=request,
            rows=tuple(
                KeggPairRow(
                    line_number=index,
                    source_id=f"path:{pathway_id}",
                    target_id=target,
                )
                for index, target in enumerate(targets.split(), start=1)
            ),
            batches=(_provenance(KeggOperation.LINK, marker=12),),
        )

    def _module_get(self, request: GetRequest) -> GetResult:
        documents: list[KeggFlatFileDocument] = []
        batches: list[KeggBatchProvenance] = []
        for start in range(0, len(request.entries), self.module_batch_size):
            entries = request.entries[start : start + self.module_batch_size]
            body = "".join(
                self.modules[entry.identifier]
                for entry in entries
                if entry.identifier in self.modules
            ).encode()
            documents.append(parse_flat_file_response(body))
            cached = all(entry.identifier in self.cached_module_ids for entry in entries)
            batches.append(
                _provenance(
                    KeggOperation.GET,
                    marker=len(self.get_requests) + start + 1,
                    cached=cached,
                    stale=cached,
                )
            )
        missing = tuple(entry for entry in request.entries if entry.identifier not in self.modules)
        return GetResult(
            request=request,
            documents=tuple(documents),
            missing_entries=missing,
            batches=tuple(batches),
        )

    def _pathway_get(self, request: GetRequest) -> GetResult:
        pathway_id = request.entries[0].identifier
        _, name, pathway_class = self.pathways[pathway_id]
        body = (
            f"ENTRY       {pathway_id}                    Pathway\n"
            f"NAME        {name}\n"
            f"CLASS       {pathway_class}\n"
            "///\n"
        ).encode()
        return GetResult(
            request=request,
            documents=(parse_flat_file_response(body),),
            missing_entries=(),
            batches=(_provenance(KeggOperation.GET, marker=14),),
        )


def _module_ids(client: _FakeClient) -> list[tuple[str, ...]]:
    return [
        tuple(entry.identifier for entry in request.entries)
        for request in client.get_requests
        if request.entries[0].database is KeggGetDatabase.MODULE
    ]


def _assert_error(error: pytest.ExceptionInfo[KeggMcpError], code: ErrorCode) -> None:
    assert error.value.detail.code is code


def _safe_details(error: pytest.ExceptionInfo[KeggMcpError]) -> dict[str, str]:
    return {item.name: item.value for item in error.value.detail.safe_details}


def test_module_loading_recurses_in_root_order_and_retains_batch_provenance() -> None:
    client = _FakeClient(
        modules={
            "M00001": _module_text(
                "M00001",
                "M00002 M00003",
                name_lines=("Root", "continued name"),
                definition_lines=("M00002", "M00003"),
            ),
            "M00010": _module_text("M00010", "M00003"),
            "M00002": _module_text("M00002", "M00004+K00002"),
            "M00003": _module_text("M00003", "K00003"),
            "M00004": _module_text("M00004", "K00004"),
        },
        module_batch_size=1,
        cached_module_ids=frozenset({"M00010"}),
    )
    options = KeggRequestOptions(refresh=True)

    graphs = load_module_graphs(
        client,
        ("M00001", "M00010"),
        options=options,
    )

    assert _module_ids(client) == [
        ("M00001", "M00010"),
        ("M00002", "M00003"),
        ("M00004",),
    ]
    assert [graph.root_module_id for graph in graphs] == ["M00001", "M00010"]
    assert [item.definition.module_id for item in graphs[0].modules] == [
        "M00001",
        "M00002",
        "M00004",
        "M00003",
    ]
    root = graphs[0].modules[0].definition
    assert root.module_name == "Root continued name"
    assert root.definition == "M00002 M00003"
    assert root.provenance.origin is ModuleDefinitionOrigin.KEGG_NETWORK
    assert root.provenance.retrieval is not None
    assert root.provenance.retrieval.retrieved_at == _NOW + timedelta(minutes=2)
    assert root.provenance.retrieval.database_release == "Release 116.0+/07-14"
    second_root = graphs[1].modules[0].definition
    assert second_root.provenance.origin is ModuleDefinitionOrigin.KEGG_CACHE
    assert second_root.provenance.retrieval is not None
    assert second_root.provenance.retrieval.is_stale is True
    assert all(option is options for option in client.options)


def test_cycles_and_missing_references_are_explicit_and_never_retried() -> None:
    client = _FakeClient(
        modules={
            "M00001": _module_text("M00001", "M00002"),
            "M00002": _module_text("M00002", "M00001+M00003"),
        }
    )

    graph = load_module_graphs(
        client,
        ("M00001",),
        options=KeggRequestOptions(),
    )[0]

    assert _module_ids(client) == [("M00001",), ("M00002",), ("M00003",)]
    assert [issue.kind for issue in graph.issues] == [
        ModuleReferenceIssueKind.CYCLE,
        ModuleReferenceIssueKind.UNRESOLVED,
    ]
    assert graph.issues[1].target_module_id == "M00003"
    assert len(graph.retrieval_provenance) == 3
    assert all(batch.operation is KeggOperation.GET for batch in graph.retrieval_provenance)


def test_wide_module_round_uses_typed_chunks_of_at_most_ten_entries() -> None:
    referenced_ids = tuple(f"M{index:05d}" for index in range(2, 14))
    modules = {
        "M00001": _module_text("M00001", " ".join(referenced_ids)),
        **{
            module_id: _module_text(module_id, f"K{index:05d}")
            for index, module_id in enumerate(referenced_ids, start=1)
        },
    }
    client = _FakeClient(modules=modules)

    graph = load_module_graphs(
        client,
        ("M00001",),
        options=KeggRequestOptions(),
    )[0]

    assert _module_ids(client) == [
        ("M00001",),
        referenced_ids[:10],
        referenced_ids[10:],
    ]
    assert all(len(request.entries) <= 10 for request in client.get_requests)
    assert [item.definition.module_id for item in graph.modules] == [
        "M00001",
        *referenced_ids,
    ]
    assert not graph.issues


def test_module_response_byte_budget_accumulates_across_chunks_in_one_round() -> None:
    referenced_ids = tuple(f"M{index:05d}" for index in range(2, 14))
    modules = {
        "M00001": _module_text("M00001", " ".join(referenced_ids)),
        **{module_id: _module_text(module_id, "K00001") for module_id in referenced_ids},
    }
    client = _FakeClient(modules=modules)

    with pytest.raises(KeggMcpError) as caught:
        load_module_graphs(
            client,
            ("M00001",),
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_total_response_bytes=250),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught)["metric"] == "total_response_bytes"
    assert _safe_details(caught)["observed"] == "300"
    assert [len(request.entries) for request in client.get_requests] == [1, 10, 2]


def test_missing_root_fails_with_entry_not_found() -> None:
    client = _FakeClient()

    with pytest.raises(KeggMcpError) as caught:
        load_module_graphs(client, ("M00001",), options=KeggRequestOptions())

    _assert_error(caught, ErrorCode.KEGG_ENTRY_NOT_FOUND)
    assert _module_ids(client) == [("M00001",)]


@pytest.mark.parametrize(
    "body",
    [
        _module_text("M00001", "K00001", include_name=False),
        _module_text("M00001", "K00001", duplicate_definition=True),
    ],
)
def test_malformed_required_module_fields_fail_closed(body: str) -> None:
    client = _FakeClient(modules={"M00001": body})

    with pytest.raises(KeggMcpError) as caught:
        load_module_graphs(client, ("M00001",), options=KeggRequestOptions())

    _assert_error(caught, ErrorCode.KEGG_PARSE_FAILED)


@pytest.mark.parametrize(
    "module_ids, limits, expected_code",
    [
        (("M00001", "M00001"), ReferenceLoadingLimits(), ErrorCode.ANALYSIS_CONFIGURATION_INVALID),
        (("not-a-module",), ReferenceLoadingLimits(), ErrorCode.ANALYSIS_CONFIGURATION_INVALID),
        (
            ("M00001", "M00002"),
            ReferenceLoadingLimits(max_module_roots=1),
            ErrorCode.INPUT_LIMIT_EXCEEDED,
        ),
    ],
)
def test_invalid_duplicate_and_excess_roots_fail_before_io(
    module_ids: tuple[str, ...],
    limits: ReferenceLoadingLimits,
    expected_code: ErrorCode,
) -> None:
    client = _FakeClient()

    with pytest.raises(KeggMcpError) as caught:
        load_module_graphs(
            client,
            module_ids,
            options=KeggRequestOptions(),
            limits=limits,
        )

    _assert_error(caught, expected_code)
    assert not client.get_requests


def test_round_entry_and_reference_limits_stop_before_excess_io() -> None:
    modules = {
        "M00001": _module_text("M00001", "M00002 M00003"),
        "M00002": _module_text("M00002", "K00002"),
        "M00003": _module_text("M00003", "K00003"),
    }
    cases = (
        ReferenceLoadingLimits(max_module_rounds=1),
        ReferenceLoadingLimits(max_module_entries=1, max_module_roots=1),
        ReferenceLoadingLimits(max_module_reference_occurrences=1),
    )
    for limits in cases:
        client = _FakeClient(modules=modules)
        with pytest.raises(KeggMcpError) as caught:
            load_module_graphs(
                client,
                ("M00001",),
                options=KeggRequestOptions(),
                limits=limits,
            )
        _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
        assert _module_ids(client) == [("M00001",)]


def test_module_response_byte_budget_fails_after_the_first_typed_result() -> None:
    client = _FakeClient(
        modules={"M00001": _module_text("M00001", "M00002")},
    )

    with pytest.raises(KeggMcpError) as caught:
        load_module_graphs(
            client,
            ("M00001",),
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_total_response_bytes=99),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught) == {
        "metric": "total_response_bytes",
        "observed": "100",
        "limit_name": "max_total_response_bytes",
        "limit": "99",
    }
    assert _module_ids(client) == [("M00001",)]


def test_pathway_loading_uses_exact_typed_calls_and_preserves_spec_order() -> None:
    client = _FakeClient(
        pathways={
            "ko00010": (
                "ko:K00002 ko:K00001",
                "Glycolysis / Gluconeogenesis",
                "Metabolism; Carbohydrate metabolism",
            ),
            "map01100": (
                "ko:K00003",
                "Metabolic pathways",
                "Metabolism; Global and overview maps",
            ),
        }
    )
    options = KeggRequestOptions(allow_stale=True)
    specs = (
        PathwaySpec(
            pathway_id="ko00010",
            reference_namespace=PathwayReferenceNamespace.KO,
        ),
        PathwaySpec(
            pathway_id="map01100",
            reference_namespace=PathwayReferenceNamespace.MAP,
        ),
    )

    references = load_pathway_references(client, specs, options=options)

    assert client.call_log == [
        ("link", "ko00010"),
        ("get", "ko00010"),
        ("link", "map01100"),
        ("get", "map01100"),
    ]
    assert [reference.pathway_id for reference in references] == ["ko00010", "map01100"]
    assert references[0].reference_kos == ("K00001", "K00002")
    assert references[1].reference_scope is PathwayReferenceScope.GLOBAL_OR_OVERVIEW
    assert all(
        request.relationship is KeggLinkRelationship.PATHWAY_TO_KO
        and len(request.source_identifiers) == 1
        for request in client.link_requests
    )
    pathway_gets = [
        request
        for request in client.get_requests
        if request.entries[0].database is KeggGetDatabase.PATHWAY
    ]
    assert all(len(request.entries) == 1 for request in pathway_gets)
    assert all(option is options for option in client.options)


def test_organism_pathway_spec_is_explicit_and_empty_denominator_is_retained() -> None:
    client = _FakeClient(
        pathways={
            "hsa00010": (
                "",
                "Human glycolysis",
                "Metabolism; Carbohydrate metabolism",
            )
        }
    )

    reference = load_pathway_references(
        client,
        (
            PathwaySpec(
                pathway_id="hsa00010",
                reference_namespace=PathwayReferenceNamespace.ORGANISM,
            ),
        ),
        options=KeggRequestOptions(),
        pathway_limits=PathwayCoverageLimits(max_reference_kos=10),
    )[0]

    assert reference.reference_namespace is PathwayReferenceNamespace.ORGANISM
    assert reference.kegg_organism_code == "hsa"
    assert reference.reference_kos == ()


def test_pathway_response_byte_budget_is_checked_after_each_typed_result() -> None:
    client = _FakeClient(
        pathways={
            "ko00010": (
                "ko:K00001",
                "Glycolysis / Gluconeogenesis",
                "Metabolism; Carbohydrate metabolism",
            )
        }
    )
    spec = PathwaySpec(
        pathway_id="ko00010",
        reference_namespace=PathwayReferenceNamespace.KO,
    )

    with pytest.raises(KeggMcpError) as caught:
        load_pathway_references(
            client,
            (spec,),
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_total_response_bytes=150),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught)["metric"] == "total_response_bytes"
    assert _safe_details(caught)["observed"] == "200"
    assert client.call_log == [("link", "ko00010"), ("get", "ko00010")]


def test_aggregate_pathway_row_budget_fails_before_the_next_get() -> None:
    client = _FakeClient(
        pathways={
            "ko00010": (
                "ko:K00001 ko:K00002",
                "First pathway",
                "Metabolism; Carbohydrate metabolism",
            ),
            "ko00020": (
                "ko:K00003 ko:K00004",
                "Second pathway",
                "Metabolism; Carbohydrate metabolism",
            ),
        }
    )
    specs = tuple(
        PathwaySpec(
            pathway_id=pathway_id,
            reference_namespace=PathwayReferenceNamespace.KO,
        )
        for pathway_id in ("ko00010", "ko00020")
    )

    with pytest.raises(KeggMcpError) as caught:
        load_pathway_references(
            client,
            specs,
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_total_pathway_relationship_rows=3),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught)["metric"] == "total_pathway_relationship_rows"
    assert _safe_details(caught)["observed"] == "4"
    assert client.call_log == [
        ("link", "ko00010"),
        ("get", "ko00010"),
        ("link", "ko00020"),
    ]


def test_per_pathway_row_budget_also_fails_before_metadata_get() -> None:
    client = _FakeClient(
        pathways={
            "ko00010": (
                "ko:K00001 ko:K00002",
                "Glycolysis / Gluconeogenesis",
                "Metabolism; Carbohydrate metabolism",
            )
        }
    )
    spec = PathwaySpec(
        pathway_id="ko00010",
        reference_namespace=PathwayReferenceNamespace.KO,
    )

    with pytest.raises(KeggMcpError) as caught:
        load_pathway_references(
            client,
            (spec,),
            options=KeggRequestOptions(),
            pathway_limits=PathwayCoverageLimits(max_relationship_rows=1),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught)["metric"] == "pathway_relationship_rows"
    assert client.call_log == [("link", "ko00010")]


def test_aggregate_pathway_ko_budget_preserves_caller_order_and_fails_closed() -> None:
    client = _FakeClient(
        pathways={
            "ko00010": (
                "ko:K00001 ko:K00002",
                "First pathway",
                "Metabolism; Carbohydrate metabolism",
            ),
            "ko00020": (
                "ko:K00003 ko:K00004",
                "Second pathway",
                "Metabolism; Carbohydrate metabolism",
            ),
        }
    )
    specs = tuple(
        PathwaySpec(
            pathway_id=pathway_id,
            reference_namespace=PathwayReferenceNamespace.KO,
        )
        for pathway_id in ("ko00010", "ko00020")
    )

    with pytest.raises(KeggMcpError) as caught:
        load_pathway_references(
            client,
            specs,
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_total_pathway_reference_kos=3),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught)["metric"] == "total_pathway_reference_kos"
    assert _safe_details(caught)["observed"] == "4"
    assert client.call_log == [
        ("link", "ko00010"),
        ("get", "ko00010"),
        ("link", "ko00020"),
        ("get", "ko00020"),
    ]


def test_aggregate_pathway_exclusion_budget_fails_after_bounded_build() -> None:
    client = _FakeClient(
        pathways={
            "ko00010": (
                "cpd:C00001",
                "First pathway",
                "Metabolism; Carbohydrate metabolism",
            ),
            "ko00020": (
                "cpd:C00002",
                "Second pathway",
                "Metabolism; Carbohydrate metabolism",
            ),
        }
    )
    specs = tuple(
        PathwaySpec(
            pathway_id=pathway_id,
            reference_namespace=PathwayReferenceNamespace.KO,
        )
        for pathway_id in ("ko00010", "ko00020")
    )

    with pytest.raises(KeggMcpError) as caught:
        load_pathway_references(
            client,
            specs,
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_total_pathway_reference_exclusions=1),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught)["metric"] == "total_pathway_reference_exclusions"
    assert _safe_details(caught)["observed"] == "2"
    assert client.call_log == [
        ("link", "ko00010"),
        ("get", "ko00010"),
        ("link", "ko00020"),
        ("get", "ko00020"),
    ]


def test_total_request_budget_is_checked_before_issuing_the_next_call() -> None:
    client = _FakeClient(
        pathways={
            "ko00010": (
                "ko:K00001",
                "Glycolysis / Gluconeogenesis",
                "Metabolism; Carbohydrate metabolism",
            )
        }
    )
    spec = PathwaySpec(
        pathway_id="ko00010",
        reference_namespace=PathwayReferenceNamespace.KO,
    )

    with pytest.raises(KeggMcpError) as caught:
        load_pathway_references(
            client,
            (spec,),
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_total_kegg_requests=1),
        )

    _assert_error(caught, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert _safe_details(caught)["metric"] == "total_kegg_requests"
    assert _safe_details(caught)["observed"] == "2"
    assert client.call_log == [("link", "ko00010")]


@pytest.mark.parametrize(
    "pathway_id, namespace",
    [
        ("map00010", PathwayReferenceNamespace.KO),
        ("ko00010", PathwayReferenceNamespace.ORGANISM),
        ("ec00010", PathwayReferenceNamespace.MAP),
    ],
)
def test_pathway_spec_rejects_namespace_mismatch(
    pathway_id: str,
    namespace: PathwayReferenceNamespace,
) -> None:
    with pytest.raises(ValidationError):
        PathwaySpec(pathway_id=pathway_id, reference_namespace=namespace)


@pytest.mark.parametrize("pathway_id", ["ko00010", "map00010"])
def test_pathway_spec_infers_canonical_ko_reference(pathway_id: str) -> None:
    spec = PathwaySpec.model_validate({"pathway_id": pathway_id})

    assert spec.pathway_id == "ko00010"
    assert spec.reference_namespace is PathwayReferenceNamespace.KO
    assert spec.pathway_number == "00010"
    assert spec.namespace == "ko"
    assert spec.paired_reference_id == "map00010"


def test_duplicate_and_excess_pathway_specs_fail_before_io() -> None:
    spec = PathwaySpec(
        pathway_id="ko00010",
        reference_namespace=PathwayReferenceNamespace.KO,
    )
    duplicate_client = _FakeClient()
    with pytest.raises(KeggMcpError) as duplicate:
        load_pathway_references(
            duplicate_client,
            (spec, spec),
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(),
        )
    _assert_error(duplicate, ErrorCode.ANALYSIS_CONFIGURATION_INVALID)
    assert not duplicate_client.get_requests
    assert not duplicate_client.link_requests

    second = PathwaySpec(
        pathway_id="ko00020",
        reference_namespace=PathwayReferenceNamespace.KO,
    )
    excess_client = _FakeClient()
    with pytest.raises(KeggMcpError) as excess:
        load_pathway_references(
            excess_client,
            (spec, second),
            options=KeggRequestOptions(),
            limits=ReferenceLoadingLimits(max_pathway_specs=1),
        )
    _assert_error(excess, ErrorCode.INPUT_LIMIT_EXCEEDED)
    assert not excess_client.get_requests
    assert not excess_client.link_requests


def test_public_loading_contracts_round_trip_and_forbid_extra_fields() -> None:
    limits = ReferenceLoadingLimits(
        max_module_roots=3,
        max_pathway_specs=4,
        max_module_rounds=5,
        max_module_entries=6,
        max_module_reference_occurrences=7,
        max_total_response_bytes=8,
        max_total_kegg_requests=9,
        max_total_pathway_relationship_rows=10,
        max_total_pathway_reference_kos=11,
        max_total_pathway_reference_exclusions=12,
    )
    spec = PathwaySpec(
        pathway_id="ko00010",
        reference_namespace=PathwayReferenceNamespace.KO,
    )

    assert ReferenceLoadingLimits.model_validate_json(limits.model_dump_json()) == limits
    assert PathwaySpec.model_validate_json(spec.model_dump_json()) == spec
    assert ReferenceLoadingLimits.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:reference-loading-limits:1"
    )
    assert PathwaySpec.model_json_schema()["$id"] == (
        "urn:kegg-mcp:schema:pathway-reference-spec:1"
    )
    assert ReferenceLoadingLimits().max_pathway_specs == 25
    with pytest.raises(ValidationError):
        PathwaySpec.model_validate(
            {
                **spec.model_dump(mode="json"),
                "allow_global_or_overview": True,
            }
        )
    with pytest.raises(ValidationError):
        ReferenceLoadingLimits(max_module_roots=True)
