"""Deterministic pathway ranking from selected KO annotation evidence."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from kegg_mcp.domain.annotations import (
    JSON_SCHEMA_DIALECT,
    AnnotationDataset,
    EvidenceMode,
    FrozenModel,
    KNumber,
    ModuleId,
    build_ko_evidence_view,
    select_ko_ids,
)
from kegg_mcp.domain.errors import ErrorCode, fail
from kegg_mcp.domain.identifiers import try_normalize_ko_id
from kegg_mcp.kegg.contracts import KeggPairRow, is_kegg_pathway_identifier

PATHWAY_RANKING_METHOD = "selected_unique_ko_count"
PATHWAY_RANKING_VERSION = "1"
MODULE_RANKING_METHOD = "selected_unique_ko_count"
MODULE_RANKING_VERSION = "1"


class PathwaySelection(FrozenModel):
    """Bounded automatic pathway target-selection request."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:pathway-selection:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    top_n: int = Field(default=5, strict=True, ge=1, le=25)


class ModuleSelection(FrozenModel):
    """Bounded automatic MODULE target-selection request."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:module-selection:2",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    top_n: int = Field(default=5, strict=True, ge=1, le=25)


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


def _validate_sorted_unique_ko_ids(ko_ids: tuple[str, ...], *, field_name: str) -> None:
    if ko_ids != tuple(sorted(set(ko_ids))):
        raise ValueError(f"{field_name} must be sorted and unique")


def _validate_ranking_row_counts(
    detected_ko_ids: tuple[str, ...],
    detected_unique_ko_count: int,
    relationship_row_count: int,
) -> None:
    _validate_sorted_unique_ko_ids(detected_ko_ids, field_name="detected_ko_ids")
    if detected_unique_ko_count != len(detected_ko_ids):
        raise ValueError("detected_unique_ko_count must match detected_ko_ids")
    if relationship_row_count < detected_unique_ko_count:
        raise ValueError("relationship_row_count cannot be smaller than the unique KO count")


def _validate_contiguous_ranks(ranks: tuple[int, ...], *, target_name: str) -> None:
    if ranks != tuple(range(1, len(ranks) + 1)):
        raise ValueError(f"{target_name} ranking rows must use contiguous one-based ranks")


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
        _validate_ranking_row_counts(
            self.detected_ko_ids,
            self.detected_unique_ko_count,
            self.relationship_row_count,
        )
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
        _validate_sorted_unique_ko_ids(self.selected_ko_ids, field_name="selected_ko_ids")
        _validate_contiguous_ranks(
            tuple(item.rank for item in self.rows),
            target_name="pathway",
        )
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


class KoModuleRelationship(FrozenModel):
    """One normalized KO-to-MODULE row retained for ranking provenance."""

    source_ko_id: KNumber
    module_id: ModuleId
    source_namespace: Literal["ko"] = "ko"
    target_namespace: Literal["md", "module"]
    batch_index: int = Field(strict=True, ge=0)
    line_number: int = Field(strict=True, gt=0)


class ModuleRankingRow(FrozenModel):
    """One complete MODULE candidate row in deterministic rank order."""

    module_id: ModuleId
    detected_unique_ko_count: int = Field(strict=True, gt=0)
    detected_ko_ids: Annotated[tuple[KNumber, ...], Field(min_length=1, max_length=100_000)]
    relationship_row_count: int = Field(strict=True, gt=0)
    rank: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def validate_row(self) -> Self:
        _validate_ranking_row_counts(
            self.detected_ko_ids,
            self.detected_unique_ko_count,
            self.relationship_row_count,
        )
        return self


class ModuleRankingResult(FrozenModel):
    """Complete selected-evidence relationship and MODULE ranking result."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "urn:kegg-mcp:schema:module-ranking-result:1",
            "$schema": JSON_SCHEMA_DIALECT,
        }
    )

    method: Literal["selected_unique_ko_count"] = MODULE_RANKING_METHOD
    method_version: Literal["1"] = MODULE_RANKING_VERSION
    evidence_mode: EvidenceMode
    selected_ko_ids: Annotated[tuple[KNumber, ...], Field(max_length=100_000)]
    relationships: Annotated[tuple[KoModuleRelationship, ...], Field(max_length=1_000_000)]
    rows: Annotated[tuple[ModuleRankingRow, ...], Field(max_length=100_000)]

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        _validate_sorted_unique_ko_ids(self.selected_ko_ids, field_name="selected_ko_ids")
        _validate_contiguous_ranks(
            tuple(item.rank for item in self.rows),
            target_name="MODULE",
        )
        sort_keys = tuple((-item.detected_unique_ko_count, item.module_id) for item in self.rows)
        if sort_keys != tuple(sorted(sort_keys)):
            raise ValueError("MODULE ranking rows are not in deterministic rank order")
        relationship_counts: dict[str, int] = {}
        relationship_kos: dict[str, set[str]] = {}
        for relationship in self.relationships:
            module_id = relationship.module_id
            relationship_counts[module_id] = relationship_counts.get(module_id, 0) + 1
            relationship_kos.setdefault(module_id, set()).add(relationship.source_ko_id)
        if set(relationship_counts) != {item.module_id for item in self.rows}:
            raise ValueError("ranking rows must represent every retained MODULE relationship")
        for row in self.rows:
            if relationship_counts[row.module_id] != row.relationship_row_count:
                raise ValueError("MODULE ranking relationship counts do not match retained rows")
            if tuple(sorted(relationship_kos[row.module_id])) != row.detected_ko_ids:
                raise ValueError("MODULE ranking detected KO sets do not match relationships")
        return self


