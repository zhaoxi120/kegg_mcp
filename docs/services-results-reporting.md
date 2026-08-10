# Services, Result Storage, and Reporting

This document describes the current transport-independent orchestration, reporting, output-bundle,
and retained-result contracts. MCP tools call these public services; they do not reimplement
normalization, KEGG retrieval, entity resolution, relation tracing, BRITE classification,
typed card/reference projection, annotation mapping audit, local reference comparison, selected
reference export, external-input handoff preparation, MODULE evaluation, pathway coverage, or
result retention.

This document owns service composition, serializer behavior, transactional bundle writing, and
storage internals. The [Core MCP server](mcp-server.md) owns public tool schemas, direct-response
fields, resource URIs, pagination, protocol errors, and deployment environment variables.

## Layer boundaries

- `kegg_mcp.kegg` owns typed low-level KEGG operations, authorization, rate limiting, caching,
  batching, strict parsing, limits, and retrieval provenance.
- `kegg_mcp.services` composes importers, the typed KEGG client, query workflows, analysis,
  reporting, and storage.
- `kegg_mcp.services.kegg_entries`, `kegg_mcp.services.entry_cards`,
  `kegg_mcp.services.kegg_search`, `kegg_mcp.services.entity_resolution`,
  `kegg_mcp.services.relation_tracing`, `kegg_mcp.services.brite_hierarchy`,
  `kegg_mcp.services.annotation_audit`, `kegg_mcp.services.reference_snapshots`,
  `kegg_mcp.services.reference_bundles`, and `kegg_mcp.services.external_handoff` own bounded
  retrieval, query, evidence-routing, local reference-comparison, and handoff semantics.
- `kegg_mcp.reporting` serializes already-computed evidence and analysis without network or file I/O.
- `SQLiteResultStore` retains immutable artifacts under an explicit local scope.
- `kegg_mcp.mcp` owns transport schemas, resources, protocol errors, and stdio lifecycle.

The injected KEGG client remains responsible for authorization, deployment-wide rate limiting,
cache behavior, batching, limits, strict endpoint-response validation, and retrieval provenance.
Query services compose that client; MCP handlers call the services and do not reproduce lookup,
traversal, classification, or audit logic.

## High-level analysis

`analyze_ko_annotations` is the supported one-call service for a K-number list or annotation table.
It constructs one compact accepted-KO analysis view, optionally selects bounded MODULE and pathway
targets, loads references, evaluates requested targets, renders retained artifacts, and optionally
writes one transactional output bundle. Narrower normalization, mapping, MODULE, pathway, and
comparison tools reuse the same service functions.

Every supported high-level input produces a first-class `KoAnalysisView` containing sorted unique
accepted K numbers, aggregate intake/status counts, provenance, and bounded diagnostics. Record
evidence, protein-to-KO mappings, and duplicate/conflict accounting are unavailable in this
workflow. `normalize_ko_annotations` remains the separate full-record workflow. Allowed DeepKOALA
detailed file input is streamed; other formats and bounded inline input use their applicable
importer bounds without changing the analysis semantics.

Compact intake does not raise KEGG request, relationship-row, reference-loading, ranking, report,
or output limits. Automatic Top-N mapping can still reject a large accepted-KO set at its own
budget. The caller should supply bounded explicit MODULE/pathway targets when known or split the
input into independently meaningful analysis units; the service does not merge arbitrary shards
into one ranking.

When neither explicit targets nor explicit selection are supplied, the service independently
selects up to five MODULEs and five canonical KO reference pathways. Ranking uses sorted unique
accepted K numbers, sorts by descending overlap, and uses the canonical target ID as
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

Before a tool that retains a result starts KEGG access, the MCP dispatch layer verifies that the
scoped result store can initialize and acquire a write transaction. A local result-store failure
therefore returns `RESULT_STORE_FAILED` without first consuming a KEGG request. Connectivity
probing separately reports local cache or rate-limit state failures as `local_storage_failure`
rather than misclassifying them as endpoint authorization failures.

