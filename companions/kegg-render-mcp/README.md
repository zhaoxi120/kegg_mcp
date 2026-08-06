# kegg-render-mcp

`kegg-render-mcp` is the independently packaged local stdio renderer for KEGG annotation-evidence
graphics. It consumes the complete `render_input.json` schema version 4 handoff produced by a
compatible `kegg-mcp` analysis. It never imports annotation tables, assigns K numbers, evaluates
MODULE completion, or recomputes pathway coverage.

It supports regular canonical `koNNNNN` box overlays, explicitly opted-in canonical KO
global/overview total-map line overlays from one matching KEGG PNG/KGML pair, and project-owned
MODULE logic diagrams from the authoritative core AST. SVG is canonical, PNG is an optional bounded
derivative, and artifacts are available through local directories and scoped MCP resources.

Accepted evidence uses a solid vivid-red (`#FF0000`) overlay. Policy-defined uncertain evidence
uses orange (`#E69F00`) plus a dashed outline or polyline, and accepted evidence retains precedence
on shared graphics. Unmatched graphics remain unchanged. Original pathway-category colors in the
source PNG are background context, not evidence of biological presence or absence.

Core continues to exclude Global, Overview, and higher-level Overview maps from automatic Top-N
selection. An explicit broad target is renderable only when Core evaluated a canonical KO reference
with `allow_global_or_overview=true` and emitted complete evidence in the version 4 handoff. The
renderer follows bounded KGML line coordinates while preserving arrows already present in the
source PNG; it does not reconstruct arrow direction or infer pathway direction, activity,
completeness, flux, phenotype, or experimental validation. `map` and organism-specific targets
remain summary-only.

## Installation

For the complete Codex suite on Linux or native Apple Silicon macOS 14 or later, use the repository
suite installer described in [Installation and operation](../../docs/installation.md). It creates
an isolated Renderer runtime and registers this server together with the matching Skill, Core, and
DeepKOALA companion while preserving three independent stdio processes. DeepKOALA uses explicit
CUDA on Linux or explicit MPS on supported macOS only when the configured runtime reports that
backend available; CPU remains the default.

For component development or manual registration by another MCP client, use Linux or native Apple
Silicon macOS 14 or later with CPython 3.11 and synchronize this directory independently:

```bash
uv sync --frozen --all-groups
```

Native Intel macOS and native Windows are unsupported because the Renderer requires reviewed POSIX
no-follow path operations, ownership checks, atomic publication, and file locks. Windows hosts use
WSL2 as the Linux route; keep the checkout, state, cache, input, and output paths in the WSL Linux
filesystem rather than under `/mnt/c`.

The package declares its compatible `kegg-mcp` range for the renderer contract and typed pathway
asset client. It bundles no KEGG payload, database, model, weight, or font. Pillow performs bounded
local PNG decoding and raster output; no browser, JavaScript, shell, or external rendering command
is used. On macOS, the shared Core rate limiter obtains the boot timestamp through one fixed,
bounded `/usr/sbin/sysctl` call without a shell or caller-controlled arguments.

## Configuration

The state root and at least one allowed file root are required:

```bash
export KEGG_RENDER_MCP_STATE_ROOT=/absolute/private/renderer-state
export KEGG_RENDER_MCP_ALLOWED_ROOTS=/absolute/analysis-results
```

`KEGG_RENDER_MCP_ALLOWED_ROOTS` is a platform path-separator-delimited allowlist. Renderer input and
explicit output directories must be direct, traversal-free paths below an allowed root. The last
configured root is the default output root when `output_directory` is omitted. The private state
root must not overlap an allowed root. Symlink escapes and unsafe writable ancestry are rejected.
Multiple renderer processes may share one deployment state root. Each process holds an isolated
live scope, and abandoned-scope cleanup never removes a scope whose lease is still active.

Pathway access uses one deployment-wide mode:

The defaults below describe a directly configured Renderer distribution. A suite installation does
not silently inherit them: its deployment TOML requires the operator to select and confirm the KEGG
access mode explicitly.

