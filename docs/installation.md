# Installation and operation

KEGG MCP provides three independent local stdio servers and three focused Codex Skills:

- `deepkoala-mcp` annotates an allowlisted protein FASTA with a configured local DeepKOALA
  installation;
- `kegg-mcp` normalizes KO evidence and performs KEGG-aware MODULE, pathway, and comparison
  analyses; and
- `kegg-render-mcp` renders the core's typed handoff as bounded static SVG or PNG.

The supported complete-suite Codex path is the repository suite installer on Linux or Apple
Silicon macOS. It installs all three servers into separate locked runtimes and registers one
generated local plugin containing all three Skills and MCP bindings. Generic MCP clients can
instead install and configure supported component servers manually.

The [release-readiness checklist](release-readiness.md) owns current release status, exact-candidate
installation evidence, and archive review. Tool schemas and result resources are documented in
[MCP tools, resources, and configuration](mcp-server.md).

## Requirements and support

| Platform | Core | DeepKOALA companion | Renderer | Suite installer |
| --- | --- | --- | --- | --- |
| Linux with CPython 3.11.x | Supported and tested | Supported and tested | Supported and tested | Supported and tested |
| Apple Silicon, macOS 14+, native CPython 3.11.x | Supported | Supported with CPU or explicit MPS | Supported | Supported; exact-candidate smoke required |
| Native Intel macOS | Unsupported | Unsupported | Unsupported | Unsupported |
| Windows host with WSL2 Linux | Use the Linux route | Use the Linux route | Use the Linux route | Use the Linux route |
| Native Windows | Unsupported | Unsupported | Unsupported | Unsupported |

Core and Renderer require POSIX no-follow filesystem operations, ownership checks, atomic
publication, and the deployment-wide file-locking backend. Native Windows does not provide the
same reviewed guarantees, so there is no weakened fallback. Its diagnostic reports the unsupported
platform instead of starting a server.

WSL2 is the formal Windows-host route. Install and run the Linux suite entirely inside WSL2. Keep
the source checkout, installation, state, cache, biological inputs, and generated outputs in the
WSL Linux filesystem, such as beneath `/home`, rather than under `/mnt/c`. Apple Silicon macOS uses
the complete suite with a native arm64 interpreter. Native Intel macOS is unsupported.

