# Release readiness

This document is the evidence checklist for a supported KEGG MCP release. Completing an item in
source code is not sufficient: the release owner must verify every gate against the exact commit
and distributions proposed for publication.

Current status: **unreleased core 0.3.0 GitHub candidate as of 2026-07-16**. The only published
GitHub release is core `v0.1.0`; core 0.2.0 was an unpublished intermediate candidate. No package
in this repository is currently published to a Python package registry. Every applicable gate
below must be verified against the exact merged commit before tagging. Candidate identity and
distribution digests belong in the GitHub release notes, not biological workflows.

| Distribution | Source version | Release state | Compatibility |
| --- | --- | --- | --- |
| `kegg-mcp` | `0.3.0` | Unreleased candidate | Produces `RenderInput`; Python 3.11.x |
| `deepkoala-mcp` | `0.2.0` | Unreleased candidate | Controlled detailed-CSV handoff; Python 3.11.x |
| `kegg-render-mcp` | `0.1.0` | Unreleased candidate | Requires `kegg-mcp>=0.3,<0.4`; Python 3.11.x |

The unreleased visualization extension adds a second independently packaged companion and a
renderer-specific gate set. Its implementation uses the three-process boundary and synthetic-only
renderer test policy approved on 2026-07-16. Final publication still requires passing the core,
DeepKOALA companion, renderer companion, and Skill gates against the exact merged commit plus a
specific rights review for any distributed rendered derivative.

The `render_input.json` version 2 handoff first appears in the unreleased core 0.3 series. The
renderer dependency must be `kegg-mcp>=0.3,<0.4`; the published core 0.1 release and unpublished
0.2 candidate are incompatible and must fail dependency resolution rather than reaching a runtime
schema error.

The DeepKOALA provenance correction is the independently distributed companion's 0.2.0 contract.
Its generated CSV and caller-supplied original FASTA are validated separately; 0.1.0 must not be
presented as compatible with that corrected handoff.

Earlier integration evidence was collected on 2026-07-16 with the core and companion suites, an
authorized bounded KEGG campaign, and one CPU-only companion handoff against fixed DeepKOALA
commit `bebbe0c43f50a26488f7092f6b355aae870a4ed9` using its bundled `202502` full resource. That
working-tree evidence does not replace validation of the exact merged release candidate. Current
pull-request CI is the automatic bounded live gate.

The current candidates support and are tested only on Linux with Python 3.11.x. Package metadata
excludes Python 3.12 and later, and macOS and Windows are not release-supported. A wider Python or
platform range requires a separately tested compatibility change.

## Candidate identity

Record these values in the release issue or signed release notes:

- Git commit and tag;
- package version;
- Python versions and operating systems tested;
- `uv.lock` digest;
- wheel and source-distribution SHA-256 digests;
- independent `deepkoala-mcp` and `kegg-render-mcp` versions, lock digests, and distribution
  digests when those companions are included;
- reviewer for KEGG access and data-rights boundaries;
- reviewer for security and privacy boundaries; and
- date of the final test run.

Do not put usernames, local paths, credentials, licensed endpoints, or private biological data in
the release record.

## Required automated validation

GitHub pull-request CI is the authoritative automated gate. Local commands are optional and the
default local pytest invocation does not contact KEGG:

```bash
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
```

The release-contract subset is available for focused auditing:

```bash
uv run --frozen pytest tests/release
```

Pull-request CI explicitly runs the serialized 120-request live campaign. A passing test run does
not independently establish KEGG eligibility for another deployment. The independent renderer job
uses only synthetic offline assets and makes no live KEGG requests. The workflow has no `push`
trigger, so merging to `main` does not repeat either validation campaign.

When the optional companion is part of the candidate, validate its independent distribution with
no installed DeepKOALA checkout or model data required:

```bash
cd companions/deepkoala-mcp
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
uv build --no-sources --out-dir /tmp/deepkoala-mcp-dist
```

Its tests must use synthetic FASTA and fake subprocess fixtures. They must cover path
escape, queue bounds, explicit acknowledgement, timeout, cancellation, process-group cleanup,
output bounds, stdio cleanliness, and controlled core-import handoff.

Validate the renderer as a third, independently locked distribution:

```bash
cd companions/kegg-render-mcp
uv sync --frozen --all-groups
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
uv build --no-sources --out-dir /tmp/kegg-render-mcp-dist
```

Renderer tests and release audits must use generated synthetic PNG, KGML, MODULE contracts, and
handoffs only. They must validate version 2 compatibility, pathway and MODULE rendering semantics,
XML/image/SVG bounds, filesystem and scope isolation, stdio schemas, binary resource MIME types,
and independent wheel/source-distribution contents without contacting KEGG.

## Build and clean-install verification

Build from the exact candidate with dependency resolution forced local after the locked
environment has been synchronized:

```bash
UV_OFFLINE=1 uv build --no-sources --out-dir dist
```

Inspect both archives before publication:

```bash
python -m zipfile --list dist/kegg_mcp-*.whl
tar --list --file dist/kegg_mcp-*.tar.gz
```