| Mode | Configuration and behavior |
| --- | --- |
| `public_academic` | Live public KEGG access for eligible academic use; requires `KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED=true`. |
| `licensed` | Set `KEGG_RENDER_MCP_LICENSED_USE_CONFIRMED=true` and `KEGG_RENDER_MCP_LICENSED_ENDPOINT` for an authorized HTTPS endpoint. |
| `offline_cache` | Set `KEGG_RENDER_MCP_CACHE_PATH` to one existing Core-compatible cache; access is network-disabled and read-only. |
| `unconfigured` | Default; MODULE-only rendering with no pathway asset access. |

Select another mode with `KEGG_RENDER_MCP_ACCESS_MODE`. `public_academic` never activates from the
mode alone; the explicit confirmation is mandatory.

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

`tools/list` publishes self-contained Draft 2020-12 input schemas with explicit properties,
bounds, descriptions, and inline format enums. Each path-or-inline alternative repeats the full
object property surface so clients that prioritize composition branches still expose
`render_input_path`, `render_input_json`, `output_directory`, `formats`, and `target_ids`. Local
Pydantic references are not exposed. This compatibility shape was reviewed against the official
[OpenAI Codex `rust-v0.144.6` source](https://github.com/openai/codex/tree/rust-v0.144.6/codex-rs/tools/src)
retrieved on 2026-07-22.

`render_analysis_bundle` is the normal multi-target entry point. `render_pathway` and
`render_module` render one canonical target. A live connectivity probe makes exactly one explicit
KEGG `INFO` request; `offline_cache` and `unconfigured` probes make zero requests. MODULE rendering
is closed-world. A pathway render may retrieve one image and one KGML document through the typed
Core client.

The multi-target operation is all-or-nothing. It preflights every selected target's capability,
encodes all requested artifacts, and only then retains and publishes one result. Failure of any
target, pathway asset, encoding bound, output bound, or publication returns no partial
`RenderResult`; a target-specific error identifies the failing `target_id`. Retry with a smaller
`target_ids` set when appropriate rather than merging partial work into the failed bundle.

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
`render_input_json`. Only schema version 4 is accepted. A schema mismatch returns an actionable
incompatible-input error; the renderer never repairs or reinterprets the handoff.

The fixed status resource is `kegg-render://status`. Result templates are:

```text
kegg-render://results/{render_id}
kegg-render://results/{render_id}/{artifact}
```

## Results and lifecycle

A successful call returns an opaque process-scoped `render_id`, the resolved output directory,
bounded metadata, warnings, server-generated resource URIs, and stable artifact output paths. Image
response metadata includes MIME type, byte size, and dimensions. The published schema-version-2
`render_manifest.json` records renderer, analysis, target, retrieval/cache, and artifact provenance
without exposing private configuration. Each image entry records a controlled relative `path`, MIME
type, byte size, width, and height. Process-scoped render IDs, expiry timestamps, and
resource URIs remain only in the MCP result metadata and are not written into the durable manifest.

When `output_directory` is omitted, the renderer allocates a fresh directory beneath the default
output root. An explicit output directory must be new or empty. Artifacts are published without
replacement and the manifest is installed last. Failed publication removes only files created by
that operation, and no partial render result is retained. SVG resources use `image/svg+xml`; PNG
resources return binary `image/png`.

Retained results have bounded count, lifetime, and disk use and belong to one active renderer
process even when several processes share the deployment state root. Unknown, expired, deleted,
and cross-process identifiers share the same safe not-found response. `delete_render_result`
removes the retained result and its artifacts; a durable exported output directory remains
operator-owned.

## Security boundary

Input JSON, targets, XML, coordinates, per-polyline and total points, total polyline length,
graphic-to-KO associations, dimensions, pixels, SVG nodes, artifact bytes, retained results,
storage, and cleanup are bounded. Geometry coordinates must be short ASCII non-negative integers;
line-coordinate lists must also be non-degenerate and inside the matching PNG. The renderer
reserves manifest capacity before generating a multi-target artifact set and fails the whole bundle
before retention or export when the cumulative result budget is exceeded. The renderer accepts the single
inert KEGG KGML v0.7.2
HTTPS `SYSTEM` declaration observed on 2026-07-21, but never resolves or fetches its DTD. Other DTD
declarations, entity declarations, external entity resolution, excessive depth, and mismatched
identities are rejected. PNG structure, dimensions, decompression, and total pixels are validated
before use.

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
