"""Redistributable synthetic contracts and assets for renderer tests."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kegg_mcp.analysis import (
    ModuleDefinition,
    ModuleDefinitionCollection,
    PathwayCoverageParameters,
    PathwayKoReference,
    PathwayReferenceNamespace,
    PathwayReferenceScope,
    evaluate_module_pair,
    evaluate_pathway_coverage,
    resolve_module_definitions,
)
from kegg_mcp.domain import CANONICAL_SOURCE_STATUS, EvidenceMode
from kegg_mcp.execution import (
    AnalysisExecutionProvenance,
    AnalysisServiceLimits,
    PathwayExecutionParameters,
    ReferenceLoadingLimits,
)
from kegg_mcp.importers import (
    GenericColumnMapping,
    ImportLimits,
    SourceProvenanceInput,
    TableDialect,
    import_generic_table,
)
from kegg_mcp.kegg import (
    AccessMode,
    CacheLookupState,
    KeggBatchProvenance,
    KeggRequestOptions,
    ResponseOrigin,
    RetrievalEndpointClass,
)
from kegg_mcp.kegg.contracts import KeggOperation
from kegg_mcp.services.render_contracts import (
    RenderInput,
    build_render_input,
    serialize_render_input,
)
from PIL import Image, ImageDraw

from kegg_render_mcp.config import RendererLimits, RendererRuntimeConfig
from kegg_render_mcp.pathway_scene import RetrievedAsset

NOW = datetime(2026, 7, 16, 8, 0, tzinfo=UTC)


def synthetic_png(width: int = 240, height: int = 140) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 220, 120), outline="#444444")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def synthetic_kgml(pathway_id: str = "ko00010") -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<pathway name="path:{pathway_id}" number="00010" title="Synthetic pathway">
  <entry id="1" name="ko:K00001 ko:K00002" type="gene">
    <graphics name="K00001..." type="rectangle" x="60" y="50" width="60" height="24"/>
  </entry>
  <entry id="2" name="ko:K00002" type="gene">
    <graphics name="K00002" type="rectangle" x="160" y="90" width="60" height="24"/>
  </entry>
</pathway>
""".encode()


class SyntheticProvider:
    def __init__(self, *, pathway_id: str = "ko00010") -> None:
        self.pathway_id = pathway_id
        self.calls: list[tuple[str, str]] = []
        self.image = synthetic_png()
        self.kgml = synthetic_kgml(pathway_id)
        self.reachable = True

    @property
    def configured(self) -> bool:
        return True

    async def get_asset(self, pathway_id: str, kind: str) -> RetrievedAsset:
        self.calls.append((pathway_id, kind))
        if kind == "image":
            return RetrievedAsset(
                pathway_id=self.pathway_id,
                kind=kind,
                content=self.image,
                mime_type="image/png",
                width=240,
                height=140,
                provenance={
                    "request_key": f"synthetic:{pathway_id}:image",
                    "retrieved_at": NOW.isoformat(),
                    "origin": "cache",
                    "is_stale": False,
                    "parser_version": "1",
                },
            )
        return RetrievedAsset(
            pathway_id=self.pathway_id,
            kind=kind,
            content=self.kgml,
            mime_type="application/xml",
            width=None,
            height=None,
            provenance={
                "request_key": f"synthetic:{pathway_id}:kgml",
                "retrieved_at": NOW.isoformat(),
                "origin": "cache",
                "is_stale": False,
                "parser_version": "1",
            },
        )

    async def probe(self) -> bool:
        return self.reachable


def _provenance(operation: KeggOperation) -> KeggBatchProvenance:
    return KeggBatchProvenance(
        operation=operation,
        request_key=f"synthetic:{operation.value}",
        access_mode=AccessMode.PUBLIC_ACADEMIC,
        retrieval_endpoint_class=RetrievalEndpointClass.PUBLIC_ACADEMIC,
        endpoint_label="public-academic-endpoint",
        origin=ResponseOrigin.CACHE,
        cache_lookup_state=CacheLookupState.FRESH_HIT,
        retrieved_at=NOW,
        served_at=NOW,
        expires_at=NOW + timedelta(days=1),
        response_bytes=200,
        parser_name="synthetic_parser",
        parser_version="1",
        database_release="Synthetic release",
        attempt_count=0,
        is_stale=False,
    )


