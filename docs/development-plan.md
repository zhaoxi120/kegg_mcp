# KEGG MCP Current Architecture and Development Contract

Status: implemented architecture and active development contract.

The repository contains three independently packaged local stdio MCP servers and three focused
repository Skills. Core version `0.5.0`, DeepKOALA companion version `0.4.0`, and renderer version
`0.3.0` are the current source versions. All three distributions support Linux with CPython 3.11.x
only. Wider Python or operating-system support requires separate compatibility work.

The unified Codex installer is implemented and covered by deterministic release tests. It remains
release-gated until the exact release candidate is installed through a real supported Codex path
and all three Skills and MCP registrations are discovered in a new Codex task. Public release also
requires the repository-visibility and rights checks listed below.

Last architecture review: 2026-07-21.

## Product boundary

KEGG MCP converts supplied KO annotation evidence into traceable KEGG relationships, conservative
MODULE evaluation, descriptive pathway coverage, deterministic KO-set comparisons, and bounded
static visualizations. It does not infer experimental validation, expression, activity, flux,
phenotype, or statistical significance.

The supported input routes are:

- a plain list of K numbers;
- a generic CSV or TSV annotation table with an explicit or unambiguous column mapping;
- previously generated DeepKOALA detailed output;
- a protein FASTA processed by the separately installed `deepkoala-mcp` companion; and
- an existing compatible `render_input.json` passed directly to `kegg-render-mcp`.

The common FASTA-to-image workflow is:

```text
protein FASTA
  -> deepkoala-mcp
  -> deepkoala_annotations.csv plus source provenance
  -> kegg-mcp
  -> output bundle plus render_input.json version 3
  -> kegg-render-mcp
  -> bounded static SVG or PNG artifacts
```

The current product does not provide:

- annotator execution, model loading, KGML parsing, or image generation inside the core server;
- nucleotide gene prediction, translation, sequence alignment, or unrestricted KO-to-gene search;
- abundance analysis, enrichment, differential abundance, confidence intervals, or replicate-aware
  statistics;
- metabolic modeling, flux inference, or phenotype prediction;
- remote HTTP transport, public hosting, user accounts, or multi-user result storage;
- a web UI, interactive HTML, browser automation, or active SVG content;
- public plugin marketplace distribution or automatic update services;
- KEGG dataset mirroring or redistribution; or
- automatic installation of later DeepKOALA weights, multi-domain resources, or KOfam profiles.

Organism-scoped gene discovery, abundance-aware methods, statistical enrichment, global or overview
pathway line overlays, Streamable HTTP, non-KEGG backends, and wider platform support require
separately assigned work. They must not leak into current interfaces as speculative options.

## Process, package, and Skill architecture

The repository keeps three process boundaries:

| Process | Responsibility | Explicit exclusions |
| --- | --- | --- |
| `kegg-mcp` | Import KO evidence, retrieve typed KEGG references, analyze, retain results, and produce renderer handoffs | Running an annotator, parsing KGML, or rendering images |
| `deepkoala-mcp` | Validate an allowed FASTA, run one controlled external DeepKOALA job, and deliver detailed annotation files | KEGG analysis, KO decision normalization, model updates, or multi-domain installation |
| `kegg-render-mcp` | Validate a version 3 handoff, retrieve allowed pathway assets, and render static artifacts | Annotation inference, KO normalization, MODULE recomputation, or pathway-coverage recomputation |

The distributions remain independently versioned, locked, installed, and reviewed. The renderer
depends on `kegg-mcp>=0.5,<0.6` for the typed pathway-asset interface. The core package does not
depend on either companion, and no server starts another server.

The three repository Skills have one MCP dependency each:

| Skill | MCP dependency | Responsibility |
| --- | --- | --- |
| `deepkoala-annotation` | `deepkoala-mcp` | Protein FASTA annotation and stable detailed-CSV delivery |
| `kegg-ko-analysis` | `kegg-mcp` | Existing KO evidence, MODULE/pathway analysis, and KO-set comparison |
| `kegg-pathway-rendering` | `kegg-render-mcp` | Static rendering from an existing compatible handoff |

