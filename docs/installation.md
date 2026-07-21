# Installation and operation

KEGG MCP provides three independent local stdio servers and three focused Codex Skills:

- `deepkoala-mcp` annotates an allowlisted protein FASTA with a configured local DeepKOALA
  installation;
- `kegg-mcp` normalizes KO evidence and performs KEGG-aware MODULE, pathway, and comparison
  analyses; and
- `kegg-render-mcp` renders the core's typed handoff as bounded static SVG or PNG.

The supported Codex path is the repository suite installer. It installs all three servers into
separate locked runtimes and registers one generated local plugin containing all three Skills and
MCP bindings. Generic MCP clients can instead install and configure the component servers
manually.

The installer implementation is present, but each release candidate remains gated on a real
installation and discovery of all three Skills and MCP servers in a new Codex task.

Release evidence and archive review belong in the
[release-readiness checklist](release-readiness.md). Tool schemas and result resources are
documented in [MCP tools, resources, and configuration](mcp-server.md).

## Requirements and support

| Platform | Core | DeepKOALA companion | Renderer |
| --- | --- | --- | --- |
| Linux with CPython 3.11.x | Supported and tested | Supported and tested | Supported and tested |
| macOS | Not release-supported | Not release-supported | Not release-supported |
| Windows | Not release-supported | Not release-supported | Not release-supported |

The suite installer requires existing absolute paths to:

- a CPython 3.11 executable;
- `uv` 0.11.16 or later with locked-sync support;
- Git; and
- a Codex CLI with local plugin commands.

The installer does not bootstrap or update these tools. It also does not install HMMER, KOfam
profiles, KEGG data, or later DeepKOALA model versions.

For live KEGG access, the deployment must use either:

- the public KEGG REST service only when both the user and work qualify for public academic use; or
- an HTTPS endpoint authorized under the operator's KEGG license.

## Architecture and distribution boundary

The complete FASTA-to-image workflow remains three separate processes:

```text
deepkoala-mcp -> deepkoala_annotations.csv -> kegg-mcp
kegg-mcp      -> render_input.json version 3 -> kegg-render-mcp
```

Each process has its own runtime, state, input validation, and MCP entry point. The core never
starts an annotator or renderer. The renderer does not normalize evidence or recompute MODULE
completion or pathway coverage.

The core Python wheel contains only `kegg-mcp`. A component wheel does not install
repository-scoped Skills or another server. The generated plugin is a local deployment artifact,
not a fourth distribution or a tracked copy of the Skills.

`render_input.json` uses the renderer-specific version 3 contract and carries
`AnalysisExecutionProvenance` version 3. Source KEGG PNG and KGML assets remain local and are not
included in tests, packages, or releases.

## Install the complete Codex suite

### 1. Prepare private and shared directories

Start from a reviewed release checkout or source archive. Create an owner-only private parent for
configuration and installation state, plus the shared roots that will contain user input and stable
handoff files:

```bash
mkdir -p /absolute/private
mkdir -p /absolute/project/inputs
mkdir -p /absolute/project/annotations
mkdir -p /absolute/project/analysis
mkdir -p /absolute/private/core
mkdir -p /absolute/private/kegg-rate-limit
mkdir -p /absolute/private/deepkoala-state
mkdir -p /absolute/private/renderer-state
chmod 700 /absolute/private
chmod 700 /absolute/private/core
chmod 700 /absolute/private/kegg-rate-limit
chmod 700 /absolute/private/deepkoala-state
chmod 700 /absolute/private/renderer-state
```

The installation root itself must not exist yet. Its direct parent must be owner-only. Do not place
the installation root inside the source checkout, an input/output root, a cache, or another
component's state root.

Private state roots must not overlap each other or the shared input/output roots. The core allowed
roots must cover the DeepKOALA input and output roots and every renderer handoff root.

### 2. Write the strict deployment TOML

Copy [`examples/config/kegg-mcp-suite.toml`](../examples/config/kegg-mcp-suite.toml) to the private
directory. The tracked file is a placeholder-only template, not a deployment configuration.

For an eligible public-academic deployment:

