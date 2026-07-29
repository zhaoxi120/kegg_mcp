# Services, Result Storage, and Reporting

This document describes the current transport-independent orchestration, reporting, output-bundle,
and retained-result contracts. MCP tools call these public services; they do not reimplement
normalization, KEGG retrieval, entity resolution, relation tracing, BRITE classification,
annotation mapping audit, MODULE evaluation, pathway coverage, or result retention.

This document owns service composition, serializer behavior, transactional bundle writing, and
storage internals. The [Core MCP server](mcp-server.md) owns public tool schemas, direct-response
fields, resource URIs, pagination, protocol errors, and deployment environment variables.

## Layer boundaries

- `kegg_mcp.kegg` owns typed low-level KEGG operations, authorization, rate limiting, caching,
  batching, strict parsing, limits, and retrieval provenance.
- `kegg_mcp.services` composes importers, the typed KEGG client, query workflows, analysis,
  reporting, and storage.
- `kegg_mcp.services.kegg_search`, `kegg_mcp.services.entity_resolution`,
  `kegg_mcp.services.relation_tracing`, `kegg_mcp.services.brite_hierarchy`, and
  `kegg_mcp.services.annotation_audit` own bounded query and evidence-routing semantics.
- `kegg_mcp.reporting` serializes already-computed evidence and analysis without network or file I/O.
- `SQLiteResultStore` retains immutable artifacts under an explicit local scope.
- `kegg_mcp.mcp` owns transport schemas, resources, protocol errors, and stdio lifecycle.

The injected KEGG client remains responsible for authorization, deployment-wide rate limiting,
cache behavior, batching, limits, strict endpoint-response validation, and retrieval provenance.
Query services compose that client; MCP handlers call the services and do not reproduce lookup,
traversal, classification, or audit logic.

## High-level analysis

`analyze_ko_annotations` is the supported one-call service for a K-number list or annotation table.
It normalizes evidence once, optionally selects bounded MODULE and pathway targets, loads references,
evaluates requested targets, renders retained artifacts, and optionally writes one transactional
output bundle. Narrower normalization, mapping, MODULE, pathway, and comparison tools reuse the
same service functions.

When neither explicit targets nor explicit selection are supplied, the service independently
selects up to five MODULEs and five canonical KO reference pathways. Ranking uses unique K numbers
from the requested evidence mode, sorts by descending overlap, and uses the canonical target ID as
the stable tie-breaker. MODULE ranking selects definitions; it is not completion or enrichment.

Duplicate annotation or KEGG relationship rows never inflate unique-KO counts. Complete rankings
and relationships remain in retained or bundle artifacts while direct MCP results return bounded
summaries.

## Reference loading

`load_module_graphs` retrieves ordered root MODULEs and bounded reachable references through typed
GET requests of at most ten entries. Missing roots fail; missing reachable references, cycles,
parser diagnostics, source definitions, retrieval time, cache state, and release metadata remain
explicit in the resolved graph.

`load_pathway_references` accepts ordered `PathwaySpec` values, validates namespaces, canonicalizes
an omitted `map` view to `ko`, and stably de-duplicates paired views by pathway number. It uses the
typed pathway-to-KO relationship and pathway entry operations before delegating denominator and
metadata validation to the analysis layer.

Aggregate requests, response bytes, references, relationship rows, targets, and selected K numbers
remain bounded across the complete operation.

## Bounded KEGG query services

`search_kegg_entries` composes one typed FIND request and returns a bounded projection of ordered
database-validated candidates. The complete response remains in a scoped retained artifact.
Keyword, formula, exact-mass, and molecular-weight matches are candidates only: the service does
not calculate relevance, choose a best match, or claim compound identification.

`resolve_kegg_entities` uses a discriminated gene or organism request. Gene resolution accepts
typed external or KEGG namespaces and an explicit organism where required; organism resolution
accepts code, genome, taxonomy, or name inputs. The service retains all candidates and reports
mapping yield, ambiguity, many-to-one mappings, organism mismatches, and the FIND, GET, CONV, LINK,
and LIST operations used. When a gene request supplies an organism code, a bounded typed GENOME GET
establishes its code/T-number identity before direct or converted gene prefixes are filtered; a
lexical prefix difference alone is not treated as a cross-organism mismatch. Each source-backed
organism candidate also reports the complete count
and at most twenty ordered entries from its organism-specific pathway directory; the full typed
LIST response remains in the retained artifact. That directory describes available KEGG
references, not pathway presence, completeness, activity, flux, or phenotype. Mapping failure is
not evidence that an entity does not exist.

`trace_kegg_relations` traverses an allowlist of typed KEGG LINK directions for one or two hops.
Seeds, edge types, nodes, edges, raw relationship rows, response bytes, and provenance are bounded.
Every edge contains sorted indexes into the result-level provenance sequence for the LINK and any
required genome-alias GET batches that support it. The service preserves endpoint-returned nodes
and edges; it does not calculate centrality, shortest paths, communities, regulation, causality,
or mechanism.

