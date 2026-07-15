# KEGG MCP

KEGG MCP is a local-first MCP server and repository-scoped Codex Skill for turning KEGG
Orthology (KO) annotations into traceable KEGG mappings, module evaluations, pathway coverage
summaries, and cautious biological interpretations. This repository is the canonical source for
the project.

> Project status: Version 0.2.0 implements the reviewed workflow remediation and is verified with
> offline tests. This release supports and is tested only on Python 3.11.x.

## Intended users

The project is designed for bioinformatics users who have one of the following:

- a plain list of K numbers;
- a CSV or TSV annotation table from DeepKOALA, KofamScan, BlastKOALA, GhostKOALA, or another tool;
- a protein FASTA and an explicitly configured optional local DeepKOALA companion; or
- two or more KO sets that need a deterministic, non-statistical comparison.

The primary user experience is one high-level analysis request. Lower-level MCP tools remain
available for users who need explicit control over normalization, KEGG retrieval, module
evaluation, or pathway coverage.

## Five-minute Codex academic user test

This user-facing acceptance profile uses live public KEGG access. Use it only after confirming that
you are an academic user performing academic work. From an exact source checkout, create the
locked environment and an allowed demo root:

```bash
uv sync --frozen
mkdir -p /tmp/kegg-mcp-demo
KEGG_MCP_ACCESS_MODE=public_academic \
KEGG_MCP_ACADEMIC_USE_CONFIRMED=true \
KEGG_MCP_ALLOWED_ROOTS=/tmp/kegg-mcp-demo \
.venv/bin/kegg-mcp doctor
```

Register the absolute stdio command with Codex, then restart Codex:

```bash
codex mcp add kegg-mcp \
  --env KEGG_MCP_ACCESS_MODE=public_academic \
  --env KEGG_MCP_ACADEMIC_USE_CONFIRMED=true \
  --env KEGG_MCP_ALLOWED_ROOTS=/tmp/kegg-mcp-demo \
  -- /absolute/path/to/kegg_mcp/.venv/bin/kegg-mcp
codex mcp list
```

Then try this first prompt:

> Use kegg-mcp in a bounded acceptance check. First confirm that server status is
> `public_academic`, then probe connectivity once. Retrieve only KO entry `K00844` and report its
> identifier, name, database release when available, and whether it came from network or cache.
> Stop after this single entry and do not run pathway discovery.

The connectivity probe makes one low-cost live request. The single content lookup is cached and
may be served locally on later checks. JSON or TOML MCP snippets are configuration file content,
not Bash commands. See [installation](docs/installation.md) for access modes and
[troubleshooting](docs/troubleshooting.md) if discovery fails.

## Internal development profile

Internal iteration, the default test suite, and pull-request CI stay explicitly offline so
repeated development does not contact KEGG:

```bash
export KEGG_MCP_ACCESS_MODE=offline_cache
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
```

The package-level fallback also remains `offline_cache` when no access environment is supplied.
Only the explicitly configured academic user-test profile defaults to live access. A separately
enabled, manually dispatched main-only job performs one serialized four-request campaign; it is
never part of pull-request validation. See `tests/live/README.md` for its authorization and
request budget.

## Current implementation

The Python package currently provides:

- frozen Pydantic contracts for source provenance, annotation records, datasets, import reports,
  and derived KO evidence views;
- exact K-number validation and explicit `ko:` prefix normalization;
- newline-delimited plain KO import;
- generic CSV/TSV import with safe common-column inference, explicit overrides, protein names, and
  named decision policies;
- import-only support for the documented DeepKOALA detailed CSV fields, including optional
  domain coordinates;
- deterministic strict and lenient KO views in which rejected predictions never enter lenient
  analysis;
- explicit public-academic, licensed-endpoint, and network-disabled KEGG access configuration;
- typed, bounded KEGG `info`, selected `get`, selected `link`, and selected `conv` services;
- a process-wide no-burst rate limiter, bounded retries, safe HTTPS transport, and strict text
  parsers;
- a parser-validated, endpoint-labelled SQLite cache with entry-level GET reuse, canonical
  relationship keys, explicit freshness, and offline behavior;
- a lossless, bounded KEGG MODULE tokenizer and parser with source spans, explicit unsupported
  nodes, top-level blocks, optional terms, nested expressions, and M-number references;
- pure exact-completion and project-defined block-coverage evaluation with bounded minimal missing
  alternatives, separate strict/lenient results, and uncertain-record attribution;
- typed pathway-reference construction from exact KEGG LINK and GET results, with explicit `ko`,
  `map`, and organism namespaces, CLASS-derived scope, and retrieval provenance;
- pure, bounded strict/lenient pathway KO coverage with explicit denominators, analysis-unit
  warnings, and conservative empty-denominator behavior;
- deterministic, bounded multi-set KO comparison plus shared-reference MODULE and pathway outcome
  comparison without statistical or biological change claims;
- typed, bounded service-layer loading of requested MODULE graphs and pathway references with
  ten-entry GET batching and shared aggregate request and response budgets;
