# deepkoala-mcp

`deepkoala-mcp` is an optional local stdio MCP companion that runs a configured local DeepKOALA
installation. The unified suite installer can create that installation after first-use
confirmation; manual deployments can provide an existing installation. The companion accepts an
allowlisted absolute protein FASTA path, starts one bounded detailed annotation job, and publishes
stable files in a new caller-selected output directory.

The companion does not normalize KO evidence, query KEGG, interpret pathways or MODULEs, download
DeepKOALA source or model resources, or bundle PyTorch and DeepKOALA as package dependencies. Pass
the returned detailed CSV path and source provenance to the core `kegg-mcp` importer.

## Runtime requirements

- Linux with Python 3.11;
- a suite-managed or manually supplied official DeepKOALA checkout;
- a suite-managed or manually supplied Python environment that can import `deepkoala`,
  `deepkoala.utils`, and `torch`;
- at least one readable local `weights_{full|frag}.pt` and matching
  `ko_config_{full|frag}.json` pair under a dated `resources/YYYYMM` directory;
- explicit input, output, and private state roots; and
- local stdio transport.

On an HPC system where the maintained environment is exposed as a module, activate it before
installing, testing, diagnosing, or serving:

```bash
module load pytorch
python -m pip install -e ./companions/deepkoala-mcp
```

## Deployment configuration

All deployment authority is environment-only. Paths are absolute; root lists use the platform
path separator (`:` on Linux).

| Variable | Required | Meaning |
|---|---:|---|
| `DEEPKOALA_MCP_CHECKOUT` | yes | Readable configured DeepKOALA checkout |
| `DEEPKOALA_MCP_PYTHON` | yes | Executable configured DeepKOALA Python |
| `DEEPKOALA_MCP_STATE_ROOT` | yes | Private temporary state root, separate from inputs and outputs |
| `DEEPKOALA_MCP_INPUT_ROOTS` | yes | Roots containing caller-supplied FASTA files |
| `DEEPKOALA_MCP_OUTPUT_ROOTS` | yes | Writable roots for stable result directories |
| `DEEPKOALA_MCP_ALLOWED_MODELS` | no | Comma-separated subset of `full,frag`; default `full,frag` |
| `DEEPKOALA_MCP_ALLOWED_DEVICES` | no | Must be exactly `auto` |
| `DEEPKOALA_MCP_CPU_THREADS` | no | Thread cap from 1 through 4; default 2 |
| `DEEPKOALA_MCP_MAX_FASTA_BYTES` | no | Input byte cap up to 5,000,000 |
| `DEEPKOALA_MCP_MAX_SEQUENCES` | no | Sequence cap up to 100,000 |
| `DEEPKOALA_MCP_MAX_OUTPUT_BYTES` | no | Detailed CSV cap up to 5,000,000 |
| `DEEPKOALA_MCP_MAX_TIMEOUT_SECONDS` | no | Per-job timeout cap up to 86,400; default 3,600 |

Example:

```bash
module load pytorch
export DEEPKOALA_MCP_CHECKOUT=/absolute/path/to/DeepKOALA
export DEEPKOALA_MCP_PYTHON=/absolute/path/to/deepkoala-env/bin/python
export DEEPKOALA_MCP_STATE_ROOT=/absolute/private/deepkoala-mcp-state
export DEEPKOALA_MCP_INPUT_ROOTS=/absolute/project/inputs
export DEEPKOALA_MCP_OUTPUT_ROOTS=/absolute/project/results
deepkoala-mcp doctor --json
```

`doctor` performs bounded local structure and import checks. It does not run inference, contact a
network service, or download anything. Status and diagnostics redact private paths and environment
values. An executable that cannot run the fixed Python probe, including `/bin/false`, is not ready.

## MCP client configuration

The following is JSON configuration for an MCP client, not a Bash command:

```json
{
  "mcpServers": {
    "deepkoala-mcp": {
      "command": "/absolute/path/to/bin/deepkoala-mcp",
      "args": ["serve"],
      "env": {
        "DEEPKOALA_MCP_CHECKOUT": "/absolute/path/to/DeepKOALA",
        "DEEPKOALA_MCP_PYTHON": "/absolute/path/to/deepkoala-env/bin/python",
        "DEEPKOALA_MCP_STATE_ROOT": "/absolute/private/deepkoala-mcp-state",
        "DEEPKOALA_MCP_INPUT_ROOTS": "/absolute/project/inputs",
        "DEEPKOALA_MCP_OUTPUT_ROOTS": "/absolute/project/results"
      }
    }
  }
}
```

