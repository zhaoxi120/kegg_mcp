# Core MCP Server

The core runs as a local stdio server. It never launches DeepKOALA or another annotation tool,
parses KGML, renders images, or exposes remote HTTP transport. Optional annotation and rendering
capabilities are separate local stdio processes and independently reviewed distributions:

```text
deepkoala-mcp -> detailed annotation CSV -> kegg-mcp
kegg-mcp      -> render_input.json version 3 -> kegg-render-mcp
```

See the [DeepKOALA companion README](../companions/deepkoala-mcp/README.md) and
[renderer companion README](../companions/kegg-render-mcp/README.md) for their independent
contracts.

## Start the server

After installing the project, configure an MCP client to run:

```text
uv run kegg-mcp
```

The default access mode is confirmed `public_academic`. Server logs and configuration failures are written to
stderr; stdout is reserved for MCP protocol messages.

Local pytest skips live requests by default. Pull-request CI explicitly runs the bounded
120-request public-academic compatibility campaign once.

Use the side-effect-free operator diagnostic before client startup:

```text
uv run kegg-mcp doctor
uv run kegg-mcp doctor --json
```

The diagnostic validates configuration but performs no KEGG request or SQLite inspection and
redacts allowed-root paths and endpoint values. `kegg-mcp serve` is an explicit equivalent to the
default stdio command.

## KEGG access configuration

| Environment variable | Meaning |
| --- | --- |
| `KEGG_MCP_ACCESS_MODE` | `public_academic` (default), `licensed`, or network-disabled `offline_cache` |
| `KEGG_MCP_ACADEMIC_USE_CONFIRMED` | Defaults to `true`; an explicit value other than `true` is rejected |
| `KEGG_MCP_LICENSED_ENDPOINT` | Authorized HTTPS endpoint for licensed access |
| `KEGG_MCP_LICENSED_USE_CONFIRMED` | Must equal `true` before licensed access is enabled |
| `KEGG_MCP_CACHE_PATH` | Optional absolute path to the user-local KEGG cache |
| `KEGG_MCP_CACHE_MAX_ENTRIES` | Optional positive cache-row limit; default 10,000 |
| `KEGG_MCP_CACHE_MAX_PAYLOAD_BYTES` | Optional positive cache-payload limit; default 512 MiB |
| `KEGG_MCP_CACHE_MAX_DATABASE_BYTES` | Optional positive SQLite main-database limit; default 640 MiB |
| `KEGG_MCP_RATE_LIMIT_ROOT` | Optional owner-only state root shared by Core and Renderer |
| `KEGG_MCP_RESULT_STORE_PATH` | Optional absolute path to the user-local retained-result database |
| `KEGG_MCP_ALLOWED_ROOTS` | Path-separated existing directories allowed for file input and output bundles |

The public KEGG REST service is limited to academic use by academic users. Other deployments must
use an appropriately licensed endpoint. The live client defaults to two requests per second with
no burst, enforces a hard deployment-wide maximum no greater than three requests per second for
one canonical endpoint, and batches `get` requests at no more than ten entries. Core and Renderer
coordinate through the same owner-only rate-limit root.

Cache payloads and retained results are local data and must not be committed, packaged, or attached
to CI artifacts. The low-level client keeps explicit refresh semantics, while high-level MCP
analysis uses fresh-cache-first requests so an equivalent Top-N run can report cache hits without
repeating network requests. The cached-entry resource remains a cache-only read and never falls
back to the network.

`get_server_status` and `ko-analysis://cache/info` report redacted configuration state. Status
includes `file_handoff_enabled` and `allowed_root_count`, but never the configured roots. These
surfaces do not probe connectivity or enumerate cache contents. Use the explicit connectivity
tool when a live-access preflight is required. Operators can inspect bounded cache counts with
`kegg-mcp cache status --json` and remove only expired rows with
`kegg-mcp cache cleanup --expired --json`.

