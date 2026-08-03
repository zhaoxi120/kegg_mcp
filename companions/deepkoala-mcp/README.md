# deepkoala-mcp

`deepkoala-mcp` is the optional local stdio companion that runs a configured DeepKOALA installation.
It accepts an allowlisted absolute protein FASTA path, starts one controlled detailed annotation job,
and publishes stable files in a service-allocated or caller-selected output directory. Pass the
returned detailed CSV path and source provenance to the core `kegg-mcp` importer.

The companion does not normalize KO evidence, query KEGG, interpret pathways or MODULEs, or bundle
PyTorch, DeepKOALA, model weights, or KOfam profiles as package dependencies.

## Installation and runtime requirements

The primary Codex deployment path is the repository suite installer. It creates a separate locked
companion runtime and, after the one-time `--allow-deepkoala-install` confirmation for a new suite
root, installs the official DeepKOALA checkout with its bundled `202502` resources. Follow the
[suite installation guide](../../docs/installation.md) rather than registering this server again.

Manual and development deployments require:

- Linux, or Apple Silicon with macOS 14 or later, and CPython 3.11; on macOS both the companion and
  configured DeepKOALA interpreter must run natively as arm64 rather than through Rosetta;
- an existing official DeepKOALA checkout;
- a Python environment that imports `deepkoala`, `deepkoala.utils`, and `torch`;
- a readable `weights_{full|frag}.pt` and matching `ko_config_{full|frag}.json` under a dated
  `resources/YYYYMM` directory;
- explicit input, output, and private state roots; and
- local stdio transport.

Install the companion from the repository root in the runtime that will serve it:

```bash
module load pytorch  # when required by the local environment
python -m pip install -e ./companions/deepkoala-mcp
```

## Deployment configuration

Configuration is environment-only. Every path is absolute; root lists use the platform path
separator (`:` on Linux and macOS).

| Variable | Required | Meaning |
|---|---:|---|
| `DEEPKOALA_MCP_CHECKOUT` | yes | Readable DeepKOALA checkout |
| `DEEPKOALA_MCP_PYTHON` | yes | Executable DeepKOALA Python |
| `DEEPKOALA_MCP_STATE_ROOT` | yes | Private state root, separate from inputs and outputs |
| `DEEPKOALA_MCP_INPUT_ROOTS` | yes | Roots containing caller FASTA files |
| `DEEPKOALA_MCP_OUTPUT_ROOTS` | yes | Writable roots for stable result directories; the last is the default |
| `DEEPKOALA_MCP_ALLOWED_MODELS` | no | Subset of `full,frag`; default `full,frag` |
| `DEEPKOALA_MCP_ALLOWED_DEVICES` | no | Exact `cpu`, Linux `cpu,cuda`, or macOS `cpu,mps`; defaults to the matching platform pair |
| `DEEPKOALA_MCP_CPU_THREADS` | no | 1 through 4; default 2 |
| `DEEPKOALA_MCP_MAX_SEQUENCES` | no | Sequence cap up to 100,000 |
| `DEEPKOALA_MCP_MAX_OUTPUT_BYTES` | no | Detailed CSV cap up to 5,000,000 bytes |
| `DEEPKOALA_MCP_MAX_TIMEOUT_SECONDS` | no | Job cap up to 86,400 seconds; default 3,600 |
| `DEEPKOALA_MCP_ALLOW_MULTI` | no | Exact `true` or `false`; default `false` |
| `DEEPKOALA_MCP_PROFILES_DIR` | with multi | Direct directory of installed `Kxxxxx.hmm` profiles |
| `DEEPKOALA_MCP_HMMSEARCH_EXECUTABLE` | with multi | Direct absolute path to an installed `hmmsearch` executable |

Example manual configuration:

```bash
export DEEPKOALA_MCP_CHECKOUT=/absolute/path/to/DeepKOALA
export DEEPKOALA_MCP_PYTHON=/absolute/path/to/deepkoala-env/bin/python
export DEEPKOALA_MCP_STATE_ROOT=/absolute/private/deepkoala-mcp-state
export DEEPKOALA_MCP_INPUT_ROOTS=/absolute/project/inputs
export DEEPKOALA_MCP_OUTPUT_ROOTS=/absolute/project/results
deepkoala-mcp doctor --json
```

For a non-Codex MCP client, configure the absolute installed command with `args: ["serve"]` and the
same environment. The generated Codex plugin already provides this registration for suite installs.

## Status and public tools

`deepkoala-mcp doctor [--json]` performs bounded local structure and import checks without inference,
network access, or downloads. It redacts private paths and environment values. The MCP
`get_deepkoala_runner_status` tool exposes the same readiness boundary to a client. Its
`allow_multi` field reports deployment policy, while `multi_ready` reports structural readiness of
the configured profile directory, HMMER executable, and supported upstream adapter. It does not
certify that the directory contains every profile a future prediction may use. If multi-domain
execution is enabled but unavailable, `route_state` is `multi_dependencies_unavailable`; ordinary
annotation can still be ready.

The five public tools are `get_deepkoala_runner_status`, `run_deepkoala_job`,
`get_deepkoala_job`, `cancel_deepkoala_job`, and `delete_deepkoala_job`.

Protein FASTA intake has no aggregate file-byte limit. The companion streams validation into its
private canonical staging file and continues to enforce the reported sequence-count limit, a
100,000-residue limit per sequence, a 1,024-byte header limit, and controlled-path checks. Status
therefore reports `max_input_bytes=null`; the detailed CSV output and resource pages retain their
independent byte limits.

`run_deepkoala_job` validates policy, runtime readiness, FASTA content, private staging, the output
directory, and process startup in one call. The only required field is:

