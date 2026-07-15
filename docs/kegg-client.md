# KEGG Client and Cache Contract

Status: Milestone 2 client-layer contract as implemented on 2026-07-14.

This document describes the typed KEGG request contracts, access eligibility gate, bounded request
preparation, response parsing, local cache, and retrieval provenance. It does not describe module
evaluation, pathway coverage, reporting services, MCP transport, or the repository-scoped Codex
Skill; those implemented layers are documented separately.

## Official service facts and eligibility

The external facts that affect this contract were retrieved on 2026-07-14. The
[KEGG REST overview](https://www.kegg.jp/kegg/rest/) identifies the official REST service, and the
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html) documents the supported operations,
response forms, status codes, and the limit of ten entries in one `get` request. The
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

The project defaults to public-academic live access:

- `public_academic` defaults `academic_use_confirmed=True` and always uses
  `https://rest.kegg.jp`.
- `licensed` requires `authorized_use_confirmed=True`, a caller-supplied authorized HTTPS endpoint,
  and a non-sensitive logical endpoint label.

These fields record an operator assertion. The project does not determine whether an organization
or activity is academic, does not inspect a license, and does not validate that a caller is legally
authorized to use an endpoint. This documentation is not legal advice.

## Configuration

`KeggClientConfig` is an immutable, strict Pydantic model. Its default is public-academic network
access:

```python
from kegg_mcp.kegg.contracts import KeggClientConfig

config = KeggClientConfig()
assert config.access.mode == "public_academic"
assert config.access.academic_use_confirmed is True
```

The equivalent explicit construction is:

```python
from kegg_mcp.kegg.contracts import KeggClientConfig, PublicAcademicAccess

config = KeggClientConfig(
    access=PublicAcademicAccess(academic_use_confirmed=True),
)
```

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

Endpoint authorities are canonicalized before validation and rate-limit scoping: hostnames are
lowercased, a DNS terminal dot and the default HTTPS port are removed, IP literals are normalized,
and invalid ports are rejected. Equivalent spellings therefore cannot create independent rate
limit scopes. A licensed endpoint is trusted operator-controlled startup configuration, not a
per-request value and not a future MCP tool argument. Private or loopback licensed endpoints are
permitted only because this configuration boundary is trusted; callers remain responsible for
authorizing the configured service.

### Request and retry limits

`KeggClientLimits` uses the following defaults:

| Setting | Default | Contract |
| --- | ---: | --- |
| `requests_per_second` | `2.0` | Greater than zero and no greater than `3.0` |
| `timeout_seconds` | `15.0` | Per-request timeout, at most 120 seconds |
| `max_response_bytes` | `5_000_000` | Checked before and while reading a response |
| `max_identifiers` | `100` | Per public operation before batching |
| `relation_batch_size` | `10` | LINK and CONV batch size, at most 100 |
| `max_url_bytes` | `8_192` | Bound on each prepared path and complete request URL |

Rate limiting is process-wide for one endpoint namespace. Request starts are spaced uniformly and
idle time does not accumulate burst capacity. If clients in the same process configure different
rates for one namespace, the slowest configured rate remains in force. The configuration cannot
exceed the official maximum of three requests per second; the safer project default is two.
The client always applies this limiter for live access. An optional injected limiter may add a
stricter policy but cannot replace the mandatory process-wide limiter.

`RetryPolicy` defaults to two retries after the initial attempt, exponential backoff beginning at
0.5 seconds, an 8-second backoff cap, and up to 0.25 seconds of jitter. Retries remain bounded and
every attempt passes through the process-wide rate limiter. Deterministic HTTP 400 and 404 responses
are not retried. Transport timeouts and connection failures may be retried. DNS, permission, TLS,
and other fixed configuration failures are terminal; terminal rate-limit and request failures
remain structured errors.

The HTTPS transport is GET-only. It does not read process proxy settings or follow redirects, asks
for identity content encoding, applies the response-size bound, and retains only `content-type`,
`date`, `etag`, and `last-modified` response headers. Its stable User-Agent contains the project
version and project documentation location, never a username, hostname, email address, or other
personal value.

## Typed operations

The client does not expose an arbitrary URL fetcher. All URL components come from strict request
models and fixed operation mappings.

### INFO

`InfoRequest` accepts the bounded `KeggInfoDatabase` allowlist: `kegg`, `pathway`, `brite`, `module`,
`ko`, `genes`, `genome`, `compound`, `reaction`, and `enzyme`. The parser retains every non-empty
line and conservatively extracts a release string, entry count, and linked database names when the
document supplies an unambiguous form.

### GET

`GetRequest` contains ordered, unique `KeggEntryRef` values for `ko`, `module`, `pathway`,
`reaction`, `enzyme`, `compound`, or `brite`. Each identifier is checked against its selected
database. Pathway identifiers accept the fixed `map`, `ko`, `ec`, `rn`, `vg`, and `vx` prefixes
or a three- or four-letter organism code. The configured total identifier limit is enforced before
preparation. Enzyme identifiers require an EC class from 1 through 7 and four dot-separated
elements. A partial EC number may replace only a continuous trailing sequence of elements with a
single `-` per element.

