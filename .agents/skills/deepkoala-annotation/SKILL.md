---
name: deepkoala-annotation
description: Run a configured local DeepKOALA companion on an allowlisted protein FASTA and produce a stable detailed-CSV annotation handoff plus run report. Use when a user explicitly asks for DeepKOALA annotation, supplies a protein FASTA without KO evidence, or wants to resume or inspect a local DeepKOALA job. Do not use for KO normalization, KEGG retrieval, MODULE or pathway analysis, rendering, model installation, or web-form automation.
---

# DeepKOALA annotation

## Run the local annotation stage

1. Inspect the protein FASTA path and requested output location. Require controlled absolute paths;
   do not copy sequences into a prompt or send them to a remote service. A user-specified output
   directory always wins and is passed unchanged. Otherwise, omit `output_directory`; the companion
   allocates a fresh directory beneath its configured project output root. Do not guess an output
   root from the FASTA path or create a directory with a shell command. An explicit directory may
   be new or empty and owner-only; never select an existing non-empty directory.
2. DeepKOALA is the preferred first FASTA annotation route unless the user explicitly selected
   another annotator. In that case, stop this Skill and resume core analysis only after the selected
   route supplies supported KO evidence. Otherwise require the declared `deepkoala-mcp` dependency
   and its status and job tools to be exposed in the current task. Before acting on missing or
   unready tools, read the canonical readiness matrix in
   [deployment-and-handoff.md](references/deployment-and-handoff.md). Stop before annotation until
   that route permits a job.
3. Call `get_deepkoala_runner_status`. Inspect `device_policy`, `allowed_devices`,
   `cuda_available`, and `mps_available` separately, as well as `allow_multi` and `multi_ready`.
   Report the stable route state and follow the referenced readiness action. Never install,
   download, or replace required resources silently.
4. Before the first `run_deepkoala_job` call, make a small LLM routing decision between `frag` and
   `full` from the user's description and available project provenance. An explicit user model
   choice wins. Select `frag` when the input is described as fragmented, truncated, partial, or as
   fragment-prone metagenomic or assembly-derived protein calls. Select `full` for described
   complete, reference, or isolate proteins and as the conservative default when provenance is
   ambiguous. Do not invent a sequence-length cutoff, infer completeness from length alone, or
   parse sequences in the Skill. Briefly report the selected model and reason, then pass `model`
   explicitly to `run_deepkoala_job`; this notice is not a confirmation gate. Require the selected
   model to appear in `allowed_models` with installed resources reported by status. If it is not
   ready, stop and report that deployment gap rather than silently substituting another model.
5. Before the first `run_deepkoala_job` call in the current Codex task, explicitly tell the user
   that the default job uses the `cpu` device. Also tell them that GPU execution requires an
   explicit request to the LLM, which will check readiness and select the deployment's explicit
   `cuda` or `mps` device only when status allows it. This is an informational first-run notice, not
   a confirmation gate: do not pause, wait for a reply, or repeat the notice in the same task before
   continuing an already authorized CPU job.
6. Treat an explicit request to annotate the FASTA as authorization to call
   `run_deepkoala_job` once. Omit `model_date` for the default call; supply it only when the user
   explicitly requests a specific installed model version. Do not ask for an ACK, notice digest, or
   second confirmation. Omit `device` for the default CPU call. Pass `device=cuda` only when the
   user explicitly requests GPU execution and status reports both `cuda` in `allowed_devices` and
   `cuda_available=true`. Pass `device=mps` only when status instead reports both `mps` in
   `allowed_devices` and `mps_available=true`. If the deployment's explicit accelerator is not
   both allowed and available, stop before annotation, report the required deployment change, and
   never silently substitute CPU or `device=auto`. A GPU request authorizes the device choice for
   a new job; it does not authorize installing or replacing PyTorch, CUDA, Metal support, or
   drivers, which requires separate permission. Let the companion enforce model, device, timeout,
   input, concurrency, and no-download policy. Omit `multi` by default. Pass
   `multi=true` only when the user explicitly requests multi-domain annotation and status reports
   both `allow_multi=true` and `multi_ready=true`; keep `batch_size=1` because upstream multi-domain
   execution does not use configurable batching.
