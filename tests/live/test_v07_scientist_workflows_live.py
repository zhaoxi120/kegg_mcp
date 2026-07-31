"""Opt-in live MCP workflows modeled on bounded scientist query tasks."""

from __future__ import annotations

import base64
import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast

import pytest
from mcp import ClientSession, types
from mcp.shared.memory import create_connected_server_and_client_session
from pydantic import AnyUrl

from kegg_mcp.kegg import KeggClient
from kegg_mcp.mcp.runtime import McpRuntime
from kegg_mcp.mcp.server import create_server
from kegg_mcp.services.result_store import SQLiteResultStore


class _LiveCampaign(Protocol):
    client: KeggClient
    transport: Any
    root: Path
    cache_path: Path
    result_store_path: Path
    rate_limit_root: Path
    max_requests: int


pytestmark = [
    pytest.mark.live_kegg,
    pytest.mark.skipif(
        os.environ.get("KEGG_MCP_RUN_LIVE_TESTS", "").lower() != "true",
        reason="set KEGG_MCP_RUN_LIVE_TESTS=true to run live KEGG tests",
    ),
]


def _tool_data(result: types.CallToolResult, *, private_root: Path) -> tuple[dict[str, Any], str]:
    assert result.isError is False
    structured = result.structuredContent
    assert isinstance(structured, dict)
    assert str(private_root) not in json.dumps(structured, ensure_ascii=False)
    assert structured["ok"] is True
    payload = cast(dict[str, object], structured["result"])
    data = cast(dict[str, Any], payload["data"])
    resource_uri = payload["resource_uri"]
    assert isinstance(resource_uri, str)
    assert resource_uri == f"ko-analysis://results/{data['result']['result_id']}"
    return data, resource_uri


def _local_tool_data(result: types.CallToolResult, *, private_root: Path) -> dict[str, Any]:
    assert result.isError is False
    structured = result.structuredContent
    assert isinstance(structured, dict)
    assert str(private_root) not in json.dumps(structured, ensure_ascii=False)
    assert structured["ok"] is True
    payload = cast(dict[str, object], structured["result"])
    assert payload["resource_uri"] is None
    return cast(dict[str, Any], payload["data"])


async def _text_resource(session: ClientSession, uri: str) -> str:
    response = await session.read_resource(AnyUrl(uri))
    assert len(response.contents) == 1
    content = response.contents[0]
    assert isinstance(content, types.TextResourceContents)
    return content.text


async def _read_retained_artifacts(
    session: ClientSession,
    resource_uri: str,
    *,
    private_root: Path,
) -> tuple[dict[str, bytes], bool]:
    index_text = await _text_resource(session, resource_uri)
    assert str(private_root) not in index_text
    index = cast(dict[str, Any], json.loads(index_text))
    artifacts = cast(list[dict[str, Any]], index["artifacts"])
    section_uris = cast(list[str], index["section_uris"])
    assert len(artifacts) == index["result"]["artifact_count"]
    assert len(section_uris) == len(artifacts)
    result_id = cast(str, index["result"]["result_id"])
    expected_sections = {cast(str, artifact["section"]) for artifact in artifacts}
    assert len(expected_sections) == len(artifacts)

    reconstructed: dict[str, bytes] = {}
    used_pagination = False
    for artifact, section_uri in zip(artifacts, section_uris, strict=True):
        section = cast(str, artifact["section"])
        mime_type = cast(str, artifact["mime_type"])
        total_bytes = cast(int, artifact["byte_size"])
        assert section_uri == f"ko-analysis://results/{result_id}/{section}"
        direct_text = await _text_resource(session, section_uri)
        assert str(private_root) not in direct_text
        try:
            direct_value = cast(object, json.loads(direct_text))
        except json.JSONDecodeError:
            direct_value = None
        direct_mapping = (
            cast(dict[str, object], direct_value) if isinstance(direct_value, dict) else None
        )
        if (
            direct_mapping is not None
            and direct_mapping.get("kind") == "artifact_requires_pagination"
        ):
            used_pagination = True
            assert direct_mapping["result_id"] == result_id
            assert direct_mapping["section"] == section
            assert direct_mapping["mime_type"] == mime_type
            assert direct_mapping["total_bytes"] == total_bytes
            maximum_range_bytes = cast(int, direct_mapping["maximum_range_bytes"])
            assert maximum_range_bytes > 0
            content = bytearray()
            next_uri = cast(str | None, direct_mapping["next_uri"])
            assert isinstance(next_uri, str)
            expected_offset = 0
            visited_uris: set[str] = set()
            while next_uri is not None:
                assert next_uri not in visited_uris
                visited_uris.add(next_uri)
                page_text = await _text_resource(session, next_uri)
                assert str(private_root) not in page_text
                page = cast(dict[str, Any], json.loads(page_text))
                assert page["result_id"] == result_id
                assert page["section"] == section
                assert page["mime_type"] == mime_type
                assert page["total_bytes"] == total_bytes
                assert page["offset"] == expected_offset
                chunk = base64.b64decode(page["content_base64"], validate=True)
                returned_bytes = cast(int, page["returned_bytes"])
                assert returned_bytes == len(chunk)
                assert 0 < returned_bytes <= maximum_range_bytes
                assert expected_offset + returned_bytes <= total_bytes
                content.extend(chunk)
                expected_offset += returned_bytes
                next_uri = cast(str | None, page["next_uri"])
                if next_uri is None:
                    assert expected_offset == total_bytes
                else:
                    assert expected_offset < total_bytes
            raw = bytes(content)
        else:
            raw = direct_text.encode("utf-8")
        assert len(raw) == total_bytes
        assert str(private_root).encode() not in raw
        reconstructed[section] = raw
    assert set(reconstructed) == expected_sections
    return reconstructed, used_pagination


