# Pathway Coverage and Deterministic Functional Comparison

This document specifies the current pathway and comparison analysis layer. It is a project contract
for deterministic analysis, not an official KEGG pathway-completeness method or a statistical
comparison method.

The analysis layer is pure and local. It consumes immutable annotation datasets, typed KEGG
results, resolved MODULE graphs, and pathway references that a caller has already obtained. It
does not call KEGG, read or write the KEGG cache, execute an annotation tool, persist results, or
perform MCP transport. Retrieval, licensing, rate limiting, and cache I/O remain responsibilities
of the KEGG client layer; persistence and presentation belong to the service and MCP layers.

## Pathway reference construction

`build_pathway_reference` constructs a `PathwayKoReference` from two typed client results:

- one `LinkResult` for exactly one `PATHWAY_TO_KO` source identifier; and
- one `GetResult` for exactly the same PATHWAY identifier.

The LINK rows define candidate denominator entries. The GET flat file supplies the retained
top-level `NAME` and classification metadata. The builder normally retains `CLASS` value lines.
When `CLASS` is absent, it accepts only an exact `Global Pathway` or `Overview Pathway` subtype in
the top-level `ENTRY` field and records that fallback with an `ENTRY:` source prefix. The builder
requires the LINK source, GET request, parsed entry identifier, operation-specific provenance, and
requested namespace to agree. It rejects a mixed or mismatched pair instead of guessing which
response is authoritative.

Canonical `ko:KNNNNN` LINK targets enter the denominator. Repeated relationship rows are
deduplicated and counted. Invalid or non-KO targets are retained as bounded, typed exclusions; they
are not silently discarded or counted as K numbers. An empty, successfully retrieved LINK result
therefore creates an empty reference rather than a fabricated denominator.

### Reference namespaces

Every reference and result records one `PathwayReferenceNamespace`:

| Namespace | Identifier | Permitted input context |
| --- | --- | --- |
| `ko` | `koNNNNN` | KO-only evidence; this is the default reference namespace. |
| `map` | `mapNNNNN` | KO-only evidence with an explicitly selected map reference. |
| `organism` | a canonical organism code followed by five digits, such as `hsa00010` | A compatible organism-gene context for the same code. |

At the MCP boundary, `PathwaySpec` infers namespace from `pathway_id`. An omitted namespace on
`mapNNNNN` is canonicalized to the default `koNNNNN` reference view. Explicit conflicts fail at the
field boundary, and equivalent `ko`/`map` pathway numbers are stably deduplicated. KO-to-pathway
mapping previews include `pathway_number`, `namespace`, and `paired_reference_id`; summary counts
are deduplicated by pathway number so paired views cannot inflate the result.

An organism reference is not inferred from a dataset's K numbers or taxon identifier. Evaluation
requires `PathwayInputKind.ORGANISM_GENE_CONTEXT`, an `OrganismGeneContext` containing the exact
KEGG organism code and positive qualified-gene count, and an `AnnotationDataset` for an isolate
genome or isolate proteome. The reference, dataset, and gene-context organism codes must match
exactly. These fields do not turn KO-only evidence into organism-specific gene evidence by
themselves.

Pooled, MAG, mixed, and unknown analysis units must use a `ko` or `map` reference. This prevents a
community KO union or other incompatible unit from being presented as one organism pathway.

### Scope from PATHWAY classification metadata

Reference scope is derived from retained top-level metadata, not from a pathway-number range. A
`CLASS` line containing `Global and overview maps`, or the exact source-tagged `ENTRY: Global
Pathway` / `ENTRY: Overview Pathway` fallback, produces
`PathwayReferenceScope.GLOBAL_OR_OVERVIEW`; other retained `CLASS` evidence produces `STANDARD`.
A regular pathway without `CLASS`, an unknown `ENTRY` subtype, or conflicting `CLASS` and `ENTRY`
metadata fails closed. Evaluating a global or overview reference requires
`PathwayCoverageParameters.allow_global_or_overview=True`. The opt-in is serialized and the result
always carries a broad-reference warning. This keeps a percentage over a large heterogeneous map
from being treated as an ordinary default analysis.

## Descriptive unique-KO coverage

