# Changelog

All notable changes to this project are documented in this file. The project follows the
structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [0.1.0] - 2026-07-15

First private GitHub release of the local stdio MCP server and repository-scoped Codex Skill.

### Added

- Immutable annotation-evidence contracts that preserve raw decisions, provenance, multiple KO
  assignments per sequence, ranks, and optional domain coordinates.
- Plain KO, explicit generic table, and detailed DeepKOALA import-only workflows with versioned
  normalization policies.
- A typed KEGG client with explicit public-academic, licensed, and offline-cache modes; bounded
  retries; a process-wide no-burst rate limiter; ten-entry GET batching; and a local SQLite cache.
- Lossless KEGG MODULE parsing, bounded reference resolution, exact completion, and separately
  named project block coverage.
- Descriptive pathway KO coverage with explicit namespaces, denominators, and retrieval
  provenance.
- Deterministic KO-set and analysis-outcome comparison without statistical inference.
- A one-call plain-KO analysis service, bounded JSON/Markdown/CSV reporting, and scoped local
  result retention.
- A local stdio MCP server, bounded tools, result resources, and protocol contract tests.
- A repository-scoped Codex Skill for workflow selection and conservative interpretation.
- Redistributable synthetic examples, installation guidance, and an offline release audit.

### Security

- Reject arbitrary KEGG URLs, unsafe licensed endpoints, filesystem traversal, symlink escapes,
  invalid result identifiers, oversized inputs, and oversized outputs.
- Keep stdio stdout reserved for MCP protocol messages and redact secrets and full local paths
  from status output.
- Keep live KEGG access disabled until the operator explicitly selects an eligible access mode.

### Limitations

- Version 0.1.0 supports and is tested only on Python 3.11.x; package metadata excludes
  Python 3.12 and later.
- KEGG REST public access is restricted to eligible academic use; other operators need an
  appropriately licensed endpoint or must use authorized local cache content offline.
- The server does not run DeepKOALA or another sequence annotator and does not distribute model
  weights, KOfam profiles, or KEGG datasets.
- KO annotations and pathway KO coverage do not establish experimental validation, pathway
  activity, flux, phenotype, or statistical significance.
