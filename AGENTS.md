# Repository guidance

## Purpose and current phase

This repository provides local stdio MCP servers and repository-scoped Codex Skills for
KEGG-aware analysis of KO annotations.

Milestones 0 through 8 and the first supported release are implemented and verified. The
visualization extension is an explicitly assigned post-MVP change governed by
`docs/visualization-extension-plan.md`. Further work must begin only through an assigned issue or
explicit maintainer request. Do not add empty source trees, placeholder tests, speculative code, or
a partially functional Skill. The core specification remains `docs/development-plan.md`.

## Communication and repository language

- Communicate plans, progress updates, questions, and review summaries with the maintainer in Simplified Chinese unless asked otherwise.
- Write every tracked repository file in English, including documentation, source comments, test names, fixtures, configuration comments, and examples.
- Local Simplified Chinese reference documents may use the `*.zh-CN.md` suffix. They must remain
  ignored and untracked, are non-normative, and must not be linked from tracked documentation or
  included in GitHub, packages, releases, examples, or CI artifacts. The corresponding English
  document is the source of truth.
- Publishing a Chinese document requires an explicit maintainer request that overrides the
  local-only rule for that specific file.
- Write GitHub issues, pull requests, commit messages, and release notes in English.
- Use established KEGG identifiers and biological terminology; do not translate identifiers or invent synonyms.

## Sources of truth

Use this precedence order when requirements conflict:

1. the current user request or assigned GitHub issue;
2. this `AGENTS.md`;
3. `docs/development-plan.md` for the core and `docs/visualization-extension-plan.md` for the
   visualization extension;
4. public interfaces and tests once they exist;
5. other documentation.

For facts about external systems, prefer current primary sources:

- KEGG API, MODULE, and legal documentation for KEGG behavior and usage rights;
- the official DeepKOALA repository and GenomeNet page for DeepKOALA behavior;
- the official MCP specification for protocol contracts; and
- official OpenAI Codex documentation for Skill and `AGENTS.md` conventions.

Record the retrieval date when an external fact affects a schema, parser, fixture, or acceptance test.

## Scope boundaries

- DeepKOALA, KofamScan, BlastKOALA, GhostKOALA, and similar programs are external annotation tools.
- Do not add their model code, weights, databases, inference logic, or runtime dependencies to the
  core `kegg-mcp` package or server.
- The core `kegg-mcp` server must not execute external annotation tools.
- Any automatic DeepKOALA execution must be implemented as a separately installed, explicitly
  configured companion MCP server and runner process with its own entry point, environment,
  lifecycle, and release review. It is an MCP-side capability, not Skill implementation code, and
  requires an assigned issue before implementation.
- `deepkoala-annotation` may orchestrate only an available DeepKOALA companion; `kegg-ko-analysis`
  may orchestrate only the core server; and `kegg-pathway-rendering` may orchestrate only the
  renderer. No Skill may implement inference, launch subprocesses itself, manage weights, or
  silently install or download dependencies.
- Do not download or redistribute KOfam profiles or KEGG datasets.
- Do not add enrichment, differential-abundance statistics, a web UI, multi-user hosting, or
  remote HTTP transport. The core server must not generate pathway images; approved image
  generation belongs only to the separately installed `kegg-render-mcp` companion and must follow
  `docs/visualization-extension-plan.md`.
- Keep generic KO input support; never make the core schema DeepKOALA-specific.

## Architecture invariants

- Keep domain, importer, KEGG client, analysis, and reporting code independent of MCP transport.
- MCP tools must call public service-layer functions rather than reimplementing analysis.
- Each Skill must declare exactly one MCP dependency and orchestrate only that server. Stable,
  versioned output-directory files connect stages; no Skill may duplicate deterministic
  normalization, analysis, inference, rendering, or job-control code.
- A companion runner must return detailed annotation output and provenance through the existing
  source-agnostic importer boundary rather than introducing a second KO normalization policy.
- The core server produces the authoritative typed renderer handoff but does not parse KGML or
  render images. The renderer must not normalize evidence or recompute MODULE completion or
  pathway coverage.
- Keep raw source evidence immutable. Derive normalized decisions using a named and versioned policy.
- Allow multiple KO records for one sequence, including top-k and multi-domain results.
- Treat a KO set as a derived view of annotation records, not as the primary evidence model.
- Make all analysis parameters and provenance serializable.

