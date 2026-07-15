# KEGG MCP: Reviewed Development Plan

Status: approved as a development baseline after the corrections recorded below.
Implementation status: Milestones 0 through 8 and the first supported release are implemented and
verified with offline tests. Version 0.1.0 supports Python 3.11.x only.
Last reviewed: 2026-07-15.

## Table of contents

1. [Executive decision](#1-executive-decision)
2. [Review findings and required corrections](#2-review-findings-and-required-corrections)
3. [Product definition](#3-product-definition)
4. [User experience](#4-user-experience)
5. [Scope](#5-scope)
6. [Repository and architecture](#6-repository-and-architecture)
7. [Canonical data contracts](#7-canonical-data-contracts)
8. [KEGG access layer](#8-kegg-access-layer)
9. [Module evaluation](#9-module-evaluation)
10. [Pathway coverage](#10-pathway-coverage)
11. [KO-set comparison](#11-ko-set-comparison)
12. [MCP surface](#12-mcp-surface)
13. [Codex Skill](#13-codex-skill)
14. [DeepKOALA guidance](#14-deepkoala-guidance)
15. [Reporting and provenance](#15-reporting-and-provenance)
16. [Errors, security, and privacy](#16-errors-security-and-privacy)
17. [Testing strategy](#17-testing-strategy)
18. [Milestones](#18-milestones)
19. [Initial issue backlog](#19-initial-issue-backlog)
20. [Release gates](#20-release-gates)
21. [Resolved decisions](#21-resolved-decisions)
22. [Primary references](#22-primary-references)

## 1. Executive decision

The original proposal has a sound product boundary and a sensible local-first architecture. It should proceed as one repository containing a reusable Python core, a stdio MCP server, a repository-scoped Codex Skill, tests, and user documentation.

It was not ready to serve as an implementation specification without revision. The largest gaps were not cosmetic: they affected KEGG usage rights, rate limiting, module semantics, pathway denominators, multiple KO assignments per sequence, DeepKOALA decision mapping, large MCP results, and duplication between the Skill and the core library.

This document resolves those gaps and is the implementation baseline. It intentionally creates no source-code scaffold. Executable directories should be created only when their first feature is implemented and tested.

## 2. Review findings and required corrections

| Area | Original risk | Required correction |
| --- | --- | --- |
| KEGG access | Caching and concurrency were described without making KEGG usage restrictions enforceable. | Treat academic-use eligibility, licensed endpoints, a process-wide maximum of three requests per second, and local-only cache storage as release gates. |
| KEGG batching | Generic batch retrieval did not state endpoint limits. | Split `get` operations into batches of at most ten entries and apply endpoint-specific limits elsewhere. |
| Annotation model | One record implied one KO per sequence and omitted sample, rank, domain, and score semantics. | Allow multiple records per sequence and preserve sample ID, rank, domain coordinates, raw decision, score type, threshold rule, and source/model versions. |
| Decision normalization | `accepted`, `uncertain`, and `rejected` appeared universal. | Preserve the source decision and apply a named versioned normalization policy. Never invent `uncertain` merely because a score is below a threshold. |
| DeepKOALA | The plan assumed detailed fields without requiring the detailed output mode. | Recommend `--detail`; map `annotate='*'` or `probability >= threshold` to accepted; retain below-threshold predictions as source-rejected unless a separate documented policy says otherwise. |
| Multi-domain proteins | A sequence-level set would collapse multiple domain KOs. | Support multiple KO records and optional domain coordinates for one sequence. |
| Analysis unit | A KO set could represent a genome, MAG, pangenome, or mixed community without distinction. | Require an `analysis_unit` and interpret pooled metagenomic results as community encoded potential. |
| Module semantics | A percentage was proposed without a canonical calculation rule. | Separate exact Boolean completion from a project-defined top-level block-coverage metric and report the calculation method. |
| Module parser | The example AST did not fully specify operator precedence, references, or unsupported tokens. | Implement the documented KEGG logical operators, preserve parentheses, resolve M-number references safely, and return `not_evaluable` instead of silently dropping tokens. |
| Pathway coverage | The denominator and pathway namespace were ambiguous. | State whether the reference is `map`, `ko`, or organism-specific and define the denominator as unique linked KOs retrieved at a recorded time. |
| Pathway interpretation | Fixed evidence labels could appear biologically authoritative. | Keep coverage descriptive. If bands are later added, make them optional, named, versioned presentation policies. |
| Gene mapping | Mapping a KO to all KEGG genes can be extremely large and organism-dependent. | Exclude unrestricted KO-to-all-genes expansion from the MVP. Require an organism and explicit opt-in in a later milestone. |
| Skill design | A normalization script inside the Skill would duplicate core logic. | Make the initial Skill instruction-only. It selects workflows and calls MCP tools; deterministic normalization stays in the core package. |
| MCP output | Full annotation and missing-KO lists could exceed client context limits. | Return a bounded summary and preview, plus a scoped `result_id`, resource link, or paginated continuation. |
| MCP schemas | Tool names and examples were given without complete protocol contracts. | Define input/output JSON Schemas, structured content, tool annotations, execution errors, pagination, and result retention before implementation. |
| Repository files | The planned tree implied generated files and empty code before implementation. | Keep tracked and published documentation in English. Local Simplified Chinese references use the ignored `*.zh-CN.md` convention and remain untracked and non-normative. Add each source directory only with its first tested artifact. |

## 3. Product definition

### 3.1 One-sentence description

Convert KO annotation evidence into traceable KEGG mappings, exact module evaluations, descriptive pathway coverage, and cautious functional reports while guiding users who still need to obtain KO assignments from protein sequences.

### 3.2 Product boundary

The core `kegg-mcp` server answers:

> What do these supplied KO annotations map to in KEGG, which KEGG module requirements do they satisfy, and what limited functional statements are supported?

The Codex Skill answers:

> What data does the user currently have, which workflow should be used, which configured MCP service or external annotation step is needed, and how should the structured result be explained?

An external annotation program such as DeepKOALA answers:

> Which KO assignments does this protein sequence support under that program's model and decision policy?

The optional `deepkoala-mcp` companion now implements bounded local job execution around that
external program as a separately installed service. It has not received independent release
sign-off and is not part of the signed core v0.1.0 release. The Skill may orchestrate the
companion, and the core server may import its result, but neither the Skill nor the core server
owns DeepKOALA inference. These responsibilities must remain separate.

### 3.3 Product principles

- Local-first: stdio transport, user-local inputs, user-local cache, no public service.
- Evidence-preserving: never replace raw source evidence with a normalized label.
- Source-agnostic: support plain KO lists and generic tables, not only DeepKOALA.
- Simple by default: provide one high-level analysis tool and expose primitives for advanced use.
- Biologically conservative: distinguish database relationships, annotation evidence, coverage, completion, activity, and phenotype.
- Reproducible: record the input digest, policy versions, KEGG retrieval time, parser version, and all analysis parameters.
- Bounded: cap inputs and outputs and provide explicit continuation for large results.

## 4. User experience

### 4.1 Primary workflows

#### Workflow A: plain KO list

1. Accept newline-delimited or table-based K numbers.
2. Validate, normalize prefixes, preserve order, and report invalid and duplicate values.
3. Treat user-provided K numbers as annotations supplied for analysis, not experimental validation.
4. Run the requested module and/or pathway analysis.
5. Return a concise result with provenance and interpretation limits.

No sequence-annotation guidance should be shown unless requested.

#### Workflow B: annotation table

1. Detect a known importer only when its signature is unambiguous.
2. Otherwise preview detected columns and require explicit column mapping for ambiguous fields.
3. Preserve raw rows and apply a named decision policy.
4. Report accepted, uncertain, source-rejected, unclassified, invalid, and duplicate counts.
5. Run strict analysis; run lenient analysis only when policy-defined uncertain records exist and the user requests or accepts it.

#### Workflow C: protein FASTA only

1. Confirm that the input is protein FASTA rather than nucleotide FASTA.
2. Determine whether sequences are expected to be complete, fragmented, or potentially multi-domain.
3. Explain annotation-tool options and trade-offs.
4. If the optional separately installed companion MCP is discovered, explicitly configured, and
   chosen, show its execution notice and submit the bounded job to that service. Otherwise provide
   an external local command.
5. Retain detailed machine-readable output, the command, tool/model version, effective device,
   weight source, and execution date when available.
6. Continue with Workflow B through the existing importer when the result becomes available.

The core `kegg-mcp` server must not execute the external annotator. The Skill must not implement
or launch inference itself. Only the separately installed companion MCP runner may own execution.

#### Workflow D: compare KO sets

1. Normalize all inputs using the same policy.
2. Require compatible analysis parameters and KEGG provenance, or recompute them together.
3. Report shared and set-specific KOs plus differences in module completion and pathway coverage.
4. State that the comparison is descriptive and sensitive to sequencing, assembly, gene calling, and annotation completeness.

### 4.2 Usability requirements

- The common KO-to-report workflow should require one MCP call after the user supplies data and analysis targets.
- Every response should lead with a human-readable summary and provide a machine-readable structured result.
- Unknown columns, identifiers, module tokens, or pathway namespaces must produce a repairable message rather than a silent guess.
- Large missing-KO lists and full normalized tables must be retrieved separately rather than placed in the default model context.
- Defaults must be safe: strict evidence, reference KO pathways, no gene expansion, no live refresh unless needed, and no file writes unless requested.

## 5. Scope

### 5.1 MVP capabilities

- Plain KO-list import.
- Generic CSV and TSV annotation import with explicit column mapping.
- A DeepKOALA detailed-output importer.
- KO identifier validation and normalization.
- KEGG `info`, selected `get`, selected `link`, and selected `conv` operations behind typed services.
- KO mapping to reference pathways, modules, reactions, EC numbers, and selected BRITE relationships.
- KEGG module tokenization, parsing, exact completion, and block coverage.
- Descriptive pathway KO coverage with an explicit reference namespace.
- Deterministic comparison of two or more KO sets.
- JSON-compatible structured results and Markdown summaries.
- stdio MCP transport.
- One repository-scoped Codex Skill with workflow and interpretation references.
- Local cache and offline reuse of previously retrieved entries.

### 5.2 Explicitly outside the MVP

- Running DeepKOALA, KofamScan, HMMER, BLAST, or any other annotator.
- Managing annotation-model weights or KOfam profiles.
- Nucleotide gene prediction or translation.
- Enrichment tests, differential abundance, confidence intervals, or replicate-aware statistics.
- Abundance-weighted pathway analysis.
- Unrestricted KO-to-all-genes expansion.
- Automated pathway images, KGML visualization, flux inference, or metabolic modeling.
- Remote HTTP transport, web UI, user accounts, or multi-user result storage.
- Public hosting or annotation-as-a-service.
- KEGG dataset mirroring or redistribution.

### 5.3 Later candidates

Candidates require separate design decisions and must not leak into MVP interfaces:

- organism-scoped gene mapping;
- abundance-aware summaries;
- enrichment with an explicit background universe;
- KGML-based topology summaries;
- plugin packaging for distribution;
- independent release review for the implemented optional DeepKOALA companion MCP server;
- Streamable HTTP transport with authentication and access control; and
- non-KEGG pathway backends.

## 6. Repository and architecture

### 6.1 Single-repository decision

Keep the MCP server, reusable core, repository-scoped Skill, documentation, and tests in one repository until their release cycles or licenses diverge. This keeps the workflow, tool schemas, and interpretation policy aligned.

The implemented DeepKOALA companion is maintained in this repository under an explicit maintainer
request with a separate install, entry point, environment, process, and release boundary. It must
not add DeepKOALA or accelerator dependencies to the core `kegg-mcp` distribution. Split it into
another repository if dependency, license, security, or release-cycle divergence makes that
boundary unclear.

Split only when one of these becomes true:

- the Skill supports several independent servers;
- the core server has a separate maintainer or release cycle;
- distribution requires a standalone plugin;
- different licenses are required; or
- the Skill and server are independently versioned products.

### 6.2 Target layout

```text
kegg-mcp/
├── README.md
├── AGENTS.md
├── LICENSE
├── CONTRIBUTING.md
├── SECURITY.md
├── pyproject.toml
├── uv.lock
├── src/
│   └── kegg_mcp/
│       ├── domain/
│       ├── importers/
│       ├── kegg/
│       ├── analysis/
│       ├── reporting/
│       ├── services/
│       └── mcp/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── fixtures/
├── examples/
├── docs/
└── .agents/
    └── skills/
        └── kegg-mcp/
            ├── SKILL.md
            ├── agents/
            │   └── openai.yaml
            └── references/
                ├── workflow-selection.md
                ├── annotation-tools.md
                ├── deepkoala.md
                ├── confidence-policy.md
                ├── module-interpretation.md
                └── reporting-policy.md
```

Do not commit empty directories. `uv.lock` must be generated from a real `pyproject.toml`, not written as a placeholder. Choose the code license before adding `LICENSE`.

### 6.3 Layer responsibilities

```text
Importers -> immutable source evidence + normalized records
KEGG client -> licensed/rate-limited retrieval + parsing + cache
Domain -> identifiers, schemas, policies, provenance
Analysis -> module, pathway, and set-comparison algorithms
Services -> use-case orchestration and result persistence
Reporting -> bounded JSON-compatible and Markdown views
MCP -> schemas, transport, resources, and protocol errors
Skill -> workflow selection and user-facing interpretation
```

The domain and analysis layers must not import MCP packages. The Skill must not contain a second normalization implementation.

## 7. Canonical data contracts

The names below are conceptual. Final Pydantic models and JSON Schemas are approved in Milestone 1.

### 7.1 Source provenance

```text
SourceProvenance
  source_name              deepkoala | kofamscan | blastkoala | manual | unknown | ...
  source_version           software version, when available
  model_name               full | frag | model-specific value | null
  model_version            model/database date or version, when available
  annotation_date          ISO 8601 timestamp or null
  input_uri                sanitized logical source, not an unrestricted local path
  input_sha256             digest of the imported bytes when available
  importer_name
  importer_version
```

### 7.2 Annotation record

```text
AnnotationRecord
  record_id                stable within the imported dataset
  sample_id                required; default "sample-1" only for a single unnamed input
  sequence_id              nullable only for a plain KO list
  ko_id                    normalized K number or null
  raw_ko                   original value
  raw_decision             source-provided decision or marker
  normalized_status        accepted | uncertain | rejected | unclassified | invalid
  status_reason            machine-readable reason
  decision_policy          name and version
  score                    numeric or null
  score_type               probability | bitscore | e_value | source_specific | null
  threshold                numeric or null
  threshold_rule           gte | lte | source_specific | null
  rank                     positive integer or null
  domain_start             one-based inclusive coordinate or null
  domain_end               one-based inclusive coordinate or null
  evidence                 additional source fields
  source                   SourceProvenance
```

Rules:

- Multiple records for the same `sequence_id` are valid.
- Raw values are immutable after import.
- `score` values from different `score_type` or source policies are not directly comparable.
- Invalid identifiers remain in the import report but do not enter analysis.
- Duplicate handling is deterministic and reported; conflicting duplicates are never silently collapsed.
- Plain KO-list values may be normalized as accepted for analysis under a policy named `user_supplied_ko_v1`; this describes input handling, not biological validation.

### 7.3 Dataset context

```text
AnnotationDataset
  dataset_id
  records
  sources                  retained even when no records are emitted
  analysis_unit            isolate_genome | isolate_proteome | MAG | pangenome |
                           metagenomic_community | mixed | unknown
  taxon_id                 optional
  kegg_organism_code       optional
  metadata
  import_report
```

The analysis unit is required before interpreting module or pathway results. A pooled community result must not be phrased as a complete pathway in one organism.

### 7.4 Derived KO evidence view

```text
KOEvidenceView
  accepted_kos
  uncertain_kos
  rejected_kos
  records_by_ko
  records_by_sequence
  status_counts
  policy
```

Strict mode uses `accepted_kos`. Lenient mode uses `accepted_kos union uncertain_kos`. Rejected and unclassified predictions never enter lenient mode by default.

### 7.5 Analysis provenance

```text
AnalysisProvenance
  analysis_id
  server_version
  algorithm_versions
  parameters
  evidence_mode
  input_dataset_ids
  input_digests
  kegg_endpoint_class       public_academic | licensed | offline_cache
  kegg_retrieved_at
  kegg_release_by_database  value or unknown
  cache_state
  warnings
```

## 8. KEGG access layer

### 8.1 Usage and license gate

The public `rest.kegg.jp` service is available only for academic use by academic users. Non-academic users must configure a licensed service or must use only data they are authorized to use. The software must show this constraint during setup and in the user documentation.

Before enabling live access, configuration must record one of:

- `public_academic`: the operator confirms eligible academic use;
- `licensed`: the operator supplies an authorized endpoint/configuration; or
- `offline_cache`: live access disabled.

This is not legal advice and the project must not claim to validate an organization's license.

### 8.2 Client requirements

- HTTPS endpoint configuration.
- Process-wide rate limiter with a safe default below the documented maximum of three requests per second and no burst above that maximum.
- Timeout, bounded retries, exponential backoff, and jitter for transient failures.
- No retry for deterministic 400 or 404 responses.
- Typed endpoint allowlist; do not expose an arbitrary URL fetcher.
- At most ten entries per KEGG `get` request.
- Bounded identifier count for `link`, `conv`, and service-level operations.
- Stable User-Agent containing project version and a documentation URL, not personal information.
- Structured parsing of tab-delimited, flat-file, and `info` responses.
- Offline mode that never attempts a network connection.
- Response-size limits and checksums.

### 8.3 Supported MVP operations

| KEGG operation | MVP use |
| --- | --- |
| `info` | Retrieve release/statistics metadata when available. |
| `get` | Retrieve selected KO, module, pathway, reaction, enzyme, compound, and BRITE entries under explicit size limits. |
| `link` | Resolve approved KO-to-pathway/module/reaction/EC/BRITE relationships and pathway-to-KO denominators. |
| `conv` | Convert supported external gene identifiers only in an explicitly scoped lookup; broad gene discovery remains out of MVP. |
| `list` | Use only where required for bounded metadata, never to mirror a database. |
| `find` | Optional for exact user-driven search; do not use names to guess a KO assignment. |

Unrestricted `link/genes/<KO>` expansion is out of MVP because it can return a very large cross-organism result.

### 8.4 Cache contract

Default location:

```text
${XDG_CACHE_HOME:-~/.cache}/kegg-mcp/kegg.sqlite3
```

Minimum cache metadata:

```text
operation
normalized_request_key
endpoint_class
response_body
response_sha256
retrieved_at
expires_at
parser_version
database_release_or_unknown
http_metadata_allowlist
```

Requirements:

- Cache content is user-local and ignored by Git.
- Reports may record a checksum and retrieval metadata but must not embed large raw KEGG payloads.
- Offline mode may use expired data only when explicitly allowed and must mark it stale.
- `refresh=true` bypasses a fresh cache entry but still obeys rate limits.
- Cache corruption must not be reported as a biological absence.
- Status and cache-info output shows only a redacted logical cache location. The MVP does not
  create or open SQLite solely to collect status statistics: `entry_count`,
  `stored_payload_bytes`, and `newest_entry_age_seconds` are `null`, with
  `inspection_status=not_probed`. This keeps status reads side-effect-free and avoids sensitive
  path disclosure; explicit cache inspection can be designed separately if later required.

## 9. Module evaluation

### 9.1 Terms

- `exact completion`: Boolean evaluation of the KEGG module logical definition against a KO set.
- `block coverage`: a project-defined descriptive ratio of completed required top-level blocks to all evaluable required top-level blocks.
- `not_evaluable`: the definition cannot be safely evaluated because of unsupported, malformed, unresolved, cyclic, or unavailable content.

Do not call block coverage the official KEGG completeness percentage.

### 9.2 Grammar requirements

The parser must support and test:

- top-level spaces as required-block connections (AND);
- plus signs as AND for complexes or combinations;
- commas as alternatives (OR);
- minus signs as optional components;
- parentheses and nesting;
- K-number leaves;
- M-number references where present;
- whitespace and line wrapping from flat files; and
- explicit unsupported-token nodes.

Operator precedence and associativity must be documented in the parser contract. The tokenizer must retain source spans so errors can point to the original definition.

### 9.3 Evaluation rules

1. Parse the full definition before evaluation.
2. Split required top-level blocks according to KEGG syntax while preserving nested expressions.
3. Evaluate K-number leaves against the evidence-mode KO set.
4. Evaluate AND, OR, and optional nodes deterministically.
5. Resolve M-number references with cycle detection, a depth limit, and provenance for every retrieved definition.
6. Exclude optional components from the required-block denominator but report whether they are present.
7. Return exact completion only when all required expressions are evaluable.
8. Return block coverage as `completed_required_blocks / evaluable_required_blocks`, plus numerator and denominator.
9. If any required block is not evaluable, mark the overall result `not_evaluable` or `partially_evaluable`; do not silently reduce the denominator without a warning.
10. Produce a bounded list of minimal missing alternatives. Cap combinatorial enumeration and state when results are truncated.

### 9.4 Output contract

```text
module_id
module_name
definition_sha256
evidence_mode
evaluation_status          complete | incomplete | partially_evaluable | not_evaluable
is_complete                boolean or null
block_coverage             number or null
completed_required_blocks
evaluable_required_blocks
required_block_count
present_blocks_preview
missing_blocks_preview
optional_components
uncertain_support
unresolved_references
calculation_method         named and versioned
warnings
provenance
```

Strict and lenient results are separate objects. A change from strict incomplete to lenient complete must identify the uncertain records responsible.

## 10. Pathway coverage

### 10.1 Reference namespaces

Every request and result must identify one of:

- `ko`: KO-level reference pathway, such as `ko00010`;
- `map`: reference pathway map, such as `map00010`; or
- `organism`: organism-specific pathway, requiring a KEGG organism code and compatible gene context.

KO-only inputs should default to the KO-level/reference view. They must not be presented as organism-specific evidence.

### 10.2 Denominator

For KO coverage, the denominator is the set of unique K numbers linked to the selected pathway namespace at the recorded KEGG retrieval time. The result must record:

- namespace and pathway identifier;
- unique denominator count;
- retrieval time and release information when available;
- whether the denominator came from live access or cache; and
- exclusions or unsupported entries.

Global and overview maps are allowed only with an explicit request and a warning that a single percentage over a large heterogeneous map is usually not biologically useful.

### 10.3 Output and interpretation

```text
pathway_id
pathway_name
reference_namespace
detected_unique_ko_count
reference_unique_ko_count
coverage_ratio
detected_kos_preview
missing_kos_preview
strict_or_lenient
result_resource
warnings
provenance
```

The default output must not include `pathway_present`. The standard report must state that KO coverage does not establish pathway completeness, expression, activity, flux, or phenotype.

Labels such as `limited_evidence` or `broad_coverage` may be added only as an optional presentation policy with published thresholds, a policy version, and no biological-status wording.

## 11. KO-set comparison

MVP comparison is deterministic set comparison, not statistical inference.

Required outputs:

- shared accepted KOs;
- set-specific accepted KOs;
- shared and set-specific uncertain KOs when requested;
- strict and lenient module-result differences;
- pathway-coverage differences using the same denominator;
- incompatible-provenance warnings; and
- bounded previews plus a resource for full lists.

Rules:

- Recompute comparisons under one analysis run when KEGG provenance or algorithm versions differ.
- Do not call a KO biologically sample-specific when it may only be absent because of incomplete sequencing, assembly, gene calling, or annotation.
- Do not compute p-values, fold changes, or enrichment without a separate abundance/replicate design.
- Preserve sample labels and input order.

## 12. MCP surface

### 12.1 Transport

Use stdio for the MVP. Never write logs, progress bars, warnings, or tracebacks to stdout. Use stderr or a configured log file.

### 12.2 Tool design principles

- Use explicit input and output JSON Schemas.
- Return `structuredContent` conforming to `outputSchema` and a concise text representation for compatibility where required.
- Add accurate annotations such as `readOnlyHint`, `idempotentHint`, and `openWorldHint`.
- Treat live KEGG calls as open-world even when the analytical calculation is read-only.
- Bound record counts, identifier counts, target counts, and response previews.
- Return tool execution failures in a form the model can inspect and repair, following the MCP specification and SDK conventions.
- Use a result store for large normalized data and analysis artifacts.

### 12.3 Proposed MVP tools

#### `analyze_ko_annotations`

Primary one-call workflow.

Inputs:

- inline KO text or annotation records;
- source format or explicit column mapping;
- dataset context;
- requested modules and/or pathways;
- evidence modes;
- cache policy; and
- output preview limits.

Output:

- import summary;
- analysis summary;
- warnings;
- bounded previews;
- provenance;
- `result_id`; and
- resource links for full structured results.

This tool orchestrates existing service functions; it must not contain separate analysis logic.

#### `normalize_ko_annotations`

Normalize a plain KO list or annotation table and return counts, warnings, a preview, and a `dataset_id`. Ambiguous column detection is a recoverable error requiring explicit mapping.

#### `get_kegg_entries`

Retrieve a bounded set of supported KEGG entries. Validate database/identifier compatibility, batch `get` requests at ten entries, and never behave as an arbitrary URL proxy.

#### `map_ko_ids`

Map K numbers to approved targets: pathway, module, reaction, EC, and selected BRITE relationships. Gene expansion is rejected in MVP with an actionable explanation.

#### `analyze_modules`

Evaluate selected modules or a bounded discovery set using a stored dataset or inline K numbers. Return exact completion and block coverage separately.

#### `analyze_pathways`

Compute KO coverage for selected pathways under an explicit namespace and denominator.

#### `compare_ko_sets`

Compare stored or inline datasets using compatible policy and KEGG provenance.

#### `get_server_status`

Return server version, transport, supported formats, tool capabilities, offline/live state, redacted cache status, KEGG eligibility configuration, and connectivity. Do not reveal secrets or full local paths.

### 12.4 Resources and result lifecycle

Fixed resources:

```text
ko-analysis://status
ko-analysis://cache/info
```

Resource templates:

```text
ko-analysis://results/{result_id}
ko-analysis://results/{result_id}/{section}
kegg-cache://entries/{database}/{identifier}
```

Requirements:

- Declare parameterized URIs as MCP resource templates, not as static resources.
- Validate every URI parameter and reject traversal, encoded separators, and unknown result IDs.
- Scope results to a local server instance and, where the SDK supports it, the originating client/session.
- Use opaque unpredictable result IDs.
- Define default retention, maximum disk usage, cleanup behavior, and an explicit delete operation before persistent storage is implemented.
- Return MIME types and size metadata when known.
- Paginate or section large results.

MCP Prompts are not part of the MVP because workflow instructions belong in the Skill.

## 13. Codex Skill

### 13.1 Location and contents

Use the repository-scoped location:

```text
.agents/skills/kegg-mcp/
├── SKILL.md
├── agents/
│   └── openai.yaml
└── references/
    ├── workflow-selection.md
    ├── annotation-tools.md
    ├── deepkoala.md
    ├── confidence-policy.md
    ├── module-interpretation.md
    └── reporting-policy.md
```

The initial Skill should be instruction-only. Do not add `scripts/normalize_ko_table.py`; normalization belongs in the tested core and MCP service.

### 13.2 Trigger contract

The `SKILL.md` description must include both positive triggers and boundaries. It should cover users who provide:

- protein FASTA and need a KO-annotation workflow;
- K numbers or KO annotation tables;
- KEGG modules or pathways;
- metabolic reconstruction questions; or
- multiple KO sets for descriptive comparison.

It should not trigger for general gene-expression analysis, nucleotide assembly, sequence alignment, statistical enrichment, or non-KEGG ontology analysis unless the user explicitly asks to connect those tasks to KO/KEGG analysis.

### 13.3 Skill responsibilities

- Identify the user's current data type and analysis unit.
- Avoid asking questions that can be answered from the input.
- Recommend an annotation workflow only when KO assignments are absent.
- Explain tool trade-offs and avoid presenting DeepKOALA as the only valid option.
- Request detailed, versioned output from external annotators.
- Discover and orchestrate an optional annotation companion MCP only when the client explicitly
  configures it and the user chooses it.
- Call the high-level MCP tool for common workflows and primitives for advanced workflows.
- Explain strict versus lenient evidence.
- Distinguish module completion from pathway coverage.
- Surface provenance, stale-cache state, and biological limitations.
- Never guess a KO from a protein or gene name.
- Never implement annotation inference, subprocess execution, weight management, or job scheduling
  inside the Skill.

### 13.4 Skill metadata

Add `agents/openai.yaml` during the Skill milestone with:

- a user-facing display name;
- a concise description;
- a default prompt;
- implicit invocation enabled unless testing shows excessive false positives; and
- an MCP dependency declaration that matches the actual server name and transport configuration.

Generate and validate the Skill using the current official Skill tooling at implementation time.

## 14. DeepKOALA guidance

DeepKOALA remains an optional annotator external to the core `kegg-mcp` server. Its interface is
versioned independently, so commands and output fields must be checked against the official
repository when the reference is written. The separately installed `deepkoala-mcp` companion now
controls its process under bounded local contracts, but it is not part of the signed core v0.1.0
release and the Skill remains instruction and orchestration only.

### 14.1 Documentation-derived baseline

As retrieved on 2026-07-15, the official documentation describes:

```bash
python3 -m deepkoala.cli -i proteins.fasta -o results.csv --model full --detail --device auto
python3 -m deepkoala.cli -i fragments.fasta -o results.csv --model frag --detail --device auto
```

- Use `full` for expected complete proteins.
- Use `frag` for fragmented proteins, including many metagenomic gene predictions.
- Use `--detail` because the simple output omits below-threshold candidates and their probability/threshold evidence.
- Treat `--multi` as an advanced optional workflow because it requires HMMER and KOfam profiles; it can produce multiple domain-level KO rows for one sequence.

The documented format was checked separately on 2026-07-14 against official DeepKOALA commit
`bebbe0c43f50a26488f7092f6b355aae870a4ed9` using the repository-provided full and fragment
weights, explicit CPU execution, two compute threads, and zero data-loader workers. Both models
produced detailed output that the importer accepted without schema repair. The check did not add
DeepKOALA code, weights, dependencies, or generated output to this repository and is not part of
the default test suite. DeepKOALA remains an external, independently versioned contract; the core
`kegg-mcp` server never executes it.

### 14.2 Importer interface contract

The first DeepKOALA integration is an import-only interface. The core `kegg-mcp` server accepts previously generated output and must never launch DeepKOALA, load its models, inspect GPU availability, or depend on its Python runtime.

Importer input:

- detailed CSV content supplied inline through the core service boundary after any client-owned
  resource has been read and verified by that client;
- an explicit `input_format="deepkoala_detailed"` selection;
- optional caller-supplied provenance such as DeepKOALA version, model type, model artifact identifier, command, and execution time; and
- the repository's versioned DeepKOALA decision policy, selected by the importer rather than by
  the caller.

Importer output:

- one `AnnotationDataset` containing immutable source provenance and an import report;
- zero or more `AnnotationRecord` values for each source row;
- preserved raw field values alongside normalized identifiers and decisions;
- multiple records for one sequence when domain coordinates or repeated rows are present; and
- structured diagnostics for missing columns, invalid values, duplicate rows, conflicting assignments, and unsupported format variants.

The importer must not infer a model version, database release, threshold policy, or domain boundary that is absent from the supplied evidence. If the table lacks the documented detailed fields, it must not be represented as detailed DeepKOALA evidence. The generic table importer may still accept it through explicit column mapping.

Initial importer tests should use small, static, documentation-derived fixtures. End-to-end execution against DeepKOALA is a separate, manually scheduled compatibility check and is not required to define or implement this interface.

### 14.3 Detailed-output mapping

Current documented detailed fields include:

```text
name
predict_label
probability
threshold
annotate
```

With multi-domain mode, `start` and `end` may also be present.

Default importer policy:

- `annotate == "*"` or a verified equivalent source rule -> `accepted`;
- prediction present but below the source threshold -> `rejected` with reason `below_source_threshold`;
- no usable prediction -> `unclassified`;
- malformed K number -> `invalid`.

Do not label every below-threshold prediction `uncertain` and do not include it in lenient module analysis. An uncertainty-margin policy may be added later only with explicit scientific justification, a version, and tests.

### 14.4 Interpretation limits

- An accepted prediction is not experimental validation.
- A rejected prediction is not proof of functional absence.
- The model can assign only functions represented by its output space and version.
- Multi-domain proteins may require domain-aware analysis.
- Results from different model/database versions should not be compared without preserving those versions.
- CPU compatibility does not guarantee acceptable performance for every dataset size or machine.

### 14.5 Implemented optional local companion (unreleased)

This section records the post-MVP implementation checked against the official DeepKOALA
documentation on 2026-07-15. The `companions/deepkoala-mcp/` tree is an independent Python
distribution and stdio entry point. It is implemented and tested, but it has not received its own
release sign-off and is not part of the signed core v0.1.0 release.

The candidate is POSIX-only because its bounded cancellation, timeout, and shutdown contract
requires process-group ownership and its output-byte contract requires a process file-size limit.
Startup fails before configured path handling or private state creation when those controls are
unavailable.

The companion runner controls DeepKOALA as an MCP-side process, not as Skill code or as part of the
core `kegg-mcp` process. It has its own installation, configuration, process lifecycle, lock file,
tests, and release review. Its lightweight distribution depends on MCP and Pydantic only; it does
not add DeepKOALA, PyTorch, weights, KOfam profiles, or HMMER to the core runtime or distribution.
The pipeline is:

```text
protein FASTA -> optional DeepKOALA runner -> detailed CSV plus provenance -> kegg-mcp importer
```

The implemented initial contract uses these defaults and hard limits:

| Setting | Runner default | Required behavior |
| --- | --- | --- |
| `device` | `auto` | Resolve and pin DeepKOALA's reported device during preparation. The notice warns that `auto` may select an available GPU. Use explicit `cpu` when GPU use is not authorized. |
| `weight_source` | `github_bundled` | Use weights already present in the configured DeepKOALA checkout. `user_provided` is an explicit provenance label, not a download request. |
| `model_date` | `latest` within installed resources | Resolve the DeepKOALA `--date` value and record the actual model date; never report only `latest` in final provenance. |
| `model` | `full` | Allow an explicit `frag` selection for fragmented proteins and metagenomic gene predictions. |
| `detail` | `true` | Always request detailed output so probabilities, thresholds, and source decisions remain importable. |
| `batch_size` | `32` | Omit the command override unless requested, while displaying the effective DeepKOALA default. |
| `num_workers` | `2` | Omit the command override unless requested, while displaying the effective DeepKOALA default. |
| `topk` | `1` | Omit the command override unless requested, while displaying the effective DeepKOALA default. |
| `multi` | `false` | Reject `true`; HMMER and KOfam profile execution is outside the initial security contract. |
| `cpu_threads` | `2` | Pin the child process's supported CPU thread controls to the configured bounded value. |
| `max_concurrent_jobs` | `1` | Enforce one independent DeepKOALA process as a hard initial-contract maximum. This is not an inference batch-size setting. |
| `max_queue_size` | `32` | Reject submission when the bounded process-local queue is full. |

`batch_size` and job concurrency are different controls. `batch_size` is the number of sequences
processed in one inference batch inside a single DeepKOALA process; it affects throughput and
device memory. `max_concurrent_jobs` is the number of independent FASTA annotation processes that
the runner may execute simultaneously. The initial companion does not accept values above one
because separate processes would load multiple model copies and compete for the same CPU, GPU,
and memory resources.

The stdio service exposes exactly six tools:

| Tool | Contract |
| --- | --- |
| `get_deepkoala_runner_status` | Return path-free installation readiness, defaults, bounds, supported models/devices, and scheduler counts. |
| `prepare_deepkoala_job` | Validate and privately stage one bounded protein FASTA source, resolve the installed runtime and model artifacts, and return an execution notice without inference. |
| `submit_deepkoala_job` | Start or queue only the exact prepared plan after `acknowledged=true` and a matching `notice_sha256`; idempotent replay is scoped to that plan. |
| `get_deepkoala_job` | Return current lifecycle state and, after success, a source-agnostic core importer handoff. |
| `cancel_deepkoala_job` | Cancel a queued job or terminate and reap a running process group. |
| `delete_deepkoala_job` | Delete one terminal job and its retained local artifacts; retries are recognized only within the bounded process-local tombstone window. |

Preparation and submission are deliberately separate. `prepare_deepkoala_job` returns a
time-limited `plan_id`, canonical notice digest, non-sequence FASTA summary and digest, and the full
notice without starting DeepKOALA. A client must display that notice and obtain explicit user
acknowledgement. `submit_deepkoala_job` requires the same `plan_id`, exact `notice_sha256`, and
literal `acknowledged=true`. Expired, changed, or unacknowledged plans do not execute.

The notice contains:

- the requested and resolved model date and requested and resolved device;
- a warning that `device=auto` may use an available GPU, when applicable;
- the weight source (`github_bundled` or an explicit user-provided source) and resolved weight
  version when available;
- the SHA-256 identity and size of the configured interpreter, DeepKOALA source tree, model
  weights, and model configuration;
- the FASTA SHA-256 digest, byte and residue counts, sequence count, and length range without
  sequence content or local paths;
- the effective DeepKOALA `batch_size`, `num_workers`, `topk`, and `multi` settings;
- `cpu_threads`, timeout, the hard one-job limit, and the planned running or queued disposition;
- a statement that installed weights are used as-is and are never downloaded or replaced; and
- the updated-weight location: <https://www.genome.jp/ftp/db/deepkoala/>.

The companion accepts at most 5,000,000 FASTA bytes, 5,000,000 residues, 100,000 sequences,
100,000 residues per sequence, and 1,024 bytes per header. Output is limited to 5,000,000 bytes and
100,000 rows; sanitized diagnostics are limited to 65,536 bytes. Default timeout is 3,600 seconds,
prepared plans expire after 600 seconds, and terminal artifacts are retained for 86,400 seconds
unless explicitly deleted or removed by cleanup. Status reports the effective sequence-count,
residue-count, per-sequence, and header limits after any configured reductions. Job summaries mark
bounded diagnostic tails with `diagnostics_truncated=true`, which requires a terminal
`diagnostic_uri`.

The output-byte bound is installed as `RLIMIT_FSIZE` before the upstream CLI is loaded and is
revalidated after exit. This prevents a malformed or faulty child from writing an unbounded result
before post-process validation can reject it.

The runner uses a fixed argument vector without `shell=True`, validates allowed-root file intake
against traversal and symlink escape, copies input into a private state directory, bounds both
diagnostic streams, and never returns full sequences or local paths. It rechecks the staged FASTA,
configured interpreter, DeepKOALA source, weights, and model configuration against the notice
identities at submission, immediately before launch, and again after a successful process exit.
Cancellation and timeout terminate and reap the process group; cleanup tracks retryable failures
rather than reporting false deletion.

Explicit deletion retains at most the 1,024 most recent job identifiers as process-local
tombstones. A repeated delete is recognized only inside that bounded window; tombstone eviction or
process restart results in `JOB_NOT_FOUND`, so the tool does not claim unconditional idempotence.

Successful output, provenance, and sanitized diagnostics are scoped to the companion process under
`deepkoala-job://jobs/{job_id}/{section}`. Direct resource reads are limited to 64 KiB. Larger
artifacts return a pagination notice and binary-safe ranges of at most 1 MiB, each carrying
`content_base64`, the artifact SHA-256, total size, and continuation URI. The client must decode and
assemble ranges in order and verify the declared total bytes and digest against the successful job
summary. It must then pass the verified CSV inline to core `normalize_ko_annotations` with
`input_format=deepkoala_detailed` and the returned `source_provenance_template`. The core server
cannot dereference another server's session-private companion URI, and the companion does not
introduce a second normalization policy.

On 2026-07-15, a manual CPU-only smoke check exercised this implemented preparation, submission,
execution, detailed-output validation, and result lifecycle against official DeepKOALA commit
`bebbe0c43f50a26488f7092f6b355aae870a4ed9`. It used the checkout-bundled `202502` full and fragment
weights, the existing Python 3.11/PyTorch `2.9.1+cu130` environment with
`torch.cuda.is_available() == false`, `device=cpu`, two CPU threads, `batch_size=1`,
`num_workers=0`, `topk=1`, and `multi=false`. Both models completed without a GPU, download, new
environment, or repository-tracked output and produced schema-valid detailed rows. Both terminal
jobs were deleted and the temporary state root was empty afterward. This smoke check is
compatibility evidence only, is outside the default offline suite, and does not grant the companion
release sign-off or establish biological correctness.

## 15. Reporting and provenance

### 15.1 Report layers

Every user-facing report should separate:

1. input and normalization summary;
2. annotation evidence;
3. KEGG database mappings;
4. module exact completion and block coverage;
5. pathway KO coverage;
6. comparison results, when requested;
7. caveats and unresolved data; and
8. provenance.

### 15.2 Output formats

MVP:

- structured JSON-compatible MCP content;
- Markdown summary; and
- CSV exports for flat record-level sections where lossless flattening is possible.

Do not force nested module alternatives into a lossy single CSV. Use separate normalized tables or JSON for nested structures.

### 15.3 Required provenance

- source file logical name and digest;
- annotation tool, software version, model name, and model/database version;
- annotation date when available;
- importer and decision-policy versions;
- KEGG endpoint class, retrieval time, per-database release when available, and cache state;
- server and algorithm versions;
- module-definition digest;
- pathway namespace and denominator retrieval metadata;
- strict/lenient mode;
- analysis unit and taxonomic context; and
- all non-default parameters.

Unknown values must be written as `unknown` or `null`, not guessed.

## 16. Errors, security, and privacy

### 16.1 Domain error taxonomy

```text
INVALID_KO_IDENTIFIER
INVALID_ANNOTATION_TABLE
AMBIGUOUS_COLUMN_MAPPING
MISSING_REQUIRED_COLUMN
UNSUPPORTED_INPUT_FORMAT
INPUT_LIMIT_EXCEEDED
KEGG_USAGE_NOT_CONFIGURED
KEGG_REQUEST_FAILED
KEGG_RATE_LIMITED
KEGG_ENTRY_NOT_FOUND
KEGG_PARSE_FAILED
CACHE_FAILED
OFFLINE_CACHE_MISS
MODULE_DEFINITION_INVALID
MODULE_REFERENCE_CYCLE
MODULE_NOT_EVALUABLE
PATHWAY_NAMESPACE_MISMATCH
INCOMPATIBLE_ANALYSIS_PROVENANCE
RESULT_NOT_FOUND
ANALYSIS_CONFIGURATION_INVALID
```

Every recoverable error should include:

```text
code
message
recoverable
suggested_action
safe_details
```

Errors must distinguish absence of an entry, unavailable network data, stale cache, parse failure, unsupported syntax, and biological absence.

### 16.2 Input safety

- Prefer inline text, records, or client resources over arbitrary server-side paths.
- If path import is later added, accept only paths under explicit MCP roots/allowlists.
- Resolve paths safely and reject traversal and symlink escapes.
- Cap bytes, rows, columns, field length, KO count, module count, pathway count, and decompressed size.
- Do not execute macros, formulas, shell fragments, or embedded commands from input tables.
- Do not use user strings to construct arbitrary URLs.

### 16.3 Output and operational safety

- Redact secrets and sensitive local paths.
- Keep logs on stderr and avoid logging raw protein sequences or full annotation tables by default.
- Avoid writing reports unless the user explicitly requests a destination.
- Use restrictive permissions for local result and cache files where practical.
- Treat resource and result identifiers as untrusted input.
- Provide cleanup controls for retained results.

## 17. Testing strategy

### 17.1 Unit tests

- KO validation and prefix normalization.
- Importer signatures and explicit column mappings.
- Source-decision normalization policies.
- Multiple KO rows per sequence and domain coordinates.
- Duplicate and conflict handling.
- KEGG flat-file, tabular, and `info` parsing.
- Request batching and process-wide rate limiting.
- Cache freshness, stale offline use, and corruption.
- Module tokenizer, precedence, nesting, alternatives, complexes, optional nodes, references, cycles, and unsupported tokens.
- Exact module completion and block coverage.
- Pathway namespace and denominator behavior.
- Strict/lenient separation.
- Deterministic KO-set comparison.
- Bounded report previews and serialization.

### 17.2 Integration tests

- Importer -> dataset -> analysis -> report.
- KEGG client against a local mock server.
- Cache hit, refresh, expiry, offline miss, and transient retry.
- Stored-result creation, scoped retrieval, pagination, expiry, and cleanup.
- Service-layer high-level workflow using mocked KEGG responses.

### 17.3 MCP contract tests

- Server startup and clean shutdown.
- `tools/list`, input schemas, output schemas, titles, and annotations.
- Schema-conforming `structuredContent`.
- Recoverable tool errors versus protocol errors.
- stdio stdout cleanliness.
- Resource and resource-template discovery.
- Resource URI validation, not-found behavior, scoping, and size metadata.
- Bounded responses for oversized analyses.

### 17.4 Fixtures and external services

- Use synthetic or independently authored small fixtures.
- Do not commit bulk KEGG responses, pathway images, KGML collections, KOfam profiles, model weights, or large FASTA files.
- Do not run live KEGG requests in normal or pull-request CI.
- Keep any opt-in live compatibility campaign local or in a dedicated main-only CI job that
  is explicitly enabled, serialized, rate-limited, eligibility-gated, bounded, and tolerant of
  current database content.
- Do not use strict snapshots of full live KEGG entries because database content changes.

### 17.5 Skill evaluation

Test at least these prompts after the MCP contracts are stable:

```text
I have a protein FASTA file and want to analyze metabolic functions.
Here is detailed DeepKOALA output; analyze KEGG modules.
I have one column of K numbers; check carbon-metabolism coverage.
Compare these two KO sets.
Does K00844 prove that glycolysis is active?
Map this gene name to a KO for me.
```

Success requires correct routing, minimal necessary clarification, no KO guessing, conservative interpretation, and use of the high-level tool for the common path.

## 18. Milestones

### Milestone 0: governance and project initialization

Status as of 2026-07-14: the MIT source-code license, Python package foundation, locked development environment, offline test, quality-tool configuration, CI workflow, contribution guide, security policy, and issue templates are implemented and locally verified. Milestone 0 is complete.

Tasks:

- choose the project code license;
- document the distinction between code licensing and KEGG data rights;
- create the Python project and package metadata;
- configure uv, ruff, pyright, pytest, and CI;
- add contribution, security, and issue templates; and
- keep default and pull-request CI free of live KEGG calls.

Acceptance:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

All pass on a minimal tested package.

### Milestone 1: evidence model and importers

Status as of 2026-07-14: the immutable evidence contracts, versioned decision policies, plain KO,
generic CSV/TSV, and DeepKOALA detailed importers, structured import reports, and deterministic KO
evidence views are implemented and locally verified with offline tests. Milestone 1 is complete.

Tasks:

- approve Pydantic/JSON schemas;
- implement plain KO, generic CSV/TSV, and DeepKOALA detailed importers;
- implement explicit column mapping;
- implement decision-policy versioning;
- support multiple records per sequence; and
- produce import reports and dataset context.

Acceptance:

- no raw source evidence is lost;
- DeepKOALA accepted and below-threshold rows are distinguished correctly;
- ambiguous tables require repairable explicit mapping;
- duplicates and conflicts are reported; and
- strict and lenient KO views are deterministic.

### Milestone 2: KEGG client and cache

Status as of 2026-07-14: the eligibility gate, typed and bounded KEGG operations, process-wide
rate limiting, safe transport and retries, strict response parsing, endpoint-scoped local cache,
offline behavior, and retrieval provenance are implemented and locally verified with offline
tests. Milestone 2 is complete.

Tasks:

- implement eligibility configuration;
- implement typed `info`, `get`, `link`, and selected `conv` operations;
- enforce rate and batch limits;
- parse supported response formats;
- implement cache and offline mode; and
- record retrieval provenance.

Acceptance:

- `get` batches never exceed ten entries;
- the process cannot exceed the configured maximum rate;
- no live call occurs in offline mode;
- standard tests use a mock server only; and
- cache failures cannot be mistaken for missing biology.

### Milestone 3: module parser and evaluator

Status as of 2026-07-14: the lossless bounded tokenizer, source-spanned AST and parser, local
M-number reference resolver, exact completion, project-defined top-level block coverage, bounded
minimal missing alternatives, optional-component summaries, and paired strict/lenient evidence
evaluation are implemented and locally verified with offline synthetic tests. Milestone 3 is
complete.

Tasks:

- implement tokenizer, AST, parser, references, and diagnostics;
- implement exact evaluation and block coverage;
- implement strict/lenient results and minimal missing alternatives; and
- handle unsupported content conservatively.

Acceptance:

- all synthetic grammar fixtures pass;
- optional terms do not inflate the denominator;
- cycles and unresolved references are explicit;
- no token is silently discarded; and
- strict-to-lenient changes identify supporting uncertain records.

### Milestone 4: pathway and comparison analysis

Status as of 2026-07-14: typed PATHWAY LINK/GET reference construction, explicit namespaces and
CLASS-derived scope, bounded strict/lenient pathway KO coverage, complete deterministic multi-set
KO membership partitions, and shared-reference MODULE and pathway outcome comparisons are
implemented and locally verified with offline synthetic tests. Milestone 4 is complete.

Tasks:

- implement explicit pathway namespaces and denominators;
- implement bounded coverage results;
- implement deterministic multi-set comparison; and
- add analysis-unit-aware warnings.

Acceptance:

- every ratio has a reproducible numerator and denominator;
- KO-only inputs do not become organism-specific claims;
- global maps require explicit opt-in; and
- no output implies activity, flux, phenotype, or statistical significance.

### Milestone 5: services, result store, and reporting

Status as of 2026-07-14: typed MODULE and pathway reference loading, the bounded plain-KO
one-call analysis service, deterministic structured JSON/Markdown/annotation CSV artifacts, and a
scope-isolated SQLite result store are implemented and locally verified with offline synthetic
tests. The store defaults to 24-hour retention, a 512 MiB logical artifact-payload quota, a 640 MiB
main-database page cap, and a 10,000-result cap; it supports bounded metadata pagination, artifact
byte-range reads, explicit deletion, and cleanup without silently evicting active cross-scope
results. Stored structured reports retain typed one-call execution parameters and producer,
renderer, retrieval, parser, and analysis provenance. Plain-KO requests reject organism-specific
pathway references. No MCP transport, resource URI, live KEGG test, or Codex Skill is included.
Milestone 5 is complete.

Tasks:

- add service-layer orchestration;
- implement the one-call analysis use case;
- implement scoped, bounded result storage and cleanup;
- implement JSON-compatible, Markdown, and appropriate CSV outputs; and
- complete provenance serialization.

Acceptance:

- a common KO-list workflow is one service call;
- large results return previews and retrievable artifacts;
- expired or unauthorized result IDs fail safely; and
- report claims match evidence status.

### Milestone 6: MCP server

Status as of 2026-07-14: the local stdio server, all eight approved tools, explicit input/output
schemas, structured success and repairable-error envelopes, tool annotations, fixed status/cache
resources, and four validated resource templates are implemented and locally verified with
offline contract tests. Each stdio process generates one opaque result scope. Large sections use
bounded byte-range resources, and cached-entry resources are offline-only. Protocol stdout remains
clean. Status and cache-info reads are side-effect-free: they do not open SQLite to collect
statistics, and instead return `null` values with `inspection_status=not_probed`. Milestone 6 is
complete.

Tasks:

- implement stdio transport;
- register the approved tool surface;
- define resources and templates;
- add schemas, annotations, and protocol-aware errors; and
- add contract tests.

Acceptance:

- clients discover all approved tools and resources;
- structured results validate against output schemas;
- stdout contains protocol traffic only;
- resource URIs are validated and scoped; and
- status output is useful without leaking local secrets.

### Milestone 7: Codex Skill

Status as of 2026-07-14: the instruction-only repository-scoped Skill, focused workflow and
interpretation references, real `kegg-mcp` stdio dependency metadata, and the six required routing
and refusal cases are implemented. Deterministic static contract tests validate the instruction
artifacts, and a separate recorded forward/manual review passed all six prompts; CI does not
execute the Skill through a language model. The Skill delegates normalization and deterministic
analysis to MCP tools, skips annotation guidance when K numbers already exist, and never guesses a
KO from a name or sequence. Milestone 7 is complete.

Tasks:

- initialize the Skill with current official tooling;
- write concise workflow instructions and focused references;
- add `agents/openai.yaml` with the real MCP dependency;
- document DeepKOALA and alternative annotators; and
- evaluate trigger and refusal behavior.

Acceptance:

- the six evaluation prompts route correctly;
- the Skill skips annotation guidance for KO inputs;
- it never guesses KOs from names;
- it uses detailed DeepKOALA output guidance; and
- it distinguishes module completion from pathway coverage.

### Milestone 8: release readiness

Status as of 2026-07-15: installation and MCP configuration guidance, redistributable synthetic KO
examples, data-rights and security review checklists, an English changelog and release notes, and
offline wheel/source-distribution audit tests are implemented. Version 0.1.0 is scoped to Python
3.11.x only. Release preparation and Milestone 8 are complete. The repository is private; before
any future public supported release, GitHub private vulnerability reporting must be enabled and
verified.

Tasks:

- finish installation and MCP configuration docs;
- add redistributable synthetic examples;
- complete security and data-rights review;
- build the Python package; and
- prepare changelog and release notes.

Acceptance:

A new eligible user can install, configure, normalize a KO list, run module and pathway analyses, retrieve a full result, and understand the limitations without maintainer assistance.

## 19. Initial issue backlog

1. Choose the source-code license and document KEGG data-rights boundaries.
2. Initialize the Python package, quality tools, and offline-only CI.
3. Define `SourceProvenance`, `AnnotationRecord`, and `AnnotationDataset` schemas.
4. Define versioned decision policies and strict/lenient evidence views.
5. Implement K-number validation and plain KO-list import.
6. Implement generic CSV/TSV import with explicit column mapping.
7. Implement DeepKOALA detailed-output import, including multi-domain rows.
8. Define typed KEGG request and response contracts.
9. Implement eligibility configuration, rate limiting, batching, and retries.
10. Implement the SQLite cache and offline behavior.
11. Implement KEGG flat-file, tabular, and `info` parsers.
12. Implement approved KO relationship mappings.
13. Specify module grammar, precedence, and diagnostics.
14. Implement the module tokenizer, AST, and parser.
15. Implement module reference resolution and cycle detection.
16. Implement exact strict/lenient module evaluation.
17. Implement project-defined block coverage and missing alternatives.
18. Implement pathway namespace and denominator services.
19. Implement descriptive pathway coverage.
20. Implement deterministic KO-set comparison.
21. Implement result storage, scoping, pagination, and cleanup.
22. Implement bounded structured and Markdown reporting.
23. Implement the high-level analysis service.
24. Scaffold the stdio MCP server and status tool.
25. Implement MCP tool schemas, annotations, and resources.
26. Add MCP contract and stdout-cleanliness tests.
27. Initialize and write the repository-scoped Codex Skill.
28. Write annotation-tool, DeepKOALA, confidence, and interpretation references.
29. Add synthetic end-to-end examples.
30. Complete release, security, and data-rights review.

Each issue should normally touch one layer or one contract. Cross-layer changes require an explicit integration issue.

## 20. Release gates

The first release is blocked until all of the following are true:

- The public KEGG usage restriction and licensed-use path are visible during setup.
- The live client cannot exceed documented rate and batch limits.
- No KEGG cache, bulk response, model weight, profile database, secret, or large biological input is packaged or committed.
- Annotation evidence supports multiple KOs per sequence and preserves raw decisions.
- DeepKOALA simple output is not misrepresented as detailed evidence.
- Exact module completion and block coverage are separate, named results.
- Unsupported module content never becomes a false incomplete/complete result.
- Every pathway ratio identifies its namespace, numerator, denominator, and retrieval provenance.
- Large MCP results are bounded and retrievable safely.
- Tool outputs conform to declared schemas and stdio stdout is clean.
- Reports do not claim pathway activity, flux, phenotype, or statistical significance from KO presence alone.
- All default CI tests run without live KEGG access.
- All tracked repository content is in English.
- Local Simplified Chinese references match `*.zh-CN.md`, remain ignored and untracked, and are
  absent from GitHub, packages, releases, examples, and CI artifacts.
- Version 0.1.x package metadata accepts Python 3.11.x only; wider Python support requires separate
  compatibility testing.
- The private-release security policy documents a collaborator-only reporting boundary; GitHub
  private vulnerability reporting is enabled and verified before any public supported release.

## 21. Resolved decisions

The implementation milestones resolved the original open decisions as follows:

1. **Source-code license:** project source code uses the MIT License. It grants no rights to KEGG
   content, KOfam profiles, model artifacts, or other third-party data.
2. **KEGG configuration:** the environment contract selects `offline_cache`, `public_academic`, or
   `licensed`. Live modes require explicit use confirmation, and licensed mode also requires an
   authorized HTTPS endpoint. Offline mode can select the licensed cache namespace only when both
   the licensed endpoint and licensed-use confirmation are supplied; network access remains off.
3. **Request rate:** the default is two requests per second with no burst, and configuration cannot
   exceed three requests per second.
4. **Bounds:** schemas enforce five million inline bytes, 100,000 imported rows, 100 K numbers per
   mapping call, 25 MODULE targets, 25 pathway targets, bounded previews, bounded response bytes,
   and lower operation-specific limits where applicable.
5. **Result lifecycle:** defaults are 24-hour retention, a 512 MiB logical payload quota, a 640 MiB
   main-database cap, and 10,000 active results. The scoped store provides explicit deletion,
   cleanup, metadata pagination, and bounded artifact reads.
6. **Partial MODULE evaluation:** `partially_evaluable` and `not_evaluable` results expose no block
   coverage ratio. Coverage is reported only when every required top-level block is evaluable.
7. **MODULE discovery:** the initial MVP requires explicit MODULE identifiers; automatic discovery
   is deferred rather than introducing an unbounded or speculative strategy.
8. **CSV delivery:** the complete flat annotation CSV is retained as the `annotations` result
   section and exposed through validated, bounded resources. The server does not write a caller
   path or force the full CSV into direct tool content.
9. **Python compatibility:** version 0.1.x supports and is tested only on Python 3.11.x;
   package metadata excludes Python 3.12 and later.
10. **Server and Skill identity:** the stdio server name, console command, and Skill MCP dependency
    value are all `kegg-mcp`.
11. **DeepKOALA execution boundary:** automatic FASTA-to-KO execution is implemented post-MVP as a
    separately installed companion MCP server and runner process with its own review boundary. The
    implementation has not received release sign-off and is not part of core v0.1.0. The Skill may
    orchestrate it, and the core server imports its detailed result, but neither contains inference
    or job-control logic.
12. **Documentation language:** tracked and published repository content is English. Local
    Simplified Chinese references use `*.zh-CN.md`, remain ignored and untracked, and are
    non-normative; publishing one requires an explicit file-specific maintainer request.

## 22. Primary references

External behavior is time-sensitive. Recheck these primary sources during the relevant milestone.

- [KEGG API overview, usage restriction, and rate limit](https://www.kegg.jp/kegg/rest/)
- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html)
- [KEGG copyright and licensing notice](https://www.kegg.jp/kegg/legal.html)
- [KEGG MODULE database and completeness syntax](https://www.kegg.jp/kegg/module.html)
- [KEGG MODULE entry help](https://www.kegg.jp/kegg/document/help_bget_module.html)
- [DeepKOALA GenomeNet page](https://www.genome.jp/tools/deepkoala/)
- [DeepKOALA official repository](https://github.com/zhaoxi120/deepkoala)
- [DeepKOALA updated weights directory](https://www.genome.jp/ftp/db/deepkoala/)
- [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [MCP resources specification](https://modelcontextprotocol.io/specification/2025-06-18/server/resources)
- [OpenAI Codex Skill documentation](https://developers.openai.com/codex/skills)
- [OpenAI Codex `AGENTS.md` documentation](https://developers.openai.com/codex/guides/agents-md)