def make_render_input() -> RenderInput:
    import_limits = ImportLimits(
        max_bytes=100_000,
        max_rows=100,
        max_columns=10,
        max_field_length=1_000,
    )
    dataset = import_generic_table(
        "sequence,ko,decision\na,K00001,accepted\nu,K00002,uncertain\nr,K00003,rejected\n",
        dialect=TableDialect.CSV,
        mapping=GenericColumnMapping(sequence_id="sequence", ko_id="ko", raw_decision="decision"),
        policy=CANONICAL_SOURCE_STATUS,
        limits=import_limits,
        source=SourceProvenanceInput(source_name="synthetic_annotations", source_version="1"),
    )
    graph = resolve_module_definitions(
        ModuleDefinitionCollection(
            root_module_id="M00001",
            definitions=(
                ModuleDefinition.from_text(
                    module_id="M00001",
                    module_name="Synthetic MODULE",
                    definition="K00001+K00001 (K00002,K00003) -K00003 M00002",
                ),
                ModuleDefinition.from_text(module_id="M00002", definition="K00001"),
            ),
        )
    )
    paired = evaluate_module_pair(graph, dataset)
    reference = PathwayKoReference(
        reference_namespace=PathwayReferenceNamespace.KO,
        reference_scope=PathwayReferenceScope.STANDARD,
        pathway_id="ko00010",
        pathway_name="Synthetic pathway",
        pathway_class=("Metabolism; Synthetic class",),
        reference_kos=("K00001", "K00002", "K00003"),
        relationship_row_count=3,
        link_provenance=(_provenance(KeggOperation.LINK),),
        metadata_provenance=(_provenance(KeggOperation.GET),),
    )
    coverage = evaluate_pathway_coverage(
        reference,
        dataset,
        PathwayCoverageParameters(evidence_mode=EvidenceMode.LENIENT),
    )
    execution = AnalysisExecutionProvenance(
        import_limits=import_limits,
        kegg_request_options=KeggRequestOptions(refresh=False, allow_stale=True),
        reference_loading_limits=ReferenceLoadingLimits(),
        pathway_parameters=PathwayExecutionParameters(evidence_mode=EvidenceMode.LENIENT),
        direct_result_limits=AnalysisServiceLimits(),
    )
    return build_render_input(
        dataset,
        (graph,),
        (paired,),
        (reference,),
        (coverage,),
        execution,
    )


@pytest.fixture
def allowed_root(tmp_path: Path) -> Path:
    root = tmp_path / "allowed"
    root.mkdir(mode=0o700)
    return root.resolve()


@pytest.fixture
def runtime_config(tmp_path: Path, allowed_root: Path) -> RendererRuntimeConfig:
    return RendererRuntimeConfig(
        state_root=(tmp_path / "state").resolve(),
        allowed_roots=(allowed_root,),
        access_mode="unconfigured",
        retention_seconds=30,
        limits=RendererLimits(
            max_input_bytes=4_000_000,
            max_asset_bytes=2_000_000,
            max_pixels=4_000_000,
            max_svg_bytes=4_000_000,
            max_result_bytes=16_000_000,
            max_disk_bytes=32_000_000,
            max_xml_elements=2_000,
            max_xml_attributes=10_000,
            max_xml_depth=16,
            max_svg_nodes=10_000,
        ),
    )


@pytest.fixture
def render_input_file(allowed_root: Path) -> Path:
    path = allowed_root / "render_input.json"
    path.write_text(serialize_render_input(make_render_input()), encoding="utf-8")
    return path


@pytest.fixture
def synthetic_provider() -> SyntheticProvider:
    return SyntheticProvider()