@dataclass(frozen=True, slots=True)
class _RankedRelationship:
    source_ko_id: str
    target_id: str
    canonical_target_id: str
    target_namespace: str
    batch_index: int
    line_number: int


@dataclass(frozen=True, slots=True)
class _RankedTarget:
    target_id: str
    detected_ko_ids: tuple[str, ...]
    relationship_row_count: int
    rank: int


TargetNormalizer = Callable[[str, str], tuple[str, str] | None]


def _rank_ko_targets(
    dataset: AnnotationDataset,
    relationship_rows: tuple[KeggPairRow, ...],
    evidence_mode: EvidenceMode,
    *,
    target_name: str,
    normalize_target: TargetNormalizer,
) -> tuple[tuple[str, ...], tuple[_RankedRelationship, ...], tuple[_RankedTarget, ...]]:
    """Apply one selected KO view and deterministic overlap ranking to a typed target."""
    selected_ko_ids = select_ko_ids(build_ko_evidence_view(dataset), evidence_mode)
    selected = frozenset(selected_ko_ids)
    relationships: list[_RankedRelationship] = []
    detected_by_target: dict[str, set[str]] = {}
    row_counts: dict[str, int] = {}
    for row in relationship_rows:
        source_value = row.source_id.rsplit(":", 1)[-1]
        target_value = row.target_id.rsplit(":", 1)[-1]
        source_ko_id, _ = try_normalize_ko_id(source_value)
        normalized_target = normalize_target(row.target_id, target_value)
        if source_ko_id is None or normalized_target is None:
            fail(
                ErrorCode.KEGG_PARSE_FAILED,
                f"A KO-to-{target_name} relationship row has incompatible identifiers.",
                suggested_action="Refresh the typed KEGG LINK response and retry.",
            )
        if source_ko_id not in selected:
            continue
        canonical_target_id, target_namespace = normalized_target
        relationships.append(
            _RankedRelationship(
                source_ko_id=source_ko_id,
                target_id=target_value,
                canonical_target_id=canonical_target_id,
                target_namespace=target_namespace,
                batch_index=row.batch_index,
                line_number=row.line_number,
            )
        )
        detected_by_target.setdefault(canonical_target_id, set()).add(source_ko_id)
        row_counts[canonical_target_id] = row_counts.get(canonical_target_id, 0) + 1
    ordered = sorted(
        detected_by_target,
        key=lambda target_id: (-len(detected_by_target[target_id]), target_id),
    )
    targets = tuple(
        _RankedTarget(
            target_id=target_id,
            detected_ko_ids=tuple(sorted(detected_by_target[target_id])),
            relationship_row_count=row_counts[target_id],
            rank=rank,
        )
        for rank, target_id in enumerate(ordered, start=1)
    )
    return selected_ko_ids, tuple(relationships), targets


