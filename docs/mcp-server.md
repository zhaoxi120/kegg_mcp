# MCP server

The core MVP runs as a local stdio server. It never launches DeepKOALA or another annotation tool,
and it does not expose remote HTTP transport. The optional `deepkoala-mcp` companion is a separate
process and distribution documented in its
[README](../companions/deepkoala-mcp/README.md).

## Start the server

After installing the project, configure an MCP client to run:

```text
uv run kegg-mcp
```

The default access mode is confirmed `public_academic`. Server logs and configuration failures are written to
stderr; stdout is reserved for MCP protocol messages.

Local pytest skips live requests by default. Pull-request CI explicitly runs the bounded
20-request public-academic compatibility campaign once.

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
| `KEGG_MCP_ACCESS_MODE` | `public_academic` (default) or `licensed` |
| `KEGG_MCP_ACADEMIC_USE_CONFIRMED` | Defaults to `true`; an explicit value other than `true` is rejected |
| `KEGG_MCP_LICENSED_ENDPOINT` | Authorized HTTPS endpoint for licensed access |
| `KEGG_MCP_LICENSED_USE_CONFIRMED` | Must equal `true` before licensed access is enabled |
| `KEGG_MCP_CACHE_PATH` | Optional absolute path to the user-local KEGG cache |
| `KEGG_MCP_RESULT_STORE_PATH` | Optional absolute path to the user-local retained-result database |
| `KEGG_MCP_ALLOWED_ROOTS` | Path-separated existing directories allowed for file input and output bundles |

The public KEGG REST service is limited to academic use by academic users. Other deployments must
use an appropriately licensed endpoint. The live client defaults to
two requests per second with no burst, enforces a hard process-wide maximum no greater than three
requests per second, and batches `get` requests at no more than ten entries.

Cache payloads and retained results are local data and must not be committed, packaged, or attached
to CI artifacts. Ordinary KEGG operations refresh from the network by default; the cached-entry
resource remains a cache-only read and never falls back to the network.

`get_server_status` and `ko-analysis://cache/info` report redacted configuration state. Status
includes `file_handoff_enabled` and `allowed_root_count`, but never the configured roots. These
surfaces do not probe connectivity or enumerate cache contents. Use the explicit read-only
connectivity tool when a live-access preflight is required.

The MCP initialization response guides clients toward the high-level analysis tool and records
the connectivity, file-handoff, result-scope, stable-bundle, and biological interpretation
boundaries. It does not replace the explicit input and output schemas.

## Tools

The server exposes nine tools:

- `analyze_ko_annotations`: one-call normalization and MODULE/pathway analysis. Supply either
  `ko_text` or a nested `annotations` request. If no target is supplied, accepted K numbers are
  mapped to canonical reference pathways within deployment bounds.
- `normalize_ko_annotations`: normalize inline content or an allowed-root file containing plain
  K numbers, generic CSV/TSV, or a DeepKOALA detailed table, then retain the complete dataset.
- `get_kegg_entries`: retrieve selected allowlisted KEGG entries. It is not an arbitrary URL proxy.
- `map_ko_ids`: map selected K numbers to pathways, modules, reactions, EC numbers, or BRITE.
- `analyze_modules`: evaluate exact MODULE completion and required-block coverage from inline or
  retained evidence.
- `analyze_pathways`: calculate descriptive unique-KO coverage after inferring and validating the
  pathway namespace; omitted `mapNNNNN` input is canonicalized to the `koNNNNN` reference view.
- `compare_ko_sets`: calculate deterministic set differences for two to ten datasets, with optional
  shared-reference MODULE or pathway comparisons.
- `probe_kegg_connectivity`: make one explicit low-cost INFO request and classify DNS,
  connection, or authorization/configuration outcomes.
- `get_server_status`: return redacted access, capability, and result-retention information.

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

Generic tables with unambiguous common headers are mapped automatically and the decision is
reported; ambiguous or non-standard tables require an explicit mapping. When `annotations` is used
in the high-level tool, biological context belongs inside that nested object. KO-only MCP inputs do
not accept organism-specific pathway references because they lack gene-level context. Cache tuning,
refresh flags, and internal limit models are deployment-owned rather than ordinary tool inputs.

File input and `output_directory` are disabled until `KEGG_MCP_ALLOWED_ROOTS` is configured. Paths
must be absolute and resolve beneath an allowed root. Traversal, missing files, symlink escapes, and
unsafe output ancestors are rejected. A successful normalization bundle contains
`normalized_annotations.tsv`, `protein_ko_mapping.tsv`, and `bundle_manifest.json`; analysis adds
`pathway_coverage.tsv`, `module_completion.tsv`, `analysis_report.md`, and `render_input.json`.
The report records the original absolute input path when source provenance supplies it.

Direct tool responses are bounded previews. Complete immutable evidence and analysis detail stay
in the retained resource. A K number is an annotation, MODULE exact completion is distinct from
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

Result identifiers are opaque and scoped to one stdio server process. They expire under the local
retention policy and cannot be read from another scope. The result index lists validated section
URIs. High-level analysis normally retains `structured`, `summary`, and `annotations`; normalization
retains `dataset`; primitive tools retain `detail`.

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

## Errors and testing

Repairable tool failures use a schema-conforming error envelope with `code`, `message`,
`recoverable`, `suggested_action`, and bounded safe details. Input validation includes stage,
field path, issue type, and issue count. Result-store and output-directory failures use dedicated
codes rather than `CACHE_FAILED`. Invalid or unauthorized resource URIs use MCP protocol errors.
Endpoint URLs, environment values, credentials, raw tables, and cache payloads are not included in
status or error output.

Pull-request CI runs one serialized campaign of 20 requests (five for each supported operation)
with zero retries and no uploaded KEGG payloads. Local live checks are opt-in and accept a bounded
per-operation count from 1 through 30. Additional manual checks should use only the minimum
explicit requests.
