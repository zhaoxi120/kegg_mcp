# Troubleshooting

Start with the redacted local diagnostics for the affected process:

```bash
/absolute/path/to/kegg-mcp doctor --json
/absolute/path/to/deepkoala-mcp doctor --json
```

`doctor` validates local configuration only. It does not contact KEGG, run inference, inspect cache
contents, or reveal configured paths and endpoints. A successful diagnostic does not prove that
live access is authorized or reachable.

## Diagnose the first failing layer

Check the deployment in this order and stop at the first failure:

1. **Suite transaction** — `scripts/install-suite.py` completed without an incomplete or rollback
   marker.
2. **Plugin registration** — the dedicated marketplace and generated plugin appear in
   `codex plugin list --json`.
3. **New-task loading** — a new Codex task exposes all three Skills and MCP servers.
4. **MCP startup** — each absolute launcher starts and completes MCP initialization.
5. **Runtime readiness** — each status tool reports the expected access, roots, and external runtime
   state.
6. **Handoff roots** — adjacent processes can read the intended stable file while private state
   roots remain separate.
7. **External resources** — the configured DeepKOALA checkout, Python environment, and `202502`
   resources are locally ready.

Reinstalling a Skill cannot repair a missing executable or model. A ready MCP cannot repair a
missing plugin registration.

## Suite and plugin problems

### The generated plugin is absent or stale

The suite installer owns Codex registration. Do not copy Skills, hand-edit generated `.mcp.json`,
or add duplicate manual MCP entries; these can shadow the version-bound plugin and make rollback
ambiguous.

If the plugin inventory is correct, close the installation task and open a new task outside the
source checkout. Restart Codex once only if the new task still uses stale discovery state.

### Only the core server is discovered for a FASTA-to-graphic request

`kegg-mcp` accepts existing K numbers and supported KO annotation evidence; it does not accept raw
protein FASTA or render pathway graphics. If the user explicitly selected another annotator, wait
for its supported KO evidence. Otherwise prefer `deepkoala-annotation`; when that Skill or
`deepkoala-mcp` is unavailable, stop before any core analysis call and ask once for explicit
permission to install or repair the complete suite. After that action succeeds, open a new Codex
task and confirm all three Skills and MCP servers before continuing. If permission is declined,
remain stopped until a selected route supplies supported KO evidence.

If an offline installation lacks a Python artifact, review the failure and either provide the exact
locked artifact locally or rerun with `--allow-locked-dependency-downloads`. That option authorizes
only artifacts required by checked-in lockfiles and declared build requirements. It does not
authorize Python, Codex, model updates, optional multi-domain resources, or KEGG data. Optional
resources may be provisioned separately after explicit user authorization.

`--allow-deepkoala-install` is separate: it confirms the initial official DeepKOALA clone and
upstream requirements for one new suite root. Later models and optional multi-domain dependencies
remain operator-managed.

### Multi-domain mode is unavailable

Multi-domain mode is disabled by default. A request with `multi=true` succeeds only when the private
deployment enabled the capability and status reports both `allow_multi=true` and `multi_ready=true`.
The operator must provide the configured absolute `hmmsearch` executable and local KOfam profile
directory. These optional dependencies may be provisioned separately after explicit user
authorization; the companion and suite installer do not manage that provisioning.

Use the redacted `route_state`, `issue`, and `next_action` returned by
`get_deepkoala_runner_status`. Repair or replace the configured external resource, restart the
companion, and check status again. Do not pass a profile or executable path through an MCP request.
Because the suite installer is fresh-install only, an existing default suite must be replaced by a
new suite root or a separately managed companion deployment; it cannot be reconfigured in place.

### The deployment TOML is rejected

Use the exact fields documented in [Installation and operation](installation.md). The TOML must be
an owner-owned non-symlink regular file with no group or other permissions inside a direct
owner-only parent. Configured paths must be absolute. Private state roots may not overlap each
other, handoff roots, caches, source trees, or the install root.

Fix the unsafe path or permission. Do not bypass validation with a symlink alias, unknown key,
duplicated environment override, or group-writable directory.

### A suite transaction requires manual recovery

`installation_rollback_failed`, `.rollback-required`, or a lone `.incomplete` marker means the
installer could not prove that publication completed or that every registration was safe to remove.
Preserve the private install root.

Compare its recorded managed selector, marketplace name, and absolute launchers with local output
from:

```bash
codex plugin list --json
codex plugin marketplace list --json
codex mcp list --json
```

