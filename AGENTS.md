# Repository guidance

## Purpose and current state

This repository provides three independently packaged local stdio MCP servers and three focused
Codex Skills for KEGG-aware KO analysis:

- `kegg-mcp` normalizes annotation evidence and performs authoritative KEGG analysis;
- `deepkoala-mcp` optionally runs an explicitly configured local DeepKOALA installation; and
- `kegg-render-mcp` renders the core's typed handoff as bounded static graphics.

The core, companions, visualization workflow, and unified installer implementation are present.
The unified Codex installation remains release-gated until its exact release candidate passes the
real-Codex new-task smoke and the checklist in `docs/release-readiness.md`. Further product work
requires an assigned issue or an explicit maintainer request. Do not add placeholders, speculative
extension points, empty source trees, or partially functional Skills.

## Communication and repository language

- Communicate with the maintainer in Simplified Chinese unless asked otherwise.
- Write all tracked files, source comments, tests, fixtures, examples, issues, pull requests,
  commits, and release notes in English.
- Local Simplified Chinese references may use `*.zh-CN.md`. They must remain ignored, untracked,
  non-normative, and absent from links, packages, releases, examples, and CI artifacts.
- Publishing a Chinese document requires an explicit maintainer request for that file.
- Preserve established KEGG identifiers and biological terminology.

## Sources of truth

When requirements conflict, use this order:

1. the current user request or assigned GitHub issue;
2. this `AGENTS.md`;
3. implemented public schemas, interfaces, and tests;
4. `docs/architecture.md` for current cross-component architecture and
   `docs/visualization-architecture.md` for current visualization architecture; and
5. other documentation.

For external behavior, prefer current primary sources: official KEGG API, MODULE, and legal
documentation; the official DeepKOALA repository and GenomeNet page; the MCP specification; and
official OpenAI Codex documentation. Record the retrieval date when an external fact changes a
schema, parser, fixture, or acceptance test.

## Scope and architecture

- Keep domain, importer, KEGG client, analysis, reporting, and storage code independent of MCP
  transport. MCP tools call public service functions rather than duplicating domain logic.
- Keep the three distributions, runtimes, stdio processes, state roots, entry points, and release
  reviews independent, including when the suite installer provisions them together.
- Each Skill declares exactly one MCP dependency and orchestrates only that server. Stable,
  versioned output files connect stages.
- The core never executes an annotator, parses KGML, or renders images. The DeepKOALA companion
  never normalizes evidence. The renderer never recomputes normalization, MODULE completion, or
  pathway coverage.
- A companion returns detailed annotation output and provenance through the source-agnostic
  importer boundary. Keep generic KO input support and allow multiple records per sequence,
  including top-k and multi-domain evidence.
- Treat raw source evidence as immutable. A KO set is a policy-derived view of annotation records.
  Keep analysis parameters and provenance serializable.
- Do not add external annotation code, weights, KOfam profiles, KEGG datasets, enrichment,
  differential abundance, abundance weighting, a web UI, remote HTTP transport, multi-user
  hosting, or non-KEGG backends without separately assigned scope.

## Biological interpretation

- A K-number assignment is annotation evidence, not experimental validation. A source-rejected
  prediction is not evidence that the function is absent.
- Strict analysis uses accepted K numbers only. Lenient analysis adds only records explicitly
  classified as `uncertain` by a named, versioned policy.
- Do not compare scores or thresholds across annotation systems unless their semantics are known to
  be comparable.
- Report exact MODULE completion separately from project block coverage. Follow KEGG MODULE logic:
  spaces and plus signs are AND, commas are OR, and minus marks an optional component. Preserve
  parentheses and module references. Preserve unsupported syntax; a required block whose truth
  cannot be established safely because of it is not evaluable and retains a reason. The aggregate
  result is `partially_evaluable` when another required block remains evaluable, and
  `not_evaluable` when none can be evaluated safely.
- Pathway KO coverage is descriptive. Do not call it pathway presence, completeness, activity,
  flux, phenotype, or enrichment. State the reference type and denominator.
