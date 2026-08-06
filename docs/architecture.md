# KEGG MCP Current Architecture and Development Contract

This document owns the cross-component architecture and active development contract. The
[release-readiness checklist](release-readiness.md) owns the current distribution-version matrix,
release status, and publication gates. Component documents own their public runtime details.

The repository contains three independently packaged local stdio MCP servers and three focused
repository Skills. The complete three-server suite supports Linux and native Apple Silicon macOS
14 or later with CPython 3.11.x. Windows hosts use the Linux route through WSL2; native Intel macOS
and native Windows server execution are unsupported.

Last architecture and document-ownership review: 2026-08-06.

## Product boundary

KEGG MCP converts supplied KO annotation evidence and bounded entity queries into traceable KEGG
relationships, conservative MODULE evaluation, descriptive pathway coverage, deterministic KO-set
comparisons, and bounded static visualizations. Its query surface returns candidates, typed
cards and current-scope reference differences, KEGG-supplied PubMed identifiers, crosswalks,
relation traces, BRITE classifications, and annotation mapping audits. It can persist selected
references and prepare bounded KEGG Mapper or KEGG Syntax input files, but it does not infer
experimental validation, expression, activity, flux, phenotype, or statistical significance.

The supported input routes are:

- a plain list of K numbers;
- a generic CSV or TSV annotation table with an explicit or unambiguous column mapping;
- previously generated DeepKOALA detailed output;
- a protein FASTA processed by the separately installed `deepkoala-mcp` companion;
- bounded search terms, external gene, organism, or substance identifiers, known KEGG entries, and
  typed KEGG entity seeds supplied directly to the core query services;
- selected current-session card or BRITE results, or caller-supplied KEGG identifiers prepared as
  local handoff files; and
- an existing compatible `render_input.json` passed directly to `kegg-render-mcp`.

The common FASTA-to-image workflow is:

```text
protein FASTA
  -> deepkoala-mcp
  -> deepkoala_annotations.csv plus source provenance
  -> kegg-mcp
  -> output bundle plus render_input.json version 4
  -> kegg-render-mcp
  -> bounded static SVG or PNG artifacts
```

Core initialization instructions fail closed when the only biological input is protein FASTA:
they do not call a core analysis tool. A user-selected annotator takes precedence; otherwise Codex
prefers `deepkoala-annotation`. If that Skill or `deepkoala-mcp` is unavailable, the task stops and
asks once for explicit permission to install or repair the complete suite. After the required
action succeeds, discovery and continuation occur in a new Codex task. If permission is declined,
the core remains stopped until a selected route supplies supported KO evidence.

The current product does not provide:

- annotator execution, model loading, KGML parsing, or image generation inside the core server;
- nucleotide gene prediction, translation, sequence alignment, or unrestricted genome-wide or
  cross-organism gene discovery;
- abundance analysis, enrichment statistics, differential abundance, confidence intervals, or
  replicate-aware statistics;
- metabolic modeling, flux inference, or phenotype prediction;
- arbitrary graph traversal, centrality, shortest-path, community, or causal-network analysis;
- remote HTTP transport, public hosting, user accounts, or multi-user result storage;
- a web UI, interactive HTML, browser automation, or active SVG content;
- upload, browser launch, execution, or result parsing for KEGG Mapper or KEGG Syntax;
- public plugin marketplace distribution or automatic update services;
- KEGG dataset mirroring or redistribution; or
- automatic installation of later DeepKOALA weights, multi-domain resources, or KOfam profiles.

Bounded organism-scoped gene-symbol lookup is candidate discovery and preserves every match; it is
not genome annotation or automatic identity selection. Local input preparation does not turn Core
into a KEGG Mapper or KEGG Syntax execution engine. Abundance-aware methods, statistical
enrichment, Streamable HTTP, non-KEGG backends, native Windows execution, and additional component
or Python-version support require separately assigned work.

## Process, package, and Skill architecture

The repository keeps three process boundaries:

