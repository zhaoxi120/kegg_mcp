# Changelog

All notable changes to this project are documented in this file. The project follows the
structure of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Deterministic server-side pathway ranking and bounded Top-N selection in
  `analyze_ko_annotations`, with complete ranking and KO-to-pathway relationship artifacts,
  compact direct summaries, and six-stage execution/cache metrics.
- A separately installed `kegg-render-mcp` 0.1.0 stdio companion for bounded regular-pathway
  evidence overlays and project-owned MODULE logic diagrams, with static SVG, optional PNG, scoped
  resource retention, and synthetic-only tests.
- A complete immutable `render_input.json` version 2 contract that keeps accepted and
  policy-defined uncertain evidence distinct, carries authoritative MODULE states and pathway
  coverage results, and rejects incompatible preview-only version 1 input.
- A typed single-pathway PNG/KGML asset service that reuses the core KEGG access, rate-limit,
  cache, transport, validation, and provenance boundaries without accepting arbitrary URLs.
- An instruction-only `kegg-visualization` Skill for routing compatible core handoffs through the
  independent renderer without duplicating normalization, evaluation, KGML parsing, or rendering.
- An independently installed, CPU-only `deepkoala-mcp` stdio companion with bounded local job
  control and controlled detailed-CSV handoff to the core importer.
- KEGG BRITE htext parser support for compact roots under a validated metadata envelope.
- An opt-in KEGG compatibility campaign covering all four supported operations with bounded,
  configurable repetition and a serialized 120-request pull-request CI profile with 30 requests
  per operation, one request per second, and zero retries.
- A backward-compatible command-line facade with `serve` and a side-effect-free, redacted
  `doctor [--json]` deployment diagnostic.
- Client initialization instructions, a high-star MCP repository benchmark, a Codex CLI quick
  start, a safe first prompt, and a dedicated troubleshooting guide.
- `delete_analysis_result` for immediate current-scope retained-result deletion and
  `kegg-mcp cleanup --expired [--json]` for operator-only expired-row cleanup.

### Changed

- Protein FASTA routing now treats DeepKOALA as local-only, checks the local companion first, and
  requires explicit permission before installation, resource downloads, environment changes, or
  MCP registration; the GenomeNet web form is never automated as an API substitute.
- KEGG LINK preparation now uses canonical greedy packing under identifier and complete-URL
  bounds, with isolated version 2 request keys and fresh-cache-first high-level analysis.
- The core candidate advances to 0.3.0 for the first `RenderInputV2` public contract;
  `kegg-render-mcp` requires `kegg-mcp>=0.3,<0.4`, so the published core 0.1 release and abandoned
  0.2 candidate cannot be selected as compatible renderer dependencies.
- The independently distributed DeepKOALA companion advances to 0.2.0 with a breaking provenance
  correction: the caller's original FASTA path is distinct from the companion-produced detailed
  CSV, and the private staged FASTA is never exposed.
- Analysis bundles now serialize a validated renderer-specific version 2 handoff and record its
  schema and MIME type separately from the complete bundle version.
- Analysis execution provenance advances to schema and service version 2 and records the effective
  MODULE, pathway, evidence-mode, report, reference-loading, import, request, and direct-result
  bounds needed to reproduce the core calculation.
- Pull-request CI is the single automatic validation run; local pre-commit validation is optional,
  default local pytest skips live KEGG calls, and merges to `main` do not repeat the same workflow.
- The repository Skill may orchestrate an available DeepKOALA companion for FASTA input while
  keeping inference, process control, and normalization outside Skill code.
- Companion confirmation uses a server-held opaque job identifier and explicit acknowledgement;
  biological workflow and artifact hashes remain outside the contract.
- The core client and MCP runtime now default to confirmed `public_academic` access and network
  refreshes. The removed `offline_cache` mode is replaced by a per-request `cache_only` option for
  cache resource reads that must never fall back to the network.
- Redacted server status now reports whether file handoff is enabled and the number of configured
  allowed roots without exposing their paths.
- The installation guide now identifies JSON and TOML snippets as configuration file content and
  fixes the nested biological-context placement in the annotation-file example.
- MCP tool annotations now describe local cache, retained-result, output-bundle, deletion, and
  open-world effects rather than treating analytical computation as side-effect free.
- Output-bundle schema version 2 rejects non-empty destinations, never replaces existing files,
  commits its manifest last, and redacts absolute source paths in the manifest by default with an
  explicit absolute-path opt-in.
- Retained results are explicitly stdio-session scoped, deleted on normal shutdown, and governed
  by a 24-hour active hard TTL that also bounds abnormal-exit orphan cleanup; output bundles are
  the durable cross-process artifact.
- Release documentation now identifies core 0.3.0 and both companion versions as unreleased
  candidates, records core v0.1.0 as the only published GitHub release, and states the Linux
  CPython 3.11 support matrix.

### Security

- Renderer input, XML, source images, SVG, PNG, output paths, retained artifacts, and resource URIs
  are bounded, static, scope-isolated, traversal- and symlink-safe, and free of active or external
  content.
- Renderer CI and package audits use only generated synthetic assets and reject real KEGG PNG,
  KGML, cache payloads, model resources, private paths, and cross-component implementation code.
- Portable bundle manifests no longer expose absolute source paths unless the caller explicitly
  selects the absolute-path mode, and current-scope results can be deleted immediately.

## [0.2.0] - Unpublished candidate (2026-07-15)

Workflow-remediation candidate for stable file handoff and a smaller public MCP contract. This
candidate was superseded by 0.3.0 before publication.

### Added

- Allowed-root file inputs and output-directory bundles with normalized annotations, protein-to-KO
  mappings, MODULE and pathway tables, a concise report, a renderer handoff, and a manifest.
- Automatic common-column detection, `protein_name` preservation, automatic reference-pathway
  discovery from accepted K numbers, and inferred canonical `ko` pathway namespaces.
- Entry-level GET cache reuse, order-independent relationship cache keys, readable request keys,
  and an explicit KEGG connectivity probe.
- Field-path validation details, dedicated result-store and output-write errors, and contract tests
  for JSON timezones, file handoff, traversal defense, output bundles, and cache reuse.

### Changed

- Deployment configuration owns KEGG access authorization, cache behavior, and service limits;
  ordinary tool schemas no longer expose `refresh`, `allow_stale`, or internal limit models.
- Deterministic analysis tools are declared read-only and idempotent. The server now exposes nine
  tools, including `probe_kegg_connectivity`.
- Pathway mappings return pathway number, namespace, paired `ko`/`map` identity, and deduplicated
  counts. Equivalent `ko` and `map` views cannot be double-counted.
- The repository Skill is now `kegg-ko-analysis` and covers only existing KO evidence. Protein
  annotation and pathway rendering remain responsibilities of their independent MCPs and Skills.

### Removed

- Input, dataset, KO-set, definition, response, cache, endpoint, result-artifact, and report digest
  fields from biological workflow contracts and reports.
- DeepKOALA execution guidance and rendering orchestration from the core repository Skill.

### Security

- Shared input files, original input provenance paths, and output directories must resolve beneath
  explicitly configured roots; traversal and symlink escapes fail before analysis.
- DNS and permission failures are terminal transport classes and are not repeatedly retried.

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
