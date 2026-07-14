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

## Plain-KO one-call analysis

`analyze_plain_ko` implements the common KO-list workflow in one service call. Its
`PlainKoAnalysisRequest` supplies inline plain-KO text, bounded import settings, dataset context,
one or more MODULE or pathway targets, KEGG request options, and serializable analysis and report
limits. The caller separately supplies a validated result scope, a typed KEGG reference client,
and a result store.

The retained structured artifact records the one-call service name and version, importer limits,
KEGG refresh and stale-cache options, every reference-loading aggregate limit, direct-result
preview limits, renderer name and version, and the report limits. Module and pathway results retain
their calculation parameters and versions. These values exclude the raw KO payload and local store
path.

The service performs these operations in order:

1. import the inline text as an immutable annotation dataset;
2. load requested MODULE and pathway references through typed KEGG operations;
3. evaluate strict and lenient MODULE evidence and the requested pathway evidence mode;
4. render the three report artifacts;
5. retain the complete artifacts under an opaque scoped result identifier; and
6. return a bounded summary containing import counts, MODULE and pathway previews, artifact
   metadata, expiry information, and conservative interpretation caveats.

The direct result never repeats the caller's raw KO text. Preview counts are explicit, and each
preview reports whether additional targets were omitted from the direct response. Full results
remain in the stored artifacts within configured hard limits.

At least one MODULE or pathway target is required. Target identifiers must be unique in caller
order. A plain KO list contains KO evidence, not organism-gene membership, so the request rejects
organism-specific pathway references even when dataset metadata includes a KEGG organism code.
The general pathway reference loader can represent organism references for other, appropriately
contextualized services, but this one-call service does not turn KO-only input into an
organism-specific claim.

Global or overview pathway evaluation remains an explicit opt-in. Pathway coverage is descriptive
KO coverage and is not pathway presence, completeness, expression, activity, flux, phenotype, or
statistical significance.

## Typed KEGG reference loading

`load_module_graphs` accepts ordered root M numbers and uses typed `get` requests to load each root
and its reachable MODULE references. Retrieval rounds, root count, total entries, and reference
occurrences are bounded, and each typed GET contains at most ten entries. Aggregate KEGG request
count and response bytes are bounded across the complete one-call workflow. A missing requested
root is an error. Missing reachable references,
cycles, parser diagnostics, source hashes, retrieval timestamps, cache state, and KEGG release
metadata when available remain explicit in the resolved graph and its provenance.

`load_pathway_references` accepts ordered `PathwaySpec` values. Each specification binds an exact
pathway identifier to its `ko`, `map`, or organism reference namespace. The loader uses the typed
`PATHWAY_TO_KO` link relationship and a typed pathway `get` request, then delegates denominator and
metadata validation to the Milestone 4 pathway reference builder. It does not infer a namespace
from user intent or silently substitute a different denominator. Aggregate relationship rows,
reference K numbers, exclusions, response bytes, and request count are bounded; a limit failure
stops before the next request whenever the required metric is already known.

The injected client preserves the Milestone 2 access gate, process-wide request rate, batching,
cache, offline behavior, and retrieval provenance. The Milestone 5 default test suite uses
synthetic in-process clients and makes no live KEGG requests.

## Report artifacts

`render_report` is a deterministic in-memory renderer. Every successful render produces exactly
three UTF-8 artifacts in stable order:

| Section | MIME type | Content contract |
| --- | --- | --- |
| `structured` | `application/json` | Canonical complete dataset, analyses, execution parameters, producer and renderer versions, limits, and provenance within the configured JSON byte limit. |
| `summary` | `text/markdown` | Concise evidence-aware report with bounded target, source, warning, and byte previews; truncation is explicit. |
| `annotations` | `text/csv` | Complete flat annotation-record export within the configured CSV byte limit. Nested analysis structures remain in JSON rather than being flattened lossily. |

Each artifact records its exact UTF-8 byte size and SHA-256 digest. JSON serialization is canonical
and rejects non-finite values. The CSV renderer uses a stable column order, embeds nested evidence
and source values as canonical JSON cells, and guards cells that could be interpreted as spreadsheet
formulas. Structured JSON and annotation CSV fail on their configured hard size limits instead of
returning partial content. Only the Markdown preview can be truncated, and its truncation notice
points readers to the complete bounded artifacts.

The renderer can also serialize the Milestone 4 KO-set, MODULE-outcome, and pathway-outcome
comparison contracts when supplied directly through `ReportInput`. The plain-KO one-call service
currently supplies its primary dataset, MODULE evaluations, and pathway coverage results.

Reports are rendered in memory and are written only to the configured result store. Milestone 5
does not accept an arbitrary report destination path.

## Scoped SQLite result store

`SQLiteResultStore` stores immutable artifact groups in an operator-selected local SQLite file.
Its defaults are:

- 24-hour result retention;
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

The store provides bounded operations for:

- listing active results within one scope by `offset` and `limit`;
- listing artifact metadata for one authorized result by `offset` and `limit`;
- reading one artifact by section as byte ranges with `offset`, `limit`, and `next_offset`;
- explicitly deleting one authorized active result; and
- cleaning up expired results and deterministic over-quota evictions.

Pagination and byte-range offsets are validated before SQLite binding. They must be non-negative
and must leave room for the configured page or range limit within SQLite's signed 64-bit integer
range; oversized values fail with a bounded `INPUT_LIMIT_EXCEEDED` error.

Artifact metadata includes section, MIME type, byte size, and SHA-256 digest. Range responses retain
the total size and digest so a caller can reconstruct and verify the immutable artifact without
requiring an unbounded inline response.

## Testing and external access

Milestone 5 unit and integration tests use synthetic KEGG responses and temporary local SQLite
stores. They cover typed reference traversal, missing and malformed responses, report size and
preview limits, conservative report language, cross-scope isolation, expiry, capacity failure,
pagination, byte-range reconstruction, explicit deletion, and the complete plain-KO one-call flow.
The default validation suite makes no live requests and does not require network access.

Live behavior, when used outside the default test suite, is controlled by the injected Milestone 2
client. Public `rest.kegg.jp` access remains restricted to academic use by academic users, must use
the configured process-wide request rate of no more than three requests per second, and must keep
cached KEGG payloads local and out of version control and releases.

## Layer boundary

The Milestone 5 service and storage layer itself does not provide:

- stdio MCP transport, tool registration, tool annotations, or protocol error mapping;
- MCP resource templates, result resource URIs, or client/session transport scope derivation;
- a repository-scoped Codex Skill; or
- arbitrary filesystem report export.

Milestones 6 and 7 now provide the MCP and Skill layers. The MCP implementation calls these public
service and storage interfaces rather than reimplementing normalization, analysis, reporting, or
retention logic.
