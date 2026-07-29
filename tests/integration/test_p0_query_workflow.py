"""Offline end-to-end coverage for the P0 KEGG query workflow."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

import kegg_mcp.kegg.client as client_module
from kegg_mcp.kegg import (
    CachePolicy,
    KeggClient,
    KeggClientConfig,
    KeggClientLimits,
    RateLimitPolicy,
    RetryPolicy,
)
from kegg_mcp.kegg.transport import TransportResponse
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.server import create_server
from kegg_mcp.services.result_store import SQLiteResultStore


class _NoWaitMandatoryLimiter:
    """Record synthetic requests without sleeping in an offline integration test."""

    def __init__(
        self,
        scope: str,
        requests_per_second: float,
        *,
        state_root: str,
    ) -> None:
        del scope, requests_per_second, state_root
        self.acquire_count = 0

    def acquire(self) -> None:
        self.acquire_count += 1


class _RouteTransport:
    """Return source-shaped KEGG payloads for the exact typed request routes."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def request(
        self,
        url: str,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        assert timeout_seconds > 0
        self.urls.append(url)
        path = urlsplit(url).path
        body = self._body(path)
        assert len(body) <= max_response_bytes
        return TransportResponse(status_code=200, body=body)

    @staticmethod
    def _body(path: str) -> bytes:
        if path == "/find/compound/glucose":
            return b"cpd:C00031\tD-Glucose; Grape sugar\ncpd:C00267\talpha-D-Glucose\n"
        if path == "/link/genome/taxid:562/species":
            return b"taxid:562\tgn:eco\ntaxid:562\tgn:T00068\n"
        if path == "/get/gn:eco":
            return (
                b"ENTRY       T00007            Complete  Genome\n"
                b"NAME        Escherichia coli K-12 MG1655\n"
                b"ORG_CODE    eco\n"
                b"LINEAGE     Bacteria; Proteobacteria; Gammaproteobacteria\n"
                b"///\n"
            )
        if path == "/get/gn:T00068":
            return (
                b"ENTRY       T00068            Complete  Genome\n"
                b"NAME        Escherichia coli O157:H7\n"
                b"ORG_CODE    ece\n"
                b"LINEAGE     Bacteria; Proteobacteria; Gammaproteobacteria\n"
                b"///\n"
            )
        if path == "/get/gn:hsa":
            return (
                b"ENTRY       T01001            Complete  Genome\n"
                b"NAME        Homo sapiens\n"
                b"ORG_CODE    hsa\n"
                b"LINEAGE     Eukaryotes; Animals\n"
                b"///\n"
            )
        if path == "/list/pathway/eco":
            return b"".join(
                (f"eco{index:05d}\tSynthetic E. coli pathway {index}\n").encode()
                for index in range(1, 22)
            )
        if path == "/list/pathway/ece":
            return b"ece00010\tSynthetic O157 pathway\n"
        if path == "/link/taxonomy/gn:ece+gn:eco":
            return b"gn:ece\ttaxid:562\ngn:eco\ttaxid:562\n"
        if path == "/conv/genes/uniprot:P00533":
            return b"up:P00533\thsa:1956\n"
        if path == "/link/pathway/hsa:1956":
            return b"hsa:1956\tpath:hsa04012\n"
        if path == "/link/ko/hsa:1956":
            return b"hsa:1956\tko:K04361\n"
        if path == "/link/reaction/K00844":
            return b"ko:K00844\trn:R01786\n"
        if path == "/link/brite/K00844+K99999":
            return b"ko:K00844\tbr:ko00001\n"
        if path == "/get/br:ko00001":
            return (
                b"+C\tKO hierarchy\n"
                b"A09100 Metabolism\n"
                b"B  09101 Carbohydrate metabolism\n"
                b"C    K00844 HK; hexokinase\n"
            )
        if path.startswith("/link/"):
            return _audit_link_body(path)
        raise AssertionError(f"unexpected synthetic KEGG route: {path}")