## Biological interpretation invariants

- A K number assignment is an annotation, not experimental validation.
- A source-rejected prediction is not evidence that the biological function is absent.
- Do not place every below-threshold prediction into a lenient analysis. Only records explicitly classified as `uncertain` by a documented policy may enter lenient mode.
- Do not compare scores or thresholds across annotation tools unless their semantics are known to be comparable.
- Evaluate strict analyses with accepted K numbers only. Evaluate lenient analyses with accepted plus policy-defined uncertain K numbers.
- Report exact module completion separately from the project's block-coverage metric.
- Follow KEGG module logical syntax: top-level spaces and plus signs are AND, commas are OR, and a minus sign marks an optional component. Preserve parentheses and module references.
- Never silently drop unsupported module tokens. Return `not_evaluable` with a reason.
- Pathway KO coverage is descriptive. Do not equate coverage with pathway presence, completeness, activity, flux, or phenotype.
- State the pathway reference type and denominator explicitly.
- Distinguish single-genome, MAG, isolate proteome, pangenome, and metagenomic-community analyses. Community-level completeness indicates pooled encoded potential, not a complete pathway in one organism.
- Describe KO-set comparisons as deterministic set differences, not statistical differential function.

## KEGG access and data handling

- Treat KEGG access rights as a release-blocking requirement, not a documentation footnote.
- The public KEGG REST service is for academic use by academic users. Require non-academic deployments to configure an appropriately licensed endpoint.
- Enforce a deployment-wide rate no greater than three KEGG API requests per second across local
  Core and Renderer processes; use a safer default with no burst.
- Respect endpoint-specific limits, including the maximum of ten entries for `get` requests.
- Keep cached KEGG payloads local and out of version control, packages, examples, CI artifacts, and releases.
- Retrieve pathway PNG and KGML assets only through the typed single-pathway asset interface. Keep
  source assets local and never add real KEGG-derived image or XML fixtures to the repository.
- Keep live KEGG tests explicit locally. The enabled default campaign and pull-request CI run one
  serialized 120-request campaign: 30 requests each for `INFO`, `GET`, `LINK`, and `CONV`, at one
  request per second with zero retries and no uploaded KEGG payloads.
- Store retrieval time, endpoint, request key, parser version, and release information when
  available.

## MCP and security invariants

- Use stdio transport for the MVP and write logs only to stderr or a configured file.
- Define explicit input and output schemas and return schema-conforming structured content.
- Mark read-only, idempotent, and open-world behavior accurately in tool annotations.
- Return bounded summaries for large analyses and expose full local results through validated resource templates or pagination.
- Scope result identifiers and define retention. Do not expose another client or session's results.
- Prefer inline content or client-provided resources. If filesystem paths are supported, restrict them to explicitly allowed roots and reject traversal and symlink escapes.
- Validate input size, record count, identifier count, URI parameters, and output size.
- Redact secrets, environment values, usernames, and full local cache paths from status output.
- Never use `shell=True`.
- Renderer inputs, SVG, PNG, retained results, and output paths must remain bounded, static,
  scope-isolated, symlink-safe, and free of external resources or active content.

## Development workflow

- Keep one issue focused on one layer or one contract.
- Before editing, inspect the worktree and preserve unrelated user changes.
- Update documentation and tests in the same change when a public contract or biological rule changes.
- Use small, reviewable commits and English commit messages.
- Do not claim a milestone is complete until all listed acceptance criteria pass.

Once the Python project exists, the default validation commands are expected to be:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

These commands are maintainer tools, not a mandatory pre-commit gate. Routine commits may rely on
the pull-request CI and address failures in a follow-up commit. The default local test command must
not make live KEGG requests unless the maintainer explicitly opts in.

Keep the GitHub workflow aligned with these commands now that the corresponding tools and tests
are configured. Local execution remains optional unless a release checklist explicitly requires
additional evidence.

## Review checklist

Before completing an implementation task, verify:

- the change stays within the assigned layer and MVP scope;
- schemas preserve provenance, ambiguity, and multiple assignments;
- biological claims are no stronger than the evidence;
- KEGG licensing, rate limits, and cache boundaries remain enforced;
- MCP outputs are bounded, typed, and safe for local use;
- tests cover failure and uncertainty paths, not only the happy path; and
- all tracked content is in English, while any local `*.zh-CN.md` reference remains ignored and
  excluded from distribution.