The MCP initialization response guides clients toward the high-level analysis tool and records
the connectivity, file-handoff, result-scope, stable-bundle, and biological interpretation
boundaries. It does not replace the explicit input and output schemas.

Every advertised input schema is self-contained: local Pydantic references are inlined so clients
can display nested object fields and enum values without resolving `$defs`.

## Tools

The server exposes eleven tools:

- `analyze_ko_annotations`: one-call normalization and MODULE/pathway analysis. Supply either
  `ko_text` or a nested `annotations` request. If no MODULE or pathway target and no explicit
  selection are supplied, the server independently selects up to five MODULEs and up to five
  canonical KO reference pathways by unique selected-KO overlap under the request's strict or
  lenient evidence mode. MODULE ranking selects targets; it is not completion or enrichment, and
  exact completion is evaluated separately. To override the pathway count, supply only
  `pathway_selection.top_n` from 1 through 25. The server applies its versioned
  `selected_unique_ko_count` ranking policy before reference loading.
- `normalize_ko_annotations`: normalize inline content or an allowed-root file containing plain
  K numbers, generic CSV/TSV, or a DeepKOALA detailed table, then retain the complete dataset.
- `get_kegg_entries`: retrieve selected allowlisted KEGG entries. The direct response reports the
  total retrieval-batch count, returns at most five provenance records with an explicit truncation
  flag, and retains every batch in the scoped `detail` artifact. It is not an arbitrary URL proxy.
- `map_ko_ids`: map selected K numbers to pathways, modules, reactions, EC numbers, or BRITE.
  Pathway summaries distinguish `unique_reference_pathway_number_count` from the paired
  `available_ko_reference_view_count` and `available_map_reference_view_count`; these view counts
  do not claim that LINK returned both namespaces.
- `analyze_modules`: evaluate exact MODULE completion and required-block coverage from inline or
  retained evidence.
- `analyze_pathways`: calculate descriptive unique-KO coverage after inferring and validating the
  pathway namespace; paired `mapNNNNN` input is canonicalized to the `koNNNNN` reference view,
  while organism-specific identifiers are never folded into that pair and are rejected by the
  public KO-only analysis tools.
- `compare_ko_sets`: calculate deterministic set differences for two to ten datasets, with optional
  shared-reference MODULE or pathway comparisons.
- `probe_kegg_connectivity`: make one explicit low-cost INFO request and classify DNS,
  connection, or authorization/configuration outcomes.
- `list_analysis_results`: list a bounded metadata page of active results in the current stdio
  scope without exposing any other scope.
- `delete_analysis_result`: immediately delete one retained result in the current stdio scope.
- `get_server_status`: return redacted access, capability, and result-retention information.

The advertised MCP behavior hints describe local effects as well as remote effects:

| Tool class | Read-only | Destructive | Idempotent | Open world |
| --- | --- | --- | --- | --- |
| `get_server_status`, `list_analysis_results` | Yes | No | Yes | No |
| `normalize_ko_annotations` | No | No | No | No |
| KEGG retrieval and analysis tools | No | No | No | Yes |
| `probe_kegg_connectivity` | No | No | No | Yes |
| `delete_analysis_result` | No | Yes | Yes | No |

Normalization and analysis create retained results and can create an output bundle. KEGG-facing
tools can also update the local response cache, so even a connectivity probe is not advertised as
read-only. Deletion is idempotent in environmental effect although a repeated call returns the
same safe not-found class. These hints inform clients; they do not replace server-side validation
or authorization.

Minimal plain-KO normalization input:

```json
{"text":"K00844\nK01810"}
```

Minimal one-call MODULE input:

```json
{
  "ko_text": "K00844\nK01810",
  "module_ids": ["M00001"],
  "analysis_unit": "isolate_proteome"
}
```

Minimal server-ranked Top-1 pathway input:

```json
{
  "annotations": {
    "file_path": "/absolute/private/handoff/deepkoala_annotations.csv",
    "input_format": "deepkoala_detailed"
  },
  "pathway_selection": {
    "top_n": 1
  },
  "output_directory": "/absolute/private/results/top-pathway"
}
```