Coverage is the deterministic set intersection over sorted unique accepted K numbers:

```text
detected KOs = selected input KOs intersect unique reference KOs
missing KOs  = unique reference KOs minus selected input KOs
coverage     = detected unique KO count / reference unique KO count
```

The denominator is the sorted set of unique linked K numbers retained in the
`PathwayKoReference`. It is tied to the namespace and retrieval provenance used to construct that
reference. Duplicate annotation records and duplicate LINK rows do not inflate either side of the
ratio.

If the reference contains no K numbers, `evaluation_status` is `not_evaluable`, the ratio is null,
and detected and missing counts are zero. An empty denominator is never reported as zero coverage,
pathway absence, or successful evaluation.

Rejected, unclassified, and invalid records do not enter coverage. The result records the named
decision policy, selected and reference KO sets, and their exact counts. With full-record evidence,
a caller can recompute the set calculation from the retained immutable dataset and pathway
reference. An explicit `KoAnalysisProjection` retains the selected accepted-KO set and aggregate
intake counts but intentionally cannot reproduce record-level evidence.

### Result bounds and provenance

`PathwayCoverageResult` returns exact numerator, denominator, input-evidence, input-KO, missing,
exclusion, duplicate, and relationship-row counts. `input_record_count` is the retained record
count for a full dataset and the expanded-assignment count for a projection. Detected KOs, missing
KOs, and excluded
relationship entries are bounded previews with explicit truncation flags and warnings.
`PathwayCoverageLimits` also bounds input records, input KOs, reference KOs, relationship rows,
exclusions, dataset sources, provenance batches, and retained classification-evidence lines before
expensive view or result construction.

LINK denominator provenance and GET metadata provenance remain separate. Each retained
`KeggBatchProvenance` includes the operation, readable request key, access and endpoint class,
endpoint label, retrieval and serving times, response byte count, parser name and version, database
release when available, response origin, cache lookup state, expiry, and stale status. A cached
reference therefore retains the original retrieval facts and emits a warning when served stale.
Raw responses, endpoint URLs, credentials, and local cache paths are not serialized by the result.

Every evaluated result includes an analysis-unit-specific warning. Isolate genome, isolate
proteome, MAG, pangenome, metagenomic-community, mixed, and unknown inputs have different
interpretation boundaries. In particular, pangenome and community ratios describe pooled encoded
KO potential and cannot be attributed to one organism.

## Deterministic multi-set KO comparison

`compare_ko_datasets` accepts two or more labelled `ComparisonDatasetInput` objects, preserves
caller order, and requires the same named and versioned normalization policy for every dataset.
It does not retrieve KEGG data and does not calculate abundance, confidence intervals, or
replicate-aware statistics.

The function derives one accepted-KO `KoMembershipPartition` within the serialized hard limits:

- `shared_by_all` contains K numbers present in every input;
- `set_specific` contains one ordered entry per input for K numbers present only there; and
- `partially_shared` groups K numbers by every observed proper-subset membership pattern.

These groups are disjoint and their union count is exact. Construction fails before unbounded
membership expansion if record, KO, source, sample-label, set, or total-membership limits are
exceeded. `KoSetComparisonDetail` therefore contains the complete bounded partition; it is not a
silently truncated detail object.

`summarize_ko_comparison` converts that detail into a `KoSetComparisonSummary` with exact counts,
bounded lexical KO previews, a bounded membership-pattern preview, and explicit truncation. The
complete in-memory detail is passed directly rather than identified by a workflow digest.

Each dataset retains its label, input index, dataset identifier, decision policy, analysis
unit, taxonomic context, sample labels, source provenance, record count, and selected unique-KO
count.
Different annotation-pipeline, analysis-unit, or taxonomic provenance produces explicit warnings
without changing deterministic set arithmetic. Mixed or unknown units, pooled community or
pangenome inputs, MAGs, and inputs pooling several samples receive additional scoped warnings.

## Shared-reference functional comparison

MODULE and pathway outcomes must be recomputed for every dataset against one shared reference and
one set of parameters. Comparing previously calculated results with different definitions,
denominators, retrieval provenance, algorithm versions, or limits would confound evidence
differences with reference differences.

