# MCP server

The core MVP runs as a local stdio server. It never launches DeepKOALA or another annotation tool,
and it does not expose remote HTTP transport.

## Companion annotation boundary

Automatic FASTA-to-KO assignment is not one of this server's eight version 0.1.0 tools. The
repository now contains an optional, separately installed `deepkoala-mcp` 0.1.0 candidate whose
runner owns the annotation subprocess, device and weight notice, queue, cancellation, timeout, and
cleanup. The candidate is unreleased and is not part of the supported core 0.1.0 release. It uses
a separate distribution, entry point, environment, lifecycle, and release review; the core
`kegg-mcp` process continues to import only detailed annotation evidence.

The repository-scoped Skill may orchestrate both servers after the companion is explicitly
configured and discovered, but it must not contain inference or subprocess implementation. A
successful companion result must pass through the existing DeepKOALA detailed importer and named
decision policy. Companion tools and schemas are a separate candidate contract, not an addition to
the core 0.1.0 contract.

### Companion tools

The companion exposes six tools:

| Tool | Behavior |
| --- | --- |
| `get_deepkoala_runner_status` | Return redacted installation and resource readiness, supported devices, bounds, retention, and queue counts. |
| `prepare_deepkoala_job` | Validate exactly one inline or allowlisted path FASTA, copy it to private state, resolve the execution identity, and return a notice without inference. |
| `submit_deepkoala_job` | Start or queue the exact prepared plan only with its `notice_sha256` and `acknowledged=true`; repeated submission of the same accepted plan is idempotent. |
| `get_deepkoala_job` | Return current lifecycle state and, after success, a source-agnostic core-import handoff. |
| `cancel_deepkoala_job` | Cancel a queued job or terminate and reap a running process group. |
| `delete_deepkoala_job` | Delete one terminal job and its retained artifacts; bounded process-local tombstones permit limited retry. |

Preparation and submission are deliberately separate. The execution notice includes the FASTA
digest and summary, configured interpreter identity, DeepKOALA source identity, model weight and
configuration identities, requested and resolved device, effective settings, queue disposition,
weight source, and updated-weight URL. A caller must display that complete notice before it sends
the exact digest and acknowledgement. Any identity change makes the notice stale.

The candidate supports `full` and `frag`, always runs with detailed output, and rejects
`multi=true`. Defaults mirror the supported upstream command contract: `model=full`,
`model_date=latest`, `device=auto`, `batch_size=32`, `num_workers=2`, and `topk=1`.
`device=auto` may select an available GPU. CPU-only callers must request `device="cpu"`; a small
diagnostic run can also use `batch_size=1`, `num_workers=0`, and a configured two-thread limit.
Independent job concurrency is fixed at one and is distinct from inference `batch_size`.

### Companion resources and handoff

The companion fixed resource is:

- `deepkoala-job://status`

Its scoped templates are:

- `deepkoala-job://jobs/{job_id}/{section}`
- `deepkoala-job://jobs/{job_id}/{section}/{offset}/{limit}`

The allowed sections are `output`, `provenance`, and `diagnostics`. Artifacts up to 64 KiB are
returned inline. A larger artifact returns an `artifact_requires_pagination` notice. Each range is
at most 1 MiB and contains base64 content, exact offsets and lengths, the full-artifact SHA-256
digest, and a continuation URI. The client must concatenate decoded pages in order and verify the
complete digest.

Successful `get_deepkoala_job` output includes the detailed-CSV resource URI and a
`source_provenance_template` accepted by the core importer. A resource URI is private to the
companion server; the core server cannot dereference it. The client or Skill reads and verifies the
CSV, then calls core `normalize_ko_annotations` with the decoded bytes as inline `text`,
`input_format="deepkoala_detailed"`, and the provided template as `source`. This preserves one
normalization policy and does not create a companion-specific KO evidence model.

Every job summary includes `diagnostics_truncated`. It can be true only for a terminal job with a
declared `diagnostic_uri`, and it means that the resource contains a sanitized bounded tail rather
than the complete child-process output.

