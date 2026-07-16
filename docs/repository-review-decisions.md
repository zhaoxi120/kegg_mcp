# Repository review decisions

This record evaluates the maintainer-provided repository review dated 2026-07-16. It is a scoped
implementation decision, not a replacement for `development-plan.md` or
`visualization-extension-plan.md`.

## Overall assessment

The review is technically sound. Its strongest findings are the release-state drift, inaccurate
tool side-effect hints, ambiguous retained-result lifetime, missing current-scope deletion, silent
bundle replacement risk, and absolute-path exposure in the portable manifest. Its recommendation
to preserve the three independent stdio processes and the existing scientific interpretation
boundaries is accepted.

The review intentionally leaves the approved pull-request live KEGG campaign and default
confirmed `public_academic` profile unchanged. This implementation does the same.

## Implemented now

| Review finding | Decision and contract |
| --- | --- |
| Release-state drift | Accepted. The source-version matrix is repeated in the prominent release documents and checked against all three project files, the renderer dependency, the latest published GitHub release statement, and `SECURITY.md`. |
| Tool annotations | Accepted with a stricter local-effects interpretation. Any tool that writes a retained result, output bundle, or response cache is not advertised as read-only or idempotent. KEGG-facing tools are open-world. |
| Bundle replacement | Accepted. Bundle schema version 2 requires a new or empty target, uses no overwrite mode, installs payloads without replacement, and installs the manifest last. |
| Result deletion | Accepted. `delete_analysis_result` can delete only a result in the current stdio scope and preserves one safe not-found class for unknown, expired, deleted, and cross-scope IDs. |
| Result lifetime | Accepted with clarified dual semantics. Normal shutdown deletes the current scope. The configured 24-hour value remains the hard TTL for an active result and is also the cleanup threshold for orphan rows left by abnormal termination. |
| Operator cleanup | Accepted for retained results. `kegg-mcp cleanup --expired` removes only TTL-expired result rows and neither quota-evicts active rows nor changes the KEGG response cache. |
| Manifest path privacy | Accepted. The bundle manifest uses redacted source labels by default; absolute paths require explicit `manifest_path_mode="absolute"`. |
| Platform clarity | Accepted. The release-supported matrix is Linux with CPython 3.11.x. Python 3.12, Python 3.13, macOS, and Windows require separate compatibility work. |
| Non-Codex setup | Accepted at documentation scope. Installation includes a current minimal Claude Desktop/local JSON-client example without adding a setup command or client-specific code. |

## Adjusted recommendations

The review proposed creating a unique run subdirectory beneath every supplied
`output_directory`. That would silently change the existing contract in which the caller selects
the exact bundle directory and hands exact artifact paths to another local process. This change
instead requires that exact directory to be new or empty. The caller can choose a unique directory
when desired, while an accidental reuse fails without changing existing content.

The review described 24 hours only as an orphan-result threshold. Existing service contracts also
enforce it as the hard expiry of a result during a long-running session. Removing that active TTL
would weaken a bound and create a migration incompatible with the current store. Status and
documentation therefore expose both meanings explicitly.

The review's suggested connectivity annotation treated remote read-only behavior as sufficient.
The implemented probe can update the local KEGG response cache, so it is conservatively advertised
as mutating and non-idempotent even though it does not modify KEGG.

## Already satisfied

The review correctly identifies several strengths that need no architectural change:

- the core, DeepKOALA companion, and renderer remain separate stdio processes;
- one high-level analysis tool coexists with bounded primitives without exposing arbitrary KEGG
  URLs;
- the README already provides a five-minute Codex path and installation already provides a generic
  local JSON configuration shape;
- wheels and source distributions already have build and content audits; and
- scientific language already separates KO annotation, exact MODULE completion, project block
  coverage, descriptive pathway coverage, and statistical claims.

## Deferred to focused changes

The following recommendations are reasonable but are not bundled into this release-hardening
change because each introduces a new public contract, publication action, or substantial MCP-layer
refactor:

- package-registry publication, `uvx` promises, an MCP Registry manifest, and automated release;
- setup and three-process workflow-doctor commands;
- MCP Prompts for clients that do not load repository Skills;
- a typed tool registry, handler/resource module split, correlation IDs, and narrower unexpected
  exception mapping;
- current-scope result listing;
- bounded natural-language KEGG entity resolution and MODULE Top-N discovery; and
- Python 3.12, Python 3.13, macOS, or Windows support.

No placeholder command, prompt, registry file, platform claim, or partially functional resolver is
added for deferred work.

## External contract checks

MCP annotation semantics and local-client configuration were rechecked on 2026-07-16 against
current primary documentation:

- [MCP schema: `ToolAnnotations`](https://modelcontextprotocol.io/specification/2025-11-25/schema)
- [MCP local-server connection guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers)

The GitHub release list was also checked on 2026-07-16. It contained only core `v0.1.0`; candidate
source versions are therefore not described as published releases.