`get_kegg_entries` retains the complete parsed typed GET response. Its default preview projection
returns bounded flat-file or BRITE text previews. The optional card projection accepts only KO,
MODULE, pathway, reaction, enzyme, compound, glycan, gene, and genome flat files. It
deterministically extracts typed common and entity-specific fields, preserves unrecognized field
names, and retains a versioned `entry_snapshot` with the original database-qualified request and
exact GET provenance. Card construction performs no additional KEGG request and is not an LLM
summary. Snapshot data is scoped to the current result store and is not a durable database archive.
The references projection accepts the same nine card-supported entry types and deterministically
extracts only PubMed identifiers listed in their `REFERENCE` fields. It reuses the complete
versioned `entry_snapshot` beside the common GET detail and returns bounded PMID counts and
previews. It makes no paper request, reads no full text, and performs no citation summary or
mechanistic interpretation.

`search_kegg_entries` composes one typed FIND request and returns a bounded projection of ordered
database-validated candidates. The complete response remains in a scoped retained artifact.
The direct result reports observed and bounded candidate counts, clipped candidate previews with
explicit flags, and compact retrieval accounting without full provenance.
Keyword search supports the public KO, pathway, MODULE, reaction, enzyme, compound, glycan, drug,
reaction-class, genome, and organism scopes. Formula, exact-mass, and molecular-weight modes are
limited to compound or drug. Matches are candidates only: the service does not calculate
relevance, choose a best match, or claim chemical identification.

`resolve_kegg_entities` uses a discriminated gene, organism, or substance request. Gene resolution
accepts typed external or KEGG namespaces and an explicit organism where required; organism
resolution accepts code, genome, taxonomy, or name inputs. Taxonomy lookup supports exact, species,
genus, family, order, class, and phylum ranks. Its automatic materialization policy fully validates
exact and species candidates but retains identity-only candidate rows for broader ranks unless the
caller explicitly selects full materialization. Rank expansion accepts a Taxonomy ID for the
requested rank rather than a taxon name or a descendant's Taxonomy ID. Substance resolution
accepts KEGG compound,
glycan, or drug identifiers and selected ChEBI or PubChem SID crosswalks. PubChem CID is not an
alias for SID. The service retains all candidates and reports mapping yield, ambiguity, many-to-one
mappings, organism mismatches, and the typed operations used.
When a gene request supplies an organism code, a bounded typed GENOME GET establishes its
code/T-number identity before direct or converted gene prefixes are filtered; a lexical prefix
difference alone is not treated as a cross-organism mismatch. Organism pathway-directory retrieval
is a separate opt-in projection: `include_pathway_directory=false` performs no LIST request, while
`true` retains the complete typed LIST response for every source-backed canonical candidate. The
direct result contains counts plus bounded input, candidate, projected-entity, taxonomy, and
pathway previews. Text clipping has explicit field-level flags. The direct retrieval summary
contains only counts, response bytes, and a bounded release-label preview; complete provenance
remains retained.
The directory describes available KEGG references, not pathway presence, completeness, activity,
flux, or phenotype. Mapping failure is not evidence that an entity does not exist.

`trace_kegg_relations` traverses an allowlist of typed KEGG LINK directions for one or two hops.
Seeds, edge types, nodes, edges, raw relationship rows, response bytes, and provenance are bounded.
The allowlist includes selected MODULE, glycan, and drug relations. KO-to-gene and
organism-specific pathway-to-gene are dynamically bound to one required canonical organism code;
the service does not expose unscoped KO-to-all-genes expansion or emulate it through generic
pathways. MODULE-source relations accept reference M identifiers only. Selected-entry reaction-class
relations and RMODULE are not exposed because the public endpoint behavior checked on 2026-07-30
did not provide a safe selected-entry contract for them.
The direct result contains retrieval counts plus bounded node and edge previews without embedding
complete provenance batches. The retained graph is complete within the service bounds, and every
retained edge contains sorted indexes into its complete provenance sequence for the LINK and any
required genome-alias GET batches that support it. The service preserves endpoint-returned nodes
and edges; it does not calculate centrality, shortest paths, communities, regulation, causality,
or mechanism.

