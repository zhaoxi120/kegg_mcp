# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |
| Earlier versions | No |

This table covers the core `kegg-mcp` distribution. The optional sibling `deepkoala-mcp` 0.1.0
candidate is unreleased and is not covered by the supported core release until its independent
release review is complete.

## Reporting a vulnerability

This repository is private. Repository collaborators should open an issue in this private
repository with only the minimum non-sensitive summary needed to request maintainer coordination.
Private repository issues are visible to repository collaborators, so do not include secrets,
licensed KEGG payloads, private biological data, or sensitive exploit details that should be shared
through a more restricted channel.

GitHub private vulnerability reporting is available only for public repositories. This boundary
was reviewed against the
[GitHub repository security advisory documentation](https://docs.github.com/en/code-security/concepts/vulnerability-reporting-and-management/repository-security-advisories)
on 2026-07-15. Before this repository or a supported release is made public, the maintainer must
enable and verify the repository's **Report a vulnerability** flow and update this section. Never
disclose an unpatched vulnerability in a public issue.

Include the affected revision, impact, reproduction conditions, and any suggested mitigation in
the coordinated report. Remove API credentials, local usernames, cache paths, licensed endpoint
details, and biological data.

## Security boundaries

- The MVP is a local stdio MCP server, not a multi-user or remote HTTP service.
- Filesystem inputs must be limited to explicitly allowed roots and must reject traversal and symlink escapes.
- KEGG credentials, licensed endpoints, cached responses, and user inputs must remain local and out of logs, fixtures, packages, and releases.
- External annotation tools, model code, model weights, and databases are outside the core
  `kegg-mcp` runtime boundary. The DeepKOALA companion candidate uses a separate, explicitly
  configured environment and requires an independent subprocess, filesystem, resource,
  dependency, and release security review. The Skill must not become an execution boundary.
- Default tests and pull-request CI must not access live KEGG services. The dedicated live
  compatibility job is main-only, explicitly enabled, eligibility-gated, serialized, bounded,
  and prohibited from uploading KEGG payloads or cache files.

## DeepKOALA companion boundary

The optional companion is a local single-user stdio service. It is not a remote execution service,
and its presence must not change the core package's dependency or process boundary.

- The official DeepKOALA checkout, external Python/PyTorch interpreter, and installed model
  resources are operator-provided absolute paths. The companion does not install, download,
  update, or replace them.
- Artifact hashes detect identity changes; they are not a trust or sandbox boundary. The companion
  executes the configured interpreter and checkout with the current user's permissions. Use only
  an operator-reviewed official checkout and interpreter in the documented local single-user
  deployment.
- The configured interpreter, executable source tree, weights, and model configuration are hashed
  for the execution notice and checked again around process launch. Submission requires the exact
  notice digest and explicit acknowledgement.
- The runner uses a fixed argument vector without `shell=True`, runs at most one independent job,
  installs a hard output-file size limit before upstream code runs, and terminates the entire
  spawned process group on cancellation, timeout, or server shutdown.
- The companion candidate is POSIX-only. Startup fails before configured path handling or state
  creation when the runtime cannot provide the required process-group and file-size-limit
  operations.
- `multi=true` is rejected. CPU-only operation requires an explicit `device="cpu"`; CPU thread,
  batching, worker, timeout, input, output, diagnostic, queue, and retention limits remain bounded.
- Filesystem FASTA input is restricted to configured absolute allowed roots. Traversal, symlink
  escapes, non-regular files, identity changes during intake, and overlap between checkout, state,
  and input roots are rejected. Accepted inputs and artifacts are copied into owner-only state.
- Job identifiers and resources are process-scoped. Successful output, provenance, and sanitized
  diagnostics are retained only for the configured period, support bounded pagination, and are
  deleted explicitly or by cleanup. Resource readers verify retained artifact identity.
- Normal shutdown attempts to clean the current process scope. A filesystem cleanup failure or
  abrupt process termination can leave an owner-only session directory. Stop every companion
  process before manually removing stale sessions from the configured state root; one process
  never deletes another process's scope.
- Status, errors, notices, provenance, and diagnostics must not expose raw FASTA, environment
  values, secrets, usernames, or full local paths. Diagnostic redaction is a defense in depth, not
  permission to print sensitive values.
- The client or Skill must read and verify companion output before passing it inline to the core
  importer. Neither server may dereference the other's private resource URI, and the companion may
  not implement an alternative KO normalization policy.
