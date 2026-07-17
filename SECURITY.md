# Security Policy

## Supported versions

Security fixes target the latest GitHub release and the current `main` branch. Other tags are
unsupported unless the maintainer explicitly announces otherwise.

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
- Output bundles must be created only in new or empty allowed-root directories, must never replace
  existing entries, and must redact absolute source paths in the manifest unless explicitly
  requested.
- Retained analysis results are stdio-session scoped, are deleted on normal shutdown, can be
  deleted explicitly within the current scope, and use the configured TTL as both an active hard
  limit and an abnormal-exit orphan cleanup threshold.
- KEGG credentials, licensed endpoints, cached responses, and user inputs must remain local and out of logs, fixtures, packages, and releases.
- External annotation tools, model code, model weights, and databases are outside the core
  package runtime boundary. The optional companion is a separate local process that accepts only
  an explicitly configured checkout, interpreter, private state root, and allowed input/handoff
  roots. It uses fixed automatic-device arguments, inherits existing accelerator visibility,
  enforces bounded files and one process per state root, terminates its process group and Linux
  child on parent death, and never downloads or updates dependencies or weights.
- Default local tests must not access live KEGG services. Pull-request CI runs one serialized,
  request-bounded 120-request live campaign and must never upload KEGG payloads; merging to `main`
  does not repeat that workflow.
