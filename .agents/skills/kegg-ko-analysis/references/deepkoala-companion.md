# Optional DeepKOALA companion

Use this route only for protein FASTA without existing KO evidence and only when the separately
installed local `deepkoala-mcp` server is explicitly available. The companion is not part of the
core `kegg-mcp` runtime. This Skill orchestrates its public tools; it does not implement inference,
launch subprocesses, inspect or download weights, or reinterpret predictions.

## Route states

1. **Absent:** if the companion is not discovered, stop and route to an independent annotation
   Skill and MCP. Never send FASTA to the core server.
2. **Not ready:** call `get_deepkoala_runner_status`. If the configured checkout, interpreter,
   installed resources, or private state root is unavailable, report the deployment problem and
   stop. Do not install, download, or repair dependencies silently.
3. **Prepared:** call `prepare_deepkoala_job` with exactly one inline FASTA or allowed absolute
   FASTA path. Request only the documented CPU execution settings. Present the returned execution
   notice, including model, installed resource date, top-k, timeout, input summary, and output
   boundary, before inference starts.
4. **Confirmed:** obtain explicit user confirmation for that prepared job. Only then call
   `submit_deepkoala_job` with its opaque `job_id` and `acknowledged=true`. Do not request or verify
   a digest.
5. **Running or terminal:** use `get_deepkoala_job` for bounded status checks. Use
   `cancel_deepkoala_job` only when requested or when the surrounding task is cancelled. Use
   `delete_deepkoala_job` when retained local job data is no longer needed.

## Successful handoff

After success, pass the companion's controlled absolute `output_path` to the core
`normalize_ko_annotations` or `analyze_ko_annotations` input as `file_path`, with
`input_format="deepkoala_detailed"` and the returned source provenance. The core server must allow
the companion's state root; the companion's allowed roots control FASTA intake, not output. Do not
copy a companion-private result identifier into the core server, and do not parse or normalize the
detailed CSV in the Skill.

DeepKOALA assignments are annotation evidence, not experimental validation. Preserve the
provenance fields returned by the companion and the detailed CSV's raw decisions, probabilities,
thresholds, ranks, and domain coordinates when present. The core importer's named policy alone
decides which records are accepted, uncertain, rejected, unclassified, or invalid.
