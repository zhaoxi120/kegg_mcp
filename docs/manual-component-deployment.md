# Manual Component Deployment

Use manual installation only for development or MCP clients that do not consume the generated
Codex plugin. The repository suite installer remains the supported complete-suite Codex path on
Linux and Apple Silicon macOS. Install each required component from the same reviewed source
baseline into an independent environment.

The Core and DeepKOALA wheels do not depend on another server distribution. The Renderer wheel
installs the compatible Core distribution because it reuses Core's typed contracts and KEGG asset
client, but it neither registers nor starts the Core stdio server. No component wheel installs
repository-scoped Skills. Build and inspect release wheels by following the [release-readiness
checklist](release-readiness.md).

## Platform boundary

Core, DeepKOALA, and Renderer support Linux and the reviewed native Apple Silicon route on macOS 14
or later with CPython 3.11.x. The all-component suite installer supports those same two targets.
Native Intel macOS and native Windows are unsupported because the servers require reviewed POSIX
path, lock, ownership, and atomic-publication guarantees; use WSL2 as the Linux route on Windows
hosts.

On the DeepKOALA macOS route, both `deepkoala-mcp` and the separately configured DeepKOALA Python
must be native arm64 CPython 3.11 on macOS 14 or later. Rosetta-translated runtimes are rejected.

In WSL2, keep the checkout, virtual environments, state, cache, inputs, and outputs in the Linux
filesystem, such as beneath `/home`, rather than under `/mnt/c`. The command examples below use the
POSIX virtual-environment layout shared by Linux, macOS, and WSL2.

## Core development and wheel installation

For Core development:

```bash
cd /absolute/path/to/kegg_mcp
uv sync --frozen
uv run --frozen kegg-mcp doctor
```

For an audited Core wheel, replace `VERSION` with the version in the reviewed wheel filename:

```bash
python3.11 -m venv /absolute/private/core-venv
/absolute/private/core-venv/bin/python -m pip install /absolute/path/to/kegg_mcp-VERSION-py3-none-any.whl
/absolute/private/core-venv/bin/kegg-mcp doctor
```

## Manual Core environment

The raw Core server reads environment variables and does not automatically load `.env` files. It
defaults to network-disabled `offline_cache`; the default does not fall back to live access when a
cache entry is unavailable. These examples apply only to a directly installed Core server; the
suite deployment TOML requires the operator to select the access mode explicitly.

```text
# Eligible public-academic use
KEGG_MCP_ACCESS_MODE=public_academic
KEGG_MCP_ACADEMIC_USE_CONFIRMED=true

# Licensed use
KEGG_MCP_ACCESS_MODE=licensed
KEGG_MCP_LICENSED_ENDPOINT=https://kegg.example.edu/api
KEGG_MCP_LICENSED_USE_CONFIRMED=true

# Network-disabled use
KEGG_MCP_ACCESS_MODE=offline_cache
KEGG_MCP_CACHE_PATH=/absolute/private/cache/kegg.sqlite3
```

`public_academic` never activates without the explicit `true` confirmation shown above. A directly
installed Renderer defaults to `unconfigured`, which supports MODULE-only rendering without
pathway asset access. Set both `KEGG_RENDER_MCP_ACCESS_MODE=public_academic` and
`KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED=true` only for eligible public-academic use.

File handoff is disabled unless `KEGG_MCP_ALLOWED_ROOTS` lists existing absolute roots separated
by the platform path separator:

```text
KEGG_MCP_ALLOWED_ROOTS=/absolute/project/inputs:/absolute/project/annotations:/absolute/project/analysis
KEGG_MCP_RESULT_STORE_PATH=/absolute/private/core/results.sqlite3
```

Inputs and output directories must resolve beneath an allowed root. Traversal, symlink escapes, and
non-empty output targets are rejected. KO-analysis bundles, selected-reference bundles, and KEGG
Mapper/Syntax input bundles all use this same boundary; the latter two require an explicit output
directory.

## Generic JSON client configuration

The following public-academic example is configuration-file content, not a shell command. Use it
only when the user and work qualify for public academic KEGG access.

```json
{
  "mcpServers": {
    "kegg-mcp": {
      "command": "/absolute/private/core-venv/bin/kegg-mcp",
      "env": {
        "KEGG_MCP_ACCESS_MODE": "public_academic",
        "KEGG_MCP_ACADEMIC_USE_CONFIRMED": "true",
        "KEGG_MCP_ALLOWED_ROOTS": "/absolute/project/inputs:/absolute/project/annotations:/absolute/project/analysis"
      }
    },
    "kegg-render-mcp": {
      "command": "/absolute/private/renderer-venv/bin/kegg-render-mcp",
      "env": {
        "KEGG_RENDER_MCP_STATE_ROOT": "/absolute/private/renderer-state",
        "KEGG_RENDER_MCP_ALLOWED_ROOTS": "/absolute/project/analysis",
        "KEGG_RENDER_MCP_ACCESS_MODE": "public_academic",
        "KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED": "true"
      }
    }
  }
}
```

Register `deepkoala-mcp` as a third independent server only when FASTA annotation is required. A
manual deployment needs an existing official DeepKOALA checkout and Python environment; follow the
[DeepKOALA companion README](../companions/deepkoala-mcp/README.md). Renderer installation,
licensed/offline configuration, and cache rules are in the
[Renderer companion README](../companions/kegg-render-mcp/README.md).

When Core will consume DeepKOALA output, include every configured DeepKOALA input and output root in
`KEGG_MCP_ALLOWED_ROOTS`. Core validates both the unchanged source input path and the generated CSV
path; the shared stable output path is required for files larger than 5,000,000 bytes. The
controlled resource-to-inline recovery route is only for smaller successful outputs after the exact
typed Core path rejection; it is not a large-file transport. The supported suite installer
validates this root coverage automatically.

Use direct absolute stdio commands. Do not use a remote URL, shell activation wrapper, `module
load`, or output redirection. Stdout is reserved for MCP protocol messages; diagnostics use stderr.
The component tools, resource schemas, and Core environment contract are documented in
[Core MCP server](mcp-server.md).