def _audit_link_body(path: str) -> bytes:
    suffix = "/K00844+K01810"
    if not path.endswith(suffix):
        raise AssertionError(f"unexpected synthetic KEGG LINK route: {path}")
    target = path.removesuffix(suffix).removeprefix("/link/")
    if target == "pathway":
        return b"ko:K00844\tpath:ko00010\nko:K01810\tpath:ko00020\n"
    targets = {
        "module": b"md:M00001",
        "reaction": b"rn:R01786",
        "enzyme": b"ec:2.7.1.1",
        "brite": b"br:ko00001",
    }
    mapped = targets.get(target)
    if mapped is None:
        raise AssertionError(f"unexpected synthetic KEGG LINK target: {target}")
    return b"ko:K00844\t" + mapped + b"\n"


def _tool_data(result: types.CallToolResult) -> dict[str, Any]:
    assert result.isError is False
    structured = result.structuredContent
    assert isinstance(structured, dict)
    structured_mapping = cast(dict[str, object], structured)
    assert structured_mapping["ok"] is True
    payload = structured_mapping["result"]
    assert isinstance(payload, dict)
    payload_mapping = cast(dict[str, object], payload)
    data = payload_mapping["data"]
    assert isinstance(data, dict)
    resource_uri = payload_mapping["resource_uri"]
    assert isinstance(resource_uri, str)
    assert resource_uri.startswith("ko-analysis://results/")
    return cast(dict[str, Any], data)


async def _read_all_artifacts(
    session: ClientSession,
    data: dict[str, Any],
) -> dict[str, str]:
    result = cast(object, data["result"])
    assert isinstance(result, dict)
    result_mapping = cast(dict[str, object], result)
    result_id = result_mapping["result_id"]
    assert isinstance(result_id, str)

    root = await session.read_resource(AnyUrl(f"ko-analysis://results/{result_id}"))
    root_content = root.contents[0]
    assert isinstance(root_content, types.TextResourceContents)
    root_metadata_value = cast(object, json.loads(root_content.text))
    assert isinstance(root_metadata_value, dict)
    root_metadata = cast(dict[str, object], root_metadata_value)

    raw_artifacts_value = cast(object, data.get("artifacts"))
    if raw_artifacts_value is None:
        raw_artifacts_value = [cast(object, data["artifact"])]
    assert isinstance(raw_artifacts_value, list)
    raw_artifacts = cast(list[object], raw_artifacts_value)
    root_result = root_metadata["result"]
    assert isinstance(root_result, dict)
    root_result_mapping = cast(dict[str, object], root_result)
    assert root_result_mapping["artifact_count"] == len(raw_artifacts)
    section_uris = root_metadata["section_uris"]
    assert isinstance(section_uris, list)

    contents: dict[str, str] = {}
    for raw_artifact in raw_artifacts:
        assert isinstance(raw_artifact, dict)
        raw_artifact_mapping = cast(dict[str, object], raw_artifact)
        section = raw_artifact_mapping["section"]
        assert isinstance(section, str)
        uri = f"ko-analysis://results/{result_id}/{section}"
        assert uri in section_uris
        resource = await session.read_resource(AnyUrl(uri))
        content = resource.contents[0]
        assert isinstance(content, types.TextResourceContents)
        assert content.text
        contents[section] = content.text
    return contents