- one-call KO import, automatic reference-pathway discovery, MODULE/pathway analysis, concise
  output-directory bundles, and scoped result retention with bounded direct previews;
- deterministic structured JSON, concise Markdown, and flat annotation CSV report artifacts with
  complete one-call execution provenance and explicit hard limits; and
- a scope-isolated SQLite result store with 24-hour default retention, a 512 MiB logical payload
  quota, a 640 MiB main-database page cap, a 10,000-result cap, metadata pagination, artifact
  byte-range reads, explicit deletion, and cleanup;
- a local stdio MCP server exposing nine bounded tools with explicit schemas, structured
  success/error envelopes, accurate annotations, and clean protocol stdout;
- a backward-compatible CLI with explicit `serve` and side-effect-free, redacted
  `doctor [--json]` deployment diagnostics;
- two fixed status resources and four validated resource templates for scoped results, bounded
  result ranges, and offline-only reads of configured cache entries;
- side-effect-free status and cache-info reads that redact local paths and report SQLite
  statistics as `null` with `inspection_status=not_probed`, plus file-handoff readiness and an
  allowed-root count, rather than opening a database or exposing configured paths;
- an instruction-only `kegg-ko-analysis` Skill for existing KO evidence and optional companion
  handoff, with confidence, MODULE/pathway interpretation, and reporting references; and
- redistributable synthetic examples, installation guidance, release gates, and offline package
  audit tests.

Importers accept UTF-8 content. The MCP boundary may read a controlled absolute annotation path
and write a requested output bundle only beneath `KEGG_MCP_ALLOWED_ROOTS`. It does not execute
DeepKOALA or another annotation program, and it never infers missing tool versions, model
identities, model/database versions, annotation dates, or organism metadata.

See the [Milestone 1 import contracts](docs/import-contracts.md) for the public API and decision
rules, and the [Milestone 2 KEGG client contract](docs/kegg-client.md) for access, request, cache,
and provenance behavior. See the [Milestone 3 MODULE analysis contract](docs/module-analysis.md) for
grammar, reference resolution, exact evaluation, and interpretation boundaries. See the
[Milestone 4 pathway and comparison contract](docs/pathway-comparison-analysis.md) for pathway
denominators, coverage, deterministic set comparison, and shared-reference outcome comparison. See
the [Milestone 5 services, result storage, and reporting contract](docs/services-results-reporting.md)
for the one-call workflow, report artifacts, result isolation, retention, and retrieval behavior.

## Optional local DeepKOALA companion

`companions/deepkoala-mcp/` is a separate, CPU-only stdio distribution for an explicitly
configured official DeepKOALA checkout and an existing PyTorch interpreter. It is not imported by
the core package, does not install or download DeepKOALA, weights, PyTorch, HMMER, KOfam profiles,
or KEGG data, and is not included in core wheel or source-distribution artifacts.

The companion uses a prepare/confirm/submit lifecycle, one concurrent CPU job, a small thread
limit, fixed subprocess arguments, bounded FASTA and output files, and controlled absolute-path
handoff. It returns detailed CSV and readable source provenance to the existing source-agnostic
core importer. It deliberately has no workflow or artifact hash protocol and does not duplicate KO
normalization. See the [companion README](companions/deepkoala-mcp/README.md) for its independent
installation and configuration contract.

## MCP tools and resources

The installed entry point is `kegg-mcp`: no arguments starts stdio, `serve` is an explicit
equivalent, and `doctor [--json]` validates redacted deployment configuration without network or
database probes. The server exposes:

- `analyze_ko_annotations`;
- `normalize_ko_annotations`;
- `get_kegg_entries`;
- `map_ko_ids`;
- `analyze_modules`;
- `analyze_pathways`;
- `compare_ko_sets`;
- `probe_kegg_connectivity`; and
- `get_server_status`.

Fixed resources are `ko-analysis://status` and `ko-analysis://cache/info`. Validated templates
cover a result index, a result section, a bounded result byte range, and an offline-only cached
KEGG entry. Each stdio server process generates an opaque scope, so retained results are not
readable from another process scope. See the [installation and operation guide](docs/installation.md)
for exact access-mode configuration, calls, and result retrieval.

The initialization response also supplies bounded workflow instructions covering connectivity
preflight, allowed roots, result scope, stable bundles, and biological interpretation boundaries.

## Repository-scoped Codex Skill

The instruction-only Skill is located at `.agents/skills/kegg-ko-analysis/` and declares the actual
`kegg-mcp` stdio dependency. It routes existing K numbers, annotation tables, MODULE/pathway
questions, and deterministic KO-set comparisons without duplicating normalization or analysis
code. For FASTA without KO evidence, it may orchestrate an explicitly available local
`deepkoala-mcp` companion and then hand the controlled detailed CSV path to the core importer.
Inference, process control, weights, and normalization remain outside the Skill. Pathway rendering
belongs to an independent Skill and MCP.