7. Poll `get_deepkoala_job` at bounded intervals until a terminal state. Call
   `cancel_deepkoala_job` only for a user cancellation, an agreed deadline, or safe recovery from
   a lost client operation.
8. On success, require handoff `schema_version="2"` and return the companion-provided absolute
   `deepkoala_annotations.csv` and `deepkoala_run_report.md` paths, schema/tool versions, original
   FASTA path, model parameters, timing, caveats, and the bounded `output_coverage` aggregate
   counts. Explicitly state the resolved model name and model version reported by the service, plus
   the actual reported `multi` value. Never parse or normalize the CSV in this Skill.
9. Keep the stable handoff files for the next independent stage. Use `delete_deepkoala_job` only
   when the user requests cleanup; job deletion must not be presented as deletion of already
   committed output-directory files.

## Continue the original request across focused Skills

- If the original request ends at protein annotation, return the stable CSV, run report, and
  source provenance, then stop.
- If the original request also asks for KO analysis, MODULE evaluation, descriptive pathway KO
  coverage, deterministic KO-set comparison, or reporting work,
  automatically continue with the installed `kegg-ko-analysis` Skill after the annotation job
  succeeds. Prefer the returned
  `annotations_path` and pass
  `input_format="deepkoala_detailed"` and the `source` object unchanged. Core derives its compact
  sorted unique accepted-KO analysis view from this handoff; request full normalization separately
  only when record-level evidence is needed. The companion already caps its generated detailed CSV
  at 5,000,000 bytes.
  Do not ask the user to copy the path, send another prompt, restate the analysis goal, or confirm
  continuation. During the normal shared-path transition, do not read, parse, or rewrite the CSV.
  Unless the user specified that stage's output directory, let Core allocate its fresh project
  output directory.
- If Core rejects that successful path handoff with `ANALYSIS_CONFIGURATION_INVALID`, the typed
  message `A local handoff path is outside the configured allowed roots.`, and a `safe_details`
  entry of `field="file_path"`, do not rerun DeepKOALA,
  copy the CSV, weaken either server's allowed roots, or retry the same path. While the job remains
  retained, read the returned `annotations_resource_uri` through the controlled resource fallback
  defined in the handoff guide, reconstruct the byte-identical strict UTF-8 payload, and resume
  `kegg-ko-analysis` with nested `annotations.text` rather than `annotations.file_path`. Pass the
  original `input_format` and `source` unchanged, never send both payload selectors, and keep
  annotation context only inside the nested `annotations` object.
  The same message with `field="output_directory"` is an output-location error and must not trigger
  this annotation-resource fallback.
- If the original request also asks for graphics, preserve its requested formats and target scope
  as a downstream goal. The KO-analysis stage can then continue to the installed
  `kegg-pathway-rendering` Skill after it writes a compatible `render_input.json`; this Skill must
  not call either downstream MCP itself. Unless the user supplies a rendering directory, let the
  renderer allocate its fresh project output directory.
- Continue only from a successful stable handoff. The referenced handoff guide owns unavailable
  downstream-component and resumption behavior; never rerun DeepKOALA to repair a later stage.

Read [deployment-and-handoff.md](references/deployment-and-handoff.md) when status is unready, a
policy check fails, or another MCP client must consume the output.

## Preserve evidence boundaries

- DeepKOALA output is computational annotation evidence, not experimental validation.
- A rejected or below-threshold prediction is not evidence that a function is absent.
- Do not alter thresholds, infer K numbers, compare scores across tools, or select analysis K
  numbers here; those decisions belong to the independent `kegg-ko-analysis` stage.
- Do not launch subprocesses, inspect weights, parse output, or implement job control in the Skill.
  Use only declared `deepkoala-mcp` tools and their structured results.
