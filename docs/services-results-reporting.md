# Milestone 5 Services, Result Storage, and Reporting

Status as of 2026-07-14: the service orchestration, typed reference loading, bounded report
rendering, and scoped local result storage described here are implemented. Milestone 5 does not
implement MCP transport, MCP resource URIs, or the repository-scoped Codex Skill.

## Layer boundaries

The Milestone 5 packages remain independent of MCP transport:

- `kegg_mcp.services` composes existing importer, KEGG client, analysis, reporting, and storage
  interfaces;
- `kegg_mcp.reporting` renders already-computed evidence and analyses without retrieving KEGG data
  or writing files; and
- `SQLiteResultStore` retains immutable artifacts under an explicit local scope without defining a
  client-facing resource URI.

The service layer does not duplicate importer or analysis rules. It accepts an injected typed KEGG
client and calls the public Milestone 1 through 4 interfaces.

## High-level annotation analysis

`analyze_ko_annotations` is the supported one-call workflow for a KO list or annotation table. It
normalizes evidence once, optionally selects bounded MODULE and canonical pathway targets, loads
requested references, evaluates MODULE and pathway targets, and writes one transactional output
bundle when an output directory is supplied. The narrower normalization, mapping, MODULE, pathway,
and comparison tools reuse the same public service functions without a second legacy orchestration
layer.

Ordinary MCP inputs expose only choices that change the requested analysis. Cache policy,
reference budgets, result retention, and report bounds remain deployment-owned. An opaque result
identifier is a same-process pagination aid; the versioned output bundle is the durable handoff.
Pathway coverage remains descriptive and never establishes pathway presence, completeness,
expression, activity, flux, phenotype, or statistical significance.

## Typed KEGG reference loading

`load_module_graphs` accepts ordered root M numbers and uses typed `get` requests to load each root
and its reachable MODULE references. Retrieval rounds, root count, total entries, and reference
occurrences are bounded, and each typed GET contains at most ten entries. Aggregate KEGG request
count and response bytes are bounded across the complete one-call workflow. A missing requested
root is an error. Missing reachable references,
cycles, parser diagnostics, exact source definitions, retrieval timestamps, cache state, and KEGG
release metadata when available remain explicit in the resolved graph and its provenance.

`load_pathway_references` accepts ordered `PathwaySpec` values. Each specification infers and
validates its namespace from the pathway identifier; omitted `map` input is canonicalized to the
default `ko` reference view, and paired views with the same pathway number are stably deduplicated
with the KO reference preferred. The loader uses the typed
`PATHWAY_TO_KO` link relationship and a typed pathway `get` request, then delegates denominator and
metadata validation to the Milestone 4 pathway reference builder. Aggregate relationship rows,
reference K numbers, exclusions, response bytes, and request count are bounded; a limit failure
stops before the next request whenever the required metric is already known.

The injected client preserves the Milestone 2 access gate, deployment-wide request rate, batching,
cache-only behavior, and retrieval provenance. Milestone 5 tests use synthetic in-process clients;
the opt-in live suite is enabled by pull-request CI for the bounded compatibility campaign.

## Server-side MODULE and pathway ranking and Top-N selection

When no MODULE or pathway target and no explicit selection are supplied, the high-level service
independently selects the Top-5 MODULEs and Top-5 canonical KO reference pathways. Both ranking
policies use the unique K numbers selected by the strict or lenient evidence mode, sort by
descending selected-KO overlap, and use the canonical target identifier as the stable tie-breaker.
MODULE ranking selects which definitions to load. It is not MODULE completion or enrichment;
exact completion and required-block coverage are evaluated separately after reference loading.

The high-level annotation service accepts an optional `PathwaySelection` containing only `top_n`.
It derives the strict or lenient selected KO set once, maps those canonical unique K numbers to
pathways, and applies the versioned `selected_unique_ko_count` ranking policy. A K number contributes
at most one detected node to one canonical pathway number, regardless of duplicate annotation
records, duplicate LINK rows, or paired `ko`/`map` relationships. Candidates sort by descending
detected unique-KO count and then ascending canonical `koNNNNN` identifier.

Pathway ranking occurs before `load_pathway_references`. `top_n` is bounded from 1 through 25 and must not
exceed the deployment-owned `max_pathway_specs`; a large candidate set therefore does not trigger
the pathway-reference target limit when only Top-N references are requested. Explicit pathways
retain their previous behavior when no selection is supplied.