async def _call_retained(
    session: ClientSession,
    name: str,
    arguments: Mapping[str, object],
    *,
    private_root: Path,
) -> tuple[dict[str, Any], dict[str, bytes], bool]:
    data, resource_uri = _tool_data(
        await session.call_tool(name, dict(arguments)),
        private_root=private_root,
    )
    artifacts, used_pagination = await _read_retained_artifacts(
        session,
        resource_uri,
        private_root=private_root,
    )
    return data, artifacts, used_pagination


def _assert_private_state_tree(campaign: _LiveCampaign) -> None:
    assert campaign.cache_path.is_file()
    assert campaign.result_store_path.is_file()
    assert campaign.rate_limit_root.is_dir()
    assert any(campaign.rate_limit_root.iterdir())
    for path in (campaign.root, *campaign.root.rglob("*")):
        metadata = path.lstat()
        assert not stat.S_ISLNK(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) & 0o077 == 0


@pytest.mark.asyncio
async def test_v07_scientist_workflows_through_live_mcp(
    live_campaign: _LiveCampaign,
    live_requests_per_operation: int,
) -> None:
    if live_requests_per_operation < 20:
        pytest.skip("the complete scientist workflow runs only with the default live budget")

    store = SQLiteResultStore(live_campaign.result_store_path)
    runtime = McpRuntime(
        client=live_campaign.client,
        result_store=store,
        scope_id="live-v07-scientist-workflows",
    )
    server = create_server(runtime)
    paginated_artifact_observed = False

    try:
        async with create_connected_server_and_client_session(server) as session:
            status = _local_tool_data(
                await session.call_tool("get_server_status", {}),
                private_root=live_campaign.root,
            )
            assert status["access_mode"] in {"public_academic", "licensed"}
            assert status["network_enabled"] is True
            assert status["result_scope"] == "stdio_session"

            probe = _local_tool_data(
                await session.call_tool("probe_kegg_connectivity", {}),
                private_root=live_campaign.root,
            )
            assert probe["state"] == "reachable"

            card_entries = [
                {"database": "ko", "identifier": "K00844"},
                {"database": "module", "identifier": "M00001"},
                {"database": "pathway", "identifier": "map00010"},
                {"database": "reaction", "identifier": "R01786"},
                {"database": "enzyme", "identifier": "2.7.1.1"},
                {"database": "compound", "identifier": "C00031"},
                {"database": "glycan", "identifier": "G00001"},
                {"database": "gene", "identifier": "hsa:7157"},
                {"database": "genome", "identifier": "T01001"},
            ]
            first_cards, first_card_artifacts, _ = await _call_retained(
                session,
                "get_kegg_entries",
                {"entries": card_entries, "projection": "card"},
                private_root=live_campaign.root,
            )
            assert first_cards["requested_count"] == len(card_entries)
            assert first_cards["returned_count"] == len(card_entries)
            assert first_cards["projection"] == "card"
            assert set(first_card_artifacts) == {"detail", "entry_snapshot"}
            first_snapshot = cast(
                dict[str, Any],
                json.loads(first_card_artifacts["entry_snapshot"]),
            )
            assert len(first_snapshot["entries"]) == len(card_entries)

            requests_before_second_cards = live_campaign.transport.request_count
            second_cards, second_card_artifacts, _ = await _call_retained(
                session,
                "get_kegg_entries",
                {"entries": card_entries, "projection": "card"},
                private_root=live_campaign.root,
            )
            assert live_campaign.transport.request_count == requests_before_second_cards, (
                second_cards["provenance"]
            )
            assert any(item["origin"] == "cache" for item in second_cards["provenance"])
            assert set(second_card_artifacts) == {"detail", "entry_snapshot"}

            flat_preview, flat_preview_artifacts, _ = await _call_retained(
                session,
                "get_kegg_entries",
                {
                    "entries": [
                        {"database": "drug", "identifier": "D00109"},
                        {"database": "rclass", "identifier": "RC00002"},
                    ],
                    "projection": "preview",
                },
                private_root=live_campaign.root,
            )
            assert flat_preview["requested_count"] == 2
            assert flat_preview["returned_count"] == 2
            assert flat_preview["projection"] == "preview"
            assert set(flat_preview_artifacts) == {"detail"}

            requests_before_comparison = live_campaign.transport.request_count
            comparison, comparison_artifacts, _ = await _call_retained(
                session,
                "compare_kegg_reference_snapshots",
                {
                    "left": {"result_id": first_cards["result"]["result_id"]},
                    "right": {"result_id": second_cards["result"]["result_id"]},
                },
                private_root=live_campaign.root,
            )
            assert live_campaign.transport.request_count == requests_before_comparison
            assert comparison["parser_compatible"] is True
            assert comparison["endpoint_context_compatible"] is True
            assert comparison["shared_entry_count"] == len(card_entries)
            assert comparison["added_entry_count"] == 0
            assert comparison["removed_entry_count"] == 0
            assert comparison["field_change_count"] == 0
            assert set(comparison_artifacts) == {"reference_diff"}

            search_cases = (
                ("glycan", "mannose", "keyword"),
                ("drug", "aspirin", "keyword"),
                ("rclass", "RC00002", "keyword"),
                ("drug", "C9H8O4", "formula"),
                ("drug", "180.063", "exact_mass"),
                ("drug", "180-181", "molecular_weight"),
            )
            for database, query, mode in search_cases:
                searched, search_artifacts, _ = await _call_retained(
                    session,
                    "search_kegg_entries",
                    {
                        "database": database,
                        "query": query,
                        "mode": mode,
                        "max_results": 20,
                    },
                    private_root=live_campaign.root,
                )
                assert searched["database"] == database
                assert searched["mode"] == mode
                if database == "rclass":
                    # Observed 2026-07-31: the public FIND endpoint returns an empty
                    # but well-formed result for a known RCLASS identifier.
                    assert searched["candidate_count"] == 0
                    assert searched["observed_count"] == 0
                else:
                    assert searched["candidate_count"] > 0
                assert all("score" not in item for item in searched["candidate_preview"])
                assert set(search_artifacts) == {"detail"}
                if mode == "exact_mass":
                    assert any(
                        caveat.startswith("Exact-mass matches are candidates")
                        and "identifications" in caveat
                        for caveat in searched["interpretation_caveats"]
                    )

            substance_cases = (
                {
                    "kind": "substance",
                    "source_namespace": "kegg_compound",
                    "identifiers": ["C00031"],
                    "targets": ["kegg_compound", "reaction", "pathway"],
                    "ambiguity_policy": "report_all",
                },
                {
                    "kind": "substance",
                    "source_namespace": "chebi",
                    "identifiers": ["CHEBI:4167"],
                    "targets": ["kegg_compound", "reaction", "pathway"],
                    "ambiguity_policy": "report_all",
                },
                {
                    "kind": "substance",
                    "source_namespace": "pubchem_sid",
                    "identifiers": ["3333"],
                    "targets": ["kegg_compound", "reaction", "pathway"],
                    "ambiguity_policy": "report_all",
                },
                {
                    "kind": "substance",
                    "source_namespace": "pubchem_sid",
                    "identifiers": ["124490636"],
                    "targets": ["kegg_glycan", "reaction", "pathway"],
                    "ambiguity_policy": "report_all",
                },
                {
                    "kind": "substance",
                    "source_namespace": "pubchem_sid",
                    "identifiers": ["7847177"],
                    "targets": ["kegg_drug", "pathway"],
                    "ambiguity_policy": "report_all",
                },
                {
                    "kind": "substance",
                    "source_namespace": "kegg_glycan",
                    "identifiers": ["G00001"],
                    "targets": ["kegg_glycan", "reaction", "pathway"],
                    "ambiguity_policy": "report_all",
                },
                {
                    "kind": "substance",
                    "source_namespace": "kegg_drug",
                    "identifiers": ["D00109"],
                    "targets": ["kegg_drug", "pathway"],
                    "ambiguity_policy": "report_all",
                },
            )
            for request in substance_cases:
                resolved, resolution_artifacts, _ = await _call_retained(
                    session,
                    "resolve_kegg_entities",
                    request,
                    private_root=live_campaign.root,
                )
                assert resolved["kind"] == "substance"
                assert resolved["mapped_input_count"] == 1, (request, resolved)
                assert resolved["mapping_yield"] == 1.0, (request, resolved)
                assert set(resolution_artifacts) == {"detail"}

            taxonomy_cases = (
                ("exact", "228908"),
                ("species", "160232"),
                ("genus", "193568"),
                ("family", "1890941"),
                ("order", "1890940"),
                ("class", "2885752"),
                ("phylum", "192989"),
            )
            for rank, taxonomy_id in taxonomy_cases:
                resolved, resolution_artifacts, _ = await _call_retained(
                    session,
                    "resolve_kegg_entities",
                    {
                        "kind": "organism",
                        "source_namespace": "taxonomy",
                        "identifiers": [taxonomy_id],
                        "taxonomy_rank": rank,
                        "candidate_materialization": "auto",
                        "ambiguity_policy": "report_all",
                    },
                    private_root=live_campaign.root,
                )
                assert resolved["kind"] == "organism"
                assert resolved["mapped_input_count"] == 1, (rank, resolved)
                preview = resolved["resolution_previews"][0]
                assert preview["candidate_count"] >= 1
                expected_materialization = (
                    "full" if rank in {"exact", "species"} else "identity_only"
                )
                assert all(
                    candidate["organism_materialization"] == expected_materialization
                    for candidate in preview["candidate_preview"]
                )
                assert set(resolution_artifacts) == {"detail"}

            organism, organism_artifacts, _ = await _call_retained(
                session,
                "resolve_kegg_entities",
                {
                    "kind": "organism",
                    "source_namespace": "code",
                    "identifiers": ["eco"],
                    "include_pathway_directory": True,
                    "ambiguity_policy": "report_all",
                },
                private_root=live_campaign.root,
            )
            organism_candidate = organism["resolution_previews"][0]["candidate_preview"][0]
            assert organism_candidate["organism_pathway_count"] > 0
            assert set(organism_artifacts) == {"detail"}

            relation_cases = (
                (
                    {
                        "seeds": [{"kind": "ko", "identifier": "K01810"}],
                        "edge_types": ["ko_to_gene"],
                        "organism_scope": "eco",
                    },
                    {"ko_to_gene"},
                ),
                (
                    {
                        "seeds": [{"kind": "pathway", "identifier": "hsa00010"}],
                        "edge_types": ["pathway_to_gene"],
                        "organism_scope": "hsa",
                    },
                    {"pathway_to_gene"},
                ),
                (
                    {
                        "seeds": [{"kind": "module", "identifier": "M00001"}],
                        "edge_types": [
                            "module_to_ko",
                            "module_to_pathway",
                            "module_to_reaction",
                        ],
                    },
                    {"module_to_ko", "module_to_pathway", "module_to_reaction"},
                ),
                (
                    {
                        "seeds": [
                            {"kind": "pathway", "identifier": "map01200"},
                            {"kind": "pathway", "identifier": "map00510"},
                        ],
                        "edge_types": ["pathway_to_module", "pathway_to_glycan"],
                    },
                    {"pathway_to_module", "pathway_to_glycan"},
                ),
                (
                    {
                        "seeds": [{"kind": "reaction", "identifier": "R05969"}],
                        "edge_types": ["reaction_to_glycan"],
                    },
                    {"reaction_to_glycan"},
                ),
                (
                    {
                        "seeds": [{"kind": "glycan", "identifier": "G00001"}],
                        "edge_types": ["glycan_to_reaction", "glycan_to_pathway"],
                    },
                    {"glycan_to_reaction", "glycan_to_pathway"},
                ),
                (
                    {
                        "seeds": [{"kind": "drug", "identifier": "D00109"}],
                        "edge_types": ["drug_to_pathway"],
                    },
                    {"drug_to_pathway"},
                ),
            )
            for request, expected_relationships in relation_cases:
                traced, trace_artifacts, _ = await _call_retained(
                    session,
                    "trace_kegg_relations",
                    request,
                    private_root=live_campaign.root,
                )
                assert traced["edge_count"] > 0
                retained_trace = cast(dict[str, Any], json.loads(trace_artifacts["detail"]))
                observed_relationships = {edge["relationship"] for edge in retained_trace["edges"]}
                assert expected_relationships <= observed_relationships
                assert all(edge["provenance_batch_indexes"] for edge in retained_trace["edges"])

            depth_two, depth_two_artifacts, _ = await _call_retained(
                session,
                "trace_kegg_relations",
                {
                    "seeds": [{"kind": "glycan", "identifier": "G00001"}],
                    "edge_types": ["glycan_to_reaction", "reaction_to_pathway"],
                    "max_depth": 2,
                },
                private_root=live_campaign.root,
            )
            assert depth_two["edge_count"] > 0
            retained_depth_two = cast(
                dict[str, Any],
                json.loads(depth_two_artifacts["detail"]),
            )
            assert any(edge["depth"] == 2 for edge in retained_depth_two["edges"])

            brite, brite_artifacts, _ = await _call_retained(
                session,
                "map_brite_hierarchy",
                {
                    "entity_ids": [
                        {"kind": "ko", "identifier": "K00844"},
                        {"kind": "ko", "identifier": "K99999"},
                    ],
                    "include_all_paths": True,
                    "include_unmatched": True,
                },
                private_root=live_campaign.root,
            )
            assert brite["path_count"] > 0
            assert {"brite_hierarchy.json", "brite_hierarchy.tsv"} == set(brite_artifacts)
            retained_brite = cast(
                dict[str, Any],
                json.loads(brite_artifacts["brite_hierarchy.json"]),
            )
            assert retained_brite["paths"]
            assert brite_artifacts["brite_hierarchy.tsv"].startswith(b"record_type\t")

            audited, audit_artifacts, _ = await _call_retained(
                session,
                "audit_annotation_mapping",
                {
                    "source": {"ko_text": "K00844\nK01810"},
                    "mapping_targets": ["pathway"],
                },
                private_root=live_campaign.root,
            )
            assert audited["mapping_execution"]["status"] == "completed"
            assert audited["mapping_execution"]["completed_targets"] == ["pathway"]
            assert set(audit_artifacts) == {"detail"}

            requests_before_local_audits = live_campaign.transport.request_count
            evidence_only, evidence_artifacts, _ = await _call_retained(
                session,
                "audit_annotation_mapping",
                {
                    "source": {"ko_text": "K00844\nK01810"},
                    "mapping_targets": [],
                },
                private_root=live_campaign.root,
            )
            assert evidence_only["mapping_execution"]["status"] == "not_requested"
            assert set(evidence_artifacts) == {"detail"}
            assert live_campaign.transport.request_count == requests_before_local_audits

            large_ko_text = "\n".join(f"K{index:05d}" for index in range(1, 8_002))
            skipped, skipped_artifacts, skipped_used_pagination = await _call_retained(
                session,
                "audit_annotation_mapping",
                {"source": {"ko_text": large_ko_text}},
                private_root=live_campaign.root,
            )
            assert skipped["mapping_execution"]["status"] == "skipped_request_limit"
            assert skipped["mapping_execution"]["planned_request_count"] > 100
            assert set(skipped_artifacts) == {"detail"}
            assert live_campaign.transport.request_count == requests_before_local_audits
            retained_skipped = cast(
                dict[str, Any],
                json.loads(skipped_artifacts["detail"]),
            )
            skipped_detail = cast(dict[str, Any], retained_skipped["detail"])
            assert skipped_detail["evidence"]["input_rows"] == 8_001
            assert skipped_detail["evidence"]["emitted_records"] == 8_001
            assert skipped_detail["mapping_execution"]["status"] == "skipped_request_limit"
            assert skipped_detail["mapping_execution"]["completed_targets"] == []
            paginated_artifact_observed |= skipped_used_pagination

            assert paginated_artifact_observed
            assert live_campaign.transport.request_count <= live_campaign.max_requests
            assert store.list_results(runtime.scope_id).total_items > 0
            _assert_private_state_tree(live_campaign)
    finally:
        store.delete_scope(runtime.scope_id)

    assert store.list_results(runtime.scope_id).total_items == 0
    _assert_private_state_tree(live_campaign)