Skills contain instructions, not deterministic implementation code. They do not launch
subprocesses, manage weights, normalize records, call KEGG directly, parse KGML, calculate analysis,
construct SVG, manipulate pixels, or invent resource URIs. Stable versioned files in user-selected
allowed output directories connect stages. Process-scoped job and result identifiers are local
optimizations, not cross-process authorization or durable handoff tokens.

Domain, importer, KEGG client, analysis, reporting, service, and storage code remain independent of
MCP transport. MCP handlers call public service functions and do not duplicate domain logic.

## Annotation evidence and provenance

The implemented evidence boundary is source-agnostic. `SourceProvenance`, `AnnotationRecord`,
`AnnotationDataset`, and the derived KO evidence views retain the information required to explain
how a K number entered an analysis.

The following rules are invariant:

- Raw source evidence is immutable after import.
- One sequence may have multiple KO records, ranks, or domain-coordinate assignments.
- A KO set is a derived view of records, not the primary evidence model.
- The source decision is preserved separately from a named and versioned normalization policy.
- Normalized states are `accepted`, `uncertain`, `rejected`, `unclassified`, and `invalid`.
- Malformed identifiers and unparsed rows remain visible in the import report.
- Duplicate and conflicting evidence is reported and never silently collapsed.
- Scores from different sources or score semantics are not compared as if they shared a scale.
- Dataset context records the analysis unit and optional taxonomic context.
- All analysis parameters, algorithm versions, source versions, and KEGG retrieval provenance are
  serializable.

Strict analysis uses accepted K numbers only. Lenient analysis uses accepted plus records that a
documented policy explicitly classifies as uncertain. Rejected or merely below-threshold
predictions never enter lenient analysis by default.

A K number assignment is an annotation, not experimental validation. A rejected assignment is not
evidence that the function is biologically absent. Community or mixed-sample results describe
pooled encoded potential and must not be reported as a complete pathway in one organism.

Plain user-supplied K numbers may be accepted for analysis under the versioned
`user_supplied_ko` input policy. That policy describes input handling and does not elevate the
evidence to experimental validation.

## KEGG access, rights, rate limiting, and cache

The public KEGG REST service is available for academic use by academic users. Public-academic
deployment records the operator's confirmation; the software does not determine legal eligibility.
Non-academic use requires an appropriately licensed endpoint and explicit licensed-use
configuration. Offline-cache operation never falls back to the network.

Core and Renderer share a deployment-wide, no-burst rate budget. The configured rate defaults to
two requests per second and cannot exceed three requests per second. One KEGG `get` request contains
at most ten entries. Single-pathway PNG and KGML retrieval uses one asset per request. All live
operations use typed allowlists, bounded identifiers and responses, timeouts, bounded retries for
transient failures, and no retry for deterministic client errors.

The client exposes bounded typed `info`, selected `get`, selected `link`, and selected `conv`
operations. It is not an arbitrary URL proxy. `list`, `find`, whole-database conversion, broad gene
discovery, and unrestricted `link/genes/<KO>` expansion are absent from the public surface.

Cache behavior is deployment-owned:

- cache rows are isolated by canonical endpoint identity and access namespace;
- credentials and licensed endpoint URLs never enter public status or readable request keys;
- fresh, stale, missing, malformed, and failed retrievals remain distinguishable;
- cache-only reads make zero network requests;
- stale data is used only under an explicit deployment policy and is reported as stale;
- cache corruption or network failure is never reported as biological absence;
- raw KEGG payloads remain local and are excluded from Git, tests, packages, CI artifacts, and
  releases; and
- status reads do not create or open SQLite merely to collect statistics and report
  `inspection_status=not_probed` for uninspected counts.

The default local test profile performs no live KEGG requests. Pull-request CI runs one serialized
campaign of 30 `INFO`, 30 `GET`, 30 `LINK`, and 30 `CONV` requests at one request per second with
zero retries and no uploaded KEGG payloads. The workflow has no merge-push repetition. This campaign
is an access and compatibility check, not permission to redistribute responses.

## MODULE, pathway, and comparison semantics

