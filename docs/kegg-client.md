# KEGG Client and Cache Contract

This document describes the typed KEGG request contracts, access eligibility gate, bounded request
preparation, response parsing, local cache, and retrieval provenance, including the public library
interface used by the independent renderer. It does not describe module evaluation, pathway
coverage, reporting services, MCP transport, or repository-scoped Codex Skills; those implemented
layers are documented separately.

## Official service facts and eligibility

The external facts that affect this contract were retrieved on 2026-07-30. The
[KEGG REST overview](https://www.kegg.jp/kegg/rest/) identifies the official REST service, and the
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html) documents the supported operations,
response forms, status codes, organism-scoped pathway LIST form, FIND query modes, selected-entry
LINK forms, and the limit of ten entries in one `get` request. The
[KEGG PATHWAY database page](https://www.kegg.jp/kegg/pathway.html) defines the supported
reference, organism-specific, virus, and virus-extension pathway identifier forms. The
[KEGG organism catalog](https://www.kegg.jp/kegg/catalog/org_list.html) and the
[Dictyostelium discoideum organism entry](https://www.kegg.jp/kegg-bin/show_organism?org=ddi)
show that `ddi` is a valid organism code even though `ddi` is also a KEGG API operation. The
[KEGG database entry field documentation](https://www.kegg.jp/kegg/docs/dbentry.html) defines the
12-column flat-file field region and its two-column-indented nested fields. The
[KEGG legal notice](https://www.kegg.jp/kegg/legal.html) states that the public KEGG REST service is
for academic use by academic users and describes licensing for other use. The
[NCBI Gene processing documentation](https://www.ncbi.nlm.nih.gov/datasets/docs/v2/data-processing/gene-processing/gene/)
defines GeneID as a stable numeric identifier; this contract therefore accepts only a positive
decimal value after the `ncbi-geneid:` prefix. The
[IUBMB enzyme nomenclature rules](https://iubmb.qmul.ac.uk/enzyme/rules.html) define the four-part
EC number form and the use of a hyphen for a missing classification level.

The project defaults to network-disabled access:

- `offline_cache` is the default, is network-disabled, and selects the public endpoint's cache
  namespace unless an explicitly confirmed licensed namespace is configured.
- `public_academic` requires explicit `academic_use_confirmed=True` and always uses
  `https://rest.kegg.jp`.
- `licensed` requires `authorized_use_confirmed=True`, a caller-supplied authorized HTTPS endpoint,
  and a non-sensitive logical endpoint label.

The readable label is used only in status and provenance. Cache and rate-limit isolation use an
opaque SHA-256 fingerprint of the canonical endpoint; the licensed URL and its fingerprint are not
returned to MCP callers.

These fields record an operator assertion. The project does not determine whether an organization
or activity is academic, does not inspect a license, and does not validate that a caller is legally
authorized to use an endpoint. This documentation is not legal advice.

## Configuration

`KeggClientConfig` is an immutable, strict Pydantic model. Its default is network-disabled cache
access:

```python
from kegg_mcp.kegg.contracts import KeggClientConfig

config = KeggClientConfig()
assert config.access.mode == "offline_cache"
```

Public-academic access is always explicit:

```python
from kegg_mcp.kegg.contracts import KeggClientConfig, PublicAcademicAccess

config = KeggClientConfig(
    access=PublicAcademicAccess(academic_use_confirmed=True),
)
```

Callers can also construct the default network-disabled profile explicitly:

```python
from kegg_mcp.kegg.contracts import KeggClientConfig, OfflineCacheAccess

config = KeggClientConfig(access=OfflineCacheAccess())
```

Every operation under this profile is forced to a cache-only read before cache lookup. The HTTP
transport and deployment-wide limiter are never invoked. Cache misses return
`CACHE_ENTRY_NOT_FOUND`; stale entries still require `allow_stale=True`. The client also forces its
SQLite adapter into read-only mode. A missing database is a cache miss and neither the database nor
its parent is created; cache writes and cleanup fail closed.

Licensed access also requires an explicit assertion. The endpoint must use HTTPS, must not contain
credentials, query parameters, fragments, percent-encoded components, backslashes, or traversal
segments, and must not relabel the official public endpoint as licensed:

```python
import os

from kegg_mcp.kegg.contracts import KeggClientConfig, LicensedAccess

config = KeggClientConfig(
    access=LicensedAccess(
        authorized_use_confirmed=True,
        endpoint=os.environ["KEGG_LICENSED_ENDPOINT"],
        endpoint_label="institutional-kegg",
    ),
)
```

Endpoint authorities are canonicalized before fingerprinting: hostnames are lowercased, a DNS
terminal dot and the default HTTPS port are removed, IP literals are normalized, and invalid ports
are rejected. Equivalent spellings therefore cannot create independent cache or rate-limit
namespaces. A licensed endpoint is trusted operator-controlled startup configuration, not a
per-request value and not a future MCP tool argument. Private or loopback licensed endpoints are
permitted only because this configuration boundary is trusted; callers remain responsible for
authorizing the configured service.

### Request and retry limits

`KeggClientLimits` uses the following defaults:

| Setting | Default | Contract |
| --- | ---: | --- |
| `requests_per_second` | `2.0` | From `1/60` through `3.0`, inclusive |
| `timeout_seconds` | `15.0` | Per-request timeout, at most 120 seconds |
| `max_response_bytes` | `5_000_000` | Checked before and while reading; hard maximum 50,000,000 |
| `max_identifiers` | `100` | Per public operation before batching |
| `relation_batch_size` | `10` | Maximum CONV identifiers per prepared batch |
| `link_batch_size` | `100` | Maximum LINK identifiers per prepared batch; URL packing may lower it |
| `max_url_bytes` | `8_192` | Prepared request bound; FIND/LINK include the endpoint; hard maximum 65,536 |

The readable normalized request key is limited to 65,536 characters, matching the maximum
configured URL bound and the cache key limit. This prevents a request that passed URL preparation
and cache storage from failing later only when provenance is constructed. FIND queries are
separately limited to 4,096 Unicode characters, and percent encoding is accounted for against the
complete URL-byte limit.

Rate limiting is deployment-wide for one endpoint fingerprint. Core and Renderer use the same
owner-only state root beneath `XDG_CACHE_HOME` when set; otherwise the base is `~/.cache` on Linux
and `~/Library/Caches` on macOS. The relative path is `kegg-mcp/rate-limit`, unless
`KEGG_MCP_RATE_LIMIT_ROOT` selects another shared absolute root. Advisory file locks serialize
request starts across processes, starts are spaced uniformly, and idle time does not accumulate
burst capacity. If clients configure different rates for one endpoint and state root, the strictest
observed rate remains in force. The configuration cannot exceed the official maximum of three
requests per second and cannot be lower than one request per minute because the cross-process state
contract retains at most a 60-second interval. The safer project default is two requests per second.
The client always applies this limiter for live access. An optional injected limiter may add
stricter spacing but cannot replace the mandatory deployment-wide limiter. State files are opened
relative to one validated owner-only directory descriptor so replacing the configured path cannot
redirect a pending state-file open.

`RetryPolicy` defaults to two retries after the initial attempt, exponential backoff beginning at
0.5 seconds, an 8-second backoff cap, and up to 0.25 seconds of jitter. Retries remain bounded and
every attempt passes through the deployment-wide rate limiter. Deterministic HTTP 400 and 404 responses
are not retried. Transport timeouts and connection failures may be retried. DNS, permission, TLS,
and other fixed configuration failures are terminal; terminal rate-limit and request failures
remain structured errors.

The HTTPS transport is GET-only. It does not read process proxy settings or follow redirects, asks
for identity content encoding, applies the response-size bound, and retains only `content-type`,
`date`, `etag`, and `last-modified` response headers. Its stable User-Agent is only
`kegg-mcp/<package-version>`; it contains no project URL, username, hostname, email address, path,
or environment value.

## Typed operations

The client does not expose an arbitrary URL fetcher. All URL components come from strict request
models and fixed operation mappings.

### INFO

`InfoRequest` accepts the bounded `KeggInfoDatabase` allowlist: `kegg`, `pathway`, `brite`, `module`,
`ko`, `genes`, `genome`, `compound`, `glycan`, `reaction`, `rclass`, `enzyme`, and `drug`. The
parser retains every non-empty line and conservatively extracts a release string, entry count, and
linked database names when the document supplies an unambiguous form.

### LIST

`OrganismPathwayListRequest` exposes exactly one bounded LIST form for the pathway directory of one
canonical three- or four-letter KEGG organism code. It prepares
`/list/pathway/<organism>`; callers cannot select another database, rank, group, URL, or
whole-database listing.

The wire parser accepts only an ordered two-column tab table whose identifiers use the official
simplified organism-specific `<organism>NNNNN` form and whose names are non-empty bounded UTF-8
text. Typed rows normalize those identifiers to `path:<organism>NNNNN`. An empty successful
response remains an empty directory. The complete response stays under the configured response-byte
limit. The organism resolver uses the row count and a bounded preview; it does not infer pathway
presence, completeness, activity, or phenotype from directory availability.

### FIND

`FindRequest` exposes one bounded candidate search, not an arbitrary database query. Keyword mode
accepts only `ko`, `pathway`, `module`, `reaction`, `enzyme`, `compound`, `glycan`, `drug`,
`rclass`, and `genome`; the high-level `organism` alias uses `genome` on the wire. Formula,
exact-mass, and molecular-weight modes are valid only for `compound` or `drug` and use the official
`formula`, `exact_mass`, and `mol_weight` wire options. Formula and mass values use strict bounded
syntax, including an ordered numeric range for the two mass modes.

The query must be non-empty valid UTF-8 without outer whitespace or control characters. Slash,
backslash, question-mark, fragment, and dot-segment path forms are rejected at the typed boundary.
Spaces, plus signs, percent signs, and non-ASCII text are then encoded as one canonical uppercase
percent-encoded UTF-8 path segment. The HTTPS transport decodes that segment strictly for
validation and rejects malformed escapes, encoded structural delimiters, controls, and traversal.
FIND has no low-level
`max_results` URL parameter: the transport reads one response under the configured
5,000,000-byte default, the parser retains its complete ordered rows, and the calling service
applies its own smaller result-preview bound.

On 2026-07-31, the public FIND endpoint returned a well-formed empty response for the known
`RC00002` identifier and its definition fragments. RCLASS therefore remains an allowlisted typed
FIND database, but callers must treat zero candidates as a valid upstream result and must not
promise positive RCLASS keyword discovery. Use selected RCLASS GET when an identifier is already
known.

The internal gene resolver may use the additional typed `genes` FIND database with an explicit
canonical three- or four-letter organism code. That scope becomes the wire database, for example
`/find/hsa/...`, and every returned canonical KEGG gene identifier must have the same organism
prefix before the response is cached. The generic public search operation does not expose this
internal gene-search route.

Each FIND row retains the database-validated simplified KEGG identifier and raw matched text
returned by KEGG. Gene FIND rows retain their organism-qualified gene identifier. Higher-level
services convert those source identifiers into typed `KeggEntityRef` values. Rows are candidates
only. A keyword, formula, exact-mass, or molecular-weight match does not establish a unique identity,
annotation, biological role, or experimental validation, and the client does not manufacture a
relevance score or select a best match.

### GET

`GetRequest` contains ordered, unique `KeggEntryRef` values for `ko`, `module`, `pathway`,
`reaction`, `enzyme`, `compound`, `glycan`, `drug`, `rclass`, `brite`, `gene`, or `genome`. Each
identifier is checked against its selected database. A gene entry requires a canonical
database-qualified KEGG gene identifier such as `hsa:10458`. A genome entry accepts either a T
number or a canonical organism code and is sent in the qualified `gn:<identifier>` form. Pathway
identifiers accept the fixed `map`, `ko`, `ec`, `rn`, `vg`, and `vx` prefixes or a three- or
four-letter organism code. The configured total identifier limit is enforced before preparation.
Enzyme identifiers require an EC class from 1 through 7 and four dot-separated elements. A partial
EC number may replace only a continuous trailing sequence of elements with a single `-` per
element.

The project does not bundle or mirror the KEGG organism catalog. Organism-code validation checks
the documented three- or four-letter wire syntax and excludes fixed database prefixes that are
ambiguous in the same identifier position; it does not assert current catalog membership. API
operation names are not globally reserved because valid collisions such as `ddi` exist. Entry
existence remains an endpoint response decision.

Non-BRITE entries are sent in batches of at most ten, regardless of broader configuration.
Potential genome code/T-number aliases and gene identifiers with the same suffix under different
prefixes are isolated into separate batches so one flat-file entry cannot satisfy two requested
references. BRITE hierarchy htext entries are isolated into one request each because their response
format differs.
The caller must set `brite_kind=KeggBriteEntryKind.HIERARCHY`; BRITE HTML table files are not
supported because their identifier syntax does not distinguish them safely from
hierarchy files. Misclassified content fails parsing rather than being returned as hierarchy data.
The result echoes the typed request; documents and provenance follow prepared batch order, while
explicit missing entries follow caller request order.

KEGG gene flat files return an unqualified `ENTRY` value, and a genome requested by organism code
returns its T number in `ENTRY`. Before caching, the client therefore reconciles gene results
against `ENTRY` plus `ORGANISM`, and genome aliases against `ENTRY` plus `ORG_CODE`. Each returned
entry must match exactly one requested typed reference. The same reconciliation controls result
ordering and entry-level cache splitting; ambiguous, duplicate, or cross-organism content fails
closed.

### Pathway assets

The implemented renderer integration provides a typed single-pathway asset interface reviewed
against the official KEGG API manual on 2026-07-30. `PathwayAssetRequest` accepts one canonical
pathway identifier and exactly one fixed kind: `image`, `image2x`, or `kgml`. It cannot accept a URL.
`KeggClient.get_pathway_asset` reuses the same access gate, endpoint-scoped no-burst limiter,
retry policy, HTTPS transport, response-size bound, local cache, and retrieval provenance as the
text operations.

PNG responses require a valid signature, bounded dimensions, pixel and decompressed-scanline
counts, canonical critical-chunk ordering, valid CRC values, and one complete zlib IDAT stream whose
scanlines match the IHDR contract. The core applies only bounded UTF-8, declaration-policy, and
obvious `pathway` root-prefix preflight checks to KGML bytes; it does not parse XML, require a
well-formed complete document, or assert pathway identity. KGML may omit a DOCTYPE or contain the
single inert KEGG KGML v0.7.2 HTTPS `SYSTEM` declaration observed on 2026-07-30 in the XML prolog.
Other DTD declarations and all entity declarations are rejected, and the accepted system identifier
is never resolved or fetched. The independent renderer owns bounded XML structure parsing,
pathway-identity checks, and PNG/KGML dimension compatibility. Cached assets are validator-versioned
and revalidated before use. This public library interface supports the separately installed renderer
and is intentionally not exposed as a core MCP tool.

### LINK

`LinkRequest` supports only these directions:

- KO to pathway, MODULE, reaction, enzyme, BRITE, or an explicitly scoped organism gene;
- pathway to KO, MODULE, reaction, compound, or glycan;
- an organism-specific pathway to a gene in the same explicitly scoped organism;
- gene to KO or pathway;
- enzyme to reaction;
- reaction to enzyme, KO, compound, glycan, or pathway;
- compound to reaction or pathway;
- glycan to reaction or pathway;
- drug to pathway;
- MODULE to KO, pathway, or reaction;
- genome to taxonomy; and
- taxonomy to genome.

These 30 directions are defined by one authoritative relation contract. It binds each direction to
one source identifier kind, one fixed wire formatter, the exact source namespace expected in the
response, and one target namespace validator. Source identifiers must match the selected direction
and must be unique. Gene sources use canonical KEGG gene identifiers; enzyme sources use
caller-facing EC numbers that are qualified with `ec:` on the wire; reaction and compound sources
use R and C numbers; glycan, drug, and MODULE sources use G, D, and M numbers; genome sources
accept an organism code or T number and are qualified with `gn:`; taxonomy sources require
`taxid:<positive integer>`. Taxonomy-to-genome targets may be an organism-code or T-number form
under the fixed `gn:` namespace.

`LinkRequest.taxonomy_rank` accepts `exact`, `species`, `genus`, `family`, `order`, `class`, or
`phylum` and defaults to `exact`. Only taxonomy-to-genome accepts a non-default value. Exact lookup
uses `/link/genome/<taxid>`; the other ranks append their fixed rank suffix, which participates in
URL-size validation and the cache key. The client never retries an empty narrower result at a
broader rank automatically. On 2026-07-30, the official endpoint returned an empty exact result for
`taxid:562` and multiple strain genomes for its species-ranked request, so callers must select the
intended taxonomy semantics explicitly.

KO-to-gene and pathway-to-gene require `organism_scope`; the wire target database is that canonical
organism code rather than the global `genes` collection, and response targets must use the same
prefix. The public contract therefore never expands one KO into all KEGG genes.

Selected-entry reaction-to-RCLASS or RCLASS-to-reaction LINK directions are not exposed. Live
public endpoint probes on 2026-07-30 did not establish a non-empty, selected-entry response shape
even though database-wide forms could return rows, and database-wide expansion is outside this
client. RMODULE was also omitted because its INFO/GET/LIST/LINK behavior did not establish one
consistent typed selected-entry contract in the same review. RCLASS remains available through
typed INFO, FIND, and GET, subject to the documented public FIND empty-result observation; RMODULE
is not a public client database.

Every LINK call remains selected-entry and bounded; database-to-database expansion is not exposed.
Preparation canonicalizes identifier order and greedily packs the largest next batch that satisfies
the configured identifier count, LINK-batch ceiling, and complete URL-byte limit. The transport
independently enforces the response-byte limit. Thus 73 ordinary K numbers fit in one default
KO-to-pathway request, while longer identifiers split deterministically when the URL boundary
requires it. Equivalent identifier sets produce the same batches and cache keys regardless of
caller order; raw response-row order is preserved. Successful response rows are checked before
caching: every source must belong to its prepared batch and every target must match the selected
relationship namespace. An empty successful response, including one for a syntactically valid
genome T-number source, remains an empty relation result and is not interpreted as absence.

### CONV

`ConvRequest` converts only an explicitly supplied, bounded identifier set. Gene conversion links
KEGG genes with `ncbi-geneid`, `ncbi-proteinid`, or `uniprot`; substance conversion links KEGG
compound, glycan, or drug entries with ChEBI or PubChem SID. The wire database name `pubchem`
denotes SID in this contract and never accepts or implies a PubChem CID. Both source and target
databases are typed, and source identifiers must include the matching namespace prefix. KEGG gene
identifiers may use a three- or four-letter organism code, an official T number, or the bounded
KEGG GENES collection prefixes `ag`, `vg`, and `vp`. Response sources and target namespaces are
reconciled before caching. Whole-database conversion is not exposed. Organism-scoped candidate
gene discovery uses the separate typed FIND contract and does not widen CONV.

Other `LIST` forms, arbitrary database pairs, arbitrary endpoint paths, and operations whose
contract is whole-database enumeration are not public client operations. The one
organism-pathway LIST form is not a generic listing API, and typed FIND is a bounded candidate
search rather than a replacement for such an API. The client does not download, mirror, package,
or redistribute a KEGG database. Whole-database CONV and LINK forms remain unavailable.

## Text parsing

All response parsers require strict UTF-8 and reject unsupported control characters. They do not
silently reinterpret malformed output:

- INFO output must be non-empty and start with the requested database header. The explicit
  `linked db` block, including its `<org>` placeholder, is parsed separately from database
  statistics. Unknown non-empty lines remain available even when no known metadata can be
  extracted.
- Organism-pathway LIST output is an ordered two-column tab table. Each identifier must be an
  organism-specific pathway for the exact requested code, each name must be non-empty, and an
  empty successful response is a valid empty directory.
- FIND output is an ordered two-column tab table containing a typed candidate identifier and raw
  matched text. An empty successful response is a valid empty candidate document. The identifier
  must match the requested database, and an organism-scoped gene response must match the requested
  organism before caching.
- LINK and CONV output is an ordered two-column tab table. An empty successful response is a valid
  empty document. Non-empty rows must contain exactly two non-empty identifiers. Row order and
  duplicates are preserved.
- Standard GET flat files use the 12-character field-name region and preserve whether each field
  is top-level or uses the documented two-column nested indentation. Continuation lines, repeated
  fields, and unknown uppercase fields are retained. An unindented `ENTRY` field and the `///`
  terminator are required. A whitespace-only successful body represents zero returned entries;
  malformed indentation and unterminated content are errors.
- BRITE hierarchy htext preserves ordered lines and requires a documented root `A` hierarchy
  level; metadata headers such as `+C`, `+D`, or `+E` are retained but not assumed to have one
  fixed depth. A blank successful body remains empty so the client can reconcile it against the
  requested entry. BRITE HTML tables are intentionally not parsed by this contract.

Parsing a response is separate from interpreting biology. A missing entry, a source-rejected
annotation, or an empty LINK result must not be presented as experimental evidence that a function
is absent. Every endpoint-returned name, definition, hierarchy label, equation, reference, raw
match, and other payload string remains untrusted database data; it is never an instruction to an
LLM, MCP client, parser, or service.

## Local SQLite cache

The default cache is `kegg-mcp/kegg.sqlite3` beneath `XDG_CACHE_HOME` when set. Without that
override, its base is `~/.cache` on Linux and `~/Library/Caches` on macOS. Newly created cache
directories and files are tightened to user-only permissions where the platform permits. Cache
payloads are local runtime data and must remain outside version control, packages, examples, test
artifacts, and releases.

An explicitly configured cache path must resolve from an absolute path and must not contain lexical
`..` traversal. Existing parent or final symlinks, non-directory parent components, non-regular
cache files, parents not owned by the current user, and group- or world-writable final parents are
rejected. Newly created parent directories use user-only permissions, and the cache file is opened
without following a final symlink where the platform supports it and tightened to user read/write
permissions. Storage failures return a redacted `CACHE_FAILED` error rather than exposing the local
path.

Each cache key includes:

- operation;
- normalized request key;
- retrieval endpoint class; and
- the SHA-256 fingerprint of the canonical endpoint.

The readable endpoint label is not a cache identity. Therefore equivalent canonical endpoints
share a cache namespace even when their labels differ, while distinct licensed endpoints remain
isolated even when an operator gives them the same label. Neither the endpoint nor its label is
stored in a cache key.

Each stored parsed response includes its normalized bytes, retrieval and expiry times, parser
version, KEGG release when known, and allowlisted HTTP metadata. A typed GET 404 is normalized to
an empty document so the requested entries remain explicit in `missing_entries` with network
provenance. Reads verify the schema, timestamp ordering, parser version, metadata shape, and parser
compatibility before returning a payload. A malformed row is `CACHE_FAILED`, never a cache miss or
biological absence.

Multi-entry flat-file GET responses are also split into entry-level cache records after successful
parsing and identifier reconciliation. Returned entries retain their parsed bytes; omitted entries
receive bounded empty negative-cache records for the same TTL. A later single-entry GET or cached
subset is reconstructed from those records. For a partially cached ordered request, only contiguous
cache misses are sent to KEGG, while cached entries and returned flat-file entries are merged back
into caller order. The live request still obeys KEGG's maximum of ten GET entries.

The response parser contract version is `4`. Normalized request keys are readable canonical
unprefixed request paths and are bounded to 65,536 characters. LINK keys are produced after
adaptive packing; FIND keys contain the canonical percent-encoded query segment. The parser records
nested flat-file field indentation, accepts documented BRITE root forms only within a complete
htext metadata envelope, and applies the current identifier reconciliation rules before cache use.
Cache rows produced under an incompatible parser version fail closed instead of being silently
reinterpreted. A cache created for a different schema is incompatible and should be replaced rather
than migrated implicitly.

`CachePolicy` defaults to a seven-day TTL, 10,000 rows, 512 MiB of response payloads, and a
640 MiB SQLite main-database limit. Before each write, expired rows are removed. Fresh rows are
never silently evicted to satisfy a quota; a write fails safely when active content would exceed a
logical or physical bound. The database uses bounded page allocation and controlled vacuuming.
Operators can inspect or remove expired cache rows without exposing paths, endpoints, or payloads:

```bash
kegg-mcp cache status --json
kegg-mcp cache cleanup --expired --json
```

When the configured database does not exist, status and expired-row cleanup return zero counts
without creating the database or its parent directory.

An `offline_cache` client opens only an existing owner-controlled database through a validated
file descriptor, enables query-only and untrusted-schema protections, and requires an owner-only
`0600` regular file beneath a safe owner-controlled parent. Linux uses SQLite `mode=ro`; Darwin
holds a non-blocking POSIX shared lock on the descriptor for the connection lifetime and uses
SQLite's read-only immutable descriptor URI to avoid path-derived locking and sidecar access on
the descriptor filesystem. Cache connections are serialized inside the process so closing another
SQLite descriptor cannot release the process-scoped POSIX lock while an immutable reader is active.
Operators must not bypass SQLite locking to modify that database while an offline deployment uses
it. The client validates the schema version,
auto-vacuum mode, journal mode, parser metadata, and configured logical and physical size bounds
before serving a row. It does not initialize or migrate a database and cannot write, clean up, or
fall back to HTTP. Operators must populate or refresh the selected public-academic or confirmed
licensed endpoint namespace in a separately authorized live deployment.

The Darwin descriptor-open behavior was reviewed on 2026-08-01 against SQLite's
[URI filename contract](https://www.sqlite.org/uri.html) and its official
[file-descriptor-only discussion](https://sqlite.org/forum/forumpost/c15bf2e7df289a5f).

At lookup time:

- a response is fresh only while `now < expires_at`;
- `refresh=False` permits a fresh cache hit;
- `refresh=True` is the default and bypasses even a fresh hit;
- a stale response is served only when `allow_stale=True`;
- `cache_only=True` requires `refresh=False`, never calls the HTTP transport, and returns
  `CACHE_ENTRY_NOT_FOUND` on a miss or disallowed stale entry.

The high-level KEGG retrieval, query, and audit services supply `refresh=False` when no explicit
options are provided, making repeated MCP calls fresh-cache first. In an explicitly configured
live deployment, `KeggRequestOptions(refresh=True)` still forces a bounded live refresh. Offline
mode never invokes the transport regardless of refresh intent.

Network responses are parsed and reconciled with their typed request before being committed to the
cache. The active response-size bound is rechecked for both injected transports and cached payloads,
including data written under an older, larger limit. Cache write or integrity failures are surfaced
rather than converting a failed retrieval into an apparently valid result.

## Result provenance

Every result echoes its typed request and records provenance per HTTP/cache batch. The provenance
contains:

- operation and the readable normalized request key;
- configured access mode, original retrieval endpoint class, and endpoint label;
- response origin (`network` or `cache`) and cache lookup state;
- retrieval, serving, and expiry timestamps;
- response byte count;
- parser name and parser version;
- KEGG database release when known;
- allowlisted HTTP metadata;
- network attempt count; and
- an explicit stale flag.

Endpoint URLs, credentials, environment values, usernames, and complete local cache paths are not
part of result provenance.

## Error semantics

Invalid strict Pydantic configuration or request fields fail model validation before request
preparation. Operational failures use structured `KeggMcpError` details with a stable error code,
safe details, and a suggested action:

| Code | Meaning |
| --- | --- |
| `INPUT_LIMIT_EXCEEDED` | Identifier count or prepared request size exceeds a configured bound. |
| `CACHE_ENTRY_NOT_FOUND` | No permitted cached response can satisfy an explicit cache-only read. |
| `KEGG_ENTRY_NOT_FOUND` | A non-GET endpoint returned a deterministic not-found response. |
| `KEGG_RATE_LIMITED` | Bounded retries ended with a rate-limit response. |
| `KEGG_REQUEST_FAILED` | A deterministic request error or exhausted transport retry could not produce a response. |
| `KEGG_PARSE_FAILED` | Successful response bytes do not conform to the selected strict parser. |
| `CACHE_FAILED` | Cache I/O, schema, metadata, or parser-version validation failed. |
| `LOCAL_STATE_FAILED` | The private deployment-wide rate-limit state could not be read or written safely. |

HTTP 400 and 404 responses are not retried. A GET 404 becomes an empty typed batch with provenance,
and a multi-entry GET may return only some requested entries; both cases are represented by
`missing_entries` rather than fabricated records. Other endpoint 404 responses remain
`KEGG_ENTRY_NOT_FOUND`. A successful FIND may return no candidates, and a successful empty LINK or
CONV response remains an empty mapping result. Callers must preserve these distinctions and must
not convert cache, transport, or parser errors into evidence of biological absence.

## Minimal client example

The following example performs one typed GET operation. Use this access configuration only after
the operator has independently determined that both the user and the intended use qualify for the
public academic service. Setting `academic_use_confirmed=True` records that operator decision; it
does not make an ineligible use eligible:

```python
from kegg_mcp.kegg.client import KeggClient
from kegg_mcp.kegg.contracts import (
    GetRequest,
    KeggClientConfig,
    KeggEntryRef,
    KeggGetDatabase,
    PublicAcademicAccess,
)

client = KeggClient(
    KeggClientConfig(
        access=PublicAcademicAccess(academic_use_confirmed=True),
    )
)
request = GetRequest(
    entries=(
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00001"),
        KeggEntryRef(database=KeggGetDatabase.KO, identifier="K00002"),
    )
)

result = client.get(request)
print([entry.identifier for entry in result.missing_entries])
```

The implemented service orchestration and MCP tools consume these contracts through the public
client/service layer rather than duplicating request normalization, cache policy, or parsing
behavior.

## Test policy

Local pytest skips the governed live compatibility campaign by default. Its authoritative request
matrix, limits, CI behavior, and opt-in local command are defined in
[the live-test guide](../tests/live/README.md). Other unit and integration network behavior uses
injected transports or local mock servers.
