"""Deterministic pathway ranking from selected KO annotation evidence."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnnotationDataset,
    EvidenceMode,
    FrozenModel,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.kegg.contracts import KeggPairRow, is_kegg_pathway_identifier

PATHWAY_RANKING_METHOD = "selected_unique_ko_count"
PATHWAY_RANKING_VERSION = "1"

KNumber = Annotated[str, Field(pattern=r"^K[0-9]{5}$")]


class PathwayRankingMetric(StrEnum):
    """Supported deterministic ranking metrics."""

    UNIQUE_SELECTED_KO_COUNT = "unique_selected_ko_count"


class PathwaySelectionMode(StrEnum):
    """How pathway targets are chosen for a high-level analysis."""

    EXPLICIT = "explicit"
    TOP_DETECTED = "top_detected"


class PathwaySelection(FrozenModel):
    """Bounded pathway target-selection request."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-selection:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    mode: PathwaySelectionMode
    top_n: int = Field(default=1, strict=True, ge=1, le=25)
    metric: Literal[PathwayRankingMetric.UNIQUE_SELECTED_KO_COUNT] = (
        PathwayRankingMetric.UNIQUE_SELECTED_KO_COUNT
    )


class KoPathwayRelationship(FrozenModel):
    """One normalized KO-to-pathway row retained for ranking provenance."""

    source_ko_id: KNumber
    target_id: str = Field(pattern=r"^(?:ko|map)[0-9]{5}$")
    pathway_number: str = Field(pattern=r"^[0-9]{5}$")
    canonical_pathway_id: str = Field(pattern=r"^ko[0-9]{5}$")
    source_namespace: Literal["ko"] = "ko"
    target_namespace: Literal["ko", "map"]
    batch_index: int = Field(strict=True, ge=0)
    line_number: int = Field(strict=True, gt=0)


class PathwayRankingRow(FrozenModel):
    """One complete candidate row ordered by a deterministic ranking policy."""

    pathway_id: str = Field(pattern=r"^ko[0-9]{5}$")
    pathway_number: str = Field(pattern=r"^[0-9]{5}$")
    detected_unique_ko_count: int = Field(strict=True, gt=0)
    detected_ko_ids: Annotated[tuple[KNumber, ...], Field(min_length=1, max_length=100_000)]
    relationship_row_count: int = Field(strict=True, gt=0)
    rank: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_row(self) -> Self:
        if self.pathway_id != f"ko{self.pathway_number}":
            raise ValueError("pathway_id must be the canonical KO-reference pathway identifier")
        if self.detected_ko_ids != tuple(sorted(set(self.detected_ko_ids))):
            raise ValueError("detected_ko_ids must be sorted and unique")
        if self.detected_unique_ko_count != len(self.detected_ko_ids):
            raise ValueError("detected_unique_ko_count must match detected_ko_ids")
        if self.relationship_row_count < self.detected_unique_ko_count:
            raise ValueError("relationship_row_count cannot be smaller than the unique KO count")
        return self


