# High-star MCP repository benchmark

This review records which public MCP repository practices were considered for KEGG MCP and which
ones were adopted. GitHub star counts are discovery signals, not evidence of correctness,
security, biological validity, or fitness for this repository.

## Snapshot and method

Repository metadata and the linked project documentation were retrieved from GitHub on
2026-07-15. The snapshot intentionally includes several kinds of MCP server: reference servers, a
large product integration, browser automation servers, and a focused documentation server.

| Repository | Stars at review | Relevant public pattern |
| --- | ---: | --- |
| [modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers) | 88,504 | Reference implementations, explicit security caveats, allowed-directory discovery, and precise tool annotations |
| [upstash/context7](https://github.com/upstash/context7) | 59,134 | Crisp single-purpose positioning, one-command setup, first prompts, and a deliberately small tool surface |
| [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | 46,988 | Client-specific setup, first-prompt validation, privacy disclosure, troubleshooting, and optional capability reduction |
| [microsoft/playwright-mcp](https://github.com/microsoft/playwright-mcp) | 35,109 | Short onboarding, explicit operating modes, generated tool documentation, and accurate annotations |
| [github/github-mcp-server](https://github.com/github/github-mcp-server) | 31,461 | Client-specific installation, selectable toolsets, read-only controls, secret guidance, and dedicated security/support documentation |

The review focused on stable repository practices visible in source and documentation. It did not
infer implementation quality from popularity or copy product-specific features into this server.

## Patterns worth adopting

### Make the first successful interaction obvious

High-adoption repositories put a short, client-specific path before generic configuration. They
also supply a first prompt that proves discovery without requiring the user to understand the MCP
protocol. KEGG MCP therefore documents a Codex CLI registration command, a discovery check, and a
bounded live content check for eligible academic acceptance. Local pytest keeps that check opt-in,
while pull-request CI runs the bounded public-academic profile once.

### Separate operator diagnostics from protocol traffic

A stdio server cannot print human diagnostics to stdout while serving MCP. A small out-of-band
diagnostic command gives operators useful deployment facts without corrupting JSON-RPC. KEGG MCP
therefore provides `kegg-mcp doctor` while preserving bare `kegg-mcp` as the stdio command.

The diagnostic is intentionally side-effect-free: it validates configuration but does not open
SQLite files, contact KEGG, enumerate cache entries, or reveal configured paths and endpoint
values. Live connectivity remains an explicit MCP tool because it has an external side effect.

### Guide clients with initialization instructions

Tool descriptions are necessary but do not communicate the full workflow. The server now supplies
bounded initialization instructions covering the preferred high-level tool, connectivity
preflight, allowed roots, process-scoped result IDs, stable output bundles, and biological claim
boundaries.

### Keep status operational and redacted

Useful status answers capability questions without publishing secrets. `get_server_status` now
reports whether file handoff is enabled and the number of configured roots, while continuing to
redact every path, endpoint value, username, environment value, and cache payload.

### Treat troubleshooting as part of the product contract

Mature MCP repositories document discovery failures, configuration mistakes, permissions, and
privacy constraints. KEGG MCP now has a dedicated troubleshooting guide that separates technical
failures from biological conclusions and gives safe support-report guidance.

## Patterns not adopted

| Pattern | Decision |
| --- | --- |
| Remote HTTP transport | Not adopted. The reviewed project scope is local stdio, and remote hosting would add authentication, tenancy, and data-governance requirements. |
| Telemetry or automatic update checks | Not adopted. Local KO evidence, KEGG access configuration, and paths should not create unrequested outbound traffic. |
| Large selectable toolsets or a slim mode | Not adopted. The server has eleven bounded tools; another selection layer would add configuration without solving a current context problem. |
| Dynamic MCP Roots negotiation | Deferred. Deployment-owned `KEGG_MCP_ALLOWED_ROOTS` is already explicit, testable, and fail-closed. A protocol-driven root contract needs a separate threat-model review. |
| Interactive authentication | Not adopted. Public-academic eligibility and licensed KEGG endpoints are deployment decisions, not MCP login flows. |
| Browser-oriented screenshots or automation | Out of scope. Core `kegg-mcp` analyzes existing KO evidence and does not render pathway images or run external annotators; static rendering belongs only to the separate `kegg-render-mcp` companion. |
| Tool discovery as a meta-tool | Not adopted. Eleven explicit tools remain easier to inspect and validate than a dynamic tool-search surface. |

## Resulting local contract

The benchmark produced the following bounded changes:

- `kegg-mcp` still starts the stdio server with no application output on stdout;
- `kegg-mcp serve` is an explicit equivalent;
- `kegg-mcp doctor [--json]` validates deployment configuration without network or database probes;
- status includes `file_handoff_enabled` and `allowed_root_count` but never root paths;
- initialization instructions explain the supported workflow and interpretation boundaries;
- installation starts with a Codex CLI command and documents a bounded academic live acceptance
  check; the default local test profile remains offline; and
- troubleshooting has a stable, privacy-preserving support checklist.

These changes do not alter the explicit biological analysis surface, KEGG rate limits, access
rights, cache boundaries, result isolation, or the prohibition on external annotation execution.