The shared selected-entry relationship helper uses the low-level client's canonical LINK
preparation to issue one client call per actual endpoint batch. Before another batch begins, it
checks the aggregate request count and records that batch's rows, response bytes, and provenance.
Reference-loading budgets likewise count returned provenance batches rather than treating a
multi-batch client result as one request; GET callers submit chunks of no more than ten entries.
When a caller does not supply cache options, the high-level KEGG query services prefer a fresh
local cache entry and preserve an explicit refresh request unchanged.

`map_brite_hierarchy` maps bounded typed entities into selected or safely discovered BRITE
hierarchies. It preserves source-backed hierarchy paths, supports multiple memberships, reports
unmatched entities, and retains complete bounded JSON and formula-safe TSV artifacts behind a
compact preview. The direct result includes bounded lightweight path, classification, and
unmatched-entity previews, clipped node names with explicit flags, and compact retrieval
accounting. Classification counts are unique supplied-entity counts without abundance weighting or
statistical enrichment.

`audit_annotation_mapping` reuses the imported immutable full-record annotation dataset and
selected fixed KO relationship mappings. The caller may select any subset of pathway, MODULE,
reaction, enzyme, and
BRITE targets; all five are the default, while an empty selection performs an evidence-only audit
without KEGG calls. Before mapping, the service uses the same typed LINK preparation as execution
to count exact endpoint batches. If the selection would exceed the 100-request audit budget,
`mapping_execution.status` is `skipped_request_limit`: no relationship request runs, but the local
evidence audit and explicit skipped-target metadata are still returned. A completed mapping reports
accepted-KO yields, one-to-many and unmapped K numbers, source-provenance warnings, and KEGG
cache, release, and retrieval summaries. The audit does not alter source decisions, fill missing K
numbers, compare incompatible scores, or infer biological absence. The direct result contains the
evidence summary, mapping execution state, compact per-target counts and yields, compact retrieval
accounting, and bounded clipped warning previews. Complete degree distributions, KO previews,
warnings, relationship rows, and provenance remain retained.

If an in-progress target exceeds the aggregate relationship-row or response-byte limit, the audit
reports `incomplete_row_limit` or `incomplete_response_limit`. Previously completed target
summaries and rows remain valid; partial rows for the incomplete target are discarded, later
targets are marked skipped, and the complete local evidence audit is still returned. Mapping yield
is never calculated from a partial target.

`compare_kegg_reference_snapshots` is a local deterministic service over two current-scope
`entry_snapshot` artifacts. It requires the current card schema and the same requested
database-qualified entry set on both sides. It can compare common entry fields, typed relationship
fields, parsed MODULE definitions, and pathway KO denominators while separately recording whether
an entry changed between returned and missing. Parser, endpoint, cache, retrieval, and release
contexts remain explicit. The direct projection contains counts and value-free change locations;
the retained artifact contains the complete bounded field diff. The service makes no KEGG request,
does not build a historical mirror, and does not interpret a structural difference as biological
gain, loss, or validation.

`write_kegg_reference_bundle` reads one current-scope `entry_snapshot`, validates an optional
entry subset, and may attach one current-scope BRITE `detail` artifact. It creates no KEGG request
and exports neither raw cache payloads nor unbounded database content. The committed bundle has
stable `reference_snapshot.json`, `relationships.tsv`, optional `brite_paths.tsv`, and
`reference_manifest.json` files. The snapshot records the selected cards, request, parser/schema,
complete sanitized retrieval batches, and optional BRITE request and provenance. The manifest
records the producer, selection and optional BRITE summary, sanitized retrieval summary, and
payload MIME types, sizes, and SHA-256 hashes without process-scoped result identifiers, request
keys, endpoint values, or local paths.

`prepare_external_handoff` validates one allowlisted KEGG Mapper Reconstruct, Search, Color, Join,
or MWsearch request, or one KEGG Syntax KO Composition or KO Sequence request. It writes the
target's upload-shaped data file and `handoff_manifest.json` without a KEGG API call, upload,
browser, subprocess, or downstream-result parsing. KO Sequence requires
`order_semantics="caller_supplied_genomic_order"`; the service never infers genomic order.

These services are query and evidence-routing paths, not extensions of the annotator or renderer.
Their retained cards, snapshots, BRITE, audit, and diff artifacts do not enter
`render_input.json`, and neither companion retrieves or recomputes them.

