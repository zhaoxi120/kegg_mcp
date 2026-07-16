# Release readiness

This document is the evidence checklist for a supported KEGG MCP release. Completing an item in
source code is not sufficient: the release owner must verify every gate against the exact commit
and distributions proposed for publication.

Current status: **validated for the private v0.2.0 GitHub release candidate on 2026-07-15**. The
applicable gates below must be verified against the merged commit before tagging. Candidate
identity and distribution digests belong in the GitHub release notes, not biological workflows.

The current integration amendment was validated on 2026-07-16 with the full core and
companion suites, one four-request authorized KEGG campaign, and one CPU-only companion handoff
against fixed DeepKOALA commit `bebbe0c43f50a26488f7092f6b355aae870a4ed9` using its bundled
`202502` full resource. Final packaging is still rechecked against the exact merged commit.
The earlier expanded 120-request KEGG campaign was additionally checked against the official API
on 2026-07-16: 686 tests passed in 127.04 seconds from the implementation working tree. Current
pull-request CI uses the reduced 20-request profile and is the automatic live gate for a candidate.

Version 0.2.0 supports and is tested only on Python 3.11.x. Its package metadata excludes
Python 3.12 and later; a wider Python range requires a separately tested compatibility change.

## Candidate identity

Record these values in the release issue or signed release notes:

- Git commit and tag;
- package version;
- Python versions and operating systems tested;
- `uv.lock` digest;
- wheel and source-distribution SHA-256 digests;
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

Pull-request CI explicitly runs the serialized 20-request live campaign. A passing test run does
not independently establish KEGG eligibility for another deployment.

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
not install the optional companion, the repository-scoped Skill, or the complete repository
documentation and examples. Verify the companion as its own distribution and verify the Skill
separately from the exact GitHub repository checkout or tag source archive proposed for the
release; do not treat a clean core-wheel installation as either verification.

Install the wheel into a newly created Python 3.11.x environment, confirm that package metadata
rejects Python 3.12 or later, connect an MCP client in the default `public_academic` mode, and verify
all of the following:

1. the server starts and stops cleanly over stdio;
2. tool and resource discovery succeeds;
3. stdout contains only MCP protocol traffic;
4. `get_server_status` reports public-academic mode without a full cache or result-store path;
5. `probe_kegg_connectivity` reports a bounded live connectivity result;
6. the synthetic KO example can be normalized;
7. a failed request remains a technical retrieval error, not a biological absence claim; and
8. unknown, expired, or cross-scope result identifiers fail as `RESULT_NOT_FOUND`.

Pull-request CI makes 20 serialized requests at one request per second with zero retries: five each
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
- [x] The optional companion is independently packaged, imports neither the core package nor
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
- [x] Result and cache paths reject traversal, unsafe parents, and symlink escape.
- [x] Status and errors redact secrets, environment values, usernames, licensed endpoint values,
      and full local paths.
- [x] No command uses `shell=True` and no input content is executed.
- [x] Companion execution is CPU-only, uses fixed arguments, limits threads and output bytes,
      owns the complete child process group, and requires explicit acknowledgement after prepare.
- [x] Cache corruption, network failure, unsupported MODULE syntax, and missing entries remain
      distinguishable from biological absence.
- [x] Private annotation tables and protein sequences are not logged by default.
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
- [x] Reports describe KO coverage as encoded-potential evidence and do not infer expression,
      activity, flux, phenotype, or statistical significance.
- [x] Community-level results are not phrased as completeness within one organism.
- [x] KO-set comparison remains deterministic set comparison, not differential-function
      statistics.

## MCP and Skill gates

- [x] Every approved tool is discoverable with explicit input and output schemas.
- [x] Structured content validates against the declared output schema.
- [x] Tool annotations accurately describe read-only, idempotent, and open-world behavior.
- [x] Fixed resources and resource templates are discoverable and URI parameters are validated.
- [x] Large results return bounded previews and remain retrievable through scoped resources.
- [x] Status and cache-info reads remain side-effect-free and report SQLite statistics as `null`
      with `inspection_status=not_probed` rather than opening or inspecting a database.
- [x] The repository-scoped Skill declares the actual MCP dependency, its deterministic static
      contract tests pass, and the six-prompt forward/manual review is repeated and recorded
      against the exact candidate.
- [x] The Skill never guesses a KO from a sequence, gene name, or protein name.

## Release sign-off

The release owner may mark the candidate ready only after all applicable gates above have evidence
attached to the exact candidate. Any failed applicable gate blocks publication. If a live KEGG
compatibility check cannot be authorized, record it as not run; never silently replace it with an
unauthorized request. The package, contract, and interpretation gates are still mandatory.

## Release notes

The first Python distribution provides a local stdio MCP server for importing KO annotations,
retrieving authorized KEGG references, evaluating MODULE definitions, reporting descriptive
pathway KO coverage, comparing KO sets deterministically, and retrieving bounded full results.
The exact GitHub repository checkout or tag source archive also provides a repository-scoped Codex
Skill that selects workflows and explains evidence limits; the Python wheel and source
distribution do not install that Skill.

The core distribution does not annotate sequences, redistribute KEGG or KOfam data, run
enrichment, infer pathway activity, or provide a remote service. The optional companion is a
separately reviewed local runner for an existing DeepKOALA installation and returns evidence to
the same core importer. Public KEGG REST access is available only after an eligible academic
operator uses the default confirmed academic profile; other users need an appropriately licensed
endpoint.