The WSL route was reviewed on 2026-08-01 against Microsoft's official
[WSL installation guide](https://learn.microsoft.com/en-us/windows/wsl/install) and
[filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems). Keeping this
deployment inside the Linux filesystem is also a project security boundary for its POSIX ownership
and descriptor-relative path checks, not only a performance recommendation.

The suite installer requires Linux or Apple Silicon macOS and existing absolute paths to:

- a CPython 3.11 executable;
- `uv` 0.11.16 or later with locked-sync support;
- Git; and
- a Codex CLI with local plugin commands.

The installer does not bootstrap or update these tools. It also does not install HMMER, KOfam
profiles, KEGG data, or later DeepKOALA model versions.

For live KEGG access, the deployment must use either:

- the public KEGG REST service only when both the user and work qualify for public academic use; or
- an HTTPS endpoint authorized under the operator's KEGG license.

A direct Core installation defaults to network-disabled `offline_cache`. A direct Renderer
installation defaults to `unconfigured`, which permits MODULE rendering but no pathway asset
access. Selecting `public_academic` requires the component's explicit confirmation variable:
`KEGG_MCP_ACADEMIC_USE_CONFIRMED=true` for Core or
`KEGG_RENDER_MCP_ACADEMIC_USE_CONFIRMED=true` for Renderer. The suite TOML always requires an
explicit access mode and confirmation; it never inherits component defaults.

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

For a direct, manually configured Core server, file handoff remains disabled until
`KEGG_MCP_ALLOWED_ROOTS` is configured; the complete manual environment belongs in
[Manual component deployment](manual-component-deployment.md).

`render_input.json` uses the renderer-specific version 3 contract and carries
`AnalysisExecutionProvenance` version 3. Source KEGG PNG and KGML assets remain local and are not
included in tests, packages, or releases.

## Install the complete Codex suite

Run this section on Linux, including a WSL2 Linux environment, or on native Apple Silicon macOS.
Native Intel macOS and native Windows are unsupported.

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
allow_multi = false

[renderer]
state_root = "/absolute/private/renderer-state"
allowed_roots = ["/absolute/project/analysis"]
offline_allow_stale = false
```

Root order defines service-managed defaults when a caller omits `output_directory`: Core uses the
last `core.allowed_roots` entry, DeepKOALA uses the last `deepkoala.output_roots` entry, and the
Renderer uses the last `renderer.allowed_roots` entry. In this example, annotation output goes
beneath `/absolute/project/annotations`, while Core and Renderer output goes beneath
`/absolute/project/analysis`. Explicit allowed output paths still take precedence.

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
the user that the installer will initialize a private checkout, fetch the pinned official
DeepKOALA revision, and install its upstream Python requirements, run:

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

The installer initializes an empty local repository, fetches only the exact official DeepKOALA
revision `bebbe0c43f50a26488f7092f6b355aae870a4ed9`, which introduced the reviewed explicit MPS
device interface, and verifies it before use. The resolved revision is written to the private
installation record; the installer neither fetches nor follows a mutable upstream branch.

The suite-managed DeepKOALA checkout uses its bundled `202502` resources by default. The companion
defaults to `device=cpu`, accepts explicit `device=cuda` on Linux or `device=mps` on macOS only when
deployment policy allows the backend and the matching runtime probe reports it available, and never
uses `device=auto` or silent MPS-to-CPU fallback. A missing MPS device does not invalidate an
otherwise CPU-ready macOS installation. The companion allows only `full` and `frag` models
configured by the deployment and defaults every request to single-domain mode. Successful
annotation output reports the resolved model, model date, device, and whether multi-domain mode was
used.

#### Optional multi-domain capability

The suite never downloads HMMER or KOfam profiles. An operator who already has authorized local
resources may make the capability available by setting all three fields before installing a new
suite root:

```toml
[deepkoala]
# Keep the other required DeepKOALA fields from the complete template.
allow_multi = true
profiles_dir = "/absolute/path/to/kofam/profiles"
hmmsearch_executable = "/absolute/path/to/hmmsearch"
```

The profile directory and executable must be direct, non-symlink, safely permissioned paths and may
not overlap suite state or biological input/output roots. Configuration preflight checks the local
paths; post-install verification checks the executable, profiles, and supported upstream adapter
interface without running inference. Enabling this deployment capability does not enable it for
every job: `run_deepkoala_job` still uses `multi=false` unless the user explicitly requests
multi-domain annotation. Multi-domain requests must keep `batch_size=1`. The companion never
accepts either resource path from an MCP request.

The suite installer is fresh-install only. To add this capability after a default suite install,
either configure a manually managed companion deployment or install a new suite root with the
three fields above; the installer does not mutate an existing deployment in place.

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

The successful machine-readable summary includes:

```json
{
  "status": "installed",
  "new_task_required": true,
  "current_task_reload_supported": false,
  "repeat_installation_required": false,
  "next_action": "open_new_codex_task"
}
```

These fields describe Codex activation, not a partial installation. The task that ran the installer
keeps its original tool snapshot and cannot call MCP servers registered later in that task. Do not
run the installer again. Close the installation task and open one new task outside this checkout;
only that fresh task is valid discovery evidence.

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
2. Confirm that `codex mcp list --json` contains enabled entries for all three exact server names.
   If both inventories are complete but the installation task lacks the tools, classify
   `task_reload_required`; do not reinstall.
3. Close the installation task.
4. Start one new Codex task in a workspace outside this source checkout.
5. Confirm discovery of these three Skills:
   - `deepkoala-annotation`
   - `kegg-ko-analysis`
   - `kegg-pathway-rendering`
6. Confirm that the plugin contributes `deepkoala-mcp`, `kegg-mcp`, and `kegg-render-mcp`.
7. Keep that task loaded, start a second new task outside the source checkout, and repeat the three
   Skill, MCP, and status checks there.
8. Return to the first new task and confirm that its three status tools remain available. This
   concurrent-task check detects companion state-root conflicts that static Codex inventory cannot.

Use one bounded status-only request for each dependency:

```text
Use $deepkoala-annotation to report its companion readiness only; do not submit a job.
Use $kegg-ko-analysis to report core server status only; do not retrieve KEGG data.
Use $kegg-pathway-rendering to report renderer status only; do not render an artifact.
```

The DeepKOALA companion must return structured `route_state="local_ready"`. If multi-domain
capability was configured, it must also report `allow_multi=true` and `multi_ready=true`. Core and
Renderer status must show the intended access mode without exposing credentials, endpoints,
usernames, or full local paths.

For an eligible live acceptance check, ask:

> Use kegg-mcp in a bounded acceptance check. First confirm server status, then probe connectivity once.
> Retrieve only KO entry `K00844` and report its identifier, name, database release when
> available, and whether it came from network or cache. Stop after this entry.

This is a bounded discovery check, not a bulk compatibility campaign.

## Manual deployment for other MCP clients

The component Python wheel installs the server command only; it does not install
repository-scoped Skills or another server. Development environments and clients that do not
consume the generated Codex plugin should follow
[Manual component deployment](manual-component-deployment.md). Keep manual registrations isolated
from a suite-managed Codex deployment.

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

Core can also write two other durable bundle families beneath the same allowed roots:

- a selected KEGG reference bundle with `reference_snapshot.json`, deterministic
  `relationships.tsv`, optional `brite_paths.tsv`, and `reference_manifest.json`; and
- a validated KEGG Mapper/Syntax input file with `handoff_manifest.json`.

These tools require an explicit output directory. They do not export the KEGG cache, upload data,
open a browser, execute KEGG Mapper or Syntax, or parse an external result.

Opaque job and result identifiers belong to the stdio process that created them. Use the returned
resource URI for bounded in-session retrieval. Export a canonical entry snapshot from card or
references projection through `write_kegg_reference_bundle` before its result ID expires when
durable selected references are needed. Use output-directory artifacts for durable cross-process
handoff. Result storage,
retention, deletion, and resource templates are documented in
[Services, result storage, and reporting](services-results-reporting.md).

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

On native Windows, the diagnostic returns a structured unsupported-platform result. This is a
deployment-routing check, not a supported server runtime; use WSL2 for operation.

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