```toml
schema_version = 1

[kegg]
access_mode = "public_academic"
academic_use_confirmed = true
licensed_use_confirmed = false
rate_limit_root = "/absolute/private/kegg-rate-limit"

[core]
result_store_path = "/absolute/private/core/results.sqlite3"
allowed_roots = [
  "/absolute/project/inputs",
  "/absolute/project/annotations",
  "/absolute/project/analysis",
]

[deepkoala]
state_root = "/absolute/private/deepkoala-state"
input_roots = ["/absolute/project/inputs"]
output_roots = ["/absolute/project/annotations"]
allowed_models = ["full", "frag"]
cpu_threads = 2

[renderer]
state_root = "/absolute/private/renderer-state"
allowed_roots = ["/absolute/project/analysis"]
offline_allow_stale = false
```

Protect the file and its direct parent:

```bash
chmod 600 /absolute/private/kegg-mcp-deployment.toml
chmod 700 /absolute/private
```

The parser rejects unknown fields, wrong types, relative paths, symlinks, unsafe ownership or
permissions, missing roots, and inconsistent overlaps. Every configured path must be absolute.

### 3. Select one KEGG access mode

Keep the complete `core`, `deepkoala`, and `renderer` tables from the deployment template. Replace
the `kegg` table with exactly one of the following access profiles.

#### Public academic

Use `public_academic` only when the user and work qualify:

```toml
[kegg]
access_mode = "public_academic"
academic_use_confirmed = true
licensed_use_confirmed = false
rate_limit_root = "/absolute/private/kegg-rate-limit"
```

The endpoint is fixed to `https://rest.kegg.jp`. Core and Renderer share the configured owner-only
rate-limit root. The deployment defaults to two requests per second without burst and cannot be
configured above three requests per second. A KEGG `get` request contains at most ten entries.

#### Licensed

Non-academic operation requires an authorized HTTPS endpoint:

```toml
[kegg]
access_mode = "licensed"
academic_use_confirmed = false
licensed_endpoint = "https://kegg.example.edu/api"
licensed_use_confirmed = true
rate_limit_root = "/absolute/private/kegg-rate-limit"
```

Replace the example endpoint with the exact licensed endpoint. Do not include credentials in the
URL or store credentials in the repository. The confirmation records the operator's assertion; the
software does not determine whether an institution or activity is licensed.

#### Offline cache

Use `offline_cache` when the deployment must make no KEGG HTTP requests:

```toml
[kegg]
access_mode = "offline_cache"
academic_use_confirmed = false
licensed_use_confirmed = false
cache_path = "/absolute/private/cache/kegg.sqlite3"
rate_limit_root = "/absolute/private/kegg-rate-limit"
```

The cache must already exist as an owner-controlled regular file with mode `0600`. A missing or
disallowed stale entry returns a typed cache miss and never falls back to the network.

To read an existing licensed cache namespace offline, also provide the same canonical licensed
endpoint and set `licensed_use_confirmed=true`. The endpoint is used only to select the existing
namespace and is not contacted.

### 4. Run preflight

Use direct absolute executable paths:

```bash
/absolute/path/to/python3.11 \
  /absolute/path/to/kegg_mcp/scripts/install-suite.py \
  --config /absolute/private/kegg-mcp-deployment.toml \
  --install-root /absolute/private/kegg-mcp-install \
  --python /absolute/path/to/python3.11 \
  --uv /absolute/path/to/uv \
  --git /absolute/path/to/git \
  --codex /absolute/path/to/codex \
  --dry-run
```

Preflight validates the source tree, external tools, configuration, paths, and Codex conflicts. It
does not create a persistent installation or change Codex registration.

### 5. Confirm and install DeepKOALA

Each new suite installation root requires one explicit first-install confirmation. After informing
the user that the installer will clone the official DeepKOALA repository and install its upstream
Python requirements, run:

```bash
/absolute/path/to/python3.11 \
  /absolute/path/to/kegg_mcp/scripts/install-suite.py \
  --config /absolute/private/kegg-mcp-deployment.toml \
  --install-root /absolute/private/kegg-mcp-install \
  --python /absolute/path/to/python3.11 \
  --uv /absolute/path/to/uv \
  --git /absolute/path/to/git \
  --codex /absolute/path/to/codex \
  --allow-deepkoala-install
```

The suite-managed DeepKOALA checkout uses its bundled `202502` resources by default. The companion
keeps `device=auto`, allows only `full` and `frag` models configured by the deployment, and does not
install multi-domain dependencies. Successful annotation output reports the resolved model and
model date.

