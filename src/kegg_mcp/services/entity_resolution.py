"""Dispatch bounded KEGG entity resolution by request kind."""

from __future__ import annotations

from kegg_mcp.kegg import KeggRequestOptions
from kegg_mcp.services.gene_resolution import resolve_gene_request
from kegg_mcp.services.organism_resolution import resolve_organism_request
from kegg_mcp.services.query_models import (
    GeneResolutionRequest,
    ResolveKeggEntitiesRequest,
    ResolveKeggEntitiesResult,
    SubstanceResolutionRequest,
)
from kegg_mcp.services.reference_budget import KeggQueryClient, effective_query_options
from kegg_mcp.services.result_store import SQLiteResultStore
from kegg_mcp.services.substance_resolution import resolve_substance_request


def resolve_kegg_entities(
    request: ResolveKeggEntitiesRequest,
    *,
    client: KeggQueryClient,
    result_store: SQLiteResultStore,
    scope_id: str,
    options: KeggRequestOptions | None = None,
) -> ResolveKeggEntitiesResult:
    """Resolve genes, organisms, or substances without hiding ambiguous candidates."""
    effective_options = effective_query_options(options)
    if isinstance(request, GeneResolutionRequest):
        return resolve_gene_request(
            request,
            client=client,
            result_store=result_store,
            scope_id=scope_id,
            options=effective_options,
        )
    if isinstance(request, SubstanceResolutionRequest):
        return resolve_substance_request(
            request,
            client=client,
            result_store=result_store,
            scope_id=scope_id,
            options=effective_options,
        )
    return resolve_organism_request(
        request,
        client=client,
        result_store=result_store,
        scope_id=scope_id,
        options=effective_options,
    )


__all__ = ["resolve_kegg_entities"]
