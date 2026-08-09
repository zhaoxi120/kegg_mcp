# Deployment and handoff

## Readiness routing

- `local_ready`: run the requested job directly.
- `multi_dependencies_unavailable`: ordinary annotation remains available, but do not pass
  `multi=true`; report the operator action needed to repair HMMER, profiles, or the supported
  upstream interface.
- missing declared MCP dependency or required tool in the task immediately after a successful suite
  installation: classify `task_reload_required`, do not install again, and resume the preserved
  request in one new Codex task outside the source checkout.
- missing declared MCP dependency in a fresh task while the exact enabled suite plugin and all
  three MCP registrations remain present: classify `plugin_discovery_stale`, restart Codex once,
  and retry in one new task. Do not reinstall or add duplicate MCP registrations.
- missing declared MCP dependency in a fresh task with incomplete Codex plugin or MCP inventory:
  stop before annotation, report an incomplete suite deployment, request explicit permission once
  to install or repair the complete repository suite, and resume in a new task after discovery.
- missing companion registration in another MCP client: explain how to register the existing
  executable, then stop.
- missing checkout, interpreter, or model resources: identify the missing deployment component and
  ask permission only for the missing installation or repair action before changing it.
- incompatible runtime, state root, output root, or device policy: return the companion's stable
  diagnostic and the named operator action. Do not work around policy in the Skill.

Suite installation permission applies once to each new suite installation root. An installed
`local_ready` deployment does not repeat that question for later FASTA jobs; a separate new root is
a separate first installation.

The installer success fields `new_task_required=true`, `current_task_reload_supported=false`, and
`repeat_installation_required=false` are activation state, not a failed installation. A task cannot
use MCP tools added after its tool snapshot was created; that is a stale tool snapshot. Never turn
that state into a second install request: do not request or perform another installation; use a new
task first.

An explicit CUDA or Apple MPS readiness or policy failure stops the requested GPU job; do not
substitute CPU or automatic device selection. Installing or replacing PyTorch, CUDA, Metal support,
drivers, or other runtime resources still requires separate permission.

DeepKOALA is the preferred first route for protein FASTA unless the user explicitly selected
another annotator. In that case, this Skill stops and the independent core stage can resume only
after the selected workflow supplies supported KO evidence. If the user instead declines a
requested suite action, remain stopped until a user-selected route supplies that evidence.

The companion must use an existing official checkout and existing local resources. Multi-domain
execution additionally requires deployment `allow_multi`, a direct trusted profile directory, a
direct trusted absolute `hmmsearch` executable, and `multi_ready=true`. These optional dependencies
are operator-managed and may be provisioned separately after explicit user authorization; this
Skill does not configure them. The companion must not automate the GenomeNet web form or make
network requests.

## Stable file contract

A successful job provides:

- handoff `schema_version="2"` and `tool_version`;
- the original allowlisted absolute protein FASTA path;
- an absolute `deepkoala_annotations.csv` path;
- an absolute `deepkoala_run_report.md` path;
- `input_format="deepkoala_detailed"`;
- bounded `output_coverage` counts for input sequences, output rows, distinct output sequence IDs,
  missing input sequences, and unexpected output sequences;
- source provenance without workflow digests; and
- model, installed resource date, fixed execution parameters, and timestamps.

The execution parameters include the actual boolean `multi` value. In multi-domain mode, a fully
empty prediction, score, annotation marker, and coordinate tuple is an unclassified row rather than
a KO assignment; single-domain empty predictions and partially empty or malformed evidence are
invalid.

Before publishing a successful handoff, the companion proves that output sequence IDs cover
all and only the unique input FASTA IDs. Single-domain output has exactly the requested `topk`
rows per input sequence. Multi-domain output has at least one row per input sequence and may retain
multiple domain or top-k rows. Missing and unexpected counts are therefore zero in every successful
version 2 handoff. Input IDs remain private process memory and are never returned as a list or digest.

