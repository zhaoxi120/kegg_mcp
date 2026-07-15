# Changelog

All notable changes to this project are documented in this file. The project follows the
structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added an optional, independently installed `deepkoala-mcp` 0.1.0 candidate with six bounded
  stdio tools for redacted status, two-phase job preparation and acknowledged submission, job
  polling, cancellation, and terminal deletion.
- Added scoped detailed-CSV, provenance, and sanitized-diagnostic resources with bounded byte-range
  pagination and a source-agnostic handoff to the existing core DeepKOALA importer.
- Added CPU-bounded runner controls, one-job concurrency, queue and retention limits,
  identity-bound execution-artifact rechecks, and separate offline tests and packaging metadata.
- Added explicit effective FASTA structure limits and diagnostic-truncation state to companion
  status/job contracts, and documented the bounded process-local deletion tombstone window.

### Documentation

- Added an English repository capabilities and usage guide for the first supported release.
- Defined `*.zh-CN.md` as a local-only, ignored convention for non-normative Simplified Chinese
  reference documents that are not published to GitHub or included in distributions.
- Documented installation and operation of the separately configured DeepKOALA companion while
  keeping it explicitly outside the supported core 0.1.0 release.

### Security

- Kept DeepKOALA, PyTorch, model code, and model artifacts outside the core package and process;
  the companion uses an absolute external interpreter and never downloads or replaces weights.
- Added private FASTA staging, allowed-root and symlink enforcement, fixed argument-vector process
  launch without a shell, a pre-execution hard output-file limit, bounded sanitized diagnostics,
  and digest acknowledgement before inference.
- Made the companion candidate fail closed before path handling or state creation on runtimes
  without the required POSIX process-group and file-size-limit support.

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
