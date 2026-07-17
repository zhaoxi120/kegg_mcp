# Troubleshooting

Start with the redacted local diagnostic:

```bash
/absolute/path/to/.venv/bin/kegg-mcp doctor
/absolute/path/to/.venv/bin/kegg-mcp doctor --json
```

`doctor` validates environment configuration only. It does not contact KEGG, open or create a
cache or result database, enumerate stored entries, or reveal configured paths and endpoint
values. A successful diagnostic does not prove that live KEGG access is authorized or reachable.

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