Deterministic static tests cover the Skill's instruction contract; they do not execute a language
model. The six required prompts also have a recorded independent forward/manual review in the
[Skill evaluation record](docs/skill-evaluation.md). That review was repeated against the exact
v0.2.0 candidate for publication sign-off. Synthetic inputs and access-mode templates are under
`examples/`.

## Distribution boundary

The core Python wheel and Python source distribution contain the MCP Python server, package
metadata, and required license notices. They do not install the repository-scoped Skill, the
optional companion, or the complete repository documentation and examples. The companion has its
own package metadata, lock file, entry point, validation, and release review.

Use `.agents/skills/kegg-ko-analysis/` from an exact GitHub repository checkout or tag source archive when
the Codex Skill is required. That Skill can depend on a separately installed `kegg-mcp` Python
server, but installing the wheel alone does not make the Skill available to Codex.

## Scope

The implemented MVP can:

- validate and normalize KO annotation records without assuming a single annotation source;
- preserve source decisions, scores, thresholds, model versions, domain coordinates, and provenance when available;
- query supported KEGG REST endpoints through a rate-limited, cached client;
- map K numbers to pathways, modules, reactions, EC numbers, and selected BRITE entries;
- evaluate exact KEGG module completion and a separately named project-defined block-coverage metric;
- summarize pathway KO coverage without claiming pathway activity or phenotype;
- compare KO sets descriptively; and
- return structured MCP results plus concise Markdown reports.

The core MVP does not run sequence annotation software, perform enrichment or
differential-abundance statistics, redistribute KEGG datasets, generate pathway images, host a
public annotation service, or infer pathway activity from KO presence alone. The optional local
companion is a separately installed MCP-side runner and does not broaden the core package boundary.

## Important KEGG usage constraint

The public KEGG REST API is restricted to academic use by academic users and requests must be limited to no more than three calls per second. Non-academic use requires an appropriate KEGG license. This project's source-code license will not grant rights to KEGG content.

Cached KEGG responses must remain local to the user, must not be committed to this repository, and must not be included in releases, test fixtures, or example outputs.

See the [KEGG API page](https://www.kegg.jp/kegg/rest/) and [KEGG legal notice](https://www.kegg.jp/kegg/legal.html) before implementing or operating the live client.

## Development documentation

- [Installation and operation](docs/installation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [MCP server tools, resources, and configuration](docs/mcp-server.md)
- [High-star MCP repository benchmark](docs/mcp-benchmark-review.md)
- [Release-readiness checklist](docs/release-readiness.md)
- [Codex Skill evaluation record](docs/skill-evaluation.md)
- [Development plan](docs/development-plan.md)
- [Milestone 1 import contracts](docs/import-contracts.md)
- [Milestone 2 KEGG client and cache contract](docs/kegg-client.md)
- [Milestone 3 KEGG MODULE analysis contract](docs/module-analysis.md)
- [Milestone 4 pathway coverage and comparison contract](docs/pathway-comparison-analysis.md)
- [Milestone 5 services, result storage, and reporting contract](docs/services-results-reporting.md)
- [Repository instructions for Codex and contributors](AGENTS.md)

The development plan records the reviewed architecture, data contracts, biological interpretation rules, MCP surface, repository layout, milestones, and acceptance criteria.

## Current repository layout

```text
.
├── AGENTS.md
├── CHANGELOG.md
├── README.md
├── SECURITY.md
├── examples/                         # Redistributable KO inputs and access templates
├── docs/
│   ├── development-plan.md
│   ├── installation.md
│   ├── import-contracts.md
│   ├── kegg-client.md
│   ├── mcp-benchmark-review.md
│   ├── mcp-server.md
│   ├── module-analysis.md
│   ├── pathway-comparison-analysis.md
│   ├── release-readiness.md
│   ├── skill-evaluation.md
│   ├── services-results-reporting.md
│   └── troubleshooting.md
├── companions/deepkoala-mcp/         # Independent optional CPU-only runner distribution
├── pyproject.toml
├── src/kegg_mcp/
│   ├── execution.py                  # Neutral service limits and execution provenance
│   ├── domain/                       # Immutable evidence and policy contracts
│   ├── importers/                    # Inline plain, generic, and DeepKOALA importers
│   ├── kegg/                         # Typed KEGG client, parsers, transport, and local cache
│   ├── analysis/                     # Pure MODULE, pathway, and comparison analysis
│   ├── reporting/                    # Bounded structured, Markdown, and CSV artifacts
│   ├── services/                     # Reference loading, orchestration, and result storage
│   └── mcp/                          # Stdio tools, resources, schemas, and configuration
├── .agents/skills/kegg-ko-analysis/  # KO-analysis-only Codex Skill and references
└── tests/                            # Unit, integration, MCP, Skill, release, and opt-in live tests
```

## Language policy

Maintainer collaboration may be conducted in Simplified Chinese. All repository-tracked files, code identifiers, comments, examples, issue text, pull request text, and release material should be written in English.

## License

The project source code is licensed under the [MIT License](LICENSE). This license does not grant rights to KEGG data, KOfam profiles, DeepKOALA model artifacts, or other third-party content.
