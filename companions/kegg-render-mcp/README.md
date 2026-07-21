# kegg-render-mcp

`kegg-render-mcp` is the independently packaged local stdio renderer for KEGG annotation-evidence
graphics. It consumes the complete `render_input.json` schema version 3 handoff produced by a
compatible `kegg-mcp` analysis. It never imports annotation tables, assigns K numbers, evaluates
MODULE completion, or recomputes pathway coverage.

It supports regular canonical `koNNNNN` overlays from one matching KEGG PNG/KGML pair and
project-owned MODULE logic diagrams from the authoritative core AST. SVG is canonical, PNG is an
optional bounded derivative, and artifacts are available through local directories and scoped MCP
resources.

Global and overview pathway overlays are unsupported because they require a separately reviewed
line-overlay policy. `map` and organism-specific targets remain explicit summary-only core results;
only regular KO reference pathways are renderable. Graphics describe annotation evidence and do not
establish pathway presence, completeness, activity, flux, phenotype, or experimental validation.

## Installation

For Codex, use the repository suite installer described in
[Installation and operation](../../docs/installation.md). It creates an isolated Renderer runtime
and registers this server together with the matching Skill, Core, and DeepKOALA companion while
preserving three independent stdio processes.

For component development or manual registration by another MCP client, use Linux with Python 3.11
and synchronize this directory independently:

```bash
uv sync --frozen --all-groups
```

The package declares its compatible `kegg-mcp` range for the renderer contract and typed pathway
asset client. It bundles no KEGG payload, database, model, weight, or font. Pillow performs bounded
local PNG decoding and raster output; no browser, JavaScript, subprocess, or shell command is used.

## Configuration

The state root and at least one allowed file root are required:

```bash
export KEGG_RENDER_MCP_STATE_ROOT=/absolute/private/renderer-state
export KEGG_RENDER_MCP_ALLOWED_ROOTS=/absolute/analysis-results
```

`KEGG_RENDER_MCP_ALLOWED_ROOTS` is a platform path-separator-delimited allowlist. Renderer input and
optional output directories must be direct, traversal-free paths below an allowed root. The private
state root must not overlap an allowed root. Symlink escapes and unsafe writable ancestry are
rejected.

Pathway access uses one deployment-wide mode:

| Mode | Configuration and behavior |
| --- | --- |
| `public_academic` | Default live public KEGG access for eligible academic use; `KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED` defaults to `true`. |
| `licensed` | Set `KEGG_RENDER_MCP_LICENSED_USE_CONFIRMED=true` and `KEGG_RENDER_MCP_LICENSED_ENDPOINT` for an authorized HTTPS endpoint. |
| `offline_cache` | Set `KEGG_RENDER_MCP_CACHE_PATH` to one existing Core-compatible cache; access is network-disabled and read-only. |
| `unconfigured` | MODULE-only rendering with no pathway asset access. |

Select a non-default mode with `KEGG_RENDER_MCP_ACCESS_MODE`.

Offline mode makes no HTTP request, creates or mutates no cache, and returns a typed unavailable
result for a missing or unusable entry. It selects the public-academic cache namespace by default.
To select a previously populated licensed namespace, also provide the licensed confirmation and
canonical endpoint; the endpoint is used only for namespace identity and is never contacted or
returned.

Stale offline assets are rejected by default. `KEGG_RENDER_MCP_OFFLINE_ALLOW_STALE=true` permits
them for the whole deployment.

Accepted stale use remains explicit in warnings, timestamps, and artifact provenance. Core and
Renderer share `KEGG_MCP_RATE_LIMIT_ROOT` and the same endpoint-scoped no-burst limit of at most
three requests per second. Raw KEGG assets and cache payloads remain local.

Optional retention settings are `KEGG_RENDER_MCP_RETENTION_SECONDS` (default 86,400, maximum 30
days), `KEGG_RENDER_MCP_MAX_RESULTS` (default 128, maximum 4,096), and
`KEGG_RENDER_MCP_MAX_DISK_BYTES` (default 256 MiB, maximum 2 GiB).

## MCP surface

Run the manually configured stdio server with:

```bash
uv run kegg-render-mcp
```

The six tools are `get_renderer_status`, `probe_renderer_kegg_connectivity`,
`render_analysis_bundle`, `render_pathway`, `render_module`, and `delete_render_result`.

`render_analysis_bundle` is the normal multi-target entry point. `render_pathway` and
`render_module` render one canonical target. A live connectivity probe makes exactly one explicit
KEGG `INFO` request; `offline_cache` and `unconfigured` probes make zero requests. MODULE rendering
is closed-world. A pathway render may retrieve one image and one KGML document through the typed
Core client.

Example high-level input:

```json
{
  "render_input_path": "/absolute/analysis-results/render_input.json",
  "output_directory": "/absolute/analysis-results/images",
  "formats": ["svg", "png"],
  "target_ids": ["ko00010", "M00001"]
}
```

Every render tool accepts exactly one handoff source: an allowed `render_input_path` or bounded
`render_input_json`. Older or malformed schema versions return an actionable incompatible-input
error; the renderer never repairs or upgrades a handoff.

The fixed status resource is `kegg-render://status`. Result templates are:

```text
kegg-render://results/{render_id}
kegg-render://results/{render_id}/{artifact}
```

## Results and lifecycle

A successful call returns an opaque process-scoped `render_id`, bounded metadata, warnings, and
server-generated resource URIs. Image metadata includes MIME type, byte size, and dimensions. The
published `render_manifest.json` records renderer, analysis, target, retrieval/cache, and artifact
provenance without exposing private configuration.

An optional output directory must be new or empty. Artifacts are published without replacement and
the manifest is installed last. Failed publication removes only files created by that operation.
SVG resources use `image/svg+xml`; PNG resources return binary `image/png`.

Retained results have bounded count, lifetime, and disk use and belong to one active renderer
process. Unknown, expired, deleted, and cross-process identifiers share the same safe not-found
response. `delete_render_result` removes the retained result and its artifacts; a durable exported
output directory remains operator-owned.

## Security boundary

Input JSON, targets, XML, coordinates, dimensions, pixels, SVG nodes, artifact bytes, retained
results, storage, and cleanup are bounded. KGML DTDs, entities, external resolution, excessive
depth, and mismatched identities are rejected. PNG structure, dimensions, decompression, and total
pixels are validated before use.

Generated SVG has no scripts, event handlers, active links, remote fonts, or external resources;
the validated source PNG is embedded as static data. Artifact names derive only from canonical
identifiers and fixed suffixes, and writes use restrictive permissions. Status and errors redact
credentials, environment values, usernames, endpoint URLs, cache identity, and full local paths.

## Validation

All component tests use synthetic assets and make no live KEGG requests:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Release tests build and audit the independent wheel and source distribution. Packages and fixtures
exclude KEGG payloads, KEGG-derived images or XML, cache databases, and Core or DeepKOALA
implementation code.