| Process | Responsibility | Explicit exclusions |
| --- | --- | --- |
| `kegg-mcp` | Import KO evidence, perform bounded typed KEGG query and evidence routing, analyze, retain results, and prepare controlled local handoffs | Running an annotator, parsing KGML, rendering images, executing statistical enrichment or external KEGG web tools, or performing arbitrary graph analysis |
| `deepkoala-mcp` | Validate an allowed FASTA, run one controlled external DeepKOALA job, and deliver detailed annotation files | KEGG analysis, KO decision normalization, model updates, or multi-domain installation |
| `kegg-render-mcp` | Validate a version 4 handoff, retrieve allowed pathway assets, and render static artifacts | Annotation inference, KO normalization, MODULE recomputation, or pathway-coverage recomputation |

### Platform boundary

| Deployment | Supported components | Installation boundary |
| --- | --- | --- |
| Linux, CPython 3.11.x | Core, DeepKOALA, and Renderer | Independent distributions or the unified suite installer |
| Apple Silicon, macOS 14+, native CPython 3.11.x | Core, DeepKOALA, and Renderer | Independent distributions or the unified suite installer |
| Native Intel macOS | None | Unsupported; no release claim or CI gate |
| Windows host with WSL2 Linux | Linux component set | Install, store data, and execute inside the WSL Linux filesystem |
| Native Windows | None | Diagnostic routing only; server startup is rejected |

Core and Renderer preserve the same POSIX filesystem, no-follow path, atomic-publication, process,
and deployment-wide locking guarantees on Linux and native Apple Silicon macOS. No unsupported
platform fallback weakens those controls. DeepKOALA uses the Linux parent-death signal backend or
the Darwin parent-sentinel backend without weakening process-group cleanup. The unified installer
provisions all three components on Linux, including WSL2, and on native Apple Silicon macOS.