`delete_deepkoala_job` is safely replayable only while its identifier remains among the 1,024 most
recent process-local deletion tombstones. Tombstone eviction or a server restart changes a later
retry to `JOB_NOT_FOUND`, so the tool intentionally does not declare an unconditional
`idempotentHint`.

### Companion process and storage bounds

The companion candidate is POSIX-only. Startup requires POSIX process-group and file-size-limit
support before it handles configured paths or creates private state. The former makes cancellation,
timeout, and shutdown cover the complete external job; the latter bounds output before upstream
code runs.

The companion never downloads or replaces weights. `DEEPKOALA_MCP_WEIGHT_SOURCE` records either
`github_bundled` or `user_provided`; it is not a download selector. The fixed runner argument
vector does not use a shell. The staged FASTA, configured Python interpreter, source tree,
weights, and model configuration are hashed before launch, and their identities are checked again
around execution.

Status is a redacted structural readiness check; preparation is the authoritative per-job device
and artifact preflight. Hashes provide provenance and change detection, not a trust or sandbox
boundary: the configured external interpreter and checkout execute with the current user's
permissions and must be operator-reviewed.

Preparation does not load model weights or prove that an installed weight/configuration pair is
semantically valid. It binds the selected artifact identities and resolved device into the notice;
the acknowledged inference can still fail under the bounded lifecycle.

Input and output are each capped at 5,000,000 bytes, diagnostics at 65,536 bytes, sequence count at
100,000, and individual sequence length at 100,000 residues. These hard maxima can be lowered by
environment configuration. Runner status exposes the effective `max_sequences`, `max_residues`,
`max_sequence_length`, and `max_header_length` together with the byte and scheduler limits. The job
queue is capped at 32, the default job timeout is 3600 seconds, prepared plans default to 600
seconds, and terminal artifacts default to 86400-second retention.

A full execution queue rejects preparation with `QUEUE_FULL` before new FASTA staging. If the
queue reaches capacity during preflight, the companion removes or tracks the private staging
directory before returning the same bounded error.

An existing companion state root must be owned by the current user with mode `0700`. It cannot
overlap the DeepKOALA checkout or an allowed input root. Filesystem input is accepted only from
explicit absolute roots, and traversal, symlink escapes, non-regular inputs, or input identity
changes are rejected. Status, errors, and sanitized diagnostics do not expose raw FASTA, secrets,
environment values, usernames, or full local paths.

Normal shutdown attempts to remove the current process scope. A filesystem cleanup failure or
abrupt termination can leave an owner-only session directory. Stop all companion processes before
manually removing stale sessions from the configured state root; one process does not delete
another process's scope.

