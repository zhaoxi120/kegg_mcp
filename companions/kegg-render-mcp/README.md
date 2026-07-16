# kegg-render-mcp

`kegg-render-mcp` is an independently installed local stdio MCP companion for bounded KEGG
annotation-evidence graphics. It accepts the complete `render_input.json` version 2 handoff
written by `kegg-mcp`; it never imports annotation tables, assigns K numbers, evaluates MODULE
completion, or recomputes pathway coverage.

The first release renders:

- regular reference-pathway overlays from one matching KEGG PNG and KGML document; and
- project-owned MODULE logic diagrams from the authoritative core AST and complete render state.

SVG is canonical. PNG is an optional bounded derivative. Global and overview pathways are rejected
because their line-oriented graphics require a separately reviewed policy. `map` and
organism-specific pathway targets remain explicit summary-only core results; the first renderer
release accepts only regular `koNNNNN` reference targets. Graphics represent annotation evidence
and do not establish pathway presence, activity, flux, phenotype, or experimental validation.

## Installation

Install this directory independently with Python 3.11. The companion depends on a compatible
`kegg-mcp` 0.3.x release for the renderer contract and typed pathway-asset client.

```bash
uv sync --frozen --all-groups
```

No KEGG payload, model, weight, database, or font is bundled. Pillow performs bounded local PNG
decoding and raster output; no browser, JavaScript, subprocess, or shell command is used.

## Configuration

Two paths are required:

```bash
export KEGG_RENDER_MCP_STATE_ROOT=/absolute/private/renderer-state
export KEGG_RENDER_MCP_ALLOWED_ROOTS=/absolute/analysis-results
```

`KEGG_RENDER_MCP_ALLOWED_ROOTS` is a platform path-separator-delimited allowlist. Renderer input
and optional output directories must be direct, traversal-free paths below one of these roots.
Symlink escapes and unsafe writable intermediate directories are rejected.

Pathway access defaults to `public_academic`, which is permitted only for eligible academic users
and academic use. The explicit confirmation defaults to `true` in this academic repository:

```bash
export KEGG_RENDER_MCP_ACCESS_MODE=public_academic
export KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED=true
```

For an appropriately licensed deployment:

```bash
export KEGG_RENDER_MCP_ACCESS_MODE=licensed
export KEGG_RENDER_MCP_LICENSED_USE_CONFIRMED=true
export KEGG_RENDER_MCP_LICENSED_ENDPOINT=https://licensed.example.invalid
```

Set `KEGG_RENDER_MCP_ACCESS_MODE=unconfigured` for MODULE-only rendering. Status never returns
endpoint URLs, credentials, environment values, usernames, or local paths. Pathway assets use the
core client's HTTPS validation, cache, retry policy, and process-wide no-burst rate limit of no
more than three requests per second. Raw KEGG PNG and KGML payloads remain local and must not be
uploaded or redistributed without a specific rights review.

Optional bounded deployment settings are:

- `KEGG_RENDER_MCP_RETENTION_SECONDS` (default `86400`, maximum 30 days); and
- `KEGG_RENDER_MCP_MAX_DISK_BYTES` (default 256 MiB, maximum 2 GiB).

## MCP surface

Run the stdio server with:

```bash
uv run kegg-render-mcp
```

Tools:

- `get_renderer_status`
- `probe_renderer_kegg_connectivity`
- `render_analysis_bundle`
- `render_pathway`
- `render_module`
- `delete_render_result`

The probe performs exactly one explicit KEGG `INFO` request. Ordinary MODULE rendering is
closed-world. Pathway rendering may retrieve one image and one KGML document through the typed
core client.

Example high-level input:

```json
{
  "render_input_path": "/absolute/analysis-results/render_input.json",
  "output_directory": "/absolute/analysis-results/images",
  "formats": ["svg", "png"],
  "target_ids": ["ko00010", "M00001"]
}
```

Every successful call returns an opaque process-scoped `render_id`, bounded metadata, warnings,
provenance, and server-generated resource URIs:

```text
kegg-render://results/{render_id}
kegg-render://results/{render_id}/{artifact}
```

SVG resources use `image/svg+xml`; PNG resources return binary `image/png`. Unknown, expired,
deleted, and cross-process identifiers share the same safe not-found response. Explicit deletion
removes all artifacts in the result.

One state root is owned exclusively by one active renderer process. An owner-only advisory lock
prevents concurrent processes from sharing a quota namespace. After a crashed process releases the
lock, the next process removes only the bounded `scope_*` layout created by this companion before
accepting work, so abandoned artifacts cannot evade the configured disk quota.

## Security boundary

- Only schema version 2 is accepted. Version 1 is not losslessly upgradeable and requires a new
  core analysis bundle.
- The first release intentionally accepts a direct allowed-root `render_input.json` path only;
  inline renderer JSON is not part of the MCP request surface.
- Input JSON, target counts, assets, XML structure, coordinates, pixels, SVG nodes, artifact bytes,
  retained bytes, and disk use are bounded.
- KGML DTDs, entities, external resolution, excessive depth, and mismatched identities are
  rejected.
- PNG signatures, chunks (in the core client), decoded dimensions, decompression limits, and total
  pixels are validated before use.
- Generated SVG contains no scripts, event handlers, active links, remote fonts, or external image
  references. The matching source PNG is embedded as a static data URI.
- Artifact names derive only from validated canonical identifiers and fixed suffixes. Writes are
  atomic and owner-only.
- The server never uses `shell=True` and launches no subprocess.

## Development

All tests are synthetic and offline:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Release tests build the independent wheel and source distribution and reject KEGG payloads,
fixtures derived from KEGG, cache databases, and core or DeepKOALA implementation code.
