# Release readiness

This document is the evidence checklist for the first KEGG MCP release. Completing an item in
source code is not sufficient: the release owner must verify every gate against the exact commit
and distributions proposed for publication.

Current status: **signed off for the private v0.1.0 GitHub release on 2026-07-15**. The applicable
gates below were verified against the release candidate. Candidate identity and artifact digests
are recorded in the GitHub release notes.

Version 0.1.0 supports and is tested only on Python 3.11.x. Its package metadata excludes
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
5. the synthetic KO example can be normalized;
6. an absent reference produces `OFFLINE_CACHE_MISS`, not a biological absence claim; and
7. unknown, expired, or cross-scope result identifiers fail as `RESULT_NOT_FOUND`.

Then, only if the release reviewer is an eligible academic user performing academic work or is
using an appropriately licensed endpoint, run the separately approved minimal live smoke test.
Record the endpoint class and number of requests, but do not publish response bodies. The smoke
test remains outside the default test suite. A guarded live CI compatibility campaign does not by
itself authorize or replace this release-specific review.

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
- [x] Local `*.zh-CN.md` reference documents are ignored and absent from GitHub, Python
      distributions, source archives, release artifacts, examples, and CI artifacts.
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

## Unreleased DeepKOALA companion change control

Current status: the optional `deepkoala-mcp` companion is implemented under an explicit maintainer
request as a separate distribution, stdio entry point, environment contract, runner process, lock
file, test suite, and artifact boundary. It has not received independent release sign-off and is
not part of the signed core v0.1.0 candidate. Its presence in the repository does not alter any
completed checkbox or sign-off above.

The implementation exposes exactly six schema-defined tools:
`get_deepkoala_runner_status`, `prepare_deepkoala_job`, `submit_deepkoala_job`,
`get_deepkoala_job`, `cancel_deepkoala_job`, and `delete_deepkoala_job`. Preparation privately
stages and hashes bounded FASTA without inference. Submission requires literal
`acknowledged=true`, the same time-limited `plan_id`, and the exact notice SHA-256 after the client
has displayed the complete notice and obtained explicit user confirmation.

The current implementation provides the following release evidence, which must be reverified
against an exact companion candidate:

- its distribution depends on MCP and Pydantic only and excludes the core package, DeepKOALA,
  PyTorch, weights, KOfam profiles, HMMER, generated FASTA, and generated results;
- it declares a POSIX-only platform contract and fails startup before path handling or state
  creation when required process-group or file-size-limit operations are unavailable;
- it always requests detailed output, rejects `multi=true`, never downloads or replaces weights,
  fixes `cpu_threads=2` by default, and enforces `max_concurrent_jobs=1` as a hard limit;
- it uses fixed argument vectors without `shell=True`, a pre-execution hard output-file limit,
  private state, allowed-root and symlink validation, process-group cancellation and timeout,
  bounded sanitized diagnostics, retention, explicit terminal deletion, and retryable cleanup;
- status, notices, errors, and provenance expose path-free identities and hashes rather than local
  paths, environment values, sequence content, or secrets;
- input and output are capped at 5,000,000 bytes, diagnostics at 65,536 bytes, direct resource
  content at 64 KiB, and binary-safe resource ranges at 1 MiB; status exposes effective FASTA
  structure limits, and job summaries explicitly mark truncated diagnostic tails;
- successful jobs expose detailed output, provenance, and diagnostics only through opaque,
  process-scoped resources, plus a source-agnostic core importer handoff; and
- on 2026-07-15, both bundled `202502` full and fragment models completed an explicit CPU-only
  smoke run against official DeepKOALA commit
  `bebbe0c43f50a26488f7092f6b355aae870a4ed9` using the existing Python 3.11/PyTorch
  `2.9.1+cu130` environment, `torch.cuda.is_available() == false`, two CPU threads,
  `batch_size=1`, `num_workers=0`, `topk=1`, and no download or new environment. Both terminal
  jobs were deleted and the temporary state root was empty afterward.

Terminal deletion is safely replayable only for the 1,024 most recent process-local tombstones.
The delete tool must not advertise unconditional idempotence because eviction or process restart
causes a later retry to return `JOB_NOT_FOUND`.

Before an independent companion release, run its locked validation from its own project root:

```bash
cd companions/deepkoala-mcp
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
uv build --offline --no-python-downloads --no-sources --no-progress --no-create-gitignore \
  --out-dir dist
```

The companion release remains blocked until the exact candidate receives independent dependency,
license, security/privacy, process-isolation, clean-install, distribution-content, MCP stdio, and
full/fragment CPU compatibility review. Any authorized accelerator test must be separately
approved and must verify device disclosure and resource contention; the existing CPU smoke is not
GPU evidence. Inspect companion wheel and source archives independently and record their hashes.
Never add them to the signed core v0.1.0 artifact set retroactively.

The Skill may orchestrate the companion only after MCP discovery, explicit configuration, and user
selection. It must not implement inference, subprocess control, weight installation, or silent
downloads. On success, the client must decode `content_base64`, assemble any paginated companion
output in order, verify its total byte count and SHA-256 digest, and pass the verified detailed CSV
plus the returned source-provenance template to the existing core importer. The core server cannot
dereference a session-private resource owned by another MCP server, and the companion must not
replace the existing normalization policy.

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

The signed core v0.1.0 release does not annotate sequences, redistribute KEGG or KOfam data, run
enrichment, infer pathway activity, or provide a remote service. The unreleased optional companion
does not change that release statement. Public KEGG REST access is available only after an
eligible academic operator explicitly confirms academic use; other users need an appropriately
licensed endpoint or authorized offline cache content.
