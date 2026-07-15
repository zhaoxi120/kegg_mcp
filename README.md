# KEGG MCP

KEGG MCP is a local-first MCP server and repository-scoped Codex Skill for turning KEGG
Orthology (KO) annotations into traceable KEGG mappings, module evaluations, pathway coverage
summaries, and cautious biological interpretations. This repository is the canonical source for
the project.

> Project status: Version 0.1.0 is the first supported private GitHub release. Milestones 0 through
> 8 are implemented and verified with offline tests. This release supports and is tested only on
> Python 3.11.x. The repository also contains an optional, independently installed
> `deepkoala-mcp` 0.1.0 candidate; that companion is unreleased and is not part of the supported
> core 0.1.0 release.

## Intended users

The project is designed for bioinformatics users who have one of the following:

- a plain list of K numbers;
- a CSV or TSV annotation table from DeepKOALA, KofamScan, BlastKOALA, GhostKOALA, or another tool;
- protein FASTA sequences and a need for guidance on obtaining KO assignments first; or
- two or more KO sets that need a deterministic, non-statistical comparison.

The primary user experience is one high-level analysis request. Lower-level MCP tools remain
available for users who need explicit control over normalization, KEGG retrieval, module
evaluation, or pathway coverage.

## Current implementation

The Python package currently provides:

- frozen Pydantic contracts for source provenance, annotation records, datasets, import reports,
  and derived KO evidence views;
- exact K-number validation and explicit `ko:` prefix normalization;
- newline-delimited plain KO import;
- generic CSV/TSV import with explicit column mapping and a named decision policy;
- import-only support for the documented DeepKOALA detailed CSV fields, including optional
  domain coordinates;
- deterministic strict and lenient KO views in which rejected predictions never enter lenient
  analysis;
- explicit public-academic, licensed-endpoint, and network-disabled KEGG access configuration;
- typed, bounded KEGG `info`, selected `get`, selected `link`, and selected `conv` services;
- a process-wide no-burst rate limiter, bounded retries, safe HTTPS transport, and strict text
  parsers;
- an integrity-checked, endpoint-scoped SQLite cache with explicit freshness and offline behavior;
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
- one-call plain-KO import, reference loading, MODULE/pathway analysis, report rendering, and scoped
  result retention with bounded direct previews;
- deterministic structured JSON, concise Markdown, and flat annotation CSV report artifacts with
  complete one-call execution provenance and explicit hard limits; and
- a scope-isolated SQLite result store with 24-hour default retention, a 512 MiB logical payload
  quota, a 640 MiB main-database page cap, a 10,000-result cap, metadata pagination, artifact
  byte-range reads, explicit deletion, and cleanup;
- a local stdio MCP server exposing eight bounded tools with explicit schemas, structured
  success/error envelopes, accurate annotations, and clean protocol stdout;
- two fixed status resources and four validated resource templates for scoped results, bounded
  result ranges, and offline-only reads of configured cache entries;
- side-effect-free status and cache-info reads that redact local paths and report SQLite
  statistics as `null` with `inspection_status=not_probed` rather than opening a database;
- an instruction-only repository-scoped Codex Skill with workflow, annotation-tool, confidence,
  MODULE/pathway interpretation, and reporting references; and
- redistributable synthetic examples, installation guidance, release gates, and offline package
  audit tests.

All importers accept inline UTF-8 text or bytes. They do not read arbitrary paths, access KEGG,
execute DeepKOALA or another annotation program, or infer missing tool versions, model identities,
model/database versions, annotation dates, or organism metadata. Importer-specific default source
labels such as `manual`, `unknown`, and `deepkoala` are explicit contract values.

See the [Milestone 1 import contracts](docs/import-contracts.md) for the public API and decision
rules, and the [Milestone 2 KEGG client contract](docs/kegg-client.md) for access, request, cache,
and provenance behavior. See the [Milestone 3 MODULE analysis contract](docs/module-analysis.md) for
grammar, reference resolution, exact evaluation, and interpretation boundaries. See the
[Milestone 4 pathway and comparison contract](docs/pathway-comparison-analysis.md) for pathway
denominators, coverage, deterministic set comparison, and shared-reference outcome comparison. See
the [Milestone 5 services, result storage, and reporting contract](docs/services-results-reporting.md)
for the one-call workflow, report artifacts, result isolation, retention, and retrieval behavior.

## MCP tools and resources

The installed stdio entry point is `kegg-mcp`. The server exposes:

- `analyze_ko_annotations`;
- `normalize_ko_annotations`;
- `get_kegg_entries`;
- `map_ko_ids`;
- `analyze_modules`;
- `analyze_pathways`;
- `compare_ko_sets`; and
- `get_server_status`.

Fixed resources are `ko-analysis://status` and `ko-analysis://cache/info`. Validated templates
cover a result index, a result section, a bounded result byte range, and an offline-only cached
KEGG entry. Each stdio server process generates an opaque scope, so retained results are not
readable from another process scope. See the [installation and operation guide](docs/installation.md)
for exact access-mode configuration, calls, and result retrieval.