Minimal explicit pathway input:

```json
{
  "ko_text": "K00844\nK01810",
  "analysis_unit": "isolate_proteome",
  "pathways": [{"pathway_id": "ko00010"}]
}
```

The server selects K numbers from the requested evidence mode, performs one logical KO-to-pathway
mapping stage, de-duplicates each pathway's K numbers, sorts by descending unique selected-KO count
and then canonical pathway ID. Automatic selection directly excludes the current KEGG Global,
Overview, and higher-level Overview KO map identifiers before Top-N truncation and fills up to the
requested count from subsequent regular references; the complete overlap ranking remains retained.
The fixed identifier set was checked against the official KEGG PATHWAY identifier classes and map
list on 2026-07-22. An explicitly requested `ko01100` graphic remains outside these three MCP
servers; a client may create a separately labelled model-generated conceptual diagram, but must not
present it as a KEGG-derived coverage overlay. The server loads pathway LINK/GET references only for
the selected targets. Duplicate annotation records and duplicate LINK rows cannot increase the
detected node count.

Generic tables with unambiguous common headers are mapped automatically and the decision is
reported; ambiguous or non-standard tables require an explicit mapping. When `annotations` is used
in the high-level tool, biological context belongs inside that nested object. KO-only MCP inputs do
not accept organism-specific pathway references because they lack gene-level context. Cache tuning,
refresh flags, and internal limit models are deployment-owned rather than ordinary tool inputs.

File input and `output_directory` are disabled until `KEGG_MCP_ALLOWED_ROOTS` is configured. Paths
must be absolute and resolve beneath an allowed root. Traversal, missing files, symlink escapes, and
unsafe output ancestors are rejected. Once an output path enters a directory owned by the service
user, that directory and every descendant directory on the path must remain owned by the service
user and must not be group- or world-writable; listing a path in `KEGG_MCP_ALLOWED_ROOTS` does not
waive this rule. An output directory must be new or empty;
any existing entry causes `OUTPUT_ALREADY_EXISTS`, and this release exposes no overwrite operation.
A successful normalization bundle contains
`normalized_annotations.tsv`, `protein_ko_mapping.tsv`, and `bundle_manifest.json`; analysis adds
`pathway_coverage.tsv`, `module_completion.tsv`, `analysis_report.md`, and `render_input.json`.
Automatic MODULE selection also adds `module_ranking.tsv` and `ko_module_relationships.tsv`;
server-ranked pathway selection adds `pathway_ranking.tsv` and `ko_pathway_relationships.tsv`.
The report records the original absolute input path when source provenance supplies it.
`render_input.json` is an immutable renderer-specific version 3 contract: it distinguishes
accepted from policy-defined uncertain KOs, carries complete-within-limit pathway evidence and
authoritative MODULE states, and records producer and calculation provenance. Version 1 previews
cannot be upgraded losslessly. Bundle schema version 3 installs its manifest last as a commit
marker and represents source paths with redacted labels by default. The explicit
`manifest_path_mode="absolute"` option includes absolute paths in the manifest when required. The
bundle manifest records the renderer schema and MIME type.
`AnalysisExecutionProvenance` version 3 also records the applicable MODULE analysis limits,
MODULE- and pathway-ranking parameters when applicable, pathway parameters, pathway coverage
limits, and report limits.

The three analysis tools use separate concise output models. Their shared `summary` contains only
record/status counts, selected unique-KO count, aggregate logical/network/cache request counts,
response bytes, warnings, and interpretation caveats. `analyze_modules` returns MODULE previews
only, `analyze_pathways` returns pathway previews only, and `analyze_ko_annotations` may return both
plus bounded `automatic_module_selection` and `automatic_pathway_selection` summaries and
output-bundle metadata. Direct responses do not expose import structures, annotation or KEGG batch
provenance, execution parameters, stage metrics, complete relationship rows, or detected-KO lists.

