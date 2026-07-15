# DeepKOALA MCP companion

`deepkoala-mcp` is an optional local stdio MCP server that runs a separately installed official
DeepKOALA checkout under strict input, process, and output bounds. This directory is an independent
Python distribution with its own `deepkoala-mcp` entry point and lock file. Installing it does not
add DeepKOALA, PyTorch, model weights, HMMER, or KOfam profiles to the core `kegg-mcp` package.

> Release status: this is an unreleased `0.1.0` candidate. It is not part of the supported core
> `kegg-mcp` 0.1.0 release and requires its own release review before support is claimed.

The companion uses a two-phase workflow. `prepare_deepkoala_job` validates and privately copies a
protein FASTA input, resolves installed model resources and the execution device, and returns an
execution notice without starting inference. `submit_deepkoala_job` starts or queues only the exact
reviewed plan, and only when the caller supplies its notice digest and `acknowledged=true`.

The candidate always requests detailed CSV output and rejects multi-domain mode (`multi=false`).
Successful CSV is exposed through scoped, paginated resources together with a provenance template
for the existing source-agnostic core importer. The companion never downloads or replaces weights.

## Requirements and independent installation

- a POSIX platform with process-group and file-size-limit support;
- Python 3.11.x for the companion;
- an official DeepKOALA checkout already present on the local machine;
- a separate Python interpreter in which that checkout's existing PyTorch runtime works; and
- an MCP client that can start a local stdio command.

From the repository root, install only the lightweight companion dependencies:

```bash
uv sync --project companions/deepkoala-mcp --frozen
```

This creates the companion environment independently of the core environment. The executable is
`companions/deepkoala-mcp/.venv/bin/deepkoala-mcp`. The dependency set contains MCP and validation
libraries; it intentionally does not contain PyTorch or DeepKOALA.

The candidate is POSIX-only because safe cancellation, timeout, and shutdown require ownership of
the complete child process group, while output bounding requires a per-process file-size limit. On
a runtime without those controls, startup fails before configuration paths are handled or a
private state directory is created.

On a module-based system, resolve the external execution interpreter in an interactive shell:

```bash
module load pytorch
command -v python
```

Set `DEEPKOALA_MCP_PYTHON` to the resulting absolute path. An MCP client does not inherit an
interactive module operation reliably, so do not use `module load`, a shell wrapper, or an
environment activation script as the MCP command.

## Required configuration

The server reads configuration from its process environment and does not load `.env` files.

| Variable | Contract |
| --- | --- |
| `DEEPKOALA_MCP_CHECKOUT` | Required absolute path to the existing official DeepKOALA checkout. |
| `DEEPKOALA_MCP_PYTHON` | Required absolute executable for the external DeepKOALA/PyTorch environment. |
| `DEEPKOALA_MCP_STATE_ROOT` | Required dedicated absolute private state directory; an existing directory must be owned by the current user with mode `0700`. |
| `DEEPKOALA_MCP_ALLOWED_ROOTS` | Optional OS-path-separator list of existing absolute roots from which `fasta_path` inputs may be copied. |
| `DEEPKOALA_MCP_WEIGHT_SOURCE` | `github_bundled` (default) or `user_provided`; this is provenance, not a download selector. |
| `DEEPKOALA_MCP_CPU_THREADS` | Process thread limit, default `2`, range 1-32. |
| `DEEPKOALA_MCP_MAX_CONCURRENT_JOBS` | Fixed at `1` in this candidate. |
| `DEEPKOALA_MCP_MAX_QUEUE_SIZE` | Default and hard maximum `32`. |
| `DEEPKOALA_MCP_DEFAULT_TIMEOUT_SECONDS` | Default `3600`, maximum `86400`. |
| `DEEPKOALA_MCP_PLAN_TTL_SECONDS` | Default `600`, maximum `86400`. |
| `DEEPKOALA_MCP_RETENTION_SECONDS` | Default `86400`, maximum `2592000`. |

Additional variables may lower the hard input, output, diagnostic, sequence-count, residue-count,
sequence-length, and header-length bounds. They cannot raise the built-in maxima. The default hard
input and output limits are 5,000,000 bytes, diagnostics are capped at 65,536 bytes, and no more
than 100,000 sequences are accepted. Runner status reports the effective `max_sequences`,
`max_residues`, `max_sequence_length`, and `max_header_length` values after those reductions.
The output-byte limit is installed as a POSIX `RLIMIT_FSIZE` before the upstream CLI starts and is
also revalidated after execution, so a faulty child cannot first create an unbounded result file.
When the execution queue is already at capacity, preparation returns `QUEUE_FULL` before staging
new input. A capacity race detected after preflight also removes or tracks the private staging
directory before returning an error.