The complete rankings and normalized KO-to-target relationships are retained in scoped JSON
artifacts and, when an output directory is supplied, in `module_ranking.tsv`,
`ko_module_relationships.tsv`, `pathway_ranking.tsv`, and `ko_pathway_relationships.tsv` as
applicable. The report and manifest record the evidence mode, decision policy, ranking
method/version, candidate count, selected identifiers, and compact request/cache summary. The
direct response returns only aggregate counts and bounded selected-target rows without complete
detected-KO lists.

Analysis tools no longer share one provenance-heavy direct-result model. A small shared summary
holds record, KO, request/cache, warning, and caveat fields, while three tool-specific result models
expose only their relevant MODULE and/or pathway previews. Automatic MODULE/pathway selection and
output bundle metadata occur only on the high-level model. Full annotation provenance, KEGG batch
provenance, execution parameters, and stage metrics are retained rather than repeated directly.

The authoritative high-level `structured` artifact contains six fixed `StageMetric` rows:
`annotation_import`, `ko_target_mapping`, `target_ranking`, `reference_loading`, `analysis`, and
`bundle_write`, together with complete mapping provenance. Each row uses integer elapsed
milliseconds and sanitized logical request, actual network-attempt, cache-hit, and response-byte
counts. Narrow MODULE and pathway services retain the same execution and provenance classes in
their `detail` artifacts. Endpoint URLs, credentials, environment values, usernames, and private
cache paths are absent.

## Report artifacts

`render_report` is a deterministic in-memory renderer. Every successful render produces exactly
three UTF-8 artifacts in stable order:

| Section | MIME type | Content contract |
| --- | --- | --- |
| `structured` | `application/json` | Canonical complete dataset, analyses, execution parameters, producer and renderer versions, limits, and provenance within the configured JSON byte limit. |
| `summary` | `text/markdown` | Concise evidence-aware report with bounded target, source, warning, and byte previews; truncation is explicit. |
| `annotations` | `text/csv` | Complete flat annotation-record export within the configured CSV byte limit. Nested analysis structures remain in JSON rather than being flattened lossily. |

Each artifact records its exact UTF-8 byte size. JSON serialization is canonical and rejects
non-finite values. The CSV renderer uses a stable column order, embeds nested evidence
and source values as canonical JSON cells, and guards cells that could be interpreted as spreadsheet
formulas. Structured JSON and annotation CSV fail on their configured hard size limits instead of
returning partial content. Only the Markdown preview can be truncated, and its truncation notice
points readers to the complete bounded artifacts.

The renderer can also serialize the Milestone 4 KO-set, MODULE-outcome, and pathway-outcome
comparison contracts when supplied directly through `ReportInput`. The plain-KO one-call service
currently supplies its primary dataset, MODULE evaluations, and pathway coverage results.

Reports are rendered in memory. The MCP high-level workflow may additionally write a concise bundle
to an allowed-root `output_directory`: normalized annotations, protein-to-KO mapping, MODULE and
pathway tables, optional full MODULE/pathway ranking and relationship tables, Markdown report,
canonical renderer input, and a versioned manifest. Every returned bundle file carries MIME type,
exact byte size, and controlled absolute path. Bundle schema version `3` requires a new or empty
directory,
never replaces an existing entry, and installs the manifest last as the commit marker. Safe atomic
file writes reject symlinks and report dedicated already-exists or output-write errors. The
manifest uses redacted source labels by default; absolute source paths require an explicit
`manifest_path_mode="absolute"` request.

`render_input.json` now uses the public, transport-independent `RenderInput` contract. Renderer
schema version `3` is independent of output-bundle schema version `3`; the bundle manifest records
the renderer schema version and
`application/vnd.kegg-mcp.render-input+json;version=3` MIME type explicitly. The handoff contains
separate accepted and policy-defined uncertain KO sets, complete pathway detected-KO evidence when
it fits the renderer limit, resolved MODULE definitions and ASTs, exact strict and lenient
completion summaries, and complete required-block and optional-component states when renderable.
Rejected, unclassified, and invalid records remain in summary counts but never enter visualization
evidence.

The bundle writer receives the same loaded MODULE graphs, pathway references, analysis results,
and execution provenance used by the core calculation. It validates their identities before any
file is written. MODULEs or pathways that exceed per-target renderer limits are retained only as
explicit `not_renderable` summaries; no preview is relabeled as complete evidence. Target counts,
evidence identifiers, and canonical serialized bytes are bounded. A total renderer-input byte
failure raises `OUTPUT_LIMIT_EXCEEDED` before the output directory is created, so it cannot leave a
partial renderer handoff.

