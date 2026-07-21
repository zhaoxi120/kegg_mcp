---
name: deepkoala-annotation
description: Run a configured local DeepKOALA companion on an allowlisted protein FASTA and produce a stable detailed-CSV annotation handoff plus run report. Use when a user explicitly asks for DeepKOALA annotation, supplies a protein FASTA without KO evidence, or wants to resume or inspect a local DeepKOALA job. Do not use for KO normalization, KEGG retrieval, MODULE or pathway analysis, rendering, model installation, or web-form automation.
---

# DeepKOALA annotation

## Run the local annotation stage

1. Inspect the protein FASTA path and requested output location. Require controlled absolute paths;
   do not copy sequences into a prompt or send them to a remote service.
2. DeepKOALA is the preferred first FASTA annotation route unless the user explicitly selected
   another annotator. In that case, stop this Skill and resume core analysis only after the selected
   route supplies supported KO evidence. Otherwise require the declared `deepkoala-mcp` dependency
   and its status and job tools to be exposed in the current task. If they are unavailable in a
   Codex deployment, stop before annotation, preserve the original downstream goals, report an
   incomplete suite deployment, and request explicit permission once to install or repair the
   complete repository suite. If the user declines that action, remain stopped until a
   user-selected route supplies supported KO evidence. After a successful suite action, resume the
   original request in a new Codex task after the components are discovered.
3. Call `get_deepkoala_runner_status`. If the companion is unready, report its stable route state
   and ask permission only for the missing installation or repair action. Suite installation
   permission is requested once for each new installation root; an already installed `local_ready`
   deployment does not ask again for later FASTA jobs. Inspect `allow_multi` and `multi_ready`
   separately. Never install, download, or replace required resources silently.
4. Treat an explicit request to annotate the FASTA as authorization to call
   `run_deepkoala_job` once. Omit `model_date` for the default call; supply it only when the user
   explicitly requests a specific installed model version. Do not ask for an ACK, notice digest, or
   second confirmation. Let the companion enforce model, device, timeout, input, concurrency, and
   no-download policy. Omit `multi` by default. Pass `multi=true` only when the user explicitly
   requests multi-domain annotation and status reports both `allow_multi=true` and
   `multi_ready=true`; keep `batch_size=1` because upstream multi-domain execution does not use
   configurable batching.
5. Poll `get_deepkoala_job` at bounded intervals until a terminal state. Call
   `cancel_deepkoala_job` only for a user cancellation, an agreed deadline, or safe recovery from
   a lost client operation.
6. On success, return the companion-provided absolute `deepkoala_annotations.csv` and
   `deepkoala_run_report.md` paths, schema/tool versions, original FASTA path, model parameters,
   timing, and caveats. Explicitly state the resolved model name and model version reported by the
   service, plus the actual reported `multi` value. Never parse or normalize the CSV in this Skill.
7. Keep the stable handoff files for the next independent stage. Use `delete_deepkoala_job` only
   when the user requests cleanup; job deletion must not be presented as deletion of already
   committed output-directory files.

## Continue the original request across focused Skills

- If the original request ends at protein annotation, return the stable CSV, run report, and
  source provenance, then stop.
- If the original request also asks for KEGG KO, MODULE, pathway, metabolic-reconstruction, or
  reporting work, automatically continue with the installed `kegg-ko-analysis` Skill after the
  annotation job succeeds. Pass the returned `annotations_path`,
  `input_format="deepkoala_detailed"`, and `source` object unchanged. Do not ask the user to copy
  the path, send another prompt, restate the analysis goal, or confirm continuation. Do not read,
  parse, or rewrite the CSV during the transition.
- If the original request also asks for graphics, preserve its requested formats and target scope
  as a downstream goal. The KO-analysis stage can then continue to the installed
  `kegg-pathway-rendering` Skill after it writes a compatible `render_input.json`; this Skill must
  not call either downstream MCP itself.
- Continue only from a successful stable handoff. If a downstream Skill or its one declared MCP
  dependency is unavailable, stop without rerunning DeepKOALA, retain the unfinished downstream
  goal, and request explicit permission once to repair the complete suite. Resume in a new Codex
  task after the component is discovered.

Read [deployment-and-handoff.md](references/deployment-and-handoff.md) when status is unready, a
policy check fails, or another MCP client must consume the output.

## Preserve evidence boundaries

- DeepKOALA output is computational annotation evidence, not experimental validation.
- A rejected or below-threshold prediction is not evidence that a function is absent.
- Do not alter thresholds, infer K numbers, compare scores across tools, or select strict/lenient
  evidence here; those decisions belong to the independent `kegg-ko-analysis` stage.
- Do not launch subprocesses, inspect weights, parse output, or implement job control in the Skill.
  Use only declared `deepkoala-mcp` tools and their structured results.