@pytest.mark.asyncio
async def test_p0_query_workflow_through_mcp_and_real_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        client_module,
        "DeploymentRateLimiter",
        _NoWaitMandatoryLimiter,
    )
    transport = _RouteTransport()
    client = KeggClient(
        KeggClientConfig(
            limits=KeggClientLimits(requests_per_second=3.0),
            retry=RetryPolicy(max_retries=0),
            cache=CachePolicy(path=str(tmp_path / "kegg-cache.sqlite3")),
            rate_limit=RateLimitPolicy(state_root=str(tmp_path / "rate-limit")),
        ),
        transport=transport,
    )
    runtime = McpRuntime(
        client=client,
        result_store=SQLiteResultStore(tmp_path / "results.sqlite3"),
        scope_id="p0-e2e-scope",
    )

    async with create_connected_server_and_client_session(create_server(runtime)) as session:
        normalized = _tool_data(
            await session.call_tool(
                "normalize_ko_annotations",
                {
                    "text": (
                        "sequence,ko,decision\n"
                        "p1,K00844,accepted\n"
                        "p2,K01810,uncertain\n"
                        "p3,K00001,rejected\n"
                    ),
                    "input_format": "generic_csv",
                    "column_mapping": {
                        "sequence_id": "sequence",
                        "ko_id": "ko",
                        "raw_decision": "decision",
                    },
                    "decision_policy": "canonical_source_status",
                    "source": {"source_name": "synthetic-annotator"},
                },
            )
        )
        normalized_id = normalized["result"]["result_id"]
        normalized_artifacts = await _read_all_artifacts(session, normalized)
        assert "dataset" in normalized_artifacts

        searched = _tool_data(
            await session.call_tool(
                "search_kegg_entries",
                {
                    "database": "compound",
                    "query": "glucose",
                    "mode": "keyword",
                    "max_results": 2,
                },
            )
        )
        assert searched["returned_count"] == 2
        assert [item["entity"]["identifier"] for item in searched["candidates"]] == [
            "C00031",
            "C00267",
        ]
        assert all("score" not in item for item in searched["candidates"])
        assert any(
            "not relevance-ranked" in caveat for caveat in searched["interpretation_caveats"]
        )
        search_artifacts = await _read_all_artifacts(session, searched)
        assert json.loads(search_artifacts["detail"])["request"]["query"] == "glucose"

        resolved = _tool_data(
            await session.call_tool(
                "resolve_kegg_entities",
                {
                    "kind": "organism",
                    "source_namespace": "taxonomy",
                    "identifiers": ["562"],
                    "taxonomy_rank": "species",
                    "ambiguity_policy": "report_all",
                },
            )
        )
        assert resolved["kind"] == "organism"
        assert resolved["ambiguous_input_count"] == 1
        assert resolved["resolutions"][0]["status"] == "one_to_many"
        assert {
            candidate["canonical_entity"]["identifier"]
            for candidate in resolved["resolutions"][0]["candidates"]
        } == {"eco", "ece"}
        organism_candidates = {
            candidate["canonical_entity"]["identifier"]: candidate
            for candidate in resolved["resolutions"][0]["candidates"]
        }
        eco_pathways = organism_candidates["eco"]["organism_pathways"]
        assert eco_pathways["total_count"] == 21
        assert len(eco_pathways["preview"]) == 20
        assert eco_pathways["preview"][0]["pathway"] == {
            "kind": "pathway",
            "identifier": "eco00001",
        }
        assert eco_pathways["truncated"] is True
        assert organism_candidates["ece"]["organism_pathways"]["total_count"] == 1
        assert "list" in resolved["resolutions"][0]["operations_used"]
        assert any(
            "without automatic selection" in caveat for caveat in resolved["interpretation_caveats"]
        )
        assert any(
            "do not establish pathway presence" in caveat
            for caveat in resolved["interpretation_caveats"]
        )
        resolution_artifacts = await _read_all_artifacts(session, resolved)
        resolution_detail = json.loads(resolution_artifacts["detail"])
        assert resolution_detail["request"]["kind"] == "organism"
        assert resolution_detail["request"]["taxonomy_rank"] == "species"
        list_steps = [step for step in resolution_detail["steps"] if step["operation"] == "list"]
        assert len(list_steps) == 2
        eco_list_step = next(step for step in list_steps if step["organism"] == "eco")
        assert len(eco_list_step["result"]["document"]["rows"]) == 21

        gene_resolution = _tool_data(
            await session.call_tool(
                "resolve_kegg_entities",
                {
                    "kind": "gene",
                    "source_namespace": "uniprot",
                    "identifiers": ["P00533"],
                    "organism": "hsa",
                    "targets": ["gene", "ko", "pathway"],
                    "ambiguity_policy": "report_all",
                },
            )
        )
        assert gene_resolution["kind"] == "gene"
        assert gene_resolution["resolutions"][0]["status"] == "one_to_one"
        gene_candidate = gene_resolution["resolutions"][0]["candidates"][0]
        assert gene_candidate["canonical_entity"] == {
            "kind": "gene",
            "identifier": "hsa:1956",
        }
        assert {
            (entity["kind"], entity["identifier"]) for entity in gene_candidate["entities"]
        } == {
            ("gene", "hsa:1956"),
            ("ko", "K04361"),
            ("pathway", "hsa04012"),
        }
        assert gene_resolution["resolutions"][0]["operations_used"] == [
            "conv",
            "get",
            "link",
        ]
        gene_artifacts = await _read_all_artifacts(session, gene_resolution)
        gene_detail = json.loads(gene_artifacts["detail"])
        assert gene_detail["request"]["kind"] == "gene"
        assert gene_detail["request"]["source_namespace"] == "uniprot"

        traced = _tool_data(
            await session.call_tool(
                "trace_kegg_relations",
                {
                    "seeds": [{"kind": "ko", "identifier": "K00844"}],
                    "edge_types": ["ko_to_reaction"],
                    "max_depth": 1,
                    "max_nodes": 10,
                    "max_edges": 10,
                },
            )
        )
        assert traced["edge_count"] == 1
        assert traced["edges"][0]["target"] == {
            "kind": "reaction",
            "identifier": "R01786",
        }
        assert traced["edges"][0]["provenance_batch_indexes"] == [0]
        assert len(traced["provenance"]) == 1
        assert any(
            "not evidence of regulation" in caveat for caveat in traced["interpretation_caveats"]
        )
        assert any(
            "not evidence of biological absence" in caveat
            for caveat in traced["interpretation_caveats"]
        )
        trace_artifacts = await _read_all_artifacts(session, traced)
        retained_trace = json.loads(trace_artifacts["detail"])
        assert retained_trace["edges"][0]["depth"] == 1
        assert retained_trace["edges"][0]["provenance_batch_indexes"] == [0]

        brite = _tool_data(
            await session.call_tool(
                "map_brite_hierarchy",
                {
                    "entity_ids": [
                        {"kind": "ko", "identifier": "K00844"},
                        {"kind": "ko", "identifier": "K99999"},
                    ],
                    "include_all_paths": True,
                    "include_unmatched": True,
                },
            )
        )
        assert brite["path_count"] == 1
        assert brite["path_preview"][0]["nodes"][-1]["node_id"] == "K00844"
        assert brite["classification_count"] == 3
        assert brite["unmatched_entities"] == [{"kind": "ko", "identifier": "K99999"}]
        assert "without statistical testing" in brite["count_semantics"]
        assert "enrichment" not in brite["count_semantics"].lower()
        brite_artifacts = await _read_all_artifacts(session, brite)
        brite_detail = json.loads(brite_artifacts["brite_hierarchy.json"])
        assert brite_detail["paths"][0]["input_entity"]["identifier"] == "K00844"
        assert brite_artifacts["brite_hierarchy.tsv"].startswith("record_type\t")

        audited = _tool_data(
            await session.call_tool(
                "audit_annotation_mapping",
                {
                    "source": {"result_id": normalized_id},
                    "quality_context": {
                        "assembly_completeness": 82.5,
                        "assembly_contamination": 1.2,
                        "genome_type": "MAG",
                        "gene_caller": "synthetic-caller",
                        "annotation_tool": "synthetic-annotator",
                        "annotation_database_version": "2026-07",
                    },
                },
            )
        )
        detail = audited["detail"]
        assert detail["evidence"]["strict_unique_ko_count"] == 1
        assert detail["evidence"]["lenient_unique_ko_count"] == 2
        assert detail["lenient_only_ko_preview"] == ["K01810"]
        assert detail["mappings"][0]["strict"]["mapped_unique_ko_count"] == 1
        assert detail["mappings"][0]["lenient"]["mapped_unique_ko_count"] == 2
        warning_codes = {warning["code"] for warning in detail["warnings"]}
        assert "incomplete_assembly_context" in warning_codes
        assert "contamination_context" in warning_codes
        audit_artifacts = await _read_all_artifacts(session, audited)
        audit_detail = json.loads(audit_artifacts["detail"])
        assert audit_detail["strict_ko_ids"] == ["K00844"]
        assert audit_detail["lenient_only_ko_ids"] == ["K01810"]

    assert "https://rest.kegg.jp/link/genome/taxid:562/species" in transport.urls
    assert "https://rest.kegg.jp/list/pathway/eco" in transport.urls
    assert "https://rest.kegg.jp/list/pathway/ece" in transport.urls
    assert transport.urls.count("https://rest.kegg.jp/find/compound/glucose") == 1
    assert runtime.result_store.list_results(runtime.scope_id).total_items == 7