The project does not bundle or mirror the KEGG organism catalog. Organism-code validation checks
the documented three- or four-letter wire syntax and excludes fixed database prefixes that are
ambiguous in the same identifier position; it does not assert current catalog membership. API
operation names are not globally reserved because valid collisions such as `ddi` exist. Entry
existence remains an endpoint response decision.

Non-BRITE entries are sent in batches of at most ten, regardless of broader configuration. BRITE
hierarchy htext entries are isolated into one request each because their response format differs.
The caller must set `brite_kind=KeggBriteEntryKind.HIERARCHY`; BRITE HTML table files are not
supported in Milestone 2 because their identifier syntax does not distinguish them safely from
hierarchy files. Misclassified content fails parsing rather than being returned as hierarchy data.
The result echoes the typed request; documents and provenance follow prepared batch order, while
explicit missing entries follow caller request order.

### LINK

`LinkRequest` supports only these directions:

- KO to pathway;
- KO to module;
- KO to reaction;
- KO to enzyme;
- KO to BRITE; and
- pathway to KO.

Source identifiers must match the selected direction and must be unique. Broad gene expansion is
not part of this contract. Equivalent identifier sets are sorted before relationship batching so
their cache keys do not depend on caller order; raw response-row order is preserved. Successful
response rows are checked before caching: every source must belong to its prepared batch and every
target must match the selected relationship namespace.

### CONV

`ConvRequest` converts only an explicitly supplied, bounded identifier set between KEGG genes and
one of `ncbi-geneid`, `ncbi-proteinid`, or `uniprot`. Both source and target databases are typed,
and source identifiers must include the matching namespace prefix. KEGG gene identifiers may use
a three- or four-letter organism code, an official T number, or the bounded KEGG GENES collection
prefixes `ag`, `vg`, and `vp`. Response sources and target namespaces are reconciled before caching.
Whole-database conversion and open-ended gene discovery are not exposed.

`list`, `find`, arbitrary database pairs, and arbitrary endpoint paths are not public Milestone 2
operations.

## Text parsing

All response parsers require strict UTF-8 and reject unsupported control characters. They do not
silently reinterpret malformed output:

- INFO output must be non-empty and start with the requested database header. The explicit
  `linked db` block, including its `<org>` placeholder, is parsed separately from database
  statistics. Unknown non-empty lines remain available even when no known metadata can be
  extracted.
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
is absent.

## Local SQLite cache

The default cache is `${XDG_CACHE_HOME:-~/.cache}/kegg-mcp/kegg.sqlite3`. Newly created cache
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
- endpoint label.

Each stored successful response includes its raw bytes, retrieval and expiry times, parser version,
KEGG release when known, and allowlisted HTTP metadata. Reads verify the schema, timestamp ordering,
parser version, metadata shape, and parser compatibility before returning a payload. A malformed
row is `CACHE_FAILED`, never a cache miss or biological absence.

Multi-entry flat-file GET responses are also split into entry-level cache records after successful
parsing and identifier reconciliation. A later single-entry GET or any fully cached subset is
reconstructed from those records without a network call. The live request still obeys KEGG's
maximum of ten GET entries.

The current parser contract version is `4`. It records nested flat-file field indentation,
accepts both legacy BRITE root lines and the current compact BRITE root only within a complete
htext metadata envelope, and applies the current identifier reconciliation rules before cache
use. Cache rows produced under an incompatible parser version fail closed instead of being
silently reinterpreted.

`CachePolicy.ttl_seconds` defaults to 604,800 seconds (seven days). At lookup time:

- a response is fresh only while `now < expires_at`;
- `refresh=False` permits a fresh cache hit;
- `refresh=True` is the default and bypasses even a fresh hit;
- a stale response is served only when `allow_stale=True`;
- `cache_only=True` requires `refresh=False`, never calls the HTTP transport, and returns
  `CACHE_ENTRY_NOT_FOUND` on a miss or disallowed stale entry.

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
| `KEGG_ENTRY_NOT_FOUND` | The endpoint returned a deterministic not-found response. |
| `KEGG_RATE_LIMITED` | Bounded retries ended with a rate-limit response. |
| `KEGG_REQUEST_FAILED` | A deterministic request error or exhausted transport retry could not produce a response. |
| `KEGG_PARSE_FAILED` | Successful response bytes do not conform to the selected strict parser. |
| `CACHE_FAILED` | Cache I/O, schema, metadata, or parser-version validation failed. |

HTTP 400 and 404 responses are not retried. A successful multi-entry GET may return only some
requested entries; those are represented by `missing_entries` rather than fabricated records. A
successful empty LINK or CONV response remains an empty mapping result. Callers must preserve these
distinctions and must not convert cache, transport, or parser errors into evidence of biological
absence.

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

The default pytest suite and pull-request CI include the 120-request live compatibility campaign.
It makes 30 real requests for each of `INFO`, `GET`, `LINK`, and `CONV`, uses one request per
second, zero retries, a temporary cache, and no uploaded KEGG payloads. Other unit and integration
network behavior uses injected transports or local mock servers.