Later FASTA jobs in the same installed deployment do not repeat the installation question. A
different new installation root requires a new confirmation. This repository provides no model
updater; a later official model may be installed separately by an operator after a specific user
request and then selected by its installed date.

Dependency resolution for the three checked-in lockfiles is offline by default. If installation
reports that a locked artifact is unavailable, the operator may rerun the same installation command
with:

```text
--allow-locked-dependency-downloads
```

That switch authorizes `uv` network access only for artifacts selected by the lockfiles and their
declared build requirements. Python downloads remain disabled. It does not authorize model
updates, KOfam profiles, KEGG data, or KEGG pathway assets.

## Installed layout and lifecycle

A successful installation creates:

```text
/absolute/private/kegg-mcp-install/
  installation.json
  deployment/
  deepkoala/
  marketplace/
  runtimes/
    core/
    deepkoala/
    renderer/
```

The generated plugin contains the three canonical Skill trees and three absolute MCP launch
commands. A private launcher reads owner-only deployment metadata and directly executes exactly one
server without a shell. Private endpoints, roots, and external-runtime paths do not enter the
plugin metadata cached by Codex.

The installer is fresh-install only. It does not update, resume, or uninstall an existing
deployment. Existing marketplace, plugin, MCP names, or installation roots are conflicts rather
than update targets. Do not move an installed root because the launch commands contain absolute
paths.

If installation fails or is interrupted, follow the bounded recovery procedure in
[Troubleshooting](troubleshooting.md). Do not delete a preserved installation root while Codex
still references its marketplace or plugin.

Manual `codex mcp add` registration is not a supported substitute for the suite plugin and must not
be layered on top of it. It omits the repository Skills and can shadow the version-bound plugin
bindings. Maintainers who intentionally test legacy direct registration must isolate it from the
supported suite deployment.

## Minimal post-install verification

After installation:

1. Confirm that the generated `kegg-mcp` plugin is installed and enabled with the Codex app or
   `codex plugin list --json`.
2. Close the installation task.
3. Start a new Codex task in a workspace outside this source checkout.
4. Confirm discovery of these three Skills:
   - `deepkoala-annotation`
   - `kegg-ko-analysis`
   - `kegg-pathway-rendering`
5. Confirm that the plugin contributes `deepkoala-mcp`, `kegg-mcp`, and `kegg-render-mcp`.

Use one bounded status-only request for each dependency:

```text
Use $deepkoala-annotation to report its companion readiness only; do not submit a job.
Use $kegg-ko-analysis to report core server status only; do not retrieve KEGG data.
Use $kegg-pathway-rendering to report renderer status only; do not render an artifact.
```

The DeepKOALA companion must report `local_ready`. Core and Renderer status must show the intended
access mode without exposing credentials, endpoints, usernames, or full local paths.

For an eligible live acceptance check, ask:

> Use kegg-mcp in a bounded acceptance check. First confirm server status, then probe connectivity once.
> Retrieve only KO entry `K00844` and report its identifier, name, database release when
> available, and whether it came from network or cache. Stop after this entry.

This is a bounded discovery check, not a bulk compatibility campaign.

## Manual component installation for generic MCP clients

Use manual installation only for development or clients that do not consume the generated Codex
plugin. Install each required component from the same reviewed source baseline into an independent
environment.

For core development:

```bash
cd /absolute/path/to/kegg_mcp
uv sync --frozen
uv run --frozen kegg-mcp doctor
```

For an audited core wheel, replace `VERSION` with the version in the reviewed wheel filename:

```bash
python3.11 -m venv /absolute/private/core-venv
/absolute/private/core-venv/bin/python -m pip install /absolute/path/to/kegg_mcp-VERSION-py3-none-any.whl
/absolute/private/core-venv/bin/kegg-mcp doctor
```

The Python wheel installs the server command only; it does not install repository-scoped Skills.
Build and inspect wheels by following the [release-readiness checklist](release-readiness.md).

### Manual Core environment

The raw Core server reads environment variables and does not automatically load `.env` files.
Examples:

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

File handoff is disabled unless `KEGG_MCP_ALLOWED_ROOTS` lists existing absolute roots separated by
the platform path separator:

