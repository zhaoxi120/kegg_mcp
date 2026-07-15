# Release readiness

This document is the evidence checklist for a supported KEGG MCP release. Completing an item in
source code is not sufficient: the release owner must verify every gate against the exact commit
and distributions proposed for publication.

Current status: **validated for the private v0.2.0 GitHub release candidate on 2026-07-15**. The
applicable gates below must be verified against the merged commit before tagging. Candidate
identity and distribution digests belong in the GitHub release notes, not biological workflows.

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
- date of the final offline test run.

Do not put usernames, local paths, credentials, licensed endpoints, or private biological data in
the release record.

## Required automated validation

Run the locked validation suite with network access disabled or externally blocked:

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

The default test suite must not contact KEGG. A passing test run does not authorize a live smoke
test and does not establish KEGG eligibility.

## Build and clean-install verification

Build from the exact candidate with dependency resolution forced offline after the locked
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

The Python wheel and Python source distribution deliver the MCP Python server. They do not install
the repository-scoped Skill or ship the complete repository documentation and examples. Verify the
Skill separately from the exact GitHub repository checkout or tag source archive proposed for the
release; do not treat a clean wheel installation as a Skill installation test.

Install the wheel into a newly created Python 3.11.x environment, confirm that package metadata
rejects Python 3.12 or later, connect an MCP client in `offline_cache` mode, and verify all of the
following without network access:

1. the server starts and stops cleanly over stdio;
2. tool and resource discovery succeeds;
3. stdout contains only MCP protocol traffic;
4. `get_server_status` reports offline mode without a full cache or result-store path;
5. `probe_kegg_connectivity` reports `disabled` without making a request in offline mode;
6. the synthetic KO example can be normalized;
7. an absent reference produces `OFFLINE_CACHE_MISS`, not a biological absence claim; and
8. unknown, expired, or cross-scope result identifiers fail as `RESULT_NOT_FOUND`.

Then, only if the release reviewer is an eligible academic user performing academic work or is
using an appropriately licensed endpoint, run the separately approved minimal live smoke test.
Record the endpoint class and number of requests, but do not publish response bodies. A live smoke
test must remain outside CI and the default test suite.

## Data-rights gates

- [x] Setup prominently presents `public_academic`, `licensed`, and `offline_cache` choices.
- [x] Public access cannot start without explicit academic-use confirmation.
- [x] Licensed access cannot start without explicit authorized-use confirmation and an HTTPS
      endpoint distinct from the public endpoint.
- [x] The software and documentation do not claim to validate an institution's license.
- [x] The MIT license is described as applying to project source code only.
- [x] No KEGG payload, cache database, bulk identifier export, KGML collection, or pathway image is
      tracked or packaged.
- [x] No DeepKOALA weight, KOfam profile, annotation database, or third-party model code is tracked
      or packaged.
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
unauthorized request. The offline package, contract, and interpretation gates are still mandatory.

## Release notes

The first Python distribution provides a local stdio MCP server for importing KO annotations,
retrieving authorized KEGG references, evaluating MODULE definitions, reporting descriptive
pathway KO coverage, comparing KO sets deterministically, and retrieving bounded full results.
The exact GitHub repository checkout or tag source archive also provides a repository-scoped Codex
Skill that selects workflows and explains evidence limits; the Python wheel and source
distribution do not install that Skill.

The release does not annotate sequences, redistribute KEGG or KOfam data, run enrichment, infer
pathway activity, or provide a remote service. Public KEGG REST access is available only after an
eligible academic operator explicitly confirms academic use; other users need an appropriately
licensed endpoint or authorized offline cache content.
