# Installation and operation

KEGG MCP is a local stdio server. It accepts KO annotation evidence, performs deterministic local
analysis, and retrieves KEGG references through public-academic access by default. The core
server does not run DeepKOALA or another sequence annotator. An optional independently installed
companion can run an existing local DeepKOALA installation with its fixed automatic device policy
and hand detailed CSV to the core importer. A second independent companion can turn the core's
complete renderer handoff into bounded static pathway overlays and MODULE logic diagrams.

Release evidence and archive checks belong in the
[release-readiness checklist](release-readiness.md). Install from an exact reviewed commit, tag,
or audited wheel.

## Requirements

- Python 3.11.x;
- an MCP client that can start a local stdio command;
- local writable storage for the cache and scoped result database; and
- for live KEGG access, either eligible public-academic use or an appropriately licensed HTTPS
  endpoint.

| Platform | Core | DeepKOALA companion | Renderer |
| --- | --- | --- | --- |
| Linux with CPython 3.11.x | Supported and tested | Supported and tested | Supported and tested |
| macOS | Not release-supported | Not release-supported | Not release-supported |
| Windows | Not release-supported | Not release-supported | Not release-supported |

The guarded filesystem and process implementations use POSIX controls and fail closed when those
controls are unavailable. Python 3.12 and 3.13 are excluded by package metadata until a separate
compatibility campaign is completed.

