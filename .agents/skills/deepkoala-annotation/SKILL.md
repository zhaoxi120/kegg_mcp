---
name: deepkoala-annotation
description: Run a configured local DeepKOALA companion on an allowlisted protein FASTA and produce a stable detailed-CSV annotation handoff plus run report. Use when a user explicitly asks for DeepKOALA annotation, supplies a protein FASTA without KO evidence, or wants to resume or inspect a local DeepKOALA job. Do not use for KO normalization, KEGG retrieval, MODULE or pathway analysis, rendering, model installation, or web-form automation.
---

# DeepKOALA annotation

## Run the local annotation stage

1. Inspect the protein FASTA path and requested output location. Require controlled absolute paths;
   do not copy sequences into a prompt or send them to a remote service.
2. Call `get_deepkoala_runner_status`. If the companion is absent or unready, report its stable
   route state and ask permission only for the missing installation, registration, download, or
   repair action. Never install, download, or replace DeepKOALA resources silently.
3. Treat an explicit request to annotate the FASTA as authorization to call
   `run_deepkoala_job` once. Do not ask for an ACK, notice digest, or second confirmation. Let the
   companion enforce model, device, timeout, input, concurrency, and no-download policy.
4. Poll `get_deepkoala_job` at bounded intervals until a terminal state. Call
   `cancel_deepkoala_job` only for a user cancellation, an agreed deadline, or safe recovery from
   a lost client operation.
5. On success, return the companion-provided absolute `deepkoala_annotations.csv` and
   `deepkoala_run_report.md` paths, schema/tool versions, original FASTA path, model parameters,
   timing, and caveats. Never parse or normalize the CSV in this Skill.
6. Keep the stable handoff files for the next independent stage. Use `delete_deepkoala_job` only
   when the user requests cleanup; job deletion must not be presented as deletion of already
   committed output-directory files.

Read [deployment-and-handoff.md](references/deployment-and-handoff.md) when status is unready, a
policy check fails, or another MCP client must consume the output.

## Preserve evidence boundaries

- DeepKOALA output is computational annotation evidence, not experimental validation.
- A rejected or below-threshold prediction is not evidence that a function is absent.
- Do not alter thresholds, infer K numbers, compare scores across tools, or select strict/lenient
  evidence here; those decisions belong to the independent `kegg-ko-analysis` stage.
- Do not launch subprocesses, inspect weights, parse output, or implement job control in the Skill.
  Use only declared `deepkoala-mcp` tools and their structured results.