MODULE evaluation follows the supported KEGG logical syntax:

- top-level spaces connect required blocks with AND;
- plus signs express AND within a block;
- commas express alternatives with OR;
- a minus sign marks an optional component;
- parentheses preserve grouping; and
- M-number references are resolved with bounded depth and cycle detection.

The parser preserves source spans and unsupported content. It never drops an unknown token. A
malformed, unresolved, cyclic, unsupported, or limit-exceeding required expression produces an
explicit `partially_evaluable` or `not_evaluable` result with a reason.

Exact MODULE completion is a Boolean evaluation of the full supported definition. Project block
coverage is the ratio of completed required top-level blocks to all evaluable required top-level
blocks. It is not an official KEGG completeness percentage. No coverage ratio is exposed when every
required block cannot be evaluated. Optional terms do not increase the denominator. Minimal missing
alternatives are bounded and do not imply that adding a gene will activate a process.

Pathway coverage is descriptive unique-KO overlap. Each request and result records the canonical
pathway identifier, reference namespace, numerator, denominator, retrieval time, cache state, and
release information when available. KO-only input uses the canonical KO reference view and cannot
support an organism-specific pathway claim.

`PathwaySpec` validates the namespace, canonicalizes an omitted `map` view to `ko`, and
de-duplicates paired views by pathway number. Global and overview references require explicit core
analysis opt-in and a warning; the current renderer rejects them because it implements regular-box
overlays only.

Pathway output does not contain `pathway_present`. Coverage must not be described as pathway
presence, completeness, expression, activity, flux, phenotype, or experimental validation.

When the high-level service receives no explicit MODULE or pathway target, it independently selects
up to five MODULEs and five canonical KO reference pathways. Ranking uses the number of unique K
numbers selected by the requested evidence mode and the canonical target identifier as the stable
tie-breaker. Ranking selects definitions to analyze; it is neither MODULE completion nor enrichment.
Duplicate records and duplicate KEGG relationships never inflate the overlap.

KO-set comparison is deterministic set comparison. It preserves labels, analysis units, policies,
and compatible KEGG provenance. It reports shared and set-specific evidence and differences in
MODULE or pathway outcomes without p-values, fold changes, enrichment, differential abundance, or
biological gain/loss claims.

## Core service and MCP surface

The core MCP server uses stdio transport. Protocol messages are the only stdout content; logs and
diagnostics use stderr or a configured file. All tool inputs and outputs use explicit schemas,
schema-conforming `structuredContent`, bounded text summaries, and accurate MCP annotations.

The eleven core tools are `analyze_ko_annotations`, `normalize_ko_annotations`,
`get_kegg_entries`, `map_ko_ids`, `analyze_modules`, `analyze_pathways`, `compare_ko_sets`,
`probe_kegg_connectivity`, `list_analysis_results`, `delete_analysis_result`, and
`get_server_status`.

`analyze_ko_annotations` is the common one-call workflow. It imports evidence once, performs
bounded optional Top-N selection, loads only required references, evaluates requested targets,
retains complete bounded artifacts, and optionally writes a durable output bundle. Narrower tools
reuse the same service and domain functions.

Ordinary tool inputs expose only analysis choices. Eligibility, endpoints, cache policy, allowed
roots, storage, rate limits, and hard service limits remain deployment configuration. Connectivity
probing is explicit and open-world. Status is side-effect-free, redacted, and does not imply that
the network has been tested.

Large results return bounded previews plus scoped retrieval. The core publishes fixed status and
cache resources and these resource templates:

```text
ko-analysis://results/{result_id}
ko-analysis://results/{result_id}/{section}
ko-analysis://results/{result_id}/{section}/{offset}/{limit}
kegg-cache://entries/{database}/{identifier}
```

URI parameters, offsets, sizes, identifiers, and MIME types are validated. Unknown, expired,
deleted, invalid, and cross-scope result identifiers return the same safe not-found behavior.

