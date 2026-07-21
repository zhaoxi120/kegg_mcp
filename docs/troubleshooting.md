# Troubleshooting

Start with the redacted local diagnostic:

```bash
/absolute/path/to/.venv/bin/kegg-mcp doctor
/absolute/path/to/.venv/bin/kegg-mcp doctor --json
```

`doctor` validates environment configuration only. It does not contact KEGG, open or create a
cache or result database, enumerate stored entries, or reveal configured paths and endpoint
values. A successful diagnostic does not prove that live KEGG access is authorized or reachable.

## Classify suite, plugin, Skill, MCP, and runtime failures separately

Use the following deployment diagnosis labels in support notes. They identify different layers;
they are not interchangeable descriptions of a generic "DeepKOALA unavailable" state.

| Diagnosis | Evidence | Next check |
| --- | --- | --- |
| `suite_install_failed` | `scripts/install-suite.py` exits unsuccessfully or leaves a recovery transaction instead of publishing a complete deployment. | Preserve the reported private transaction, inspect the first structured failure, and do not infer success from partial runtime directories. |
| `plugin_not_registered` | The three runtimes were prepared, but the dedicated marketplace or generated plugin is absent from the Codex plugin inventory. | Resolve the reported registration or rollback failure; do not add duplicate manual MCP entries. |
| `plugin_not_loaded` | The plugin is installed and enabled, but the current task does not expose its three Skills or MCP servers. | Start a new Codex task, then check the app or `codex plugin list --json` before changing runtime files. |
| `skill_not_discovered` | The installed plugin contains `SKILL.md`, but a new task's discovered Skill list or selector omits it. | Confirm the plugin is enabled, then restart Codex once if a new task is still stale. |
| `mcp_not_registered` | The Skill is visible, but its one declared MCP dependency is absent from the new task. | For the suite, inspect the generated plugin; for a manual deployment, register only that exact stdio server name. |
| `mcp_not_startable` | The dependency is registered, but MCP initialization cannot start its executable. | Verify the absolute executable and run its non-protocol diagnostic outside Codex. |
| `mcp_runtime_unready` | MCP initialization and tool discovery succeed, but the server's status tool reports an unready configuration or runtime. | Follow the named status repair without reinstalling the Skill. |
| `handoff_root_mismatch` | Both adjacent MCP servers are ready, but a stable handoff path is outside the producer or consumer allowlisted roots. | Align only the intended shared handoff root; keep private state roots separate. |
| `model_resources_missing` | `deepkoala-mcp` is startable, but its status reports that the configured local DeepKOALA model resources are absent or invalid. | For a suite install, verify the managed `202502` resources and rerun a fresh confirmed installation if needed; for a manual deployment, repair the external installation. The serving companion does not download weights. |

Diagnose in this order: suite transaction, plugin registration, new-task plugin loading, Skill
discovery, MCP registration, MCP initialization, MCP status readiness, handoff-root alignment,
then external runtime or model resources. Stop at the first failing layer. A later healthy layer
cannot repair an earlier missing one, and reinstalling a Skill cannot make an MCP executable or
model ready.

### The generated suite plugin is absent or stale

The primary Codex deployment is the generated local plugin installed by
`scripts/install-suite.py`. Check the Codex app or run `codex plugin list --json`. If the dedicated
marketplace or plugin is absent, use the suite installer's reported transaction failure; do not
repair it by copying Skills, hand-editing `.mcp.json`, or running three `codex mcp add` commands.
Those changes can shadow the version-bound generated registration and make rollback ambiguous.

A successfully installed plugin is loaded for a new Codex task. Close the installation task,
start a new task, and check the three Skill and MCP names there. Restart Codex once only if the
plugin inventory is correct but a new task remains stale. An existing task is not discovery
evidence for the new plugin.

If installation failed because a Python artifact was unavailable offline, verify that the exact
checked-in lockfiles and declared build requirements are available to `uv`. The operator may rerun
with `--allow-locked-dependency-downloads` only after reviewing that failure. The switch allows
`uv`
network access only while resolving or downloading artifacts required by those lockfiles and build
requirements; it does not authorize downloading Python, uv, Codex, repository source, later model
weights, KOfam profiles, or KEGG data. The separate `--allow-deepkoala-install` switch records one
confirmation for the initial official DeepKOALA clone and upstream Python requirements in each new
suite installation root. The same installed deployment does not ask again for later FASTA jobs. The
switch does not authorize model updates or multi-domain resources.