class PathwayRankingResult(FrozenModel):
    """Complete selected-evidence relationship and pathway ranking result."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-ranking-result:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    method: Literal["selected_unique_ko_count"] = PATHWAY_RANKING_METHOD
    method_version: Literal["1"] = PATHWAY_RANKING_VERSION
    evidence_mode: EvidenceMode
    selected_ko_ids: Annotated[tuple[KNumber, ...], Field(max_length=100_000)]
    relationships: Annotated[tuple[KoPathwayRelationship, ...], Field(max_length=1_000_000)]
    rows: Annotated[tuple[PathwayRankingRow, ...], Field(max_length=100_000)]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.selected_ko_ids != tuple(sorted(set(self.selected_ko_ids))):
            raise ValueError("selected_ko_ids must be sorted and unique")
        ranks = tuple(item.rank for item in self.rows)
        if ranks != tuple(range(1, len(self.rows) + 1)):
            raise ValueError("pathway ranking rows must use contiguous one-based ranks")
        sort_keys = tuple((-item.detected_unique_ko_count, item.pathway_id) for item in self.rows)
        if sort_keys != tuple(sorted(sort_keys)):
            raise ValueError("pathway ranking rows are not in deterministic rank order")
        relationship_counts: dict[str, int] = {}
        relationship_kos: dict[str, set[str]] = {}
        for relationship in self.relationships:
            pathway_id = relationship.canonical_pathway_id
            relationship_counts[pathway_id] = relationship_counts.get(pathway_id, 0) + 1
            relationship_kos.setdefault(pathway_id, set()).add(relationship.source_ko_id)
        if set(relationship_counts) != {item.pathway_id for item in self.rows}:
            raise ValueError("ranking rows must represent every retained candidate relationship")
        for row in self.rows:
            if relationship_counts[row.pathway_id] != row.relationship_row_count:
                raise ValueError("ranking relationship counts do not match retained rows")
            if tuple(sorted(relationship_kos[row.pathway_id])) != row.detected_ko_ids:
                raise ValueError("ranking detected KO sets do not match retained relationships")
        return self


def rank_pathways(
    dataset: AnnotationDataset,
    relationship_rows: tuple[KeggPairRow, ...],
    evidence_mode: EvidenceMode,
) -> PathwayRankingResult:
    """Aggregate KO-to-pathway rows using one selected evidence view and stable ordering."""
    selected_ko_ids = select_ko_ids(build_ko_evidence_view(dataset), evidence_mode)
    selected = frozenset(selected_ko_ids)
    relationships: list[KoPathwayRelationship] = []
    detected_by_pathway: dict[str, set[str]] = {}
    row_counts: dict[str, int] = {}

    for row in relationship_rows:
        source_value = row.source_id.rsplit(":", 1)[-1]
        target_value = row.target_id.rsplit(":", 1)[-1]
        source_ko_id, _ = try_normalize_ko_id(source_value)
        if source_ko_id is None or not is_kegg_pathway_identifier(target_value):
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A KO-to-pathway relationship row has incompatible identifiers.",
                suggested_action="Refresh the typed KEGG LINK response and retry.",
            )
        namespace_prefix = target_value[:-5]
        if namespace_prefix not in {"ko", "map"}:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                "A KO-to-pathway relationship row uses an unsupported namespace.",
                suggested_action="Use canonical ko/map pathway relationships for KO evidence.",
            )
        target_namespace: Literal["ko", "map"] = "ko" if namespace_prefix == "ko" else "map"
        if source_ko_id not in selected:
            continue
        pathway_number = target_value[-5:]
        canonical_pathway_id = f"ko{pathway_number}"
        normalized = KoPathwayRelationship(
            source_ko_id=source_ko_id,
            target_id=target_value,
            pathway_number=pathway_number,
            canonical_pathway_id=canonical_pathway_id,
            target_namespace=target_namespace,
            batch_index=row.batch_index,
            line_number=row.line_number,
        )
        relationships.append(normalized)
        detected_by_pathway.setdefault(canonical_pathway_id, set()).add(source_ko_id)
        row_counts[canonical_pathway_id] = row_counts.get(canonical_pathway_id, 0) + 1

    ordered_candidates = sorted(
        detected_by_pathway,
        key=lambda pathway_id: (-len(detected_by_pathway[pathway_id]), pathway_id),
    )
    rows = tuple(
        PathwayRankingRow(
            pathway_id=pathway_id,
            pathway_number=pathway_id[-5:],
            detected_unique_ko_count=len(detected_by_pathway[pathway_id]),
            detected_ko_ids=tuple(sorted(detected_by_pathway[pathway_id])),
            relationship_row_count=row_counts[pathway_id],
            rank=rank,
        )
        for rank, pathway_id in enumerate(ordered_candidates, start=1)
    )
    return PathwayRankingResult(
        evidence_mode=evidence_mode,
        selected_ko_ids=selected_ko_ids,
        relationships=tuple(relationships),
        rows=rows,
    )


__all__ = [
    "PATHWAY_RANKING_METHOD",
    "PATHWAY_RANKING_VERSION",
    "KoPathwayRelationship",
    "PathwayRankingMetric",
    "PathwayRankingResult",
    "PathwayRankingRow",
    "PathwaySelection",
    "PathwaySelectionMode",
    "rank_pathways",
]
