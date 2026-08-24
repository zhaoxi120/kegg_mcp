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

Suite installation permission applies to each installation or managed in-place update that fetches
and installs the pinned DeepKOALA runtime. An installed `local_ready` deployment does not repeat
that question for later FASTA jobs.

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

## Direct local FASTA intake

DeepKOALA accepts an explicit absolute local FASTA path without an input-directory allowlist and
privately stages the validated file before execution. Pass a Codex drag-and-drop path, a file under
`Downloads`, or a project path unchanged; do not create a shell copy or add its parent directory to
deployment configuration. A caller-facing symlink resolves to its canonical readable regular-file
target, which is retained as provenance without being reopened by Core.

If `run_deepkoala_job` returns `PATH_NOT_ALLOWED` with `The FASTA path is unavailable or not a
supported local file.`, the path is relative, contains traversal, does not resolve to a readable
regular file, is unavailable, or changed during intake. Report that actual path condition; do not
classify it as an input-root mismatch or reinstall the suite.

## Stable file contract

A successful job provides:

- handoff `schema_version="2"` and `tool_version`;
- the original explicit absolute protein FASTA path;
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

Pass the CSV path and provenance directly to the independent KO-analysis stage. Core accepts the
explicit readable local file independently of its configured default output roots. The Skill must
not parse, transform, or validate CSV rows itself.

The companion accepts a deployment-selected generated detailed-CSV limit up to 1 GiB and validates
and publishes that file with bounded memory. Pass every successful output unchanged to Core. No
shared-root coverage or resource-to-inline recovery route is needed. The resolved FASTA
`input_path` is retained as provenance rather than another Core input. Core uses the same compact
sorted unique accepted-KO analysis view for file and bounded inline inputs. Request full
normalization separately when record-level evidence or protein mappings are required and the input
fits that operation's separate full-record limits; never truncate a large file to make it fit.

Treat private job identifiers and resource URIs as process-scoped. Stable output-directory files,
not a private identifier, are the cross-MCP handoff.

## Automatic cross-Skill continuation

When the original user request includes downstream KEGG analysis, a successful annotation stage
continues with the installed `kegg-ko-analysis` Skill using the returned `annotations_path`,
`input_format`, and `source` values unchanged. The transition uses the
stable CSV rather than the job identifier and does not require the user to copy a path, repeat the
request, or approve an already requested analysis stage. Do not replace this direct handoff with a
process-scoped resource URI or an inline copy.

When the original request also includes graphics, retain that goal for the later
`kegg-pathway-rendering` stage. Do not interpret that goal here, and do not call a core or renderer
MCP from this Skill. A failed or unready annotation stage has no valid downstream handoff, so stop
with its specific route state instead of continuing. If a required downstream component is
unavailable, preserve the requested formats and target scope for resumption after the suite is
repaired and discovered in a new Codex task.
