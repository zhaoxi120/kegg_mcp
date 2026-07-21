# Release readiness

Use this checklist against the exact commit and distributions proposed for a GitHub release.
Passing source tests is necessary but does not authorize KEGG access or redistribution of KEGG
content.

Current status: validate the exact merged release-candidate commit before creating a new tag. Never
reuse a version identifier already present in a published release. Record final CI evidence in the
GitHub release notes, not in biological workflows or repository history files.

## Distribution boundary

| Distribution | Source version | Contract |
| --- | --- | --- |
| `kegg-mcp` | `0.5.0` | Core analysis server and `RenderInput` producer |
| `deepkoala-mcp` | `0.4.0` | Optional controlled detailed-CSV handoff |
| `kegg-render-mcp` | `0.3.0` | Optional renderer requiring `kegg-mcp>=0.5,<0.6` |

All distributions support Linux with CPython 3.11.x. They remain independently packaged, locked,
installed into separate runtimes, and executed as separate stdio processes even when provisioned
together. The primary Codex deployment path is `scripts/install-suite.py`, which creates those
three runtimes in one transaction and generates one local plugin containing the three canonical
Skills and three absolute MCP launch commands. The plugin is a local deployment artifact, not a
fourth published distribution. The core Python wheel still does not install either companion or
any repository-scoped Skill.

The suite installer is the only supported Codex installation path. The generated-plugin path
targets only the Codex app and Codex CLI and remains release-gated until the exact acceptance
evidence below is recorded; other MCP clients require explicit manual server registration.
Validate a newly installed plugin in a new Codex task.

The core produces `render_input.json` schema version 3 and preserves
`AnalysisExecutionProvenance` version 3 in output-bundle schema version 3. These are data
contracts, not package-history labels.
The renderer consumes authoritative analysis and never normalizes evidence or recomputes MODULE
completion or pathway coverage.

## Release identity

Record the following in the release notes:

- exact Git commit and tag;
- operating system and Python version used for validation;
- versions for all published wheels and source distributions;
- generated plugin and runtime identities recorded by the installer, with private paths redacted
  from published notes;
- the `uv`, Git, and Codex CLI versions used for suite-installation validation;
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

The default local test campaign is offline. Pull-request CI runs one serialized live campaign with
30 requests each for `INFO`, `GET`, `LINK`, and `CONV`, one request per second, zero retries, and
no uploaded KEGG payloads. The workflow has no `push` trigger, so merge does not repeat the campaign.
Maintainer server runs that must not contact KEGG can additionally select the explicit
`KEGG_MCP_ACCESS_MODE=offline_cache` profile; the external unconfigured default remains
`public_academic`.

Renderer release validation must cover all four access modes: `public_academic`, `licensed`,
`offline_cache`, and `unconfigured`. Offline-cache tests must prove that a missing cache is not
created, no HTTP transport or rate limiter is invoked, the existing database is opened read-only,
public and confirmed licensed namespaces remain isolated, stale entries are denied by default,
and status, errors, probes, and provenance do not disclose a path, endpoint, or fingerprint.

Validate `scripts/install-suite.py` independently from all three Python distributions. Use a
reviewed release source tree and a strict deployment TOML that is a direct, non-symlink regular
file owned by the invoking user with no group or other permission bits inside a direct owner-only
parent directory. Cover
unknown keys, wrong types, unsafe ownership or mode, symlink replacement, unsafe ancestry,
relative or missing paths, overlapping private roots, uncovered handoff roots, existing-target
conflicts, interrupted publication, rollback, and preservation of
pre-existing private state.

Prove that the suite installer creates three distinct frozen runtimes and registers three direct
absolute launch commands in a generated local Codex plugin without copying private configuration
into the plugin cache. The default path must force `uv` offline and may succeed only when every
artifact needed by the checked-in lockfiles and their declared build requirements is already
available. The sole dependency-network opt-in, `--allow-locked-dependency-downloads`, may allow
only
`uv` network access while resolving or downloading artifacts required by those lockfiles and
declared build requirements. It must not
update lockfiles, select additional runtime dependency groups, or download Python, uv, Codex,
repository source, later DeepKOALA model weights, KOfam profiles, KEGG data, or KEGG assets. The
separate `--allow-deepkoala-install` confirmation may perform the initial official DeepKOALA clone
and upstream-requirements installation once for each new suite installation root. Later FASTA jobs
in that installed deployment must not repeat the installation question. Verify that its bundled
`202502` resources are selected by default and reported in the successful handoff and run report.