The wheel and source distribution must each contain `LICENSE` with the complete MIT license text,
in addition to the package code and metadata. Neither archive may contain a SQLite database, KEGG
response, generated analysis result, secret, model weight, KOfam profile, large biological input,
private fixture, bytecode, or local absolute path.

The core Python wheel and Python source distribution deliver the core MCP Python server. They do
not install either optional companion, either repository-scoped Skill, or the complete repository
documentation and examples. Verify each companion as its own distribution and verify both Skills
separately from the exact GitHub repository checkout or tag source archive proposed for the
release; do not treat a clean core-wheel installation as verification of another component.

The renderer wheel and source distribution must contain only `kegg_render_mcp` implementation,
metadata, and required license material. They must not contain core or DeepKOALA implementation
code, source or rendered KEGG assets, KGML, cache databases, biological inputs, private paths, or
repository Skills. Its compatible `kegg-mcp` range is a dependency boundary, not copied code.

Install the wheel into a newly created Linux Python 3.11.x environment, confirm that package
metadata rejects Python 3.12 or later, connect an MCP client in the default `public_academic` mode,
and verify all of the following:

1. the server starts and stops cleanly over stdio;
2. tool and resource discovery succeeds;
3. stdout contains only MCP protocol traffic;
4. `get_server_status` reports public-academic mode without a full cache or result-store path;
5. `probe_kegg_connectivity` reports a bounded live connectivity result;
6. the synthetic KO example can be normalized;
7. a failed request remains a technical retrieval error, not a biological absence claim; and
8. current-scope deletion succeeds and unknown, expired, deleted, or cross-scope identifiers fail
   as `RESULT_NOT_FOUND`;
9. normal stdio shutdown removes the current result scope; and
10. a non-empty output directory fails as `OUTPUT_ALREADY_EXISTS` without changing existing
    content.

Pull-request CI makes 120 serialized requests at one request per second with zero retries: 30 each
for `INFO`, `GET`, `LINK`, and `CONV`. Record the endpoint class and number of requests, but do not
publish response bodies. An authorized manual run may configure 1 through 30 requests per
operation when stronger repetition is justified. Record a successful campaign against the exact
candidate commit before release.

## Data-rights gates

- [x] Setup presents the default `public_academic` and optional `licensed` choices.
- [x] Public access defaults to an affirmative academic-use configuration.
- [x] Licensed access cannot start without explicit authorized-use confirmation and an HTTPS
      endpoint distinct from the public endpoint.
- [x] The software and documentation do not claim to validate an institution's license.
- [x] The MIT license is described as applying to project source code only.
- [x] No KEGG payload, cache database, bulk identifier export, KGML collection, or pathway image is
      tracked or packaged.
- [x] No DeepKOALA weight, KOfam profile, annotation database, or third-party model code is tracked
      or packaged.
- [x] No real KEGG PNG, KGML, rendered derivative, or cache payload is used as a renderer fixture,
      uploaded by CI, or packaged in either renderer archive.
- [x] Source pathway assets remain local, and redistribution of rendered derivatives requires a
      separate rights review rather than relying on the MIT source-code license.
- [x] The DeepKOALA companion is independently packaged, imports neither the core package nor
      PyTorch, and never installs or downloads external code, weights, profiles, or data.
- [x] Local cached payloads are excluded from version control, examples, CI artifacts, and
      releases.
- [x] External-system statements cite the KEGG API and legal pages with a retrieval date.

The KEGG API usage restriction, rate limit, and legal pages were last reviewed for this checklist
on 2026-07-14:

