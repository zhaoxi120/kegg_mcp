# Installation and operation

KEGG MCP is a local stdio server. It accepts KO annotation evidence, performs deterministic local
analysis, and retrieves KEGG references only under an explicitly configured access mode. It does
not run DeepKOALA or another sequence annotator.

Version 0.1.0 is distributed through the private GitHub repository and its release artifacts; it
has not been published to a package registry. Install from the exact `v0.1.0` source checkout or
from a release wheel after verifying its published SHA-256 digest.

## Requirements

- Python 3.11.x;
- an MCP client that can start a local stdio command;
- local writable storage for the cache and scoped result database; and
- for live KEGG access, either eligible public-academic use or an appropriately licensed HTTPS
  endpoint.

The source development workflow uses [uv](https://docs.astral.sh/uv/). GPU access, PyTorch,
DeepKOALA, model weights, and KOfam profiles are not server dependencies.

## Install from a source checkout

From the repository root, create the locked environment and run the offline tests:

```bash
uv sync --frozen
uv run --frozen pytest
```

The stdio executable is then available at `.venv/bin/kegg-mcp`. For an interactive development
check, run it through uv:

```bash
uv run --frozen kegg-mcp
```

The process waits for MCP JSON-RPC messages on stdin. It is not a terminal user interface. Stop a
manual check with the client's normal shutdown sequence or an interrupt.

## Install a reviewed wheel

Build and inspect the candidate by following `docs/release-readiness.md`. Install the exact wheel
in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install /path/to/kegg_mcp-0.1.0-py3-none-any.whl
kegg-mcp
```

Do not install an artifact solely because its filename matches this example. Verify its source,
version, and published SHA-256 digest first.

The Python wheel and Python source distribution install the MCP Python server only. They do not
install the repository-scoped Codex Skill or include the complete repository documentation and
examples. Obtain an exact GitHub repository checkout or tag source archive separately when the
Skill and its references are required.

## Choose one KEGG access mode

The server reads configuration from its process environment. The files under `examples/config/`
are reviewable templates; the server does not automatically load `.env` files. Put only the
selected variables in the MCP client's environment configuration.

### Offline cache, the default

With no access variables, the server uses `offline_cache` and never attempts a network
connection. The explicit equivalent is:

```text
KEGG_MCP_ACCESS_MODE=offline_cache
```

Local KO normalization does not need KEGG data. MODULE and pathway analysis in offline mode needs
previously cached references from an access mode that the operator was authorized to use. A cache
miss returns `OFFLINE_CACHE_MISS`; it does not mean that a KO, MODULE, pathway, or biological
function is absent.

An offline cache is tied to the endpoint class and endpoint fingerprint that produced it. Do not
copy or redistribute cached KEGG payloads unless you have independent permission to do so.

The default offline configuration selects the public-academic cache namespace. To reuse a local
cache previously populated through one authorized licensed endpoint, select its namespace without
enabling network access:

```text
KEGG_MCP_ACCESS_MODE=offline_cache
KEGG_MCP_LICENSED_ENDPOINT=https://kegg.example.edu/api
KEGG_MCP_LICENSED_USE_CONFIRMED=true
```

The endpoint is validated and used only to derive the licensed cache namespace fingerprint;
network access remains disabled. Both licensed values are required together in this configuration,
and a missing or invalid value prevents server startup. Keep the endpoint private even though no
live request is made.

### Public academic access

Use this mode only when the operator is an academic user and the work is academic use:

```text
KEGG_MCP_ACCESS_MODE=public_academic
KEGG_MCP_ACADEMIC_USE_CONFIRMED=true
```

The public endpoint is fixed to `https://rest.kegg.jp`; it cannot be replaced in this mode. The
client defaults to two requests per second without burst and cannot be configured above three
requests per second. KEGG GET requests contain at most ten entries. One MCP analysis call may
need several rate-limited KEGG requests, depending on its targets and cache state.

### Licensed access

Non-academic operation requires an endpoint and use authorized under the operator's KEGG license:

```text
KEGG_MCP_ACCESS_MODE=licensed
KEGG_MCP_LICENSED_ENDPOINT=https://kegg.example.edu/api
KEGG_MCP_LICENSED_USE_CONFIRMED=true
```

Replace the example host with the exact authorized HTTPS base endpoint. The server rejects
credentials in URLs, the public KEGG endpoint in licensed mode, non-HTTPS endpoints, query
strings, fragments, traversal segments, and unsafe authorities. Do not put passwords, tokens, or
private endpoint values in repository files, screenshots, reports, or support requests.

The confirmation records the operator's assertion; this project does not determine whether an
institution or activity is eligible or licensed. If an authorized service requires an
authentication mechanism this release does not support, do not place credentials in the URL.
Keep live access disabled and request a separately reviewed integration.

### Optional local storage locations

These variables select local SQLite files:

```text
KEGG_MCP_CACHE_PATH=/absolute/private/path/kegg.sqlite3
KEGG_MCP_RESULT_STORE_PATH=/absolute/private/path/results.sqlite3
```

Omit them to use the user-local defaults. Create private parent directories owned by the server
user. The result store rejects unsafe parents, traversal, symlinks, non-regular files, and unsafe
ownership or permissions. Status output reports only redacted logical locations.

Cached KEGG responses and generated results must remain local. They are excluded from source
control and must not be placed in examples, CI artifacts, Python packages, or releases.

## Configure an MCP client

Register the server under the exact name `kegg-mcp`. A generic client configuration for a wheel
installation looks like this:

```json
{
  "mcpServers": {
    "kegg-mcp": {
      "command": "/absolute/path/to/.venv/bin/kegg-mcp",
      "env": {
        "KEGG_MCP_ACCESS_MODE": "offline_cache"
      }
    }
  }
}
```

MCP client configuration formats differ; translate the `command` and `env` fields without adding
a shell wrapper. For a source checkout, either use the absolute `.venv/bin/kegg-mcp` path or set
the command to the absolute `uv` executable with arguments equivalent to
`run --frozen --directory /absolute/path/to/kegg_mcp kegg-mcp`.

Do not configure a remote URL. The MVP supports local stdio transport only. Keep stdout attached
to the MCP client because it carries protocol messages; diagnostics use stderr.

Codex discovers the repository-scoped Skill at `.agents/skills/kegg-mcp/` while working in an exact
repository checkout or tag source archive. Its MCP dependency value is also `kegg-mcp`. The Skill
can be invoked explicitly as `$kegg-mcp`; implicit invocation is enabled for KO/KEGG requests. The
Skill chooses workflows and explains results, while deterministic normalization and analysis
remain in the server. A wheel installation supplies the server command but does not install this
repository-scoped Skill.

## Verify discovery and status

Restart the MCP client after changing server configuration. Confirm that discovery shows these
eight tools:

```text
analyze_ko_annotations
normalize_ko_annotations
get_kegg_entries
map_ko_ids
analyze_modules
analyze_pathways
compare_ko_sets
get_server_status
```

The fixed resources are `ko-analysis://status` and `ko-analysis://cache/info`. Result and cache
entry URIs are declared as resource templates, not enumerated static resources.

Call `get_server_status` with an empty input:

```json
{}
```

Check the reported access mode and transport. The response should not expose environment values,
credentials, a licensed endpoint, a username, or a full local path. Connectivity in status is
configuration state, not proof of KEGG eligibility or current network availability.

## Normalize the synthetic KO list

Read `examples/plain-ko/ko-list.txt` as UTF-8 and call `normalize_ko_annotations`. The tool accepts
inline content; do not pass an arbitrary server-side path. A minimal request is:

```json
{
  "text": " K00001\nko:K00002\nK00001\nNOT_A_KO\n",
  "analysis_unit": "isolate_proteome"
}
```

The result reports four input rows, three accepted records, one invalid record, one duplicate,
and the normalized accepted KO view `K00001`, `K00002`. User-supplied K numbers are normalized as
annotations under a named policy; they are not treated as experimental validation. Invalid and
duplicate evidence remains represented in the full normalized result.

This step is local and must work in `offline_cache` mode without a network connection.
The response supplies an opaque result ID and base resource URI; its `dataset` section contains the
complete retained normalized dataset. A staged `analyze_modules`, `analyze_pathways`, or
`compare_ko_sets` request can refer to that result ID within the same scope.

## Run MODULE and pathway analysis

The high-level `analyze_ko_annotations` tool accepts inline KO text and at least one explicit
MODULE or pathway target. For an eligible live configuration or an authorized cache containing
the references, a minimal combined request is:

```json
{
  "ko_text": "K00001\nK00002\nK00003\n",
  "analysis_unit": "isolate_proteome",
  "module_ids": ["M00001"],
  "pathways": [
    {
      "pathway_id": "ko00010",
      "reference_namespace": "ko"
    }
  ]
}
```

The identifiers are demonstration inputs, not a claim that this synthetic set represents one
organism. Plain KO input cannot request an organism-specific pathway reference. Global or overview
maps require explicit opt-in.

The bounded direct response includes normalization counts, MODULE and pathway previews, caveats,
retrieval provenance, an opaque `result_id`, and a server-provided `resource_uri`. Reading that
resource index provides validated section links. Exact MODULE completion and project block
coverage are separate values. Pathway coverage is detected unique KOs divided by the recorded
unique linked-KO denominator; it does not establish pathway presence, completeness, expression,
activity, flux, phenotype, or statistical significance.

## Retrieve the full result

Use the resource URI returned by the tool instead of constructing or modifying a result ID. The
resource templates are:

```text
ko-analysis://results/{result_id}
ko-analysis://results/{result_id}/{section}
ko-analysis://results/{result_id}/{section}/{offset}/{limit}
```

The canonical sections are:

- `structured`: complete JSON-compatible analysis and provenance within its hard limit;
- `summary`: bounded Markdown; and
- `annotations`: complete flat annotation CSV within its hard limit.

Read `structured` for the full nested result. If an artifact is too large for one client read, use
the returned bounded range URI or the range template with byte `offset` and `limit` values. Treat
the opaque ID as local and session-scoped. Unknown, expired, deleted, or differently scoped IDs
all return `RESULT_NOT_FOUND` without revealing whether another scope owns a result.

By default, results expire after 24 hours. The store limits logical artifact payloads to 512 MiB,
the main database to 640 MiB, and active results to 10,000. Capacity failures do not silently evict
another active scope's result.

## Use live access sparingly

- Normalize and validate requests before enabling live access.
- Request only the MODULE and pathway identifiers needed for the analysis.
- Reuse fresh authorized local cache entries instead of forcing refresh.
- Do not run live requests in CI or the default test suite.
- Do not publish KEGG response bodies or cache databases when reporting a problem.
- Record the endpoint class, retrieval time, request key, response hash, parser version, cache
  state, and database release when available.

An optional live compatibility check requires separate operator authorization. It should use the
smallest target set, count requests, obey the process-wide limiter, and tolerate current database
content rather than snapshotting full responses.

## Troubleshooting

`KEGG_USAGE_NOT_CONFIGURED`
: An offline request tried to force `refresh=true`, which would require live access. Disable
  refresh or deliberately configure an authorized live mode. Missing confirmation or endpoint
  values for a selected live mode are startup configuration failures and prevent the MCP server
  from starting; they are not returned as this structured tool error.

`OFFLINE_CACHE_MISS`
: Offline mode has no matching authorized cached response. Enable an eligible live mode or use a
  cache you are authorized to retain; do not report the result as biological absence.

`KEGG_REQUEST_FAILED` or `KEGG_RATE_LIMITED`
: Preserve the structured error, reduce or retry the bounded request as suggested, and avoid
  repeated manual calls. A transport failure is not an absent KEGG entry.

`MODULE_NOT_EVALUABLE`
: Inspect unsupported tokens, unresolved references, cycles, or unavailable definitions. Do not
  reinterpret the result as incomplete.

`PATHWAY_NAMESPACE_MISMATCH`
: Match `koNNNNN` with `ko`, `mapNNNNN` with `map`, and use organism references only with
  compatible organism-specific evidence.

`RESULT_NOT_FOUND`
: The result is unknown, expired, deleted, or outside the current scope. Rerun the bounded analysis
  instead of guessing or reusing another session's identifier.

## Rights and interpretation notice

Project source code is MIT licensed. That license does not grant rights to KEGG content,
DeepKOALA models, KOfam profiles, annotation databases, or other third-party materials. Review the
current primary sources before enabling live access. These pages were last reviewed for this
installation contract on 2026-07-14:

- [KEGG API overview and usage restriction](https://www.kegg.jp/kegg/rest/)
- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html)
- [KEGG copyright and licensing notice](https://www.kegg.jp/kegg/legal.html)

This documentation is not legal advice. A K-number assignment is annotation evidence, not
experimental validation, and a source-rejected prediction is not evidence that a function is
absent.