The source development workflow uses [uv](https://docs.astral.sh/uv/). GPU access, PyTorch,
DeepKOALA, model weights, and KOfam profiles are not server dependencies.

The complete FASTA-to-image workflow uses three separately installed stdio processes. They may
share controlled handoff roots, but not private state:

```text
deepkoala-mcp -> detailed annotation CSV -> kegg-mcp
kegg-mcp      -> render_input.json version 2 -> kegg-render-mcp
```

The core never starts either companion. The renderer never imports annotation evidence, runs an
annotator, or recomputes MODULE completion or pathway coverage.

## Install from a source checkout

From the repository root, create the locked environment. Local validation is optional before a
commit, and the default test command does not contact KEGG:

```bash
uv sync --frozen
uv run --frozen pytest
```

The stdio executable is then available at `.venv/bin/kegg-mcp`. For an interactive development
check, run it through uv:

```bash
uv run --frozen kegg-mcp doctor
```

The diagnostic validates deployment configuration without contacting KEGG or opening local
databases. Bare `uv run --frozen kegg-mcp` starts the stdio server and waits for MCP JSON-RPC
messages on stdin; it is not a terminal user interface.

## Install a reviewed wheel

Build and inspect the distribution by following `docs/release-readiness.md`. Install the exact wheel
in an isolated environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install /path/to/kegg_mcp-*.whl
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

The supported operating profiles are:

- unconfigured operation defaults to `public_academic` with academic use confirmed;
- licensed operation configures the operator's authorized endpoint; and
- local pytest skips live KEGG tests unless explicitly enabled; and
- pull-request CI runs the bounded live compatibility campaign once.

The project never infers a live-access right from a username, institution, host, or execution
context.

Pull-request CI issues one serialized 120-request campaign without uploading KEGG payloads. It
makes 30 requests for each supported operation at one request per second with zero retries.
Merging the pull request does not trigger the same workflow again.

### Public academic access

This is the default mode and assumes the operator is an academic user performing academic work.
The following explicit configuration is equivalent to leaving both variables unset:

```text
KEGG_MCP_ACCESS_MODE=public_academic
KEGG_MCP_ACADEMIC_USE_CONFIRMED=true
```

The public endpoint is fixed to `https://rest.kegg.jp`; it cannot be replaced in this mode. The
client defaults to two requests per second without burst and cannot be configured above three
requests per second. Core and Renderer coordinate starts across processes through the same
owner-only `KEGG_MCP_RATE_LIMIT_ROOT`; its default is in the user cache directory. KEGG GET
requests contain at most ten entries. One MCP analysis call may need several rate-limited KEGG
requests, depending on its targets and batching.

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
authentication mechanism this release does not support, do not place credentials in the URL or
start the server; request a separately reviewed integration.

### Optional local storage locations

These variables select local SQLite files:

```text
KEGG_MCP_CACHE_PATH=/absolute/private/path/kegg.sqlite3
KEGG_MCP_RESULT_STORE_PATH=/absolute/private/path/results.sqlite3
```

The KEGG cache defaults to 10,000 rows, 512 MiB of response payloads, and a 640 MiB main database.
Deployments can set positive values with `KEGG_MCP_CACHE_MAX_ENTRIES`,
`KEGG_MCP_CACHE_MAX_PAYLOAD_BYTES`, and `KEGG_MCP_CACHE_MAX_DATABASE_BYTES`. Inspect only redacted
counts and capacity, or delete only expired rows, with:

```bash
kegg-mcp cache status --json
kegg-mcp cache cleanup --expired --json
```

Omit them to use the user-local defaults. Create private parent directories owned by the server
user. The result store rejects unsafe parents, traversal, symlinks, non-regular files, and unsafe
ownership or permissions. Status output reports only redacted logical locations.

Cached KEGG responses and generated results must remain local. They are excluded from source
control and must not be placed in examples, CI artifacts, Python packages, or releases.

### Allowed roots for file handoff and output bundles

File input and output-directory workflows are disabled unless the operator configures one or more
existing shared roots. Separate roots with the platform path separator (`:` on Linux):

```text
KEGG_MCP_ALLOWED_ROOTS=/absolute/private/input:/absolute/private/results
```

An input `file_path`, original source `input_path`, and requested `output_directory` must be
absolute and resolve beneath one of these roots. The server rejects traversal and symlink escapes.
An output directory must be new or empty. A non-empty target is rejected and no existing file is
replaced. This deployment setting permits stable file handoff between local MCP processes without
making a private `result_id` a cross-process contract.

## Optional DeepKOALA companion

The optional package under `companions/deepkoala-mcp/` is an independent stdio server. Installing
the core wheel does not install it. The companion also does not install DeepKOALA, PyTorch, model
weights, HMMER, KOfam profiles, or KEGG data. It requires an operator-reviewed official checkout,
an existing Python interpreter where that checkout already runs, and a private state root. Add
that state root to the core server's `KEGG_MCP_ALLOWED_ROOTS` for file handoff. When using
caller-supplied `fasta_path`, also allow the original FASTA root so the distinct provenance
`input_path` passes the core boundary. Inline FASTA provenance uses `input_path=null`. Create the
owner-only directory before starting either server, as shown in the companion README, because the
core validates allowed roots during startup.

The local-only routing policy was reviewed on 2026-07-16 against the official
[GenomeNet DeepKOALA page](https://www.genome.jp/tools/deepkoala/), the linked official
[DeepKOALA repository](https://github.com/zhaoxi120/deepkoala), and the
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html). The official page exposes a web form
and links to downloadable local software and weights; the repository documents local CLI and
Python interfaces. The KEGG API manual documents no DeepKOALA job endpoint. This project therefore
treats the absence of a documented remote API as a deployment boundary: MCP automation never
opens, submits to, or simulates the web form and uses only the configured local runtime through
`deepkoala-mcp`.

For protein FASTA without KO evidence, the Skill first discovers `deepkoala-mcp` and makes
`get_deepkoala_runner_status` its first annotation-tool call. If the runtime, model resources,
companion installation, state root, or MCP registration is missing, it reports that local state and
asks permission before making any change. The confirmation states which local checkout,
environment, resources, state, and registration would change; whether dependencies or models must
be downloaded; expected disk and compute requirements; and that FASTA remains local with no remote
upload branch. Declining preserves the FASTA and stops annotation. No package install, environment
change, checkout or model download, directory creation, or MCP configuration write occurs before
permission.

Install and validate the lightweight companion separately:

```bash
cd companions/deepkoala-mcp
uv sync --frozen
uv run pytest
uv run deepkoala-mcp doctor --json
```

On a module-based host, resolve the external interpreter before configuring the MCP client:

```bash
module load pytorch
command -v python
```

Set the resulting absolute interpreter path in the companion configuration. Do not use `module
load`, a shell wrapper, or an activation command as the MCP executable. The companion uses the
configured interpreter directly, fixes `--device auto` and data-loader workers at zero, inherits
the MCP process's existing accelerator visibility, limits CPU thread pools, and allows one running
job. Callers cannot select a device. The companion never claims which device was resolved unless
the external runtime reports it reliably, and it never downloads or replaces a weight.

Register `deepkoala-mcp` as a second local server using the configuration contract in the
[companion README](../companions/deepkoala-mcp/README.md). The companion writes successful output
beneath `DEEPKOALA_MCP_STATE_ROOT`; configure the core server to allow that same root.
`DEEPKOALA_MCP_ALLOWED_ROOTS` is separate and controls only caller-supplied FASTA intake. A
successful path-input handoff identifies that original FASTA as `source.input_path`, while inline
input leaves it null; neither form exposes the private staged FASTA. The repository Skill can then
prepare a job, retain the non-blocking execution notice as provenance, submit only the opaque job
identifier without a per-job confirmation field, poll it, and pass the successful `output_path` to the core importer as
`input_format="deepkoala_detailed"`. No digest or cross-server private result identifier is part
of this handoff.

The handoff validates the generated CSV and caller-supplied original FASTA separately. The private
staged FASTA is never exposed.

## Optional renderer companion

`companions/kegg-render-mcp/` is an independent Python 3.11 stdio distribution. It depends on a
compatible `kegg-mcp` package for the public renderer contract and typed pathway-asset client, but
it does not add tools to the core server or import the DeepKOALA companion. Install it separately
from the same reviewed checkout:

```bash
cd companions/kegg-render-mcp
uv sync --frozen --all-groups
uv run --frozen pytest
```

Create private, non-overlapping renderer state and shared analysis roots before starting it:

```bash
mkdir -p /absolute/private/renderer-state /absolute/private/analysis-results
chmod 700 /absolute/private/renderer-state /absolute/private/analysis-results
```

Configure the core to write bundles under the shared analysis root and configure the renderer to
read them. The renderer state root must not overlap any allowed root:

```text
KEGG_MCP_ALLOWED_ROOTS=/absolute/private/analysis-results
KEGG_RENDER_MCP_STATE_ROOT=/absolute/private/renderer-state
KEGG_RENDER_MCP_ALLOWED_ROOTS=/absolute/private/analysis-results
KEGG_RENDER_MCP_ACCESS_MODE=public_academic
KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED=true
```

Public-academic access is the renderer default and is only for eligible academic users performing
academic work. Licensed deployments instead set `KEGG_RENDER_MCP_ACCESS_MODE=licensed`, an
authorized HTTPS `KEGG_RENDER_MCP_LICENSED_ENDPOINT`, and
`KEGG_RENDER_MCP_LICENSED_USE_CONFIRMED=true`. Set the renderer mode to `unconfigured` only for
MODULE-only rendering; pathway rendering then returns an actionable access error.

The renderer accepts only a validated `render_input.json` schema version 2 path below an allowed
root. Incompatible handoffs must be regenerated by the core analysis. Source pathway PNG and KGML
are fetched one asset at a time through the typed core client, remain local, and are not
distributable under the project's MIT license. See the
[renderer README](../companions/kegg-render-mcp/README.md) for its six tools, resource templates,
retention settings, and exact bounds.

The renderer dependency must remain `kegg-mcp>=0.3,<0.4`, and both distributions must come from
one compatible reviewed source baseline.

## Configure an MCP client

Register the server under the exact name `kegg-mcp`.

### Codex CLI academic user-test profile

For an academic user performing academic work, create the allowed root, validate the same live
configuration outside the protocol, and register the absolute executable:

```bash
mkdir -p /tmp/kegg-mcp-demo
KEGG_MCP_ACCESS_MODE=public_academic \
KEGG_MCP_ACADEMIC_USE_CONFIRMED=true \
KEGG_MCP_ALLOWED_ROOTS=/tmp/kegg-mcp-demo \
/absolute/path/to/.venv/bin/kegg-mcp doctor

codex mcp add kegg-mcp \
  --env KEGG_MCP_ACCESS_MODE=public_academic \
  --env KEGG_MCP_ACADEMIC_USE_CONFIRMED=true \
  --env KEGG_MCP_ALLOWED_ROOTS=/tmp/kegg-mcp-demo \
  -- /absolute/path/to/.venv/bin/kegg-mcp
codex mcp list
```

Restart Codex after changing MCP configuration. Bare `kegg-mcp` remains the stdio command;
`kegg-mcp serve` is an explicit equivalent, and `kegg-mcp doctor [--json]` is the out-of-band
diagnostic.

### Claude Desktop and generic JSON client configuration

The following JSON is configuration file content for an eligible academic user. Do not paste it
at a Bash prompt. Claude Desktop and other local clients that use an `mcpServers` object can start
the installed stdio executable with this configuration:

```json
{
  "mcpServers": {
    "kegg-mcp": {
      "command": "/absolute/path/to/.venv/bin/kegg-mcp",
      "env": {
        "KEGG_MCP_ACCESS_MODE": "public_academic",
        "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "true"
      }
    }
  }
}
```

In Claude Desktop, open **Settings > Developer > Edit Config**, add the server object to the
existing `mcpServers` map, save, and completely restart the application. Client menu names and
configuration locations are client-owned and may change; this flow was checked against the
[official MCP local-server guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
on 2026-07-16. The server remains Linux-supported even though this client example is provided for
configuration portability; using an unsupported host does not expand the release support matrix.

For visualization, register the independently installed renderer alongside the core. This JSON is
also configuration file content; replace both executable paths and local roots:

```json
{
  "mcpServers": {
    "kegg-mcp": {
      "command": "/absolute/path/to/core/.venv/bin/kegg-mcp",
      "env": {
        "KEGG_MCP_ALLOWED_ROOTS": "/absolute/private/analysis-results"
      }
    },
    "kegg-render-mcp": {
      "command": "/absolute/path/to/renderer/.venv/bin/kegg-render-mcp",
      "env": {
        "KEGG_RENDER_MCP_STATE_ROOT": "/absolute/private/renderer-state",
        "KEGG_RENDER_MCP_ALLOWED_ROOTS": "/absolute/private/analysis-results"
      }
    }
  }
}
```

Add `deepkoala-mcp` as a third independent server only when protein FASTA must first be annotated.
Do not replace these direct stdio commands with shell activation wrappers.

MCP client configuration formats differ; translate the `command` and `env` fields without adding
a shell wrapper. Operators who are not eligible academic users must select `licensed`. For a source
checkout, either use the absolute `.venv/bin/kegg-mcp` path
or set the command to the absolute `uv` executable with arguments equivalent to
`run --frozen --directory /absolute/path/to/kegg_mcp kegg-mcp`.

Do not configure a remote URL. The MVP supports local stdio transport only. Keep stdout attached
to the MCP client because it carries protocol messages; diagnostics use stderr.

Codex discovers the repository-scoped Skill at `.agents/skills/kegg-ko-analysis/` while working in
an exact repository checkout or tag source archive. Its MCP dependencies are `kegg-mcp` and
`deepkoala-mcp`. The Skill can be invoked explicitly as `$kegg-ko-analysis`; implicit invocation is
enabled for KO/KEGG evidence and clear protein FASTA requests. Existing KO evidence routes directly
to the core. Protein FASTA without KO evidence checks `get_deepkoala_runner_status` first and uses
the ready companion automatically; an absent or unready companion produces a specific deployment
state and requests authorization only for installation, downloads, directory changes, or MCP
registration. The Skill chooses workflows and explains results, while deterministic normalization
and analysis remain in the core server. Codex discovers the separate
`.agents/skills/kegg-visualization/` Skill for pathway and MODULE graphics; it requires the core and
renderer stdio dependencies and contains no rendering code. A wheel installation supplies the
core server command but does not install either repository-scoped Skill or either companion.

## Verify discovery and status

Restart the MCP client after changing server configuration. Confirm that discovery shows these
ten tools:

```text
analyze_ko_annotations
normalize_ko_annotations
get_kegg_entries
map_ko_ids
analyze_modules
analyze_pathways
compare_ko_sets
probe_kegg_connectivity
delete_analysis_result
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
configuration state, not proof of KEGG eligibility or current network availability. Call
`probe_kegg_connectivity` explicitly before a network-dependent analysis when the current
connection is unknown.

For a first live user-acceptance prompt, ask the client:

> Use kegg-mcp in a bounded acceptance check. First confirm that server status is
> `public_academic`, then probe connectivity once. Retrieve only KO entry `K00844` and report its
> identifier, name, database release when available, and whether it came from network or cache.
> Stop after this single entry and do not run pathway discovery.

This verifies discovery, one explicit connectivity request, and one bounded content lookup. The
entry lookup uses the normal local cache and may not require a second network request on later
runs. It is not a bulk KEGG compatibility test.

## Normalize the synthetic KO list

Read `examples/plain-ko/ko-list.txt` as UTF-8 and call `normalize_ko_annotations`. A minimal inline
request is:

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

This normalization step is local and does not itself require a KEGG request.
The response supplies an opaque result ID and base resource URI; its `dataset` section contains the
complete retained normalized dataset. A staged `analyze_modules`, `analyze_pathways`, or
`compare_ko_sets` request can refer to that result ID within the same scope.

## Run MODULE and pathway analysis

The high-level `analyze_ko_annotations` tool accepts inline KO text or a controlled annotation
file. Targets may be explicit; when none are supplied, accepted K numbers are mapped to canonical
reference pathways within deployment bounds. For an eligible live configuration or an authorized
cache containing the references, a combined output-directory request is:

```json
{
  "ko_text": "K00001\nK00002\nK00003\n",
  "analysis_unit": "isolate_proteome",
  "output_directory": "/absolute/private/results/example",
  "module_ids": ["M00001"],
  "pathways": [
    {
      "pathway_id": "ko00010"
    }
  ]
}
```

The identifiers are demonstration inputs, not a claim that this synthetic set represents one
organism. Plain KO input cannot request an organism-specific pathway reference. Global or overview
maps require explicit opt-in.

For a file handoff from an independently operated annotation MCP, keep both the annotation file and
original FASTA under `KEGG_MCP_ALLOWED_ROOTS` and use one high-level call:

```json
{
  "annotations": {
    "file_path": "/absolute/private/handoff/deepkoala_annotations.csv",
    "input_format": "deepkoala_detailed",
    "analysis_unit": "isolate_proteome",
    "source": {
      "source_name": "deepkoala",
      "input_path": "/absolute/private/input/proteins.faa",
      "annotation_date": "2026-07-15T09:30:00Z"
    }
  },
  "output_directory": "/absolute/private/results/example"
}
```

When targets are omitted, accepted K numbers are used to discover bounded canonical reference
pathways. The independent annotation service owns FASTA execution and its run report; this core
server only validates and analyzes the resulting annotation evidence.

For a most-detected or Top-N pathway request, prefer server-side selection:

```json
{
  "annotations": {
    "file_path": "/absolute/private/handoff/deepkoala_annotations.csv",
    "input_format": "deepkoala_detailed",
    "analysis_unit": "isolate_proteome"
  },
  "pathway_selection": {
    "mode": "top_detected",
    "top_n": 1,
    "metric": "unique_selected_ko_count"
  },
  "output_directory": "/absolute/private/results/top-pathway"
}
```

This route maps the evidence once, ranks all candidates in the service, and loads denominator and
metadata references only for the selected Top-N. The Skill does not read or aggregate the full
DeepKOALA table or KO-to-pathway relationship rows.

The bounded direct response includes normalization counts, MODULE and pathway previews, caveats,
compact request/cache and six-stage execution summaries, an opaque `result_id`, and a
server-provided `resource_uri`. Top-N results include the candidate count and selected pathway
summaries but omit full relationship rows and detected-KO lists. Reading that
resource index provides validated section links. Exact MODULE completion and project block
coverage are separate values. Pathway coverage is detected unique KOs divided by the recorded
unique linked-KO denominator; it does not establish pathway presence, completeness, expression,
activity, flux, phenotype, or statistical significance.

The requested output directory receives stable handoff files:

```text
normalized_annotations.tsv
protein_ko_mapping.tsv
pathway_ranking.tsv              # Top-N workflows
ko_pathway_relationships.tsv     # Top-N workflows
pathway_coverage.tsv
module_completion.tsv
analysis_report.md
render_input.json
bundle_manifest.json
```

The direct `output_bundle.artifacts` entries report each file's MIME type, exact byte size, and
controlled absolute path. Bundle schema version 2 requires the directory to be new or empty and
installs the manifest last as the commit marker; an existing file causes
`OUTPUT_ALREADY_EXISTS`, and no overwrite mode is exposed. Complete ranking and relationship
tables remain local artifacts rather than default model-context content.

Use these files between MCP stages. The report records the original absolute input path when it is
provided as source provenance and does not display workflow digests. By default,
`bundle_manifest.json` represents source paths as stable redacted labels. Set
`manifest_path_mode="absolute"` only when the operator explicitly wants absolute source paths in
that manifest. `render_input.json` uses the renderer-specific version 2 schema, and the manifest
records that schema and MIME type. Its `AnalysisExecutionProvenance` version 2 records the MODULE
analysis limits, pathway parameters, pathway coverage limits, and report limits used to produce
the authoritative targets.

## Render a compatible analysis bundle

First call `get_renderer_status` and verify that schema version 2 is supported. For pathway
targets, call `probe_renderer_kegg_connectivity` only when an explicit live preflight is needed;
the probe makes exactly one INFO request. Then pass the controlled absolute handoff path to
`render_analysis_bundle`:

```json
{
  "render_input_path": "/absolute/private/analysis-results/example/render_input.json",
  "output_directory": "/absolute/private/analysis-results/example/images",
  "formats": ["svg", "png"],
  "target_ids": ["ko00010", "M00001"]
}
```

The renderer returns an opaque process-scoped `render_id`, artifact metadata, warnings,
provenance, and validated `kegg-render://results/...` resource URIs. Use those URIs rather than
constructing them. SVG is canonical; PNG is an optional bounded derivative. Global and overview
pathways are rejected in this release. MODULE diagrams use only the authoritative AST and states
in the handoff and display exact completion separately from project block coverage.

Graphics visualize annotation evidence. Accepted and policy-defined uncertain annotations have
distinct states; rejected predictions are not colored and unchanged graphics are not labelled as
biological absence. A pathway overlay does not establish pathway presence, completeness,
expression, activity, flux, phenotype, or statistical significance.

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
all return `RESULT_NOT_FOUND` without revealing whether another scope owns a result. Call
`delete_analysis_result` to remove one current-scope result immediately; repeated, unknown, and
cross-scope deletion attempts retain the same safe not-found behavior.

Normal stdio shutdown removes all results in the current server scope. The default 24-hour value
is both the hard TTL for an active result and the cleanup threshold for orphan rows left by an
abnormal termination; it is not a promise that a `result_id` survives a client restart. The store
limits logical artifact payloads to 512 MiB, the main database to 640 MiB, and active results to
10,000. Capacity failures do not silently evict another active scope's result. For an out-of-band
operator cleanup that never starts stdio or removes an active unexpired result, run:

```bash
kegg-mcp cleanup --expired
kegg-mcp cleanup --expired --json
```

The KEGG response cache has a separate cross-process freshness policy. Durable analysis delivery
uses a non-overwriting output bundle, which remains until the operator deletes it.

## Use live access responsibly

- Normalize and validate inputs before requesting KEGG references.
- Request only the MODULE and pathway identifiers needed for the analysis.
- Use the cache-only entry resource only when an explicit local read is intended.
- Keep pull-request CI and any explicitly enabled local campaign within the budget documented in
  `tests/live/README.md`.
- Do not publish KEGG response bodies or cache databases when reporting a problem.
- Do not publish source pathway PNG, KGML, renderer cache/state, or rendered derivatives without a
  specific rights review.
- Record the endpoint class, retrieval time, readable request key, parser version, cache
  state, and database release when available.

Any additional manual live compatibility check should use the smallest target set, count requests,
obey the deployment-wide limiter, and tolerate current database content rather than snapshotting
full responses.

## Troubleshooting

See the dedicated [troubleshooting guide](troubleshooting.md) first for client discovery, the
common mistake of pasting configuration JSON into Bash, redacted diagnostics, allowed roots,
process-scoped result IDs, protocol stdout, and safe support reports.

`CACHE_ENTRY_NOT_FOUND`
: An explicit cache-resource read has no matching cached response. Fetch the entry through an
  ordinary network-enabled request; do not report the result as biological absence.

`KEGG_REQUEST_FAILED` or `KEGG_RATE_LIMITED`
: Preserve the structured error, reduce or retry the bounded request as suggested, and avoid
  repeated manual calls. A transport failure is not an absent KEGG entry.

`MODULE_NOT_EVALUABLE`
: Inspect unsupported tokens, unresolved references, cycles, or unavailable definitions. Do not
  reinterpret the result as incomplete.

`PATHWAY_NAMESPACE_MISMATCH`
: Remove an explicitly conflicting namespace. An omitted `mapNNNNN` namespace is canonicalized to
  the `koNNNNN` reference view; organism references still require compatible gene-level evidence.

`RESULT_NOT_FOUND`
: The result is unknown, expired, deleted, or outside the current scope. Rerun the bounded analysis
  instead of guessing or reusing another session's identifier.

`OUTPUT_ALREADY_EXISTS`
: The output directory contains an existing entry. Choose a new or empty directory; this release
  has no overwrite mode.

`OUTPUT_WRITE_FAILED` or `RESULT_STORE_FAILED`
: The requested output bundle or retained-result store could not be written safely. Check the
  configured allowed root, permissions, and available storage. This is a technical failure, not a
  biological result.

## Rights and interpretation notice

Project source code is MIT licensed. That license does not grant rights to KEGG content,
DeepKOALA models, KOfam profiles, annotation databases, source pathway assets, or other third-party
materials. Redistribution of rendered derivatives requires a specific rights review. Review the
current primary sources before enabling live access. These pages were last reviewed for this
installation contract on 2026-07-16:

- [KEGG API overview and usage restriction](https://www.kegg.jp/kegg/rest/)
- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html)
- [KEGG copyright and licensing notice](https://www.kegg.jp/kegg/legal.html)

This documentation is not legal advice. A K-number assignment is annotation evidence, not
experimental validation, and a source-rejected prediction is not evidence that a function is
absent.