- [KEGG API overview](https://www.kegg.jp/kegg/rest/)
- [KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html)
- [KEGG copyright and licensing notice](https://www.kegg.jp/kegg/legal.html)

This checklist is operational guidance, not legal advice.

## Security and privacy gates

- [x] The release exposes stdio transport only; no remote HTTP listener is enabled.
- [x] Logs and diagnostics use stderr or a configured file, never protocol stdout.
- [x] Tool inputs, output previews, URI parameters, identifier counts, and retained bytes are
      bounded.
- [x] Result identifiers are opaque, scope isolated, time limited, and treated as untrusted.
- [x] Normal stdio shutdown deletes the current result scope; the active-result TTL also bounds
      orphan rows after abnormal termination, while output bundles are the durable artifact.
- [x] Current-scope result deletion and expired-only operator cleanup do not reveal or evict other
      active scopes.
- [x] Result and cache paths reject traversal, unsafe parents, and symlink escape.
- [x] Output bundles reject non-empty targets, never replace an existing entry, commit the manifest
      last, and redact source paths in the manifest by default.
- [x] Status and errors redact secrets, environment values, usernames, licensed endpoint values,
      and full local paths.
- [x] No command uses `shell=True` and no input content is executed.
- [x] Companion execution is CPU-only, uses fixed arguments, limits threads and output bytes,
      owns the complete child process group, and requires explicit acknowledgement after prepare.
- [x] Cache corruption, network failure, unsupported MODULE syntax, and missing entries remain
      distinguishable from biological absence.
- [x] Private annotation tables and protein sequences are not logged by default.
- [x] Renderer input, XML, image dimensions and pixels, SVG nodes and bytes, artifact paths,
      retained bytes, and disk use are explicitly bounded.
- [x] Renderer XML resolution is closed; generated SVG has no scripts, event handlers, active
      links, remote fonts, or external image resources.
- [x] Renderer state, allowed roots, and outputs reject traversal, unsafe ancestry, and symlink
      escape, while scoped IDs do not reveal cross-process result existence.
- [x] The vulnerability-reporting path appropriate to the candidate visibility is verified: for
      this private repository, `SECURITY.md` documents the collaborator-only issue boundary; before
      any public supported release, GitHub private vulnerability reporting must be enabled and its
      **Report a vulnerability** flow verified.

## Scientific and reporting gates

- [x] Raw source decisions and multiple assignments per sequence remain available in full results.
- [x] Detailed DeepKOALA evidence is not inferred from simple output.
- [x] Rejected predictions never enter lenient analysis by default.
- [x] Exact MODULE completion and project block coverage are distinct fields with distinct names.
- [x] Unsupported, malformed, cyclic, or unavailable MODULE content cannot become a false complete
      or incomplete result.
- [x] Every pathway ratio includes its reference namespace, numerator, denominator, and retrieval
      provenance.
- [x] `AnalysisExecutionProvenance` version 2 records MODULE analysis limits, pathway parameters,
      pathway coverage limits, and report limits used to construct the renderer handoff.
- [x] Reports describe KO coverage as encoded-potential evidence and do not infer expression,
      activity, flux, phenotype, or statistical significance.
- [x] Accepted and policy-defined uncertain rendering evidence remain visually distinct; rejected
      predictions are excluded and unchanged pathway graphics are not labelled as biological
      absence.
- [x] Renderer diagrams display exact MODULE completion separately from project block coverage and
      preserve unsupported, unresolved, cyclic, optional, grouping, and reference states.
- [x] Community-level results are not phrased as completeness within one organism.
- [x] KO-set comparison remains deterministic set comparison, not differential-function
      statistics.

## MCP and Skill gates

- [x] Every approved tool is discoverable with explicit input and output schemas.
- [x] Structured content validates against the declared output schema.
- [x] Tool annotations accurately describe local cache, retained-result, output-bundle, deletion,
      idempotence, and open-world behavior.
- [x] `delete_analysis_result` can delete only the current scope and advertises destructive,
      idempotent, closed-world behavior.
- [x] Fixed resources and resource templates are discoverable and URI parameters are validated.
- [x] Large results return bounded previews and remain retrievable through scoped resources.
- [x] Status and cache-info reads remain side-effect-free and report SQLite statistics as `null`
      with `inspection_status=not_probed` rather than opening or inspecting a database.
- [x] The repository-scoped Skill declares the actual MCP dependency, its deterministic static
      contract tests pass, and the six-prompt forward/manual review is repeated and recorded
      against the exact candidate.
- [x] The Skill never guesses a KO from a sequence, gene name, or protein name.
- [x] The core exposes a typed, immutable renderer handoff but no rendering tool; the renderer
      neither normalizes KO evidence nor recomputes MODULE or pathway results.
- [x] The independent renderer exposes explicit tool schemas, accurate open-world annotations,
      a fixed status resource, scoped artifact templates, binary PNG resources, and deletion.
- [x] The visualization Skill declares the actual core and renderer dependencies and contains no
      inference, normalization, KGML parsing, pixel manipulation, or rendering code.

## Release sign-off

The release owner may mark the candidate ready only after all applicable gates above have evidence
attached to the exact candidate. Any failed applicable gate blocks publication. If a live KEGG
compatibility check cannot be authorized, record it as not run; never silently replace it with an
unauthorized request. The package, contract, and interpretation gates are still mandatory.

## Release notes

The first Python distribution provides a local stdio MCP server for importing KO annotations,
retrieving authorized KEGG references, evaluating MODULE definitions, reporting descriptive
pathway KO coverage, comparing KO sets deterministically, and retrieving bounded full results.
The exact GitHub repository checkout or tag source archive also provides repository-scoped Codex
Skills for analysis and visualization routing; the Python wheel and source distribution do not
install either Skill.

The core distribution does not annotate sequences, redistribute KEGG or KOfam data, run
enrichment, infer pathway activity, or provide a remote service. The DeepKOALA companion is a
separately reviewed local runner for an existing installation and returns evidence to the same
core importer. Public KEGG REST access is available only after an eligible academic operator uses
the default confirmed academic profile; other users need an appropriately licensed endpoint.

The visualization extension adds `kegg-render-mcp` as another separately reviewed local stdio
distribution. The core writes a complete renderer-specific version 2 handoff; the renderer creates
bounded static regular-pathway overlays and project-owned MODULE diagrams without changing the
analysis. Source assets remain local, tests are synthetic, global and overview maps are unsupported,
and no output constitutes evidence of pathway activity, flux, phenotype, or experimental
validation.