The companion requires absolute checkout, external interpreter, and private state paths. See
[Installation and operation](installation.md#install-the-optional-deepkoala-companion-candidate)
for the complete `DEEPKOALA_MCP_*` environment contract and independent client registration.

## Start the server

After installing the project, configure an MCP client to run:

```text
uv run kegg-mcp
```

The default access mode is `offline_cache`. Server logs and configuration failures are written to
stderr; stdout is reserved for MCP protocol messages.

## KEGG access configuration

| Environment variable | Meaning |
| --- | --- |
| `KEGG_MCP_ACCESS_MODE` | `offline_cache` (default), `public_academic`, or `licensed` |
| `KEGG_MCP_ACADEMIC_USE_CONFIRMED` | Must equal `true` before public academic access is enabled |
| `KEGG_MCP_LICENSED_ENDPOINT` | Authorized HTTPS endpoint for licensed access |
| `KEGG_MCP_LICENSED_USE_CONFIRMED` | Must equal `true` before licensed access or licensed-cache reuse is enabled |
| `KEGG_MCP_CACHE_PATH` | Optional absolute path to the user-local KEGG cache |
| `KEGG_MCP_RESULT_STORE_PATH` | Optional absolute path to the user-local retained-result database |

The public KEGG REST service is limited to academic use by academic users. Other deployments must
use an appropriately licensed endpoint or an authorized local cache. The live client defaults to
two requests per second with no burst, enforces a hard process-wide maximum no greater than three
requests per second, and batches `get` requests at no more than ten entries.

To reuse a licensed cache without enabling network access, keep
`KEGG_MCP_ACCESS_MODE=offline_cache` and provide both licensed variables. The endpoint is used only
to select the matching cache namespace; no live request is made. Cache payloads and retained
results are local data and must not be committed, packaged, or attached to CI artifacts.

`get_server_status` and `ko-analysis://cache/info` report redacted configuration state. They do not
probe connectivity or enumerate cache contents.

## Tools

The server exposes eight tools:

- `analyze_ko_annotations`: one-call normalization and requested MODULE or pathway analysis. Supply
  either `ko_text` or a nested `annotations` request, plus at least one target.
- `normalize_ko_annotations`: normalize an inline plain KO list, explicitly mapped generic CSV/TSV,
  or DeepKOALA detailed table and retain the complete dataset.
- `get_kegg_entries`: retrieve selected allowlisted KEGG entries. It is not an arbitrary URL proxy.
- `map_ko_ids`: map selected K numbers to pathways, modules, reactions, EC numbers, or BRITE.
- `analyze_modules`: evaluate exact MODULE completion and required-block coverage from inline or
  retained evidence.
- `analyze_pathways`: calculate descriptive unique-KO coverage using an explicit `ko` or `map`
  reference namespace.
- `compare_ko_sets`: calculate deterministic set differences for two to ten datasets, with optional
  shared-reference MODULE or pathway comparisons.
- `get_server_status`: return redacted access, capability, and result-retention information.

Minimal plain-KO normalization input:

```json
{"text":"K00844\nK01810"}
```

Minimal one-call MODULE input:

```json
{
  "ko_text": "K00844\nK01810",
  "module_ids": ["M00001"],
  "analysis_unit": "isolate_proteome"
}
```

Generic tables require explicit column mapping and a named decision policy. When `annotations` is
used in the high-level tool, biological context belongs inside that nested object. KO-only MCP
inputs do not accept organism-specific pathway references because they lack gene-level context.
The MCP normalization boundary limits individual fields, including string-valued source metadata,
to 16,384 characters even though the lower-level importer contract permits larger explicit limits.

Direct tool responses are bounded previews. Complete immutable evidence and analysis detail stay
in the retained resource. A K number is an annotation, MODULE exact completion is distinct from
the project block-coverage metric, and pathway coverage does not establish pathway presence,
activity, flux, phenotype, or statistical significance.

## Resources and retention

Fixed resources:

- `ko-analysis://status`
- `ko-analysis://cache/info`

Resource templates:

- `ko-analysis://results/{result_id}`
- `ko-analysis://results/{result_id}/{section}`
- `ko-analysis://results/{result_id}/{section}/{offset}/{limit}`
- `kegg-cache://entries/{database}/{identifier}`

Result identifiers are opaque and scoped to one stdio server process. They expire under the local
retention policy and cannot be read from another scope. The result index lists validated section
URIs. High-level analysis normally retains `structured`, `summary`, and `annotations`; normalization
retains `dataset`; primitive tools retain `detail`.

An artifact larger than the inline resource limit returns an
`artifact_requires_pagination` notice. Range resources return base64 content, an exact byte count,
the artifact hash, and a continuation URI. Clients must concatenate decoded pages in order and may
verify the final SHA-256 digest.

The internal resource parser accepts only canonical identifiers, section names, and numeric ranges;
visible traversal segments, encoded separators, queries, fragments, and malformed ranges are
rejected. The MCP SDK canonicalizes some dot-segment URI aliases before the handler can inspect the
original spelling. Such an alias can only resolve to the same validated, scoped canonical resource;
it cannot cross a result or process scope.

`kegg-cache://entries/...` is cache-only and never triggers network access or creates a retained
result. It returns a bounded parsed preview rather than the raw cached payload.

## Errors and testing

Repairable tool failures use a schema-conforming error envelope with `code`, `message`,
`recoverable`, `suggested_action`, and bounded safe details. Invalid or unauthorized resource URIs
use MCP protocol errors. Paths, endpoint URLs, environment values, credentials, raw tables, and
cache payloads are not included in status or error output.

The default test suite is offline and must not contact KEGG. Live checks are separate manual tests
for an eligible academic user or an authorized licensed endpoint and should use only a few explicit
requests.