The SQLite result store defaults to a 24-hour hard TTL, a 512 MiB logical artifact quota, a 640 MiB
main-database cap, and 10,000 active results. It never evicts an unexpired result to make room for a
new one. Normal stdio shutdown deletes the current process scope. Explicit cleanup removes only
expired rows; durable output bundles remain operator-owned.

Output-bundle schema version 3 always writes validated normalized evidence and a
`bundle_manifest.json` beneath a configured allowed root. Analysis bundles additionally include
reports, analysis tables, and `render_input.json`; normalization-only bundles do not claim those
artifacts. A destination must be new or empty. Writes are atomic, owner-only where supported,
symlink-safe, and never replace an existing entry. The manifest is installed last as the commit
marker and redacts absolute source paths by default.

Errors use stable machine-readable codes, a bounded safe message, recoverability, a suggested
action, and redacted details. Inputs, fields, identifiers, target counts, decompressed bytes,
outputs, and retained artifacts are bounded. Filesystem access requires direct absolute paths below
configured roots and rejects traversal, unsafe ancestry, replacement races, and symlink escape.
No component uses `shell=True`.

## DeepKOALA companion contract

DeepKOALA is an external, independently versioned annotation tool. The core server imports its
previously generated detailed table and never loads its models or runs its CLI. Only
`deepkoala-mcp` may launch the configured external checkout.

The companion validates one allowed protein FASTA and a new allowed output directory, performs
runtime preflight, starts one service-owned job, and returns an opaque process-scoped job identifier
for bounded polling. It runs one job per state root with fixed arguments, `device=auto`, inherited
accelerator visibility, bounded CPU threads, zero data-loader workers, and process-group and Linux
parent-death control.

The stable handoff distinguishes the original FASTA path, `deepkoala_annotations.csv` as the core
import input, `deepkoala_run_report.md` as the human-readable report, and the resolved DeepKOALA and
model resource versions used by the job.

The default managed installation uses the official DeepKOALA repository and its bundled `202502`
resources. The suite does not pin a source revision, verify a source archive hash, download
multi-domain dependencies, or provide an in-repository weight-update mechanism. Multi-domain
capability is deployment opt-in: the operator must separately provide an absolute `hmmsearch`
executable and a local KOfam profile directory. Individual requests still default to single-domain
mode and may enable multi-domain execution only when the deployment reports it ready. A user may
manage a newer external installation separately, but every result must report the resolved model
version.

Detailed output preserves `predict_label`, probability, threshold, and source annotation marker.
The default importer accepts a verified source-positive marker or probability meeting its source
threshold. A below-threshold prediction remains source-rejected, not uncertain. Missing predictions
are unclassified and malformed K numbers are invalid. Multi-domain rows retain paired coordinates
as independent evidence records. The companion records whether multi-domain mode was used without
exposing local HMMER or profile paths.

Companion status preserves the existing readiness fields and adds one stable redacted route state,
fixed issue text, and a fixed next action. It reports structural multi-domain readiness separately
from the default request mode without claiming that a local profile collection is complete. Invalid
deployment configuration remains a doctor/startup failure rather than a fabricated MCP route.

## Renderer contract

The core produces immutable `render_input.json` schema version 3 and
`AnalysisExecutionProvenance` version 3. The handoff contains accepted and policy-defined uncertain
evidence, complete pathway detected-KO evidence within explicit limits, resolved MODULE syntax and
states, renderability results, and calculation provenance. Preview-only version 1 input cannot be
upgraded losslessly and is rejected with a recoverable instruction to rerun analysis.

The renderer consumes core-authoritative evidence and results. It does not normalize annotations,
resolve a second KO policy, or recompute MODULE completion, block coverage, pathway denominators, or
coverage ratios. It uses the core package's typed single-pathway PNG/KGML asset interface and shares
the deployment access gate, rate limiter, cache namespaces, bounds, and retrieval provenance.

The supported outputs are:

- regular reference-pathway overlays using a matching bounded PNG and KGML document;
- project-owned MODULE logic diagrams derived from the authoritative core AST;
- canonical static SVG; and
- optional bounded PNG raster derivatives.

