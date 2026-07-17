# DeepKOALA MCP companion

`deepkoala-mcp` is an optional, independently installed local stdio server. It runs an existing
official DeepKOALA checkout with an existing Python/PyTorch environment. It is not part of the core
`kegg-mcp` package and does not add DeepKOALA, PyTorch, model code, weights, or databases to that
package.

This companion is the only supported DeepKOALA automation route for this project. The GenomeNet
DeepKOALA page is a web form and does not document a remote job API, so Skills must not open,
submit to, or simulate that form. When this companion or its runtime is missing or unready, the
Skill reports the local component state and obtains permission before installation, downloads,
environment changes, state creation, or MCP registration.

The companion fixes `--device auto` and `--num_workers 0`, limits inference batches to at most 64,
limits CPU thread pools to at most four threads, and runs one job at a time. It inherits the MCP
process's existing accelerator visibility; callers cannot select a device or expand that
visibility. Provenance records `device_requested=auto` and does not claim which device was selected
unless DeepKOALA exposes a reliable result. The companion owns the complete POSIX child process
group, makes no network requests, and never downloads, updates, or replaces model resources. It
does not claim to provide an OS network sandbox for arbitrary operator-provided child code.

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
platforms. The release-supported behavior is Linux accelerator selection when available and CPU
fallback otherwise; macOS-specific accelerators are outside the supported platform matrix.

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
| `DEEPKOALA_MCP_CPU_THREADS` | CPU thread-pool limit, default 2, range 1–4. |
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

Before registering the MCP server, inspect the local deployment without starting inference or
downloading anything:

```bash
deepkoala-mcp doctor --json
```

The redacted diagnostic reports one stable route state such as `local_ready`,
`deepkoala_checkout_missing`, `deepkoala_python_missing`, `model_resources_missing`,
`state_root_missing`, or `runner_misconfigured`. It never prints configured paths. Registration
state and whether the core allowed roots include the handoff root must still be checked by the MCP
client or operator because an unregistered companion cannot diagnose itself through MCP.

## Workflow

The server exposes six tools:

1. `get_deepkoala_runner_status`
2. `prepare_deepkoala_job`
3. `submit_deepkoala_job`
4. `get_deepkoala_job`
5. `cancel_deepkoala_job`
6. `delete_deepkoala_job`

Preparation validates and privately stages one inline or allowlisted protein FASTA. It returns the
effective model/date, service-owned device and resource settings, input summary, expiry, and one
opaque `job_id` without starting inference. The retained notice is provenance, not a per-job
confirmation gate. Submission accepts only that `job_id` and is idempotent, so an already prepared
job can be submitted immediately without duplicating execution. The identifier references the
immutable server-retained plan; no client-generated or echoed digest is used.

DeepKOALA always runs with detailed output and `multi=false`. A successful job returns:

- an absolute private `output_path` for the generated detailed annotation CSV;
- `input_format="deepkoala_detailed"`; and
- readable source provenance aligned with the core `SourceProvenanceInput` contract, including a
  sanitized annotation-artifact `input_uri` and the original allowlisted FASTA `input_path` when
  path input was used.

For inline FASTA, provenance sets `input_path` to `null`. The companion never substitutes its
private staged `input.fasta` path. The generated annotation `output_path` and optional original
FASTA `source.input_path` are distinct fields with independent path validation.

Configure the core server to allow the companion state root and, for path input, the original
FASTA root. Then pass `output_path` as the core normalization tool's `file_path`, together with the
supplied input format and provenance. The core importer is the only component that validates
DeepKOALA CSV semantics or classifies KO evidence. The companion only enforces a non-empty regular
output file and a 5,000,000-byte limit.

The output path remains valid only while the companion process is alive and before retention expiry
or explicit deletion. Normal shutdown cancels the owned process group and removes its complete
session directory. FASTA headers, sequences, environment values, and local paths are not returned
by status or error responses; only the controlled annotation output path and caller-supplied
original FASTA path, when present, are exposed in a successful handoff.

One owner-only state root can be held by only one companion process. After acquiring that lock, a
new process removes only strictly named, owner-only abandoned session directories and fails closed
on unexpected entries. On Linux, the child also requests a parent-death signal in addition to the
normal timeout, cancellation, and process-group cleanup paths. Unexpected background failures
return a safe correlation ID while stderr records only that ID, a logical stage, and the exception
type. Repeating deletion may return not-found, but its filesystem effect is idempotent.