### The suite deployment TOML is rejected

Use the exact field names and access-profile combinations documented in `docs/installation.md`.
The file must be a direct, non-symlink regular file owned by the invoking user with mode `0600`,
or another mode with no group or other permission bits. Its direct parent directory must likewise
be owned by the invoking user with no group or other permission bits; mode `0700` is recommended.
Configured paths must be absolute.
Private state roots may not overlap each other, shared handoff roots, source trees, caches, or the
install root. Core and Renderer allowlists must cover the intended stable-file handoffs. Fix the
unsafe path or permission directly; do not relax the check with a group- or other-accessible parent, symlink
alias, unknown TOML key, or duplicated environment override.

### A suite transaction requires manual recovery

For the suite installer, `installation_rollback_failed` means Codex or filesystem state could not
be proved safe to remove. A process kill, host crash, or power loss can instead leave an
`.incomplete` file without a final diagnostic. In either case, preserve the private installation
root. Inspect `codex plugin list --json`, `codex plugin marketplace list --json`, and
`codex mcp list --json` locally, and compare only the exact managed selector, marketplace name, and
absolute launcher paths recorded under that root. Do not publish the command output because it may
contain local paths.

When `.rollback-required` exists, its safe `initial_failure_code` and `registration_stage` fields
retain the first classified failure and the last installer-owned registration stage. A lone
`.incomplete` marker after a hard crash records only that publication did not complete; it does not
prove which external Codex operation finished.

Do not delete or move the installation root while a plugin, marketplace, or MCP binding still
references it. The suite installer currently has no automatic resume, update, uninstall, or
hard-crash recovery command. Escalate an ambiguous or replaced registration to the maintainer;
never infer ownership from a matching name alone, and never repair the state by adding duplicate
manual MCP entries.

## Common startup and discovery problems

### Bash reports `mcpServers: command not found`

An MCP configuration object was pasted into a shell. JSON and TOML configuration blocks are file
content, not shell commands. Register the server with the client command shown in
`docs/installation.md`, or place the configuration in the exact file required by the client.

For an eligible academic user acceptance session using an intentional manual component
deployment, use:

```bash
codex mcp add kegg-mcp \
  --env KEGG_MCP_ACCESS_MODE=public_academic \
  --env KEGG_MCP_ACADEMIC_USE_CONFIRMED=true \
  -- /absolute/path/to/.venv/bin/kegg-mcp
codex mcp list
```

Those two variables may be omitted because they match the project defaults.

Do not run that command for the generated suite plugin. Its `.mcp.json` already owns the
version-bound registration.

### The executable is missing or the client cannot start it

Verify the absolute path and diagnostic outside the MCP client:

```bash
test -x /absolute/path/to/.venv/bin/kegg-mcp
/absolute/path/to/.venv/bin/kegg-mcp doctor
```

Use the executable directly as the stdio command. Do not add a shell wrapper, redirect stdout, or
run the command in a terminal and expect a human prompt. Bare `kegg-mcp` waits for MCP JSON-RPC on
stdin; `kegg-mcp doctor` is the human-facing check.

### The server is configured but tools are absent

For the suite, confirm the generated plugin is installed and enabled, then inspect a new Codex
task. Its MCP configuration must contain the exact server name and an absolute installed launcher
path. For a manual client, run `codex mcp list` or the equivalent discovery command and start a new
task after changing configuration. Check client-side MCP logs without publishing environment
values or private paths.

### Configuration is invalid

Run `kegg-mcp doctor --json`. Select one documented access mode:

- `public_academic` is the external default and treats academic use as confirmed;
- `licensed` requires an authorized HTTPS endpoint and
  `KEGG_MCP_LICENSED_USE_CONFIRMED=true`; or
- `offline_cache` disables network access for internal iteration and returns a repairable cache
  miss instead of falling back to KEGG.

Do not place credentials in an endpoint URL. The diagnostic intentionally returns a generic error
rather than echoing rejected values.

### Renderer `offline_cache` cannot provide a pathway asset