The high-level retained `structured` JSON is the authoritative detail artifact and contains the
complete normalized dataset, analyses, execution parameters, mapping provenance, and six canonical
stage-metric rows. The narrower MODULE and pathway tools retain the same classes of provenance,
parameters, metrics, and full evaluations in their `detail` JSON. Bundle artifact metadata includes
MIME type, exact byte size, and controlled path. A K number is an annotation, MODULE exact
completion is distinct from
the project block-coverage metric, and pathway coverage does not establish pathway presence,
activity, flux, phenotype, or statistical significance.

## Resources and retention

Fixed resources:

- `ko-analysis://status`
- `ko-analysis://cache/info`

Resource templates:

- `ko-analysis://results/{result_id}`
- `ko-analysis://results/{result_id}/{section}`
- `ko-analysis://results/{result_id}/{section}/{offset}/{limit}`
- `kegg-cache://entries/{database}/{identifier}`

Result identifiers are opaque and scoped to one stdio server process. They cannot be read from
another scope, and normal stdio shutdown deletes the current scope. The default 24-hour value is
both the active-result hard TTL and the eligibility threshold for cleaning orphan rows left by an
abnormal exit; it is not a cross-process persistence promise. `get_server_status` reports
`result_scope="stdio_session"`, both TTL meanings, normal-exit cleanup, and
`durable_output="output_bundle"` explicitly. The result index lists validated section URIs.
High-level analysis normally retains `structured`, `summary`, and `annotations`; automatic target
selection also retains the applicable MODULE and pathway ranking and relationship artifacts.
Normalization retains `dataset`, and primitive tools retain `detail`.

`delete_analysis_result` removes one active result only when it belongs to the current scope.
Unknown, expired, already deleted, and cross-scope identifiers all return `RESULT_NOT_FOUND`.
`list_analysis_results` returns at most 100 metadata rows per page from only the current scope.
Operators can remove TTL-expired rows without starting the stdio server by running
`kegg-mcp cleanup --expired [--json]`. This command does not remove unexpired results or KEGG cache
entries. Durable delivery uses a non-overwriting output bundle controlled by the operator.

An artifact larger than the inline resource limit returns an
`artifact_requires_pagination` notice. Range resources return base64 content, exact byte counts,
and a continuation URI. Clients concatenate decoded pages in order and validate the reconstructed
content with its declared parser or schema; workflow digests are not part of this contract.

The internal resource parser accepts only canonical identifiers, section names, and numeric ranges;
visible traversal segments, encoded separators, queries, fragments, and malformed ranges are
rejected. The MCP SDK canonicalizes some dot-segment URI aliases before the handler can inspect the
original spelling. Such an alias can only resolve to the same validated, scoped canonical resource;
it cannot cross a result or process scope.

`kegg-cache://entries/...` is cache-only and never triggers network access or creates a retained
result. It returns a bounded parsed preview rather than the raw cached payload.

## Independent renderer MCP

`kegg-render-mcp` is a separate distribution and process. It accepts the core's version 3 handoff,
renders bounded static SVG or PNG, and never normalizes evidence or recomputes analysis. Its tools,
resources, access modes, result lifecycle, and security contract are documented in the
[renderer README](../companions/kegg-render-mcp/README.md) and
[visualization architecture](visualization-extension-plan.md).

## Errors and testing

Repairable tool failures use a schema-conforming error envelope with `code`, `message`,
`recoverable`, `suggested_action`, and bounded safe details. Input validation includes stage,
field path, issue type, and issue count. Result-store and output-directory failures use dedicated
codes rather than `CACHE_FAILED`. Invalid or unauthorized resource URIs use MCP protocol errors.
Endpoint URLs, environment values, credentials, raw tables, and cache payloads are not included in
status or error output.

Default local tests are offline. The governed core live campaign and independent synthetic renderer
job are defined in [the live-test guide](../tests/live/README.md) and the release checklist.