```text
KEGG_MCP_ALLOWED_ROOTS=/absolute/project/inputs:/absolute/project/annotations:/absolute/project/analysis
KEGG_MCP_RESULT_STORE_PATH=/absolute/private/core/results.sqlite3
```

Inputs and output directories must resolve beneath an allowed root. Traversal, symlink escapes, and
non-empty output targets are rejected.

### Generic JSON client configuration

The following public-academic example is configuration-file content, not a shell command:

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

Use direct absolute stdio commands. Do not use a remote URL, shell activation wrapper, `module
load`, or output redirection. Stdout is reserved for MCP protocol messages; diagnostics use stderr.

## One end-to-end workflow

With the suite plugin loaded in a new Codex task, a single request may cover all three stages:

> Annotate `/absolute/project/inputs/proteins.faa` with the configured local DeepKOALA installation
> into `/absolute/project/annotations/run-001`. Analyze the resulting KO evidence into
> `/absolute/project/analysis/run-001`, automatically selecting up to five MODULEs and up to five
> canonical KO reference pathways, then render the selected targets as SVG into
> `/absolute/project/analysis/run-001/images`. Report the resolved DeepKOALA model version.

The installed Skills are designed to pass stable files between their separate MCP dependencies:

1. `deepkoala-annotation` writes `deepkoala_annotations.csv` and
   `deepkoala_run_report.md`.
2. `kegg-ko-analysis` consumes the detailed CSV, preserves source provenance, and writes
   `render_input.json` plus the analysis bundle.
3. `kegg-pathway-rendering` consumes the unchanged handoff and writes static graphics and
   `render_manifest.json`.

If the user already has K numbers, annotation is skipped. For example:

> Analyze `K00001`, `K00002`, and `K00003` as an isolate proteome, select up to five MODULEs and
> pathways by default, write the analysis to `/absolute/project/analysis/ko-run-001`, and render
> the selected targets as SVG.

Automatic ranking uses unique selected-KO overlap to choose targets. MODULE ranking is target
selection, not completion or enrichment. Exact MODULE completion is calculated separately.
Pathway KO coverage is descriptive and does not establish pathway presence, completeness,
expression, activity, flux, phenotype, or statistical significance.

See [MCP tools, resources, and configuration](mcp-server.md) for explicit target requests, generic
annotation tables, result pagination, and complete schemas.

## Stable result paths

A core output directory may contain:

```text
normalized_annotations.tsv
protein_ko_mapping.tsv
module_ranking.tsv
ko_module_relationships.tsv
pathway_ranking.tsv
ko_pathway_relationships.tsv
pathway_coverage.tsv
module_completion.tsv
analysis_report.md
render_input.json
bundle_manifest.json
```

The directory must be new or empty. Files are never overwritten, and the manifest is published
last. Ranking and relationship tables remain local rather than being copied into every direct MCP
response.

Opaque job and result identifiers belong to the stdio process that created them. Use the returned
resource URI for bounded in-session retrieval. Use output-directory artifacts for durable
cross-process handoff. Result storage, retention, deletion, and resource templates are documented
in [Services, result storage, and reporting](services-results-reporting.md).

## Troubleshooting

Start with the redacted diagnostic outside the MCP client:

```bash
/absolute/path/to/kegg-mcp doctor
/absolute/path/to/kegg-mcp doctor --json
```

The diagnostic validates configuration without contacting KEGG or revealing configured paths and
endpoint values. For plugin discovery, installer recovery, offline cache misses, allowed-root
errors, result scope, and protocol stdout problems, use the dedicated
[Troubleshooting guide](troubleshooting.md).

## Rights and interpretation notice

Project source code is MIT licensed. That license does not grant rights to KEGG content,
DeepKOALA models, KOfam profiles, annotation databases, source pathway assets, or other third-party
materials. Cached KEGG responses and generated assets must remain local and out of version control,
packages, examples, CI artifacts, and releases. Redistribution of rendered derivatives requires a
separate rights review.

Review current primary sources before enabling live access:

- [KEGG API overview and usage restriction](https://www.kegg.jp/kegg/rest/)
- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html)
- [KEGG copyright and licensing notice](https://www.kegg.jp/kegg/legal.html)

This documentation is not legal advice. A K-number assignment is annotation evidence, not
experimental validation, and a source-rejected prediction is not evidence that a function is
absent.