Do not publish that output. Do not delete or move the install root while Codex still references it.
The installer has no automatic resume, update, uninstall, or hard-crash recovery command. Escalate
ambiguous ownership to the maintainer instead of guessing from a matching name.

## Startup and configuration

### JSON or TOML was pasted into Bash

Configuration blocks are file content, not shell commands. Place them in the client configuration
described in [Installation and operation](installation.md). Do not manually register the same MCP
names when the generated suite plugin owns them.

### An executable is missing or cannot start

Run its human-facing diagnostic outside the MCP client:

```bash
test -x /absolute/path/to/kegg-mcp
/absolute/path/to/kegg-mcp doctor --json
```

Use the executable directly as the stdio command. Do not wrap it in a shell, redirect stdout, or
expect a terminal prompt. Bare server commands wait for MCP JSON-RPC on stdin.

### The server is visible but tools are absent

Confirm the plugin is enabled, then inspect a new Codex task. For a non-Codex client, verify that
the configured server name and absolute executable match the installed component. Read client-side
MCP logs without publishing environment values or private paths.

### Access configuration is invalid

Select one documented mode:

- `public_academic` only for eligible academic users performing academic work;
- `licensed` with an authorized HTTPS endpoint and explicit confirmation; or
- `offline_cache` for network-disabled reuse of existing local entries.

Do not place credentials in an endpoint URL. Diagnostics intentionally avoid echoing rejected
values.

## Renderer offline-cache misses

A ready `offline_cache` renderer reports a valid network-disabled configuration, not the presence
of every PNG or KGML entry. Its connectivity probe performs zero requests.

Configure an existing safe absolute `KEGG_RENDER_MCP_CACHE_PATH`. A missing entry or database
returns `ASSET_UNAVAILABLE` and creates nothing. Populate the matching namespace through a separate
authorized live deployment; there is no per-request endpoint, refresh, or network fallback.

`stale_disallowed` is a data-availability state, not biological absence. Authorized operators may
set deployment-wide `KEGG_RENDER_MCP_OFFLINE_ALLOW_STALE=true`; stale use remains explicit in
warnings and provenance. See the [renderer README](../companions/kegg-render-mcp/README.md) for the
complete cache safety contract.

## File handoff and retained results

### A path is rejected

`KEGG_MCP_ALLOWED_ROOTS` contains existing absolute directories separated by the platform path
separator. Inputs and output directories must remain beneath those roots. Relative paths, missing
roots, traversal, symlink escapes, and unsafe output ancestry are rejected. `doctor` reports only
whether handoff is enabled and the number of accepted roots.

`allowed_root_count=0` with `file_handoff_enabled=false` means only that Core has no configured
handoff root. It does not establish whether any Skill, companion, or complete suite is installed
or discovered.

### `OUTPUT_ALREADY_EXISTS`

Output bundles do not overwrite entries. Choose a new or empty directory; there is no overwrite
flag.

### `OUTPUT_WRITE_FAILED` with an allowed output root

The bundle writer checks the complete ancestor chain, not only the configured allowed root. From
the first ancestor owned by the service user onward, every directory must be owned by that user and
must not be group- or world-writable. For example, an owner-owned `775` parent causes a safe failure
even when the allowed root below it is `700`. Remove group write from that parent or choose a safe
root with an owner-only ancestor chain; do not weaken the writer or change only the child mode.

### `RESULT_NOT_FOUND`

A `result_id` belongs to one stdio process and may also have expired or been deleted. Do not copy it
between sessions. Rerun the analysis or request an output bundle for durable handoff. Use
`delete_analysis_result` for current-session deletion or `kegg-mcp cleanup --expired` for expired
orphan rows; neither operation removes live KEGG cache entries.

## KEGG access failures

`CACHE_ENTRY_NOT_FOUND` means no matching local response exists. It is not evidence that a KO,
MODULE, pathway, or function is absent.

For live DNS, connection, authorization, or rate-limit failures, confirm the intended access mode
and call `probe_kegg_connectivity` once. Do not loop the probe, bypass the deployment-wide limiter,
or reinterpret transport failure as an absent KEGG entry.

## Protocol and support hygiene

Malformed JSON-RPC commonly means a wrapper wrote banners or debug output to stdout. Keep stdout
for protocol traffic and send diagnostics to stderr.

A safe support report contains only versions, operating system, redacted `doctor --json` output,
the structured error code and safe details, and whether input was inline or an allowed-root file.
Never include tokens, endpoints, environment dumps, usernames, paths, private FASTA/KO data, KEGG
payloads, or SQLite databases.