```json
{
  "fasta_path": "/absolute/project/inputs/proteins.faa"
}
```

Omitting `output_directory` allocates a fresh directory beneath the deployment's last configured
output root; an explicit path may select a new or existing empty owner-only directory beneath an
allowed output root. Other optional fields are `model` (`full` by default, or `frag`), `model_date`
(`202502` by default,
`latest`, or an installed `YYYYMM`), `device` (`cpu` by default, or explicit `cuda` or `mps` when
allowed), `batch_size` (1-64), `topk` (1-10), and `timeout_seconds` within the deployment cap. GPU
execution requires an explicit user request and status that both allows the selected backend and
reports its matching `cuda_available` or `mps_available` field as true. `multi` is a strict boolean
and defaults to `false`. Set it to `true` only when the user requests multi-domain annotation and
status reports both `allow_multi=true` and `multi_ready=true`; multi-domain requests must keep
`batch_size=1` because the upstream multi-domain path does not use configurable batching. HMMER
remains a CPU subprocess even when neural inference uses MPS.

DeepKOALA always receives detailed-output, an explicit `device=cpu|cuda|mps`, and `num_workers=0`;
it never receives `device=auto` and the companion does not enable silent MPS-to-CPU fallback. A
multi-domain job also receives `--multi --profiles_dir`; the handoff and report record the actual
device and `multi` value. Only one job runs in a deployment; another request, including one received
by another stdio process using the same state root, returns `RUNNER_BUSY` instead of entering a
queue. Multiple client processes may share one deployment state root; each receives an isolated
process scope while the runner lease remains deployment-wide. Status lists installed resources,
the device allowlist, and separate CUDA and MPS readiness. Runtime readiness also verifies the
configured interpreter's platform contract plus the checkout's explicit CLI device choices and
device resolver before advertising an accelerator. A successful handoff and report identify
the DeepKOALA source and the actual resolved model resource version used by that job.

## Stable output and lifecycle

An explicit output directory must be new or an existing empty owner-only directory. When omitted,
the service allocates a fresh child beneath the last configured output root. A successful job
publishes exactly:

```text
deepkoala_annotations.csv
deepkoala_run_report.md
```

The annotation file is bounded UTF-8 detailed CSV containing at least:

```text
name,predict_label,probability,threshold,annotate
```

Extra columns are preserved. `start` and `end` must occur together. A fully empty prediction,
probability, threshold, annotation marker, and coordinate tuple is retained as an unclassified
multi-domain row; partially empty or malformed evidence is rejected. The companion validates the
shape and score evidence but does not normalize K numbers or decide which rows enter KEGG analysis.

The Markdown report records the original FASTA path, companion and DeepKOALA versions, resolved model
name and date, fixed parameters, sequence count, readiness, and timezone-aware timestamps. The
successful job response returns handoff schema version `1`, absolute input, annotation, and report
paths, `input_format="deepkoala_detailed"`, and source provenance accepted by the core importer.

Stable files are the cross-MCP contract. The job ID is process-scoped. Deleting a terminal job
forgets only its record; delivered files remain after deletion and server exit. Failed, cancelled,
and timed-out jobs remove controlled incomplete output when safe. Private staged input and raw runner
output are removed for every terminal outcome.

Clients without a shared allowed filesystem may use the bounded process-scoped resources under
`deepkoala://jobs/{job_id}/...`. Stable files remain the default handoff; resource IDs must not be
passed to another server as result identity.

## Process and filesystem safety

- Execution uses a fixed argument vector without a shell or `shell=True`.
- Optional HMMER execution replaces only the supported upstream `_run_hmmsearch` hook. It uses the
  configured absolute executable, bounded CPU count, private scratch files, and rejects any other
  attempted shell execution.
- The service may inherit operator accelerator visibility, defaults to `device=cpu`, and accepts
  explicit `device=cuda` or `device=mps` only through the platform deployment allowlist; it never
  requests `device=auto` or enables silent backend fallback.
- One job runs at a time across all companion processes sharing a deployment state root, with
  bounded CPU threads, FASTA structure, output, sequences, and elapsed time.
- Timeout, cancellation, process-group termination, descendant cleanup, Linux parent-death signals,
  and a Darwin sentinel process that outlives an exited group leader bound the external process
  lifecycle.
- Inputs must be direct files beneath allowed roots; output must be a new or empty owner-only
  directory beneath an allowed root. Traversal, unsafe ancestry, replacement, and symlink escape
  are rejected.
- Temporary state and generated files use restrictive permissions and are never published by
  overwriting an existing path.
- The companion server contains no network client, dependency installer, or model download path.

Enabling multi-domain policy never installs HMMER, profiles, or a different DeepKOALA revision.
Provision or repair those dependencies outside the serving companion, then rerun `doctor`.

Apple GPU execution uses PyTorch's MPS backend. It requires an MPS-capable native arm64 PyTorch
runtime and the official DeepKOALA device interface introduced by
[`bebbe0c43f50a26488f7092f6b355aae870a4ed9`](https://github.com/zhaoxi120/deepkoala/commit/bebbe0c43f50a26488f7092f6b355aae870a4ed9).
The suite installer pins and verifies that revision; a manual deployment must provide an equally
reviewed compatible checkout.

K number assignments are computational annotations, not experimental validation. A source-rejected
prediction is not evidence that a function is biologically absent.

## Validation

Run checks in the requested runtime environment:

```bash
cd companions/deepkoala-mcp
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Tests use synthetic FASTA, CSV, checkout, and resource fixtures and contact neither DeepKOALA
download services nor KEGG. Release builds and exact-suite checks follow the repository
[release-readiness checklist](../../docs/release-readiness.md).
