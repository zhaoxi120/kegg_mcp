# Security Policy

## Supported versions

| Version | Supported |
| --- | --- |
| 0.1.x | Yes |
| Earlier versions | No |

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
- External annotation tools, model code, model weights, and databases are outside this project's runtime boundary.
- Default tests and CI must not access live KEGG services.
