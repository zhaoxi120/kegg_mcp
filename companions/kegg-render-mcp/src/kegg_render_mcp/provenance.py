"""Safe, shared serialization of KEGG retrieval provenance."""

from __future__ import annotations

from kegg_mcp.kegg import KeggBatchProvenance


def safe_batch_provenance(value: KeggBatchProvenance) -> dict[str, object]:
    """Exclude endpoint labels, cache paths, and other deployment-specific details."""
    result: dict[str, object] = {
        "operation": value.operation.value,
        "request_key": value.request_key,
        "access_mode": value.access_mode.value,
        "retrieval_endpoint_class": value.retrieval_endpoint_class.value,
        "origin": value.origin.value,
        "cache_lookup_state": value.cache_lookup_state.value,
        "retrieved_at": value.retrieved_at.isoformat(),
        "served_at": value.served_at.isoformat(),
        "is_stale": value.is_stale,
        "parser_name": value.parser_name,
        "parser_version": value.parser_version,
    }
    if value.database_release is not None:
        result["database_release"] = value.database_release
    return result


__all__ = ["safe_batch_provenance"]