`compare_module_graphs` evaluates each dataset's accepted K numbers against the same
`ResolvedModuleGraph`. It preserves the shared definition and its provenance,
calculation method, and analysis limits, and reports ordered outcome/status membership without
renaming a difference as a biological change.

`compare_pathway_references` evaluates every dataset's accepted K numbers against each
supplied immutable `PathwayKoReference` and the same `PathwayCoverageLimits`. Each target retains
the complete bounded reference, unique-KO denominator, LINK and GET provenance, calculation
method, and limits. Ordered outcomes report detected-reference-KO counts, denominator counts,
ratios, statuses, and whether those summaries differ. The function reuses `compare_ko_datasets`
for compatible-policy validation, dataset provenance, and analysis-context warnings; it does not
compare heterogeneous precomputed results.

For an organism pathway, callers provide one ordered `PathwayComparisonOrganismContext` per input.
Its index and label must align with the comparison inputs, and its `OrganismGeneContext` must use
the exact organism code required by the reference and dataset. KO and map comparisons reject these
contexts instead of retaining an inapplicable organism claim. Global or overview references use
one comparison-wide explicit opt-in. Functional comparison limits bound target counts and
aggregate reference KOs and exclusions before the accepted-evidence result matrix is built.

Neither workflow reimplements normalization. Outcome differences are reported against the shared
functional reference and do not replace the complete KO membership partitions.

## Interpretation language

All outputs are descriptive. A K-number assignment remains annotation evidence, and a
K number missing from one set can reflect sequencing, assembly, binning, gene calling, annotation,
model, or database coverage.

Pathway KO coverage must not be described as pathway presence, pathway completeness, expression,
activity, flux, phenotype, or experimental validation. MODULE exact completion remains a separate
project calculation and does not change the meaning of pathway coverage. Set-specific membership
and differing outcomes must not be labelled biological gain or loss. The comparison layer does not
produce p-values, fold changes, enrichment, significance, confidence intervals, or differential
abundance claims.

## Layer boundary

The analysis layer returns immutable, JSON-compatible models in process. It provides bounded
previews and full detail only where configured in-memory limits permit it. Result storage, resource
URIs, pagination, reports, CSV export, cleanup, and transport presentation belong to the service and
MCP layers.

## Public Python surface

The public functions operate on typed objects and perform no hidden I/O:

```python
from kegg_mcp.analysis import (
    ComparisonDatasetInput,
    PathwayReferenceNamespace,
    build_pathway_reference,
    compare_ko_datasets,
    compare_module_graphs,
    compare_pathway_references,
    evaluate_pathway_coverage,
    summarize_ko_comparison,
)

reference = build_pathway_reference(
    link_result,
    get_result,
    PathwayReferenceNamespace.KO,
)
coverage_result = evaluate_pathway_coverage(reference, annotation_dataset)

inputs = (
    ComparisonDatasetInput(label="first", dataset=first_dataset),
    ComparisonDatasetInput(label="second", dataset=second_dataset),
)
detail = compare_ko_datasets(inputs)
summary = summarize_ko_comparison(detail)
module_comparison = compare_module_graphs(inputs, resolved_module_graphs)
pathway_comparison = compare_pathway_references(inputs, (reference,))
```

`link_result` and `get_result` are typed KEGG client results obtained before entering the analysis
layer. `annotation_dataset`, `first_dataset`, and `second_dataset` are immutable
`AnnotationDataset` objects produced by an importer or constructed against the domain schema.

## Primary sources

The namespace, LINK/GET, flat-file classification, and interpretation contract was checked against
these primary KEGG pages on 2026-07-14. The classless broad-entry fallback was checked again on
2026-08-07 against the current `ko01100` and `ko01200` flat files:

- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html), page updated 2026-06-17;
- [KEGG PATHWAY Database](https://www.kegg.jp/kegg/pathway.html);
- [KEGG database entry format](https://www.kegg.jp/kegg/docs/dbentry.html), page updated
  2026-06-12;
- [KEGG ko01100 entry](https://rest.kegg.jp/get/ko01100); and
- [KEGG ko01200 entry](https://rest.kegg.jp/get/ko01200).

The deterministic comparison model, bounds, and warning language are project-defined contracts.