The shared selected-entry relationship helper uses the low-level client's canonical LINK
preparation to issue one client call per actual endpoint batch. Before another batch begins, it
checks the aggregate request count and records that batch's rows, response bytes, and provenance.
Reference-loading budgets likewise count returned provenance batches rather than treating a
multi-batch client result as one request; GET callers submit chunks of no more than ten entries.
When a caller does not supply cache options, all five high-level services prefer a fresh local
cache entry and preserve an explicit refresh request unchanged.

`map_brite_hierarchy` maps bounded typed entities into selected or safely discovered BRITE
hierarchies. It preserves source-backed hierarchy paths, supports multiple memberships, reports
unmatched entities, and retains complete bounded JSON and TSV artifacts behind a compact preview.
Classification counts are unique supplied-entity counts without abundance weighting or
statistical enrichment.

`audit_annotation_mapping` reuses the imported immutable annotation dataset and the fixed KO
relationship mappings. It reports evidence-state counts, duplicate and conflicting assignments,
strict and lenient mapping yields, one-to-many and unmapped K numbers, source-provenance warnings,
and KEGG cache, release, and retrieval summaries. The audit does not alter source decisions, fill
missing K numbers, compare incompatible scores, or infer biological absence.

These services are query and evidence-routing paths, not extensions of the annotator or renderer.
Their retained BRITE and audit artifacts do not enter `render_input.json`, and neither companion
retrieves or recomputes them.

## Direct and retained results

Services produce a compact direct projection and a complete retained artifact from the same
authoritative domain result. The projection never becomes an alternative analysis or query path.
Retained artifacts preserve complete bounded provenance, parameters, candidates, crosswalks,
hierarchy paths, audit metrics, rankings, relationships, and evaluations. Public response fields
and retrieval behavior are specified only in
[Core MCP server](mcp-server.md#tools).

## Report artifacts

`render_report` deterministically produces three UTF-8 artifacts:

| Section | MIME type | Contract |
| --- | --- | --- |
| `structured` | `application/json` | Canonical complete result within the configured JSON limit. |
| `summary` | `text/markdown` | Conservative bounded report with explicit truncation. |
| `annotations` | `text/csv` | Complete flat annotation records within the CSV limit. |

Serialization rejects non-finite JSON, uses stable CSV columns, protects spreadsheet-formula cells,
and records exact UTF-8 byte sizes. Structured JSON and CSV fail rather than returning partial
content. Only the Markdown preview may be truncated.

## Output bundles and renderer handoff

The bundle writer serializes service-owned tables and reports into a new or empty allowed-root
directory, validates the complete planned artifact set, publishes files without replacement, and
installs the manifest last as the transaction marker. Source-path redaction is applied while
constructing the manifest rather than by MCP transport.

The renderer handoff is a separate typed service model. It contains only accepted and
policy-defined uncertain evidence plus complete-within-limit authoritative analysis state;
rejected, unclassified, and invalid records never enter visualization evidence. Its detailed
schema and renderability semantics are owned by
[Visualization architecture](visualization-architecture.md).

## Scoped result storage

`SQLiteResultStore` retains immutable artifact groups in an operator-selected local database. The
default limits are a 24-hour hard TTL, 512 MiB logical artifact quota, 640 MiB main-database cap,
10,000 active results, 50 metadata rows per page, and 64 KiB artifact byte ranges. Public
configuration enforces lower operational values where applicable.

Every lookup requires an opaque `result_id` and the creating `scope_id`. Invalid, unknown, expired,
deleted, and cross-scope identifiers return the same safe `RESULT_NOT_FOUND` response. Public
metadata omits scope and database paths.

The store provides bounded operations to:

- list current-scope result and artifact metadata;
- read an artifact by validated byte range;
- delete one authorized result or the current scope;
- remove TTL-expired orphan rows; and
- perform separate operator-requested over-quota maintenance.

Capacity failure never evicts an unexpired result silently. Normal stdio shutdown deletes the
current scope. Output bundles remain independently operator-owned durable files.

The configured database is operator-owned rather than client-selected. Path validation rejects
traversal, unsafe writable ancestry, symlinks, non-regular targets, and replacement races, and uses
restrictive permissions where supported.

## Interpretation and testing

Reports describe annotation evidence, and query artifacts describe candidate matches, KEGG
relationships, hierarchy membership, or mapping quality. They do not establish pathway presence,
completeness, expression, activity, flux, phenotype, experimental validation, enrichment,
statistical significance, or graph-derived mechanism.

Unit and integration tests use synthetic KEGG responses and temporary stores. Default local tests
make no live KEGG calls. The single governed live compatibility campaign is defined in
`tests/live/README.md` and remains a pull-request CI concern.