## Direct and retained results

Services produce a compact direct projection and a complete retained artifact from the same
authoritative domain result. The projection never becomes an alternative analysis or query path.
Retained artifacts preserve complete bounded provenance, parameters, candidates, crosswalks,
hierarchy paths, audit metrics, rankings, relationships, and evaluations. Public response fields
and retrieval behavior are specified only in
[Core MCP server](mcp-server.md#tools).

Bounded retrieval, query, card, audit, and reference-comparison services enforce a separate 64 KiB
serialized direct-result bound after constructing their fixed previews. A projection-model or
byte-bound failure compensates the newly created retained result instead of leaving an inaccessible
artifact. Full search matches, entry cards, candidate crosswalks, resolution steps, graph nodes and
edges, BRITE paths and classifications, audit distributions and rows, snapshot differences, and
provenance remain retained and are never rebuilt by the LLM.

## Report artifacts

`render_report` deterministically produces three UTF-8 artifacts:

| Section | MIME type | Contract |
| --- | --- | --- |
| `structured` | `application/json` | Canonical complete result within the configured JSON limit. |
| `summary` | `text/markdown` | Conservative bounded report with explicit truncation. |
| `accepted_kos` | `text/csv` | Sorted unique accepted K numbers plus their accepted status for high-level analysis. |

Serialization rejects non-finite JSON, uses stable CSV columns, protects spreadsheet-formula cells,
and records exact UTF-8 byte sizes. Structured JSON and CSV fail rather than returning partial
content. Only the Markdown preview may be truncated. High-level reports state that record-level
evidence, protein mappings, and duplicate/conflict accounting were not retained.

## Output bundles and renderer handoff

The bundle writer serializes service-owned tables and reports into a new or empty allowed-root
directory, validates the complete planned artifact set, publishes files without replacement, and
installs the manifest last as the transaction marker. Source-path redaction is applied while
constructing the manifest rather than by MCP transport.

Every analysis bundle includes `unique_accepted_kos.tsv` and omits
`normalized_annotations.tsv` and `protein_ko_mapping.tsv`. Bundle schema version 5 does not expose
a retention-mode selector or claim that omitted evidence was evaluated or retained. Use the
normalization bundle for complete normalized records and protein mappings.

Reference and input-handoff writers reuse the same fail-closed publication boundary. They
preflight per-artifact and aggregate UTF-8 bytes, create owner-only regular files without following
links, reject an existing output entry, synchronize file content, and install the manifest last.
Reference report TSV cells protect formula-like content. Mapper/Syntax upload fields instead
preserve caller text verbatim after validation rejects tabs, line breaks, NUL, and other
format-breaking controls. A failed transaction removes only files and a fresh directory proven to
belong to that transaction.

The renderer handoff is a separate typed version 6 service model. It contains sorted unique
accepted K numbers plus complete-within-limit authoritative analysis state; rejected,
unclassified, and invalid records never enter visualization evidence. Its detailed schema and
renderability semantics are owned by
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
- remove TTL-expired orphan rows.

Capacity failure never evicts an unexpired result silently. Normal stdio shutdown deletes the
current scope. Output bundles remain independently operator-owned durable files.

The configured database is operator-owned rather than client-selected. Path validation rejects
traversal, unsafe writable ancestry, symlinks, non-regular targets, and replacement races, and uses
restrictive permissions where supported. Only a database file atomically created by the current
open may initialize the current result-store schema. A preexisting `user_version=0` database or a
database that does not exactly match the current tables, columns, constraints, foreign keys, and
indexes is rejected without schema repair.

## Interpretation and testing

Reports describe annotation evidence, and query or handoff artifacts describe candidate matches,
KEGG relationships, hierarchy membership, mapping quality, or external-workflow inputs. They do not
establish pathway presence, completeness, expression, activity, flux, phenotype, experimental
validation, enrichment, statistical significance, or graph-derived mechanism.

Unit and integration tests use synthetic KEGG responses and temporary stores. Default local tests
make no live KEGG calls. The single governed live compatibility campaign is defined in
`tests/live/README.md` and remains a pull-request CI concern.
