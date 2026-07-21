# Services, Result Storage, and Reporting

This document describes the current transport-independent orchestration, reporting, output-bundle,
and retained-result contracts. MCP tools call these public services; they do not reimplement
normalization, KEGG retrieval, MODULE evaluation, pathway coverage, or result retention.

## Layer boundaries

- `kegg_mcp.services` composes importers, the typed KEGG client, analysis, reporting, and storage.
- `kegg_mcp.reporting` serializes already-computed evidence and analysis without network or file I/O.
- `SQLiteResultStore` retains immutable artifacts under an explicit local scope.
- `kegg_mcp.mcp` owns transport schemas, resources, protocol errors, and stdio lifecycle.

The injected KEGG client remains responsible for authorization, deployment-wide rate limiting,
cache behavior, batching, limits, and retrieval provenance.

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

## Direct and retained results

Direct tool output contains the compact information needed for the next decision: record and KO
counts, selected targets, bounded MODULE/pathway previews, request/cache summaries, warnings,
caveats, and output-bundle metadata when requested.

The authoritative retained JSON keeps complete information within configured limits, including:

- source and annotation provenance;
- decision and evidence policy;
- KEGG request and parser provenance;
- execution parameters and six sanitized stage metrics;
- complete bounded rankings and target relationships; and
- MODULE, pathway, comparison, and renderer-handoff details.

Opaque result identifiers are same-process retrieval aids. Versioned output files are the durable
cross-process handoff.

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

When an allowed `output_directory` is supplied, the service may write normalized annotations,
protein-to-KO mappings, MODULE/pathway tables, ranking and relationship tables, the Markdown report,
`render_input.json`, and a versioned manifest.

Output-bundle schema version 3:

- accepts only a new or empty allowed-root directory;
- rejects symlinks and never replaces an existing entry;
- installs the manifest last as the commit marker;
- records MIME type, exact byte size, and controlled path for every file; and
- redacts absolute source paths unless `manifest_path_mode="absolute"` is explicitly requested.

The public `RenderInput` schema version 3 is distinct from the bundle schema. Its MIME type is
`application/vnd.kegg-mcp.render-input+json;version=3`. It contains accepted and policy-defined
uncertain evidence, complete-within-limit pathway detected K numbers, resolved MODULE definitions
and ASTs, exact strict/lenient outcomes, required-block states, and execution provenance.

Rejected, unclassified, and invalid records never enter visualization evidence. Oversized targets
become explicit `not_renderable` summaries; no preview is relabelled as complete. Identity, count,
and byte-limit failures occur before a partial handoff is published.

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

Reports describe annotation evidence. They do not establish pathway presence, completeness,
expression, activity, flux, phenotype, experimental validation, or statistical significance.

Unit and integration tests use synthetic KEGG responses and temporary stores. Default local tests
make no live KEGG calls. The single governed live compatibility campaign is defined in
`tests/live/README.md` and remains a pull-request CI concern.
