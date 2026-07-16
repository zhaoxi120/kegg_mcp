# Optional DeepKOALA companion

Use this route only for protein FASTA without existing KO evidence. DeepKOALA execution is
local-only through the separately installed runtime and local `deepkoala-mcp` server. The
companion is not part of the core `kegg-mcp` runtime. This Skill orchestrates its public tools; it
does not implement inference, launch subprocesses, inspect or download weights, or reinterpret
predictions.

## Route states

Use these stable routing states internally: `local_ready`, `local_runner_misconfigured`,
`local_deepkoala_present_companion_missing`, `local_not_installed`, `installation_declined`, and
`remote_api_unavailable`. Do not return local paths, environment values, or credentials with a
state.

1. **Absent (`local_deepkoala_present_companion_missing` or `local_not_installed`):** report that
   the local companion is unavailable and ask whether the user wants to install and register local
   DeepKOALA and `deepkoala-mcp`. Stop if permission is not granted. Do not open, submit to, or
   automate the DeepKOALA web form. GenomeNet does not provide a DeepKOALA API, so this workflow
   has no remote execution route.
2. **Discovered:** make `get_deepkoala_runner_status` the first annotation-tool call.
3. **Not ready (`local_runner_misconfigured`):** report whether the local checkout, interpreter,
   model resources, private state root, companion package, or MCP registration is missing. Ask for
   repair or installation permission. Do not install, download, or repair dependencies silently;
   do not modify an environment or MCP configuration silently.
4. **Ready (`local_ready`) and prepared:** call `prepare_deepkoala_job` with exactly one inline FASTA or allowed absolute
   FASTA path. Request only the documented CPU execution settings. Present the returned execution
   notice, including model, installed resource date, top-k, timeout, input summary, and output
   boundary, before inference starts.
5. **Confirmed:** obtain explicit user confirmation for that prepared job. Only then call
   `submit_deepkoala_job` with its opaque `job_id` and `acknowledged=true`. Do not request or verify
   a digest.
6. **Running or terminal:** use `get_deepkoala_job` for bounded status checks. Use
   `cancel_deepkoala_job` only when requested or when the surrounding task is cancelled. Use
   `delete_deepkoala_job` when retained local job data is no longer needed.

If the user declines installation or repair, set `installation_declined`, preserve the original
FASTA, stop annotation, and make no local change. If the user asks to automate the web service,
set `remote_api_unavailable`, explain that the GenomeNet page is a web form rather than a
DeepKOALA API, refuse simulated form submission, and offer only the local installation or local
configuration route.

## Installation or repair confirmation

Before any installation action, tell the user which local component is missing, which checkout,
environment, model-resource directory, companion installation, state root, or MCP registration
would be created or changed, whether dependency or model downloads are needed, and the expected
disk and compute requirements. State that the FASTA remains local and that there is no remote
upload branch. Obtain permission before any package install, environment change, checkout or model
download, state-root creation, or MCP configuration write.

## Successful handoff

After success, pass the companion's controlled absolute `output_path` to the core
`normalize_ko_annotations` or `analyze_ko_annotations` input as `file_path`, with
`input_format="deepkoala_detailed"` and the returned source provenance. The core server must allow
the companion's state root; the companion's allowed roots control FASTA intake, not output. Do not
copy a companion-private result identifier into the core server, and do not parse or normalize the
detailed CSV in the Skill.

For an ordinary Top-N pathway request, call `analyze_ko_annotations` once with the detailed file,
`pathway_selection.mode="top_detected"`, and the requested `top_n`. Do not read the CSV,
KO-to-pathway relationships, pathway ranking, or `render_input.json` in the Skill.

DeepKOALA assignments are annotation evidence, not experimental validation. Preserve the
provenance fields returned by the companion and the detailed CSV's raw decisions, probabilities,
thresholds, ranks, and domain coordinates when present. The core importer's named policy alone
decides which records are accepted, uncertain, rejected, unclassified, or invalid.