Platform capabilities were reviewed on 2026-08-01 against the Python 3.11
[`fcntl` documentation](https://docs.python.org/3.11/library/fcntl.html) and
[`os` platform-availability documentation](https://docs.python.org/3.11/library/os.html). Release
claims still require the exact-candidate evidence in the release-readiness checklist.

The distributions remain independently versioned, locked, installed, and reviewed. The renderer
declares a bounded compatible Core range for the typed pathway-asset interface. The Core package
does not depend on either companion, and no server starts another server.

The three repository Skills have one MCP dependency each:

| Skill | MCP dependency | Responsibility |
| --- | --- | --- |
| `deepkoala-annotation` | `deepkoala-mcp` | Protein FASTA annotation and stable detailed-CSV delivery |
| `kegg-ko-analysis` | `kegg-mcp` | Bounded KEGG query and evidence routing, existing-KO analysis, selected-reference export, and controlled input handoffs |
| `kegg-pathway-rendering` | `kegg-render-mcp` | Static rendering from an existing compatible handoff |

Skills contain instructions, not deterministic implementation code. They do not launch
subprocesses, manage weights, normalize records, call KEGG directly, parse KGML, calculate analysis,
construct SVG, manipulate pixels, or invent resource URIs. Stable versioned files in user-selected
allowed output directories are the durable, default stage handoff. The sole same-task exception is
an upstream Skill reading its own MCP's bounded controlled resource after a typed downstream
`file_path` allowed-root rejection, then passing the byte-identical content through the downstream
server's bounded inline input. Each Skill still calls exactly one MCP, and no job or resource
identifier crosses into Core. Process-scoped job and result identifiers are local optimizations,
not cross-process authorization or durable handoff tokens.

Domain, importer, KEGG client, analysis, reporting, service, and storage code remain independent of
MCP transport. The low-level KEGG client owns typed endpoint contracts, authorization, request
limits, rate limiting, caching, strict parsing, and retrieval provenance. Query services compose
only those typed primitives into bounded search, resolution, relation tracing, BRITE mapping,
annotation audit, selected-reference export, and external-input handoff results. MCP handlers
validate transport models and call public service functions; they do not duplicate client, query,
or domain logic.

### Document ownership

This architecture records only cross-component boundaries and invariants:

- [Import contracts](import-contracts.md), [KEGG client](kegg-client.md), and
  [MODULE](module-analysis.md) and [pathway/comparison](pathway-comparison-analysis.md) documents
  own their corresponding Core domain contracts.
- [Core MCP server](mcp-server.md) owns public tools, resources, transport schemas, and deployment
  environment; [Services, result storage, and reporting](services-results-reporting.md) owns
  transport-independent orchestration and storage internals.
- [Visualization architecture](visualization-architecture.md) owns renderer handoffs, asset
  boundaries, rendering semantics, and graphics security.
- The component READMEs own component installation, environment variables, tools, and lifecycle.
- [Installation](installation.md) owns suite operation, while
  [release readiness](release-readiness.md) alone owns the version matrix and release gates.

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

The client exposes bounded typed `info`, organism-pathway `list`, `find`, selected `get`, selected
`link`, and selected `conv` operations. It is not an arbitrary URL proxy. The only `list` form is
one canonical organism's pathway directory; whole-database listing is unavailable. `find` returns
database-validated candidates without assigning an identity; the Core resolver uses its internal
gene route only when an organism code is explicit. Whole-database conversion or relationship
expansion, arbitrary database pairs, and unrestricted `link/genes/<KO>` expansion are absent from
the public surface.

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

The default local test profile performs no live KEGG requests. The governed pull-request
compatibility campaign is defined only in [the live-test guide](../tests/live/README.md). It is an
access and compatibility check, not permission to redistribute responses.

## Bounded query and evidence routing

The Core query layer exposes seven focused capabilities:

- `get_kegg_entries` performs bounded typed retrieval and can deterministically project supported
  flat files into typed cards plus a current-scope, versioned comparison snapshot. Its references
  projection reports only PubMed identifiers explicitly present in KEGG flat-file `REFERENCE`
  fields and does not retrieve or summarize papers. Neither projection replaces the retained
  parsed source or becomes an LLM-generated summary.
- `search_kegg_entries` performs bounded keyword and approved chemical-candidate searches. Results
  preserve KEGG match text and never invent a relevance score or confirmed identity.
- `resolve_kegg_entities` resolves gene, organism, or chemical-substance identifiers through typed
  FIND, GET, CONV, and LINK steps. Organism-pathway LIST retrieval and broad-taxonomy candidate
  materialization remain explicit projections. It reports unmapped, one-to-one, one-to-many,
  many-to-one, and organism-mismatch outcomes and never silently chooses among ambiguous
  candidates.
- `trace_kegg_relations` follows an explicit allowlist of typed KEGG relationship directions for
  one or two hops under fixed seed, node, edge, row, response-byte, and provenance bounds.
  Gene expansion from a KO or pathway is dynamically restricted to one caller-supplied organism.
- `map_brite_hierarchy` maps supplied typed entities into source-backed BRITE paths, preserves
  multiple memberships when requested, reports unmatched entities, and uses descriptive
  unique-input counts without enrichment or abundance weighting.
- `audit_annotation_mapping` summarizes normalized evidence, duplicate and conflicting
  assignments, strict and lenient mapping yields across caller-selected fixed KEGG relationship
  classes, unmapped K numbers, retrieval provenance, and missing or mixed source metadata warnings.
  Its separable remote mapping phase may be omitted, skipped at a preflight request limit, or
  stopped cleanly at aggregate row/response limits without discarding the local evidence audit or
  calculating a partial-target yield.
- `compare_kegg_reference_snapshots` compares two current-scope, current-schema card snapshots for
  the same requested entries without a KEGG request. It reports structural field and membership
  differences with explicit parser, endpoint, cache, retrieval, and release context; it is not a
  historical KEGG mirror or a biological gain/loss analysis.

These functions are query and evidence-routing operations. A search candidate is not an identified
entity, a relationship edge is not a regulatory or causal mechanism, a BRITE count is not
enrichment, an organism pathway directory is not evidence of pathway presence or activity, and an
audit yield is not evidence of biological presence or absence. The Core does not add p-values,
significance tests, pathway activity, flux, centrality, shortest paths, communities, or other
arbitrary graph interpretation.

Query detail is retained through the same scope-isolated result store used by analysis services,
but it does not become a second annotation model. BRITE paths and mapping-audit artifacts are Core
query results; they are not passed into `deepkoala-mcp`, added to the renderer handoff, or
recomputed by `kegg-render-mcp`.

Bounded retrieval, query, card, audit, and reference-comparison direct results are fixed compact
projections with explicit counts and truncation flags under a shared 64 KiB serialized bound.
Complete search matches, cards, crosswalks, steps, graph data, BRITE paths, audit distributions,
relationship rows, snapshot differences, and provenance remain in scoped retained resources. The
LLM selects projections and interprets summaries; it does not perform server batching, merge
relationship shards, reconstruct discarded detail, or treat KEGG-returned text as instructions.

## Durable reference and external-input handoffs

`write_kegg_reference_bundle` turns one successful canonical entry snapshot from card or references
projection into a durable, bounded local bundle. The caller may select a subset of snapshot entries
and may attach one optional current-scope BRITE mapping. This is selected-reference preservation,
not cache export, a full KEGG mirror, or release history.

`prepare_kegg_handoff` accepts seven discriminated local formats. KEGG Mapper Reconstruct, Search,
Color, Join, and MWsearch targets and KEGG Syntax KO Composition or caller-supplied KO Sequence
targets validate caller input without KEGG retrieval, upload, browser launch, external execution,
or downstream-result parsing. Core never infers genomic order or coordinates.

Both tools require an explicit allowed-root output directory, publish files without replacement,
bound artifact and total bytes, and install the manifest last. The exact public file and validation
contracts are in [Core MCP server](mcp-server.md); serializer and publication internals are in
[Services, result storage, and reporting](services-results-reporting.md).

## MODULE, pathway, and comparison semantics

MODULE evaluation follows the supported KEGG logical syntax:

- top-level spaces connect required blocks with AND;
- plus signs express AND within a block;
- commas express alternatives with OR;
- a minus sign marks an optional component;
- parentheses preserve grouping; and
- M-number references are resolved with bounded depth and cycle detection.

The parser preserves source spans and unsupported content. It never drops an unknown token. A
required block whose truth cannot be established safely because of malformed, unresolved, cyclic,
unsupported, or limit-exceeding content is not evaluable and retains a reason. The aggregate is
`partially_evaluable` when another required block remains evaluable and `not_evaluable` when none
can be evaluated safely.

Exact MODULE completion is a Boolean evaluation of the full supported definition. Project block
coverage is the ratio of completed required top-level blocks to all required top-level blocks and
is exposed only when every required block is evaluable. It is not an official KEGG completeness
percentage. Optional terms do not increase the denominator. Minimal missing alternatives are
bounded and do not imply that adding a gene will activate a process.

Pathway coverage is descriptive unique-KO overlap. Each request and result records the canonical
pathway identifier, reference namespace, numerator, denominator, retrieval time, cache state, and
release information when available. KO-only input uses the canonical KO reference view and cannot
support an organism-specific pathway claim.

`PathwaySpec` validates the namespace, canonicalizes an omitted `map` view to `ko`, and
de-duplicates paired views by pathway number. Global and overview references require explicit core
analysis opt-in and a warning. A canonical KO target with an evaluated denominator and complete
detected evidence is renderable through the version 4 handoff; `map` and organism references remain
summary-only renderer targets.

Pathway output does not contain `pathway_present`. Coverage must not be described as pathway
presence, completeness, expression, activity, flux, phenotype, or experimental validation.

When the high-level service receives no explicit MODULE or pathway target, it independently selects
up to five MODULEs and five canonical KO reference pathways. Ranking uses the number of unique K
numbers selected by the requested evidence mode and the canonical target identifier as the stable
tie-breaker. Automatic pathway selection directly excludes the current KEGG Global, Overview, and
higher-level Overview KO map identifiers before Top-N truncation and fills the selection from the
next ranked regular references. The fixed identifier set was checked against the official KEGG
PATHWAY identifier classes and map list on 2026-07-22. Ranking selects definitions to analyze; it is
neither MODULE completion nor enrichment. Duplicate records and duplicate KEGG relationships never
inflate the overlap.

KO-set comparison is deterministic set comparison. It preserves labels, analysis units, policies,
and compatible KEGG provenance. It reports shared and set-specific evidence and differences in
MODULE or pathway outcomes without p-values, fold changes, enrichment, differential abundance, or
biological gain/loss claims.

## Core service and MCP surface

The core MCP server uses stdio transport. Protocol messages are the only stdout content; logs and
diagnostics use stderr or a configured file. All tool inputs and outputs use explicit schemas,
schema-conforming `structuredContent`, bounded text summaries, and accurate MCP annotations.

`analyze_ko_annotations` is the common one-call workflow. It imports evidence once, performs
bounded optional Top-N selection, loads only required references, evaluates requested targets,
retains complete bounded artifacts, and optionally writes a durable output bundle. Narrower tools
reuse the same service and domain functions.

The bounded retrieval/card, search, resolver, relation-trace, BRITE, mapping-audit, local
snapshot-comparison, selected-reference export, and input-handoff tools are separate
transport-independent service workflows. They reuse the same typed client, access policy,
retrieval provenance, result storage, and safe publication primitives without being folded into
`analyze_ko_annotations`.

Ordinary tool inputs expose only analysis choices. Eligibility, endpoints, cache policy, allowed
roots, storage, rate limits, and hard service limits remain deployment configuration. Connectivity
probing is explicit and open-world. Status is side-effect-free, redacted, and does not imply that
the network has been tested.

Large results use bounded direct projections, scoped same-process retrieval, and durable versioned
output files. The [Core MCP server](mcp-server.md) owns the exact tools, resources, response
schemas, URI behavior, and public retention contract. The
[services and storage contract](services-results-reporting.md) owns orchestration, serialization,
bundle transactions, and SQLite internals.

Errors use stable machine-readable codes, a bounded safe message, recoverability, a suggested
action, and redacted details. Inputs, fields, identifiers, target counts, decompressed bytes,
outputs, and retained artifacts are bounded by schema or structure. DeepKOALA FASTA intake is the
one aggregate-byte exception: it is streamed without a total file-size cap while sequence count,
per-sequence length, header length, path, and replacement-race bounds remain enforced. Filesystem
access requires direct absolute paths below configured roots and rejects traversal, unsafe ancestry,
replacement races, and symlink escape. No component uses `shell=True`.

## DeepKOALA companion contract

DeepKOALA is an external, independently versioned annotation tool. The core server imports its
previously generated detailed table and never loads its models or runs its CLI. Only
`deepkoala-mcp` may launch the configured external checkout.

The annotator neither performs Core entity search, identifier resolution, relation tracing, BRITE
classification, or annotation mapping audit nor consumes their retained artifacts. Its stable
detailed CSV remains immutable source evidence at the Core importer boundary.

The companion owns allowed-root FASTA validation, one deployment-wide runner lease, fixed direct
subprocess arguments, explicit CPU/CUDA/MPS policy, verification of the configured checkout's CLI
and device-resolver contract plus its target interpreter platform, bounded polling and cleanup, and
stable `deepkoala_annotations.csv` and `deepkoala_run_report.md` delivery. Its output preserves
detailed source evidence and resolved model provenance; it never normalizes K numbers.

Multi-domain capability is deployment opt-in and requires separately provided local resources.
Requests remain single-domain unless the user explicitly selects a ready capability. The
[DeepKOALA companion README](../companions/deepkoala-mcp/README.md) owns installation,
configuration, tool, lifecycle, and detailed handoff behavior.

## Renderer contract

The renderer consumes the Core's immutable `render_input.json` schema version 4 and
`AnalysisExecutionProvenance` version 3. It never normalizes annotations, chooses a second KO
policy, or recomputes MODULE completion, block coverage, pathway denominators, or coverage ratios.
It produces bounded static regular-pathway box overlays, explicitly opted-in canonical KO
global/overview line overlays, and project-owned MODULE logic diagrams. Broad maps remain excluded
from automatic Top-N selection and carry conservative descriptive warnings when explicitly used.

BRITE hierarchy results and annotation mapping audits are not renderer inputs. The renderer does
not query or visualize those artifacts, traverse their relationships, or recompute their counts.

The [visualization architecture](visualization-architecture.md) owns the handoff, typed pathway
asset boundary, rendering semantics, graphics security, and data-rights rules. The
[Renderer README](../companions/kegg-render-mcp/README.md) owns component installation,
configuration, public tools, resource lifecycle, and output behavior.

## Unified Codex installation contract

`scripts/install-suite.py` is the supported complete-suite Codex installation path on Linux and
native Apple Silicon macOS. It creates three independent locked runtimes and one generated local
plugin containing the canonical Skills and absolute MCP launch commands. Publication is
transactional, private deployment data stays outside the plugin, and the default dependency path
is offline. It pins and verifies the reviewed DeepKOALA revision. Native Windows is rejected,
Windows hosts use WSL2 Linux, and native Intel macOS is unsupported.

The [installation guide](installation.md) owns operator configuration and lifecycle. The
[release-readiness checklist](release-readiness.md) owns exact-candidate installation, discovery,
archive, rights, and publication evidence.

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

## Release contract ownership

Architecture changes must preserve the boundaries and invariants in this document, but this
document does not duplicate a release status or gate list. The
[release-readiness checklist](release-readiness.md) is the sole operational publication gate, and
the [Codex Skill evaluation](skill-evaluation.md) supplies its mandatory manual route matrix.

## Primary sources and review dates

External facts are time-sensitive. Recheck a source when a change affects a parser, schema, fixture,
access rule, or acceptance test, and record the new retrieval date in the same tracked change.

KEGG service, FIND, relationship, data-format, MODULE, pathway, and rights sources were reviewed on
2026-07-30, with selected flat-file references and KEGG Mapper/Syntax input formats reviewed again
on 2026-07-31:

- [KEGG REST overview](https://www.kegg.jp/kegg/rest/),
  [API manual](https://www.kegg.jp/kegg/rest/keggapi.html), and
  [legal notice](https://www.kegg.jp/kegg/legal.html)
- [KEGG MODULE database](https://www.kegg.jp/kegg/module.html) and
  [MODULE entry help](https://www.kegg.jp/kegg/document/help_bget_module.html)
- [KEGG PATHWAY database](https://www.kegg.jp/kegg/pathway.html),
  [database entry format](https://www.kegg.jp/kegg/docs/dbentry.html), and
  [Pathway Map Viewer help](https://www.kegg.jp/kegg/document/help_pathway.html)
- [KEGG Mapper](https://www.kegg.jp/kegg/mapper/) and
  [KEGG Syntax KO sequence](https://www.kegg.jp/kegg/syntax/synteny.html)

DeepKOALA behavior and detailed-output fields were reviewed again on 2026-08-01. The explicit
CPU/CUDA/MPS device choices, Apple Silicon instructions, optional multi-domain CLI, HMMER invocation
boundary, and short-sequence output were reviewed against official commit
`bebbe0c43f50a26488f7092f6b355aae870a4ed9`:

- [DeepKOALA GenomeNet page](https://www.genome.jp/tools/deepkoala/) and
  [official repository](https://github.com/zhaoxi120/deepkoala)
- [official CLI](https://github.com/zhaoxi120/deepkoala/blob/bebbe0c43f50a26488f7092f6b355aae870a4ed9/deepkoala/cli.py) and
  [device resolver](https://github.com/zhaoxi120/deepkoala/blob/bebbe0c43f50a26488f7092f6b355aae870a4ed9/deepkoala/utils.py) and
  [multi-domain implementation](https://github.com/zhaoxi120/deepkoala/blob/bebbe0c43f50a26488f7092f6b355aae870a4ed9/deepkoala/infer_multi.py)

The official `frag` versus `full` usage descriptions were reviewed again on 2026-07-22 against the
[official repository README](https://github.com/zhaoxi120/deepkoala/blob/bebbe0c43f50a26488f7092f6b355aae870a4ed9/README.md).

MCP tool and resource contracts were reviewed on 2026-07-16 against the 2025-06-18 specification:

- [MCP tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools) and
  [resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)

Codex Skill, repository-guidance, and generated-plugin behavior was reviewed again on 2026-07-23:

- [OpenAI Codex Skills](https://learn.chatgpt.com/docs/build-skills),
  [`AGENTS.md`](https://learn.chatgpt.com/docs/agent-configuration/agents-md), and
  [plugin documentation](https://learn.chatgpt.com/docs/build-plugins)

The implementation-facing detailed contracts remain in `import-contracts.md`, `kegg-client.md`,
`module-analysis.md`, `pathway-comparison-analysis.md`, `services-results-reporting.md`,
`mcp-server.md`, and `visualization-architecture.md`. Public interfaces and tests govern exact
runtime behavior when a summary here omits a lower-level detail.