## Public tools

The public API contains five tools:

1. `get_deepkoala_runner_status`
2. `run_deepkoala_job`
3. `get_deepkoala_job`
4. `cancel_deepkoala_job`
5. `delete_deepkoala_job`

`run_deepkoala_job` performs policy validation, runtime preflight, FASTA validation and private
staging, controlled output-directory creation, and process start in one call. There is no public
prepare/submit phase, acknowledgement, plan hash, prepared state, queue, or plan TTL.

Required run fields:

```json
{
  "fasta_path": "/absolute/project/inputs/proteins.faa",
  "output_directory": "/absolute/project/results/deepkoala-run-001"
}
```

Optional fields are `model` (`full` or `frag`), `model_date` (`202502` by default, or `latest` or
an installed `YYYYMM` override), `device` (`auto` only), `batch_size` (1-64), `topk` (1-10), and
`timeout_seconds` within the deployment cap.
DeepKOALA always receives fixed detailed-output, `device=auto`, `num_workers=0`, and `multi=false`
settings. Only one job can run in a deployment at a time; another run returns `RUNNER_BUSY` rather
than entering a queue.

## Stable output contract

The requested output directory must not already exist. After a successful job, it contains exactly
the stable delivery files:

```text
deepkoala_annotations.csv
deepkoala_run_report.md
```

`deepkoala_annotations.csv` is the validated DeepKOALA detailed CSV. It must be UTF-8 CSV with at
least the documented fields:

```text
name,predict_label,probability,threshold,annotate
```

Optional extra columns are preserved. If `start` or `end` is present, both must be present. The
companion validates the detailed shape and bounded score evidence but does not normalize K numbers
or decide which evidence enters a KEGG analysis.

`deepkoala_run_report.md` records the absolute input FASTA path, companion and DeepKOALA versions,
the resolved model name and model date, effective fixed parameters, sequence count, runtime
readiness, and timezone-aware start and completion times. It contains no input, model,
configuration, output, or dataset digest.

The successful `get_deepkoala_job` response returns handoff schema version `1`, companion tool
version, absolute input/annotation/report paths, `input_format="deepkoala_detailed"`, and a source
provenance object accepted by the core importer. The stable paths are the cross-MCP contract; the
process-scoped job ID and resource URIs must not be passed to another server as result identity.

Deleting a terminal job forgets only its process record. Delivered files remain. They also remain
after the stdio server exits. Failed, cancelled, and timed-out jobs remove their controlled output
directory when it remains safe to do so. Temporary staged FASTA and raw runner output live only in
the private state root and are removed for every terminal outcome.

## Resource fallback

Clients that cannot share local paths may read process-scoped fallback resources while the job
record remains available:

```text
deepkoala://jobs/{job_id}/annotations
deepkoala://jobs/{job_id}/report
```

Artifacts up to 65,536 bytes are returned directly. Larger artifacts return a versioned JSON
pagination notice. Range resources use:

```text
deepkoala://jobs/{job_id}/{artifact}/{offset}/{limit}
```

Each range returns at most 65,536 bytes as base64 with the exact offset, returned and total byte
counts, schema version, and optional continuation URI. Missing, replaced, malformed, out-of-range,
and non-canonical resources fail closed. Resource fallback is an adapter compatibility mechanism;
stable files remain the default handoff.

## Process and filesystem controls

- no shell command construction and no `shell=True`;
- fixed DeepKOALA argv with detailed output and no worker subprocesses;
- inherited operator GPU visibility with a fixed `auto` device request;
- deployment-wide concurrency of one;
- bounded CPU thread environment;
- process-group timeout, cancellation, descendant reaping, and Linux parent-death signal;
- `RLIMIT_FSIZE` for raw runner output;
- allowlisted direct input paths and new output directories with traversal and symlink rejection;
- owner-only temporary state and output files;
- stable artifact publication without overwriting existing paths; and
- no network client or download path in the companion.

K number assignments are computational annotations, not experimental validation. A source-rejected
prediction is not evidence that a function is biologically absent.

## Validation

Run companion checks in the requested runtime environment:

```bash
module load pytorch
cd companions/deepkoala-mcp
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

The tests use only synthetic FASTA, CSV, checkout, and resource fixtures. They do not download
DeepKOALA resources or contact KEGG.