Accepted and uncertain evidence use distinct, accessible states with redundant non-color cues.
Unmatched graphics remain unchanged and are never labelled biologically absent. MODULE diagrams
preserve AND, OR, optional, grouping, reference, unsupported, and unresolved states and display
exact completion separately from block coverage.

Renderer paths, XML bytes, elements, attributes, nesting, coordinates, image bytes, decoded pixels,
canvas dimensions, SVG nodes, serialized output, retained artifacts, and resource pages are bounded.
XML processing disables DTD resolution, external entities, and network access. SVG contains no
scripts, event handlers, active links, remote fonts, or external resources.

Real KEGG PNG, KGML, cache payloads, and rendered derivatives are not tracked, packaged, or uploaded
by CI. Renderer fixtures are synthetic and redistributable. Returning a local derivative does not
grant redistribution rights; distributing a KEGG-derived image requires a separate rights review.

## Unified Codex installation contract

`scripts/install-suite.py` is the only supported Codex installation path. It consumes the three
checked-in lockfiles, creates three independent runtimes, copies the three canonical Skill trees,
and registers one generated local plugin with three absolute MCP launch commands. The plugin is a
local deployment artifact, not a fourth Python distribution and not a generic MCP-client installer.

The operator supplies absolute paths to CPython 3.11, compatible `uv`, Git, and Codex executables.
The installer does not select these tools from ambient `PATH` and never downloads Python, `uv`,
Codex, or repository source.

Deployment configuration is a strict TOML direct regular file owned by the current user, with no
group or other permission bits, inside an owner-only direct parent. Unknown fields, wrong types,
relative paths, symlinks, unsafe ancestry, unsafe or overlapping private roots, uncovered handoff
roots, incompatible access profiles, and existing registration conflicts fail before publication.
Private configuration and runtime metadata remain outside the plugin directory Codex may cache.

All `uv` work is offline by default. `--allow-locked-dependency-downloads` permits only `uv` network
access for artifacts selected by checked-in lockfiles and declared build requirements. It does not
update a lockfile or authorize another runtime group. `--allow-deepkoala-install` separately confirms
the first official DeepKOALA clone and upstream-requirements installation for each new suite root.
Later FASTA jobs in the same installation do not repeat that question.

Publication is transactional across runtimes and Codex registration. Existing marketplace names,
plugins, MCP registrations, or installation roots are never replaced. A caught failure rolls back
only state proven to belong to the new transaction. Incomplete or failed rollback state is marked
for bounded manual recovery; cleanup never deletes user biological inputs, outputs, caches,
external tools, or model resources.

Installer tests validate source completeness, strict configuration, three locked runtimes, offline
arguments, generated plugin content, private-data exclusion, Codex inventory checks, conflicts,
interruption, rollback, and managed DeepKOALA defaults. Release support additionally requires a real
Codex installation and discovery check in a new task against the exact release candidate.

## Development and validation workflow

Work begins from an assigned issue or explicit maintainer request. Change only the smallest affected
closure: implementation, directly related tests, and documentation required by a changed public
contract. Reuse existing services, schemas, errors, fixtures, configuration, and security policies
before adding another implementation path.

Do not add speculative modules, dependencies, public APIs, compatibility paths, placeholder tests,
empty package trees, or partially functional Skills. Replace and delete obsolete paths in the same
change when practical. Changes spanning more than five production files or roughly 250 net new
production lines are a review signal; explain or split them when behaviors can be reviewed
independently.

Batch coherent edits before validation. During implementation, lint and format changed Python files
and run the nearest affected test node, file, or marker once. Before handoff, run the affected
component's relevant unit, contract, and integration tests. Do not rerun an unchanged passing suite
unless a later edit can affect it.

Run the full offline profile when a change affects a shared public schema, cross-component handoff,
packaging, installation, a lockfile, CI, MCP or Skill binding, a security boundary, KEGG access
policy, or multiple components, or when the maintainer explicitly requests it:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Pure documentation changes do not require Python suites unless they change executable examples,
commands, generated artifacts, or a documented public contract. Expensive clean builds, real
installation, discovery, live-service, and artifact audits normally run in pull-request CI or the
release-readiness workflow. Local live KEGG access remains explicit opt-in.