def _normalize_pathway_target(raw_target: str, target_value: str) -> tuple[str, str] | None:
    del raw_target
    if not is_kegg_pathway_identifier(target_value):
        return None
    namespace = target_value[:-5]
    if namespace not in {"ko", "map"}:
        return None
    return f"ko{target_value[-5:]}", namespace


def _normalize_module_target(raw_target: str, target_value: str) -> tuple[str, str] | None:
    if re.fullmatch(r"M[0-9]{5}", target_value) is None:
        return None
    namespace = raw_target.split(":", 1)[0] if ":" in raw_target else "module"
    if namespace not in {"md", "module"}:
        return None
    return target_value, namespace


def rank_pathways(
    dataset: AnnotationDataset,
    relationship_rows: tuple[KeggPairRow, ...],
    evidence_mode: EvidenceMode,
) -> PathwayRankingResult:
    """Aggregate KO-to-pathway rows using one selected evidence view and stable ordering."""
    selected_ko_ids, ranked_relationships, ranked_targets = _rank_ko_targets(
        dataset,
        relationship_rows,
        evidence_mode,
        target_name="pathway",
        normalize_target=_normalize_pathway_target,
    )
    relationships = tuple(
        KoPathwayRelationship(
            source_ko_id=item.source_ko_id,
            target_id=item.target_id,
            pathway_number=item.canonical_target_id[-5:],
            canonical_pathway_id=item.canonical_target_id,
            target_namespace="ko" if item.target_namespace == "ko" else "map",
            batch_index=item.batch_index,
            line_number=item.line_number,
        )
        for item in ranked_relationships
    )
    rows = tuple(
        PathwayRankingRow(
            pathway_id=item.target_id,
            pathway_number=item.target_id[-5:],
            detected_unique_ko_count=len(item.detected_ko_ids),
            detected_ko_ids=item.detected_ko_ids,
            relationship_row_count=item.relationship_row_count,
            rank=item.rank,
        )
        for item in ranked_targets
    )
    return PathwayRankingResult(
        evidence_mode=evidence_mode,
        selected_ko_ids=selected_ko_ids,
        relationships=relationships,
        rows=rows,
    )


def rank_modules(
    dataset: AnnotationDataset,
    relationship_rows: tuple[KeggPairRow, ...],
    evidence_mode: EvidenceMode,
) -> ModuleRankingResult:
    """Aggregate KO-to-MODULE rows using the shared selected-evidence ranking policy."""
    selected_ko_ids, ranked_relationships, ranked_targets = _rank_ko_targets(
        dataset,
        relationship_rows,
        evidence_mode,
        target_name="MODULE",
        normalize_target=_normalize_module_target,
    )
    relationships = tuple(
        KoModuleRelationship(
            source_ko_id=item.source_ko_id,
            module_id=item.canonical_target_id,
            target_namespace="md" if item.target_namespace == "md" else "module",
            batch_index=item.batch_index,
            line_number=item.line_number,
        )
        for item in ranked_relationships
    )
    rows = tuple(
        ModuleRankingRow(
            module_id=item.target_id,
            detected_unique_ko_count=len(item.detected_ko_ids),
            detected_ko_ids=item.detected_ko_ids,
            relationship_row_count=item.relationship_row_count,
            rank=item.rank,
        )
        for item in ranked_targets
    )
    return ModuleRankingResult(
        evidence_mode=evidence_mode,
        selected_ko_ids=selected_ko_ids,
        relationships=relationships,
        rows=rows,
    )


__all__ = [
    "MODULE_RANKING_METHOD",
    "MODULE_RANKING_VERSION",
    "PATHWAY_RANKING_METHOD",
    "PATHWAY_RANKING_VERSION",
    "KoModuleRelationship",
    "KoPathwayRelationship",
    "ModuleRankingResult",
    "ModuleRankingRow",
    "ModuleSelection",
    "PathwayRankingResult",
    "PathwayRankingRow",
    "PathwaySelection",
    "rank_modules",
    "rank_pathways",
]