Analysis execution provenance uses schema and service version `3`. In addition to import, KEGG
request, reference-loading, and direct-result limits, it records the effective MODULE analysis
limits, MODULE-ranking provenance when applicable, pathway evidence and ranking parameters,
pathway coverage limits, and report limits. The renderer carries this provenance forward but does
not reinterpret or recompute it.

## Scoped SQLite result store

`SQLiteResultStore` stores immutable artifact groups in an operator-selected local SQLite file.
Its defaults are:

- a 24-hour hard TTL for every active result and for abnormal-exit orphan cleanup eligibility;
- a 512 MiB logical artifact-payload quota;
- a 640 MiB persistent SQLite main-database page limit;
- at most 10,000 retained results, including results with empty artifacts;
- 50 metadata items per page, with a maximum of 256;
- 64 KiB artifact byte ranges, with a maximum of 1 MiB; and
- atomic result creation and explicit deletion.

All defaults and per-result limits are represented by `ResultStoreLimits` and remain subject to
hard maxima. Before creation, the store removes expired results and checks logical-byte,
physical-page, and result-count capacity. Insufficient active-result capacity fails safely without
evicting any unexpired result, including results owned by another scope. Explicit operator cleanup
can deterministically evict complete oldest results only when an existing database is above newly
configured limits. The store never partially stores or partially deletes one result.

The logical quota counts immutable artifact payload bytes. SQLite table and index overhead count
against the separate main-database page limit. FULL auto-vacuum releases free main-database pages
after deletion. SQLite's DELETE-mode rollback journal is transient and can require additional
temporary space during a transaction; the main-database page limit is not a claim that this
short-lived journal allocation is zero.

Every lookup requires both the opaque `result_id` and the same `scope_id` used at creation. Scope
identifiers, result identifiers, and artifact section names use restricted syntax that rejects path
separators and traversal forms. Public result metadata intentionally omits the scope and database
path. An invalid, unknown, expired, deleted, or cross-scope lookup returns the same safe
`RESULT_NOT_FOUND` error and does not reveal whether a result exists in another scope.

The configured database location is operator-owned configuration rather than a client-supplied
artifact path. Existing parent or final symlinks, lexical `..` traversal, unsafe writable parent
directories, non-regular targets, and open-time inode replacement are rejected. Newly created
directories and the database use restrictive permissions where POSIX permissions are available.

The store exposes current-scope bulk deletion for transport lifecycle integration. The stdio
server calls it during normal shutdown, so a result identifier is a session optimization rather
than a durable handle. `cleanup_expired` removes only TTL-expired rows left by active or abnormal
sessions and never performs quota eviction; output bundles remain independently operator-owned.

The store provides bounded operations for:

- listing active results within one scope by `offset` and `limit`;
- listing artifact metadata for one authorized result by `offset` and `limit`;
- reading one artifact by section as byte ranges with `offset`, `limit`, and `next_offset`;
- explicitly deleting one authorized active result; and
- deleting every result in one authorized scope;
- cleaning up expired results without evicting active rows; and
- deterministic over-quota cleanup through the separate maintenance operation.

Pagination and byte-range offsets are validated before SQLite binding. They must be non-negative
and must leave room for the configured page or range limit within SQLite's signed 64-bit integer
range; oversized values fail with a bounded `INPUT_LIMIT_EXCEEDED` error.

Artifact metadata includes section, MIME type, and byte size. Range responses retain total size and
ordered offsets so a caller can reconstruct content without requiring an unbounded inline response;
the reconstructed content is validated with its declared parser or schema.

## Testing and external access

Milestone 5 unit and integration tests use synthetic KEGG responses and temporary local SQLite
stores. They cover typed reference traversal, missing and malformed responses, report size and
preview limits, conservative report language, cross-scope isolation, expiry, capacity failure,
pagination, byte-range reconstruction, explicit deletion, and the complete plain-KO one-call flow.
The default local validation suite skips live KEGG calls. Pull-request CI explicitly enables the
bounded 120-request compatibility campaign.

Live behavior is controlled by the injected Milestone 2 client. Public `rest.kegg.jp` access
remains restricted to academic use by academic users, must use the configured deployment-wide
request rate of no more than three requests per second, and must keep cached KEGG payloads local
and out of version control and releases.

## Layer boundary

The Milestone 5 service and storage layer itself does not provide:

- stdio MCP transport, tool registration, tool annotations, or protocol error mapping;
- MCP resource templates, result resource URIs, or client/session transport scope derivation;
- a repository-scoped Codex Skill; or
- unrestricted filesystem report export.

Milestones 6 and 7 now provide the MCP and Skill layers. The MCP implementation calls these public
service and storage interfaces rather than reimplementing normalization, analysis, reporting, or
retention logic.