The state root must not overlap the DeepKOALA checkout or an allowed input root. Path inputs must
be absolute regular files below an allowed root. Traversal and symlink escapes are rejected. Inline
FASTA does not require an allowed input root.

Artifact hashes provide provenance and change detection; they do not make an external interpreter
or checkout trustworthy. The companion executes both with the current user's permissions, so use
only an operator-reviewed official checkout and interpreter. Normal shutdown attempts to remove
the current process scope. After a filesystem cleanup failure or abrupt termination, stop every
companion process before manually removing any stale owner-only session directory from the
configured state root.

Example client registration:

```json
{
  "mcpServers": {
    "deepkoala-mcp": {
      "command": "/absolute/path/to/kegg_mcp/companions/deepkoala-mcp/.venv/bin/deepkoala-mcp",
      "env": {
        "DEEPKOALA_MCP_CHECKOUT": "/absolute/path/to/DeepKOALA",
        "DEEPKOALA_MCP_PYTHON": "/absolute/path/to/pytorch/bin/python",
        "DEEPKOALA_MCP_STATE_ROOT": "/absolute/private/path/deepkoala-mcp",
        "DEEPKOALA_MCP_ALLOWED_ROOTS": "/absolute/private/path/fasta-inputs",
        "DEEPKOALA_MCP_WEIGHT_SOURCE": "github_bundled",
        "DEEPKOALA_MCP_CPU_THREADS": "2"
      }
    }
  }
}
```

## Tools and resources

The companion exposes six tools:

- `get_deepkoala_runner_status` returns redacted readiness, installed model dates, defaults,
  bounds, and queue counts;
- `prepare_deepkoala_job` validates one inline or allowlisted protein FASTA input and returns a
  prepared plan, execution notice, digest, and expiry without running inference;
- `submit_deepkoala_job` requires the exact `plan_id`, `notice_sha256`, and `acknowledged=true`;
- `get_deepkoala_job` returns lifecycle state and, after success, the core-import handoff;
- `cancel_deepkoala_job` cancels a queued job or terminates a running process group; and
- `delete_deepkoala_job` deletes one terminal job and its retained artifacts.

Job summaries include `diagnostics_truncated`. A true value always accompanies a terminal
`diagnostic_uri` and means that the sanitized diagnostic resource contains only the configured
bounded tail.

Deletion is safe to retry only while the deleted job identifier remains in the bounded process
tombstone window. The companion retains at most the 1,024 most recently deleted identifiers.
After an identifier leaves that window, or after the stdio process restarts, a repeated delete
returns `JOB_NOT_FOUND`; the delete tool therefore does not advertise unconditional idempotence.

Runner status is a redacted structural readiness check. Preparation performs the authoritative
per-job device and artifact-identity preflight and binds those identities into the execution
notice. Preparation does not load the model or prove that installed weights and configuration are
semantically valid; inference can still fail safely after acknowledgement.

The fixed status resource is `deepkoala-job://status`. Scoped job artifact templates are:

```text
deepkoala-job://jobs/{job_id}/{section}
deepkoala-job://jobs/{job_id}/{section}/{offset}/{limit}
```

`section` is `output`, `provenance`, or `diagnostics`. Artifacts up to 64 KiB are returned inline.
Larger artifacts return a pagination notice; range pages are base64-encoded and limited to 1 MiB.
Clients must concatenate decoded pages in order and verify the final SHA-256 digest.

## CPU-only execution

The upstream default is `device=auto`, which may select an available GPU. For a CPU-only job,
prepare it with `device="cpu"`. A conservative compatibility request can also use
`batch_size=1`, `num_workers=0`, and `DEEPKOALA_MCP_CPU_THREADS=2`. Job concurrency remains fixed
at one. `batch_size` controls inference batching inside that one process; it does not add job
concurrency.

Both `full` and `frag` models are supported. The default model is `full`, the default installed
resource selector is `latest`, and the default `topk` is one. `multi=true` is rejected because the
candidate never invokes the upstream multi-domain path. No configuration value causes the
companion to fetch weights; use only resources already installed in the configured checkout.

## Cross-server handoff

After a job succeeds:

1. read the `output` resource, following range pages when required;
2. verify the complete bytes against the job's `output_sha256`;
3. read the `provenance` resource and retain the handoff's `source_provenance_template`; and
4. call the core `normalize_ko_annotations` tool with the decoded CSV as inline `text`,
   `input_format="deepkoala_detailed"`, and the supplied provenance template as `source`.

One MCP server cannot dereference another server's private resource URI. The client or Skill must
perform this read, verification, and inline transfer. The core importer remains the only place
that applies the named DeepKOALA normalization policy; the companion neither interprets KO
predictions nor claims that an annotation is experimental validation.

See the repository [installation guide](../../docs/installation.md) and
[MCP reference](../../docs/mcp-server.md) for the combined two-server workflow.