`get_renderer_status` can report a ready `offline_cache` deployment even when the configured
database or requested entry is absent. Readiness means that the bounded network-disabled policy is
configured; it is not a cache inventory check. `probe_renderer_kegg_connectivity` performs zero
requests in this mode and likewise does not prove that a pathway PNG or KGML entry exists.

Configure an absolute `KEGG_RENDER_MCP_CACHE_PATH`. A missing database is a typed
`ASSET_UNAVAILABLE` miss and neither the file nor its parent is created. Populate or refresh the
matching namespace through a separate authorized live Core or Renderer deployment; do not add a
per-request path, refresh flag, endpoint, or network fallback. Public-academic entries are selected
by default. Existing licensed entries require the same canonical licensed endpoint plus
`KEGG_RENDER_MCP_LICENSED_USE_CONFIRMED=true` so the renderer can derive the matching opaque
namespace without contacting or disclosing that endpoint.

If the error says the cache could not be used safely, inspect it locally without publishing its
path or content. The database must be an owner-controlled regular `0600` file below a safe
owner-controlled parent and must retain the supported Core schema, DELETE journal mode, full
auto-vacuum mode, parser metadata, and logical and physical size bounds. Symlinks, mismatched
schema objects, unsafe permissions, WAL mode, corrupt rows, and oversized files fail closed.

A `stale_disallowed` cache state is not a biological absence. Refresh the asset through authorized
live access when possible. An operator may choose deployment-wide
`KEGG_RENDER_MCP_OFFLINE_ALLOW_STALE=true`; that choice is never a per-call override, and every
served stale asset remains explicit in result warnings and `render_manifest.json` provenance.

## File handoff and retained results

### A file or output directory is rejected

`KEGG_MCP_ALLOWED_ROOTS` must contain existing absolute directories separated by the platform path
separator. Every input file, original source path, and output directory must resolve beneath one
of those roots. Relative paths, missing roots, traversal, symlink escapes, and unsafe output
ancestors are rejected.

Run `kegg-mcp doctor`; it reports only whether handoff is enabled and how many roots were accepted.
It never prints the roots. Inspect the client configuration locally when the count is unexpected.

### `OUTPUT_ALREADY_EXISTS`

Output bundles are non-overwriting. The requested output directory contains an existing entry, so
the server made no change. Choose a new or empty directory. There is no overwrite flag in this
release.

### `RESULT_NOT_FOUND`

A `result_id` belongs to one stdio server process and may also have expired or been deleted. Do not
copy opaque result IDs across client sessions. Rerun the analysis, or request an output bundle
beneath an allowed root for stable cross-process handoff. Normal shutdown deletes the current
scope; 24 hours is the active hard TTL and the cleanup threshold for rows left by abnormal exit,
not a cross-process recovery promise.

Use `delete_analysis_result` for immediate deletion in the current session. Operators can remove
TTL-expired rows without starting stdio by running `kegg-mcp cleanup --expired`; this does not
delete unexpired results or KEGG cache entries.

## KEGG access failures

### `CACHE_ENTRY_NOT_FOUND`

An explicit cache-resource read has no matching cached response. This is a technical
data-availability state, not evidence that a KO, MODULE, pathway, or function is absent. Fetch the
entry through an ordinary network-enabled operation.

### DNS, connection, authorization, or rate-limit failures

After `get_server_status` confirms the intended live mode, call `probe_kegg_connectivity` once from
the MCP client. It makes one explicit low-cost request and classifies the failure. Do not loop the
probe, bypass the deployment-wide rate limiter, or reinterpret a transport failure as an absent
KEGG entry.

## Protocol and support hygiene

### The client reports malformed JSON-RPC

Keep stdout reserved for MCP protocol messages. Send application diagnostics to stderr, and do not
wrap the stdio command with shell output, banners, `tee`, or debug prints. Use `kegg-mcp doctor`
outside the client for human-readable diagnostics.

### Prepare a safe support report

Include only:

- the KEGG MCP version;
- client name and version;
- operating system and Python version;
- redacted `kegg-mcp doctor --json` output;
- the structured MCP error code and safe details; and
- whether the input was inline or an allowed-root file.

Do not include tokens, endpoint URLs, environment dumps, usernames, full local paths, raw KO
tables, private FASTA files, KEGG payloads, SQLite databases, or cache archives.