### Optional DeepKOALA companion candidate

The independently installed `deepkoala-mcp` candidate exposes six additional tools:
`get_deepkoala_runner_status`, `prepare_deepkoala_job`, `submit_deepkoala_job`,
`get_deepkoala_job`, `cancel_deepkoala_job`, and `delete_deepkoala_job`. Preparation validates and
privately stages FASTA without inference; submission requires the exact notice digest and explicit
acknowledgement. Detailed CSV, provenance, and sanitized diagnostics are exposed through scoped
resources with bounded pagination. The client verifies and transfers successful detailed CSV to
the core importer; the two servers do not share or dereference each other's private resources.

The companion has one-job concurrency, rejects `multi=true`, never downloads weights, and can be
forced to CPU with `device="cpu"` and a small configured thread limit. The candidate is POSIX-only
because its process lifecycle requires process-group support. See its
[independent installation and operation guide](companions/deepkoala-mcp/README.md).

## Repository-scoped Codex Skill

The instruction-only Skill is located at `.agents/skills/kegg-mcp/` and declares the actual
`kegg-mcp` stdio dependency. It routes protein FASTA, existing K numbers, annotation tables,
MODULE/pathway questions, and deterministic KO-set comparisons without duplicating normalization
or analysis code. It never assigns a KO from a sequence or name and keeps exact MODULE completion
separate from descriptive pathway KO coverage.

Automatic DeepKOALA execution is not a Skill implementation responsibility. The repository now
contains an optional, separately installed companion MCP server and runner process under
`companions/deepkoala-mcp/`. When that service is explicitly configured and discovered, the Skill
may display its execution notice, call it, and pass the resulting detailed table to the core
`kegg-mcp` importer. The companion remains an unreleased 0.1.0 candidate; it is not included in or
supported as part of the core 0.1.0 release.

Deterministic static tests cover the Skill's instruction contract; they do not execute a language
model. The six required prompts also have a recorded independent forward/manual review in the
[Skill evaluation record](docs/skill-evaluation.md). That review was repeated against the exact
v0.1.0 candidate for publication sign-off. Synthetic inputs and access-mode templates are under
`examples/`.

## Distribution boundary

The core Python wheel and source distribution contain the core MCP Python server, package
metadata, and required license notices. They do not install the repository-scoped Skill, the
optional DeepKOALA companion, or the complete repository documentation and examples. The companion
has its own distribution metadata, lock file, environment, entry point, and release review.

Use `.agents/skills/kegg-mcp/` from an exact GitHub repository checkout or tag source archive when
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

The core MVP does not run sequence annotation software, perform enrichment or differential-abundance statistics, redistribute KEGG datasets, generate pathway images, host a public annotation service, or infer pathway activity from KO presence alone. The unreleased DeepKOALA runner candidate is an opt-in companion MCP service, not an expansion of the core server process.

## Important KEGG usage constraint

The public KEGG REST API is restricted to academic use by academic users and requests must be limited to no more than three calls per second. Non-academic use requires an appropriate KEGG license. This project's source-code license will not grant rights to KEGG content.

Cached KEGG responses must remain local to the user, must not be committed to this repository, and must not be included in releases, test fixtures, or example outputs.

See the [KEGG API page](https://www.kegg.jp/kegg/rest/) and [KEGG legal notice](https://www.kegg.jp/kegg/legal.html) before implementing or operating the live client.

## Development documentation

- [Repository capabilities and usage guide](docs/capabilities-and-usage.md)
- [Installation and operation](docs/installation.md)
- [MCP server tools, resources, and configuration](docs/mcp-server.md)
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
├── companions/
│   └── deepkoala-mcp/                # Optional independent companion distribution
├── examples/                         # Redistributable KO inputs and access templates
├── docs/
│   ├── development-plan.md
│   ├── capabilities-and-usage.md
│   ├── installation.md
│   ├── import-contracts.md
│   ├── kegg-client.md
│   ├── mcp-server.md
│   ├── module-analysis.md
│   ├── pathway-comparison-analysis.md
│   ├── release-readiness.md
│   ├── skill-evaluation.md
│   └── services-results-reporting.md
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
├── .agents/skills/kegg-mcp/          # Instruction-only Codex Skill and references
└── tests/                            # Unit, integration, MCP, Skill, and release tests
```

## Language policy

Maintainer collaboration may be conducted in Simplified Chinese. All tracked files, code
identifiers, comments, examples, issue text, pull request text, and release material are written in
English. Local Simplified Chinese reference documents use the `*.zh-CN.md` suffix, remain
gitignored and untracked, and are not uploaded to GitHub or included in packages, releases,
examples, or CI artifacts. Their English counterparts are normative.

## License

The project source code is licensed under the [MIT License](LICENSE). This license does not grant rights to KEGG data, KOfam profiles, DeepKOALA model artifacts, or other third-party content.