Every handoff reports the exact checks run and those deferred to CI. For larger changes, also report
production and test growth, new dependencies or public symbols, reused abstractions, obsolete paths
removed, and the reason for any unavoidable parallel implementation.

## Release blockers

A release candidate is validated from the exact merged commit and its three locked distributions.
The following conditions block publication when applicable:

- the package versions, renderer compatibility range, lockfiles, and built archives disagree;
- offline unit, contract, integration, package, and clean-wheel smoke checks fail;
- the governed pull-request live KEGG campaign fails or uploads KEGG payloads;
- any distribution contains another component, repository Skill, KEGG payload, model resource,
  secret, private path, biological input, cache, or generated result;
- the suite transaction, conflict, rollback, private-configuration, or default-offline checks fail;
- a real Codex app or CLI installation does not discover all three Skills and MCP registrations in a
  new task;
- a public release lacks a verified private vulnerability-reporting route and an updated
  `SECURITY.md`;
- a claimed KEGG-derived rendered artifact lacks a separate redistribution-rights review; or
- scientific outputs, tool schemas, resource scope, status redaction, rate limits, or cache rights
  no longer satisfy this contract.

GitHub release notes record the exact commit, tag, platform, Python and tool versions, distribution
versions, CI result, rights review, security review, and redacted suite-installation evidence. A tag
or version identifier is never reused. Release artifacts contain only audited archives.

## Primary sources and review dates

External facts are time-sensitive. Recheck a source when a change affects a parser, schema, fixture,
access rule, or acceptance test, and record the new retrieval date in the same tracked change.

KEGG service, data-format, MODULE, pathway, and rights sources were reviewed on 2026-07-14, with the
visualization-specific API and legal boundary reviewed again on 2026-07-16:

- [KEGG REST overview](https://www.kegg.jp/kegg/rest/),
  [API manual](https://www.kegg.jp/kegg/rest/keggapi.html), and
  [legal notice](https://www.kegg.jp/kegg/legal.html)
- [KEGG MODULE database](https://www.kegg.jp/kegg/module.html) and
  [MODULE entry help](https://www.kegg.jp/kegg/document/help_bget_module.html)
- [KEGG PATHWAY database](https://www.kegg.jp/kegg/pathway.html),
  [database entry format](https://www.kegg.jp/kegg/docs/dbentry.html), and
  [Pathway Map Viewer help](https://www.kegg.jp/kegg/document/help_pathway.html)

DeepKOALA behavior and detailed-output fields were reviewed on 2026-07-14. The optional
multi-domain CLI, HMMER invocation boundary, and short-sequence output were reviewed again on
2026-07-21 against official commit `bebbe0c43f50a26488f7092f6b355aae870a4ed9`:

- [DeepKOALA GenomeNet page](https://www.genome.jp/tools/deepkoala/) and
  [official repository](https://github.com/zhaoxi120/deepkoala)
- [official CLI](https://github.com/zhaoxi120/deepkoala/blob/bebbe0c43f50a26488f7092f6b355aae870a4ed9/deepkoala/cli.py) and
  [multi-domain implementation](https://github.com/zhaoxi120/deepkoala/blob/bebbe0c43f50a26488f7092f6b355aae870a4ed9/deepkoala/infer_multi.py)

MCP tool and resource contracts were reviewed on 2026-07-16 against the 2025-06-18 specification:

- [MCP tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) and
  [resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)

Codex Skill, repository-guidance, and generated-plugin behavior was reviewed on 2026-07-19:

- [OpenAI Codex Skills](https://developers.openai.com/codex/skills),
  [`AGENTS.md`](https://developers.openai.com/codex/guides/agents-md), and
  [plugin documentation](https://learn.chatgpt.com/docs/build-plugins)

The implementation-facing detailed contracts remain in `import-contracts.md`, `kegg-client.md`,
`module-analysis.md`, `pathway-comparison-analysis.md`, `services-results-reporting.md`,
`mcp-server.md`, and `visualization-extension-plan.md`. Public interfaces and tests govern exact
runtime behavior when a summary here omits a lower-level detail.
