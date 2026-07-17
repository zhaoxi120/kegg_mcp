# Release readiness

Use this checklist against the exact commit and distributions proposed for a GitHub release.
Passing source tests is necessary but does not authorize KEGG access or redistribution of KEGG
content.

Current status: validate the merged commit before tagging. Record package digests and final CI
evidence in the GitHub release notes, not in biological workflows or repository history files.

## Distribution boundary

| Distribution | Source version | Contract |
| --- | --- | --- |
| `kegg-mcp` | `0.3.0` | Core analysis server and `RenderInput` producer |
| `deepkoala-mcp` | `0.2.0` | Optional controlled detailed-CSV handoff |
| `kegg-render-mcp` | `0.1.0` | Optional renderer requiring `kegg-mcp>=0.3,<0.4` |

All distributions support Linux with CPython 3.11.x. They are independently installed and
locked. The core Python wheel does not install either companion or either repository-scoped
Skill. Use an exact GitHub checkout or tag source archive when the Skills are required.

The core produces `render_input.json` schema version 2 and preserves
`AnalysisExecutionProvenance` version 2. These are data contracts, not package-history labels.
The renderer consumes authoritative analysis and never normalizes evidence or recomputes MODULE
completion or pathway coverage.

## Release identity

Record the following in the release notes:

- exact Git commit and tag;
- operating system and Python version used for validation;
- versions and SHA-256 digests for all published wheels and source distributions;
- `uv.lock` digest for each included distribution;
- final pull-request CI result;
- KEGG access and data-rights reviewer; and
- security and privacy reviewer.

Do not publish usernames, credentials, licensed endpoints, local paths, KEGG payloads, private
biological data, cache databases, or retained results.

## Automated validation

The core validation commands are:

```bash
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
```

The default local suite is offline. Pull-request CI runs one serialized live campaign with 30
requests each for `INFO`, `GET`, `LINK`, and `CONV`, one request per second, zero retries, and no
uploaded KEGG payloads. The workflow has no `push` trigger, so merge does not repeat the campaign.

Validate the DeepKOALA companion independently:

```bash
cd companions/deepkoala-mcp
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
uv build --no-sources
```

Validate the renderer independently with synthetic assets only. These checks make no live KEGG
requests:

```bash
cd companions/kegg-render-mcp
uv sync --frozen --all-groups
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
uv build --no-sources
```

## Build audit

Build every included distribution from the exact merged commit. Inspect both its wheel and source
distribution before upload. Each archive must include the complete MIT license and must exclude:

- SQLite databases, KEGG responses, KGML, pathway images, or rendered derivatives;
- DeepKOALA weights, KOfam profiles, model code, or annotation databases;
- secrets, local absolute paths, private fixtures, biological inputs, or generated results;
- bytecode, virtual environments, caches, or repository metadata; and
- another distribution's implementation or either repository-scoped Skill.

Clean-install each wheel in a Linux CPython 3.11 environment and verify stdio startup, tool and
resource discovery, schema-conforming output, clean protocol stdout, redacted status, scoped
result deletion, and safe output-bundle behavior.

## Rights and data gates

- Public KEGG REST access is presented only for eligible academic users performing academic work.
- Licensed deployments require explicit confirmation and an authorized HTTPS endpoint.
- The process-wide request rate cannot exceed three requests per second and defaults to a safer
  no-burst rate.
- No KEGG payload, source asset, cache database, or bulk export is tracked, packaged, or uploaded
  by CI.
- Rendered derivatives require a separate rights review before redistribution.
- The MIT license covers project source code only.
- External-system statements cite current primary sources and record the retrieval date when they
  affect a parser, schema, fixture, or acceptance test.

Primary KEGG sources reviewed for these gates on 2026-07-14:

- [KEGG API overview](https://www.kegg.jp/kegg/rest/)
- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html)
- [KEGG legal notice](https://www.kegg.jp/kegg/legal.html)

This checklist is operational guidance, not legal advice.

## Security and privacy gates

- The servers expose local stdio transport only and never use `shell=True`.
- Input, identifiers, URI parameters, references, output, retained bytes, and direct summaries are
  bounded.
- Allowed-root paths reject traversal, unsafe ancestry, and symlink escape.
- Output bundles and renderer exports never replace existing entries, publish the manifest last,
  and roll back files installed by a failed operation.
- Result identifiers are opaque, scope-isolated, time-limited, and safely indistinguishable when
  unknown, expired, deleted, or cross-scope.
- Status, logs, and errors redact secrets, environment values, usernames, endpoints, and full
  local paths.
- Renderer XML, images, SVG, artifacts, and resources are bounded, static, and free of active or
  external content.
- DeepKOALA execution is CPU-only, explicitly configured, process-group controlled, bounded, and
  unable to install or download dependencies or data.
- The vulnerability-reporting route in `SECURITY.md` is verified for the repository visibility.

## Scientific and contract gates

- Raw source decisions, ambiguity, multiple assignments, and provenance remain available.
- Rejected predictions never enter lenient analysis; only policy-defined uncertain evidence may
  enter it.
- Exact MODULE completion and block coverage remain separate results.
- Unsupported MODULE syntax returns `not_evaluable` with a reason.
- Every pathway ratio records reference type, numerator, denominator, and retrieval provenance.
- Reports do not infer pathway presence, activity, flux, phenotype, experimental validation, or
  statistical significance from KO coverage.
- Community-level results describe pooled encoded potential, not completeness in one organism.
- KO-set comparisons remain deterministic set differences.
- MCP tools and resources expose explicit schemas, accurate annotations, bounded previews, and
  validated scoped retrieval.
- The Skills orchestrate declared MCP tools without duplicating normalization, analysis,
  inference, job control, KGML parsing, or rendering.

## Publication

After all gates pass for the exact merged commit:

1. confirm the tag does not already exist;
2. create final wheels and source distributions in clean directories;
3. calculate and record SHA-256 digests;
4. create an annotated tag on the merged commit;
5. create the GitHub release with concise baseline notes and attach only audited archives; and
6. verify the published tag, assets, checksums, and source archive.

Any failed applicable gate blocks publication. If a live KEGG campaign is not authorized, record
it as not run; never replace it with unauthorized access.