- Distinguish genome, MAG, isolate proteome, pangenome, and metagenomic-community analysis units.
  Community results describe pooled encoded potential.
- Describe KO-set comparisons as deterministic set differences, not statistical differential
  function.

## KEGG access and data handling

- Treat KEGG access rights as a release gate. Public KEGG REST access is for eligible academic users
  performing academic work; other deployments require an appropriately licensed endpoint.
- Enforce a deployment-wide rate no greater than three KEGG requests per second across Core and
  Renderer, with a safer no-burst default. Respect endpoint limits, including ten entries per GET.
- Keep cached KEGG payloads, PNG, KGML, and rendered derivatives local and out of version control,
  packages, examples, CI artifacts, and releases. Redistribution requires a separate rights review.
- Retrieve PNG and KGML only through the typed single-pathway asset interface. Never add real
  KEGG-derived assets as fixtures.
- Default local tests remain offline. Pull-request CI keeps the single serialized 120-request
  campaign defined in `tests/live/README.md`; it must not upload KEGG payloads.
- Record retrieval time, endpoint class and request key, parser version, and database release when
  available.

## MCP and security

- Use local stdio and write logs only to stderr or a configured file. Never use `shell=True`.
- Define explicit schemas, schema-conforming structured output, and accurate tool annotations.
- Bound inputs, identifiers, records, URI parameters, previews, retained bytes, and output sizes.
  Expose large results through validated scoped resources or controlled output bundles.
- Treat result identifiers as opaque and scope-isolated; define retention and safe deletion.
- Restrict file paths to configured roots. Reject traversal, unsafe ancestry, replacement races,
  and symlink escapes.
- Redact secrets, environment values, usernames, endpoints, and full local paths from status,
  errors, and logs.
- Keep renderer inputs, XML, images, SVG, output paths, and retained artifacts bounded, static,
  scope-isolated, and free of active or external content.

## Development workflow

- Keep one issue focused on one layer or contract. Before editing, inspect the worktree and preserve
  unrelated changes.
- Change the smallest affected closure: implementation, directly related tests, and documentation
  required by a changed public contract.
- Reuse existing service, schema, error, fixture, and configuration abstractions before adding a
  new path. Replace obsolete paths in the same change instead of retaining parallel compatibility
  implementations.
- Do not add a production module, dependency, public symbol, top-level package, or compatibility
  path unless the acceptance criteria require it.
- More than five production files or roughly 250 net new production lines is a review signal. Split
  independently reviewable behavior and explain unavoidable growth.
- Use small reviewable commits with English messages. Do not claim completion before applicable
  acceptance criteria pass.

### Incremental validation

- Batch coherent edits before validation; do not rerun checks after every patch.
- During implementation, format and lint changed Python files and run the nearest affected test or
  marker with quiet output and short tracebacks.
- Before handoff, run the affected component's relevant unit, contract, and integration tests once.
- Use the full offline profile for shared schemas, cross-component handoffs, packaging, lock files,
  CI, MCP or Skill bindings, security boundaries, KEGG policy, multi-component changes, or an
  explicit maintainer request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

- Pure prose changes need no Python suite unless they alter executable examples, commands,
  generated artifacts, or documented public contracts.
- Keep live KEGG access opt-in locally. Expensive installation, artifact, and live-service checks
  may be deferred to pull-request CI or the release-readiness workflow unless acceptance criteria
  require local evidence.
- Report checks run and checks intentionally deferred. Inspect detailed CI logs only for failed or
  inconclusive jobs.

## Review checklist

- The change stays within assigned scope and preserves component boundaries.
- Schemas preserve provenance, ambiguity, multiple assignments, and conservative interpretation.
- KEGG rights, rate limits, cache locality, and renderer asset boundaries remain enforced.
- MCP outputs and filesystem operations remain bounded, typed, scoped, and safe.
- Tracked content is English; local `*.zh-CN.md` references remain ignored and undistributed.
