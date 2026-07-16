"""Compatibility facade for bounded service use cases and models."""

from kegg_mcp.services.annotation_analysis import analyze_annotation_targets
from kegg_mcp.services.comparison import compare_annotation_sets
from kegg_mcp.services.kegg_mapping import (
    map_ko_identifiers,
    read_cached_kegg_entry,
    retrieve_kegg_entries,
)
from kegg_mcp.services.models import (
    DATASET_SECTION,
    DEFAULT_IMPORT_LIMITS,
    DETAIL_SECTION,
    AnnotationInputFormat,
    AnnotationProvenanceSummary,
    AnnotationRecordPreview,
    AnnotationSourceSummary,
    CachedKeggEntryServiceResult,
    CompareDatasetSource,
    CompareKoSetsResult,
    ConnectivityProbeResult,
    ConnectivityState,
    DatasetSource,
    FunctionalComparisonSummary,
    GenericDecisionPolicy,
    KeggEntriesServiceResult,
    KeggEntryPreview,
    KoMappingServiceResult,
    KoSetComparisonPreview,
    NormalizeAnnotationsRequest,
    NormalizeAnnotationsResult,
    PrimitiveAnalysisResult,
    SelectedPathwaySummary,
    ServerStatusResult,
)
from kegg_mcp.services.module_analysis import analyze_module_targets
from kegg_mcp.services.normalization import normalize_annotations
from kegg_mcp.services.operational import (
    delete_analysis_result,
    get_server_status_service,
    probe_kegg_connectivity_service,
)
from kegg_mcp.services.pathway_analysis import analyze_pathway_targets
from kegg_mcp.services.reference_budget import KeggConnectivityClient, KeggPrimitiveClient

__all__ = [
    "DATASET_SECTION",
    "DEFAULT_IMPORT_LIMITS",
    "DETAIL_SECTION",
    "AnnotationInputFormat",
    "AnnotationProvenanceSummary",
    "AnnotationRecordPreview",
    "AnnotationSourceSummary",
    "CachedKeggEntryServiceResult",
    "CompareDatasetSource",
    "CompareKoSetsResult",
    "ConnectivityProbeResult",
    "ConnectivityState",
    "DatasetSource",
    "FunctionalComparisonSummary",
    "GenericDecisionPolicy",
    "KeggConnectivityClient",
    "KeggEntriesServiceResult",
    "KeggEntryPreview",
    "KeggPrimitiveClient",
    "KoMappingServiceResult",
    "KoSetComparisonPreview",
    "NormalizeAnnotationsRequest",
    "NormalizeAnnotationsResult",
    "PrimitiveAnalysisResult",
    "SelectedPathwaySummary",
    "ServerStatusResult",
    "analyze_annotation_targets",
    "analyze_module_targets",
    "analyze_pathway_targets",
    "compare_annotation_sets",
    "delete_analysis_result",
    "get_server_status_service",
    "map_ko_identifiers",
    "normalize_annotations",
    "probe_kegg_connectivity_service",
    "read_cached_kegg_entry",
    "retrieve_kegg_entries",
]