Validate the generated plugin through the supported Codex app or Codex CLI path, then start a new
Codex task and confirm discovery of all three Skills and MCP servers. Record conflicts, rollback
failures, validator failures, and discovery failures as release blockers. A generated file tree,
successful installer exit, or manual MCP registration is not evidence that the exact suite
revision is supported.

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
- another distribution's implementation or any repository-scoped Skill.

The automatically generated GitHub tag source archives are reviewed sources for the three
repository Skills, but none is a Python package artifact or a supported standalone Skill
installer. The suite installer and launcher are release-audited source. Reproduce the local plugin
from the reviewed release source tree; do not attach a second, independently assembled Skill or
plugin bundle. Audit the generated plugin manifest and MCP
configuration for absolute launchers, absence of secrets and private configuration, and binding to
the reviewed source and runtime identities.

CI clean-installs each freshly built wheel in a temporary Linux CPython 3.11 environment, runs
package import from outside the checkout, and checks console `--version` where the distribution has
a non-server CLI. Before publication, additionally verify stdio startup, tool and resource
discovery, schema-conforming output, clean protocol stdout, redacted status, scoped result deletion,
and safe output-bundle behavior.

## Rights and data gates

- Public KEGG REST access is presented only for eligible academic users performing academic work.
- Licensed deployments require explicit confirmation and an authorized HTTPS endpoint.
- The deployment-wide request rate across Core and Renderer cannot exceed three requests per
  second and defaults to a safer no-burst rate.
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

Official Codex plugin documentation used for the generated local-plugin release gates was
reviewed on 2026-07-19:

- [Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins)

Those plugin gates apply only to the Codex app and Codex CLI; they do not establish installation
behavior for another MCP client.

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
- Offline Core and Renderer access never creates, initializes, migrates, cleans, or writes the
  cache database, never invokes HTTP, and fails closed on unsafe permissions, schema, journal, or
  size metadata.
- The suite installer accepts only a strict owner-only, non-symlink deployment TOML and keeps that
  configuration, generated private runtime metadata, endpoints, and private roots outside the
  Codex plugin cache.
- For caught failures and keyboard interruption, suite publication rolls back across the three
  runtimes and every registration it can prove belongs to the transaction. A hard process kill,
  host crash, or power loss must be detected through the incomplete marker and handled through the
  bounded manual recovery procedure. Neither path may silently replace an unrelated installation
  or delete user inputs, outputs, caches, external tools, or model resources.
- Generated MCP launchers use validated absolute paths and direct execution without a shell,
  ambient executable lookup, activation command, or undocumented plugin-root expansion.
- Installer dependency networking is disabled by default. Its explicit opt-in is limited to `uv`
  network access while resolving or downloading artifacts required by checked-in lockfiles and
  declared build requirements and cannot acquire Python, uv, Codex, repository source, later model
  weights, KOfam profiles, or KEGG content. The distinct confirmed DeepKOALA first-install path is
  limited to the official repository and its declared Python requirements.
- Renderer XML, images, SVG, artifacts, and resources are bounded, static, and free of active or
  external content.
- DeepKOALA execution uses a service-owned automatic-device policy, inherits existing accelerator
  visibility, holds one owner-only state root exclusively, remains process-group and parent-death
  controlled, and cannot install or download dependencies or data.
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

1. reproduce the suite installation from the exact release candidate, record its gated evidence,
   and verify the generated plugin in a new Codex task;
2. confirm the tag does not already exist;
3. create final wheels and source distributions in clean directories;
4. create an annotated tag on the merged commit;
5. create the GitHub release with concise baseline notes and attach only audited archives; and
6. verify the published tag, assets, and source archive.

Any failed applicable gate blocks publication. If a live KEGG campaign is not authorized, record
it as not run; never replace it with unauthorized access.
