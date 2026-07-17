# Troubleshooting

Start with the redacted local diagnostic:

```bash
/absolute/path/to/.venv/bin/kegg-mcp doctor
/absolute/path/to/.venv/bin/kegg-mcp doctor --json
```

`doctor` validates environment configuration only. It does not contact KEGG, open or create a
cache or result database, enumerate stored entries, or reveal configured paths and endpoint
values. A successful diagnostic does not prove that live KEGG access is authorized or reachable.

## Classify Skill, MCP, and runtime failures separately

Use the following deployment diagnosis labels in support notes. They identify different layers;
they are not interchangeable descriptions of a generic "DeepKOALA unavailable" state.

| Diagnosis | Evidence | Next check |
| --- | --- | --- |
| `skill_not_installed` | One or more managed Skill directories are absent from the workspace `.agents/skills`. | Run the reviewed `scripts/install-skills.py` command from the matching checkout or tag archive. |
| `skill_not_discovered` | `SKILL.md` exists in a scanned workspace location, but the client's discovered Skill list or selector omits it. | Confirm the Codex working directory, check whether the Skill is disabled in Codex configuration, and restart Codex. |
| `mcp_not_registered` | The Skill is visible, but its one declared MCP dependency is absent from `codex mcp list`. | Register that exact stdio server name and restart the client. |
| `mcp_not_startable` | The dependency is registered, but MCP initialization cannot start its executable. | Verify the absolute executable and run its non-protocol diagnostic outside Codex. |
| `mcp_runtime_unready` | MCP initialization and tool discovery succeed, but the server's status tool reports an unready configuration or runtime. | Follow the named status repair without reinstalling the Skill. |
| `handoff_root_mismatch` | Both adjacent MCP servers are ready, but a stable handoff path is outside the producer or consumer allowlisted roots. | Align only the intended shared handoff root; keep private state roots separate. |
| `model_resources_missing` | `deepkoala-mcp` is startable, but its status reports that the explicitly configured local DeepKOALA model resources are absent or invalid. | Have the operator repair the reviewed external DeepKOALA installation; the Skill and companion must not download it. |

Diagnose in this order: Skill installation, Skill discovery, MCP registration, MCP initialization,
MCP status readiness, handoff-root alignment, then external runtime or model resources. Stop at the
first failing layer. A later healthy layer cannot repair an earlier missing one, and reinstalling a
Skill cannot make an MCP executable or model ready.

### The repository Skills are not visible

The Python wheel does not contain the repository Skills. Obtain the matching reviewed GitHub tag
source archive or exact checkout, then use the managed installer documented in
`docs/installation.md`. If the checkout is nested below the Codex workspace, its own
`.agents/skills` is not on the upward scan path; install managed copies into the outer workspace.
If Codex runs at the checkout root itself, its source Skills are already discoverable and the
managed-copy installer must not target that same directory.

If the installer reports `skill_target_conflict`, it found an existing directory that it does not
own. It intentionally makes no partial installation and does not overwrite that content. Review
the directory's ownership and contents before moving or renaming it; do not add a fake marker. If
it reports `skill_target_modified`, preserve or reconcile the local edits before replacing the
managed copy. `skill_source_modified` means a Git checkout's Skill, version, or installer source no
longer matches `HEAD`; use a clean reviewed checkout instead of attributing modified content to that
commit. `source_commit_unavailable` requires the verified full tag commit for an archive, while
`source_tree_sha256_required` means the published archive digest was omitted and
`source_tree_sha256_mismatch` means the selected archive does not match it. Never repair either
condition by trusting a digest calculated only from the untrusted archive. `version_mismatch` means
the selected Skill source does not match the expected core wheel.

`installation_rollback_failed` means automatic recovery could not complete. Do not delete the
reported `.agents/skills/.kegg-mcp-skill-install-*` transaction. Inspect its `backup/` directory
locally and restore directories by exact Skill name; the diagnostic intentionally gives a relative
location rather than exposing a private workspace path.

`installation_cleanup_failed` means the requested Skills were committed or a rollback completed,
but the private transaction could not be removed completely. Treat the command as failed, inspect
the reported relative transaction path, and do not infer success from the installed directories
alone.

Codex should detect a successful install automatically. Perform the first check from the exact
installed workspace. If the three names do not appear in the client's discovered Skill list or
selector, restart Codex and check again. Then run one bounded `$skill-name` smoke check before
checking MCP discovery with `codex mcp list`.

## Common startup and discovery problems

### Bash reports `mcpServers: command not found`

An MCP configuration object was pasted into a shell. JSON and TOML configuration blocks are file
content, not shell commands. Register the server with the client command shown in
`docs/installation.md`, or place the configuration in the exact file required by the client.

For an eligible academic user acceptance session, prefer:

```bash
codex mcp add kegg-mcp \
  --env KEGG_MCP_ACCESS_MODE=public_academic \
  --env KEGG_MCP_ACADEMIC_USE_CONFIRMED=true \
  -- /absolute/path/to/.venv/bin/kegg-mcp
codex mcp list
```

Those two variables may be omitted because they match the project defaults.

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

Run `codex mcp list` or the equivalent discovery command for the client, then restart the client
after changing MCP configuration. Confirm that the configured name is exactly `kegg-mcp` and that
the command is an absolute executable path. Check client-side MCP logs without publishing
environment values or private paths.

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
