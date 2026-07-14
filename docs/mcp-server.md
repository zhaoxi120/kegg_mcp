# MCP server

The MVP runs as a local stdio server. It never launches DeepKOALA or another annotation tool,
and it does not expose remote HTTP transport.

## Start the server

After installing the project, configure an MCP client to run:

```text
uv run kegg-mcp
```

The default access mode is `offline_cache`. Server logs and configuration failures are written to
stderr; stdout is reserved for MCP protocol messages.

## KEGG access configuration

| Environment variable | Meaning |
| --- | --- |
| `KEGG_MCP_ACCESS_MODE` | `offline_cache` (default), `public_academic`, or `licensed` |
| `KEGG_MCP_ACADEMIC_USE_CONFIRMED` | Must equal `true` before public academic access is enabled |
| `KEGG_MCP_LICENSED_ENDPOINT` | Authorized HTTPS endpoint for licensed access |
| `KEGG_MCP_LICENSED_USE_CONFIRMED` | Must equal `true` before licensed access or licensed-cache reuse is enabled |
| `KEGG_MCP_CACHE_PATH` | Optional absolute path to the user-local KEGG cache |
| `KEGG_MCP_RESULT_STORE_PATH` | Optional absolute path to the user-local retained-result database |

The public KEGG REST service is limited to academic use by academic users. Other deployments must
use an appropriately licensed endpoint or an authorized local cache. The live client defaults to
two requests per second with no burst, enforces a hard process-wide maximum no greater than three
requests per second, and batches `get` requests at no more than ten entries.

To reuse a licensed cache without enabling network access, keep
`KEGG_MCP_ACCESS_MODE=offline_cache` and provide both licensed variables. The endpoint is used only
to select the matching cache namespace; no live request is made. Cache payloads and retained
results are local data and must not be committed, packaged, or attached to CI artifacts.

`get_server_status` and `ko-analysis://cache/info` report redacted configuration state. They do not
probe connectivity or enumerate cache contents.

## Tools

The server exposes eight tools:

- `analyze_ko_annotations`: one-call normalization and requested MODULE or pathway analysis. Supply
  either `ko_text` or a nested `annotations` request, plus at least one target.
- `normalize_ko_annotations`: normalize an inline plain KO list, explicitly mapped generic CSV/TSV,
  or DeepKOALA detailed table and retain the complete dataset.
- `get_kegg_entries`: retrieve selected allowlisted KEGG entries. It is not an arbitrary URL proxy.
- `map_ko_ids`: map selected K numbers to pathways, modules, reactions, EC numbers, or BRITE.
- `analyze_modules`: evaluate exact MODULE completion and required-block coverage from inline or
  retained evidence.
- `analyze_pathways`: calculate descriptive unique-KO coverage using an explicit `ko` or `map`
  reference namespace.
- `compare_ko_sets`: calculate deterministic set differences for two to ten datasets, with optional
  shared-reference MODULE or pathway comparisons.
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

Generic tables require explicit column mapping and a named decision policy. When `annotations` is
used in the high-level tool, biological context belongs inside that nested object. KO-only MCP
inputs do not accept organism-specific pathway references because they lack gene-level context.
The MCP normalization boundary limits individual fields, including string-valued source metadata,
to 16,384 characters even though the lower-level importer contract permits larger explicit limits.

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
`artifact_requires_pagination` notice. Range resources return base64 content, an exact byte count,
the artifact hash, and a continuation URI. Clients must concatenate decoded pages in order and may
verify the final SHA-256 digest.

The internal resource parser accepts only canonical identifiers, section names, and numeric ranges;
visible traversal segments, encoded separators, queries, fragments, and malformed ranges are
rejected. The MCP SDK canonicalizes some dot-segment URI aliases before the handler can inspect the
original spelling. Such an alias can only resolve to the same validated, scoped canonical resource;
it cannot cross a result or process scope.

`kegg-cache://entries/...` is cache-only and never triggers network access or creates a retained
result. It returns a bounded parsed preview rather than the raw cached payload.

## Errors and testing

Repairable tool failures use a schema-conforming error envelope with `code`, `message`,
`recoverable`, `suggested_action`, and bounded safe details. Invalid or unauthorized resource URIs
use MCP protocol errors. Paths, endpoint URLs, environment values, credentials, raw tables, and
cache payloads are not included in status or error output.

The default test suite is offline and must not contact KEGG. Live checks are separate manual tests
for an eligible academic user or an authorized licensed endpoint and should use only a few explicit
requests.