Pass the CSV path and provenance to the independent KO-analysis stage first; do not infer Core's
allowed-root policy from deployment topology. Only the exact typed `file_path` rejection below may
route the same successful job through the companion's bounded resource. The Skill must not parse,
transform, or validate CSV rows itself.

The companion accepts a deployment-selected generated detailed-CSV limit up to 1 GiB and validates
and publishes that file with bounded memory. Pass every successful output unchanged to Core. The
supported suite installer requires Core's allowed roots to cover every DeepKOALA output root, so an
allowed stable annotation path is the normal and large-result handoff. The original FASTA
`input_path` is retained as provenance without being opened or required beneath a Core allowed
root. Core uses the same compact sorted unique accepted-KO analysis view for file and bounded inline
inputs. Request full
normalization separately when record-level evidence or protein mappings are required and the input
fits that operation's separate full-record limits; never truncate a large file to make it fit.

Treat private job identifiers and resource URIs as process-scoped. Stable output-directory files,
not a private identifier, are the cross-MCP handoff.

### Controlled resource fallback

Prefer `annotations_path`. Use the fallback only after a successful handoff when Core returns
`ANALYSIS_CONFIGURATION_INVALID` with the typed message
`A local handoff path is outside the configured allowed roots.` and a `safe_details` entry of
`field="file_path"` for that path. The same message with `field="output_directory"` is not a
handoff failure and must not trigger this fallback. Do not use it to hide malformed
CSV, an expired or deleted job, an unsupported handoff version, or another Core validation error.
The original FASTA `input_path` is provenance only and does not trigger this fallback.
Do not rerun DeepKOALA, copy or rewrite the CSV, weaken or change allowed-root policy in a running
server, or retry the same unreadable path. Inspect the successful job's `output_bytes` first. The
inline fallback limit is 5,000,000 bytes and is separate from the companion's generated-file limit.

When `output_bytes` exceeds 5,000,000, do not read the annotation resource, follow its page chain,
place its bytes in a prompt, or send `annotations.text`. Stop and report a deployment configuration
failure: Core must be restarted with allowed roots that cover the DeepKOALA output roots,
normally by repairing the complete suite deployment. Do not copy the CSV or change either server's
running path policy. The stable CSV remains the resumption point after the shared handoff roots are
available in a new task.

Only when `output_bytes` is at most 5,000,000, read the handoff's `annotations_resource_uri` while
the process-scoped job remains retained. A direct `text/csv` response is the complete payload. For
a paged `application/json` response, require resource-page `schema_version="1"`,
`artifact="annotations"`, and `encoding="base64"`; follow only the returned `next_uri` chain,
reject repeated URIs, require contiguous offsets and stable `total_bytes`, verify
each `returned_bytes` value, and require the final byte count and stable `total_bytes` to equal the
job's `output_bytes`. Decode the completed payload as strict UTF-8 without parsing or transforming
CSV rows.

Resume the independent Core Skill with exactly one nested annotation payload selector:
`annotations.text` contains the reconstructed content and `annotations.file_path` is omitted.
Preserve the handoff's `input_format` and `source` unchanged. Put `analysis_unit` and `sample_id`
only inside `annotations` when they are supplied; never repeat them at the top level. Complete this
transfer before deleting the job record because deletion invalidates the process-scoped resource.

## Automatic cross-Skill continuation

When the original user request includes downstream KEGG analysis, a successful annotation stage
continues with the installed `kegg-ko-analysis` Skill using the returned `annotations_path`,
`input_format`, and `source` values unchanged when the path is shared. The transition uses the
stable CSV rather than the job identifier and does not require the user to copy a path, repeat the
request, or approve an already requested analysis stage. On the exact typed `file_path`
allowed-root failure, follow the controlled resource fallback above without inventing another
transition.

When the original request also includes graphics, retain that goal for the later
`kegg-pathway-rendering` stage. Do not interpret that goal here, and do not call a core or renderer
MCP from this Skill. A failed or unready annotation stage has no valid downstream handoff, so stop
with its specific route state instead of continuing. If a required downstream component is
unavailable, preserve the requested formats and target scope for resumption after the suite is
repaired and discovered in a new Codex task.
