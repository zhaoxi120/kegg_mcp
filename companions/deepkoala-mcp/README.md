# DeepKOALA MCP companion

`deepkoala-mcp` is an optional, independently installed local stdio server. It runs an existing
official DeepKOALA checkout with an existing Python/PyTorch environment. It is not part of the core
`kegg-mcp` package and does not add DeepKOALA, PyTorch, model code, weights, or databases to that
package.

The companion is deliberately CPU-only. It fixes `--device cpu`, fixes `--num_workers 0`, limits
inference batches to at most 64, limits thread pools to at most four threads, runs one job at a
time, hides CUDA/HIP/ROCm devices, and owns the complete POSIX child process group. The companion
does not make network requests and never downloads, updates, or replaces model resources. It does
not claim to provide an OS network sandbox for arbitrary operator-provided child code.

## Install

Install the lightweight MCP process independently:

```bash
uv sync --project companions/deepkoala-mcp --frozen
```

DeepKOALA and PyTorch remain in the separately configured execution interpreter. On a module-based
system, load the existing environment interactively and record its absolute Python path:

```bash
module load pytorch
command -v python
```

The companion requires POSIX process groups and `RLIMIT_FSIZE`. It fails closed on unsupported
platforms.

## Configure

The companion's deployment configuration uses these `DEEPKOALA_MCP_*` variables:

Create the shared state/handoff root before starting either MCP server so the core can validate its
allowlist during startup:

```bash
install -d -m 700 /absolute/private/path/deepkoala-jobs
```

| Variable | Meaning |
| --- | --- |
| `DEEPKOALA_MCP_CHECKOUT` | Required absolute path to an existing official DeepKOALA checkout. |
| `DEEPKOALA_MCP_PYTHON` | Required absolute executable in the existing DeepKOALA/PyTorch environment. |
| `DEEPKOALA_MCP_STATE_ROOT` | Required dedicated state directory; an existing directory must be owner-only. |
| `DEEPKOALA_MCP_ALLOWED_ROOTS` | Optional path-separator list of roots accepted by `fasta_path`. |
| `DEEPKOALA_MCP_CPU_THREADS` | CPU thread limit, default 2, range 1–4. |
| `DEEPKOALA_MCP_MAX_QUEUE_SIZE` | Queue bound, default 4, range 1–8. |
| `DEEPKOALA_MCP_DEFAULT_TIMEOUT_SECONDS` | Execution timeout, default 3600, maximum 86400. |
| `DEEPKOALA_MCP_PLAN_TTL_SECONDS` | Prepared-plan lifetime, default 600, maximum 86400. |
| `DEEPKOALA_MCP_RETENTION_SECONDS` | Terminal-result lifetime, default 86400, maximum 2592000. |

The checkout must contain `deepkoala/cli.py`, `deepkoala/utils.py`, project metadata, and an already
installed `resources/YYYYMM` directory with the selected `weights_{full|frag}.pt` and
`ko_config_{full|frag}.json` files. The companion checks only this bounded layout. It does not scan
or hash model contents and makes no cryptographic trust claim about operator-managed files.
`model_date="latest"` selects the newest matching resource already installed locally; it never
performs a network lookup or download.

Example client registration:

```json
{
  "mcpServers": {
    "deepkoala-mcp": {
      "command": "/absolute/path/to/companions/deepkoala-mcp/.venv/bin/deepkoala-mcp",
      "env": {
        "DEEPKOALA_MCP_CHECKOUT": "/absolute/path/to/deepkoala",
        "DEEPKOALA_MCP_PYTHON": "/absolute/path/to/pytorch/bin/python",
        "DEEPKOALA_MCP_STATE_ROOT": "/absolute/private/path/deepkoala-jobs",
        "DEEPKOALA_MCP_ALLOWED_ROOTS": "/absolute/private/path/fasta-inputs",
        "DEEPKOALA_MCP_CPU_THREADS": "2"
      }
    }
  }
}
```

## Workflow

The server exposes six tools:

1. `get_deepkoala_runner_status`
2. `prepare_deepkoala_job`
3. `submit_deepkoala_job`
4. `get_deepkoala_job`
5. `cancel_deepkoala_job`
6. `delete_deepkoala_job`

Preparation validates and privately stages one inline or allowlisted protein FASTA. It returns the
effective model/date, CPU settings, input summary, expiry, and one opaque `job_id` without starting
inference. Submission requires the same `job_id` and `acknowledged=true`. The identifier references
the immutable server-retained plan, so no client-generated or echoed digest is used.

DeepKOALA always runs with detailed output and `multi=false`. A successful job returns:

- an absolute private `output_path`;
- `input_format="deepkoala_detailed"`; and
- readable source provenance aligned with the core `SourceProvenanceInput` contract.

Configure the core server to allow the companion state root, then pass `output_path` as the core
normalization tool's `file_path`, together with the supplied input format and provenance. The core
importer is the only component that validates DeepKOALA CSV semantics or classifies KO evidence.
The companion only enforces a non-empty regular output file and a 5,000,000-byte limit.

The output path remains valid only while the companion process is alive and before retention expiry
or explicit deletion. Normal shutdown cancels the owned process group and removes its complete
session directory. FASTA headers, sequences, environment values, and local paths are not returned
by status or error responses; the controlled output path is exposed only in a successful handoff.

Compatibility was checked on 2026-07-16 with official DeepKOALA commit
`bebbe0c43f50a26488f7092f6b355aae870a4ed9`, the `full` 202502 resources, CPU execution with two
threads, and the complete prepare, submit, poll, handoff, core-import, delete, and shutdown flow.
The bounded detailed output produced one schema-valid imported record, classified as rejected by
the source policy with zero import diagnostics, and private session state was cleared. This is an
interoperability check, not experimental validation of the assigned function.
