---
name: kegg-ko-analysis
description: Route K numbers, KO annotation tables, KEGG module or pathway questions, metabolic reconstruction requests, descriptive comparisons of multiple KO sets, and an optional local DeepKOALA companion with local-first routing into cautious KO/KEGG analysis. Use when a user has KO evidence or asks to produce it from protein FASTA, including installation-permission routing when the local companion is unavailable. Do not use this Skill to implement annotation inference, manage models or weights, launch arbitrary subprocesses, perform pathway rendering or MODULE rendering, or perform general gene-expression analysis, nucleotide assembly, sequence alignment, statistical enrichment, or non-KEGG ontology analysis.
---

# KEGG KO analysis

## Route the request

1. Inspect the supplied input before asking questions. If it is an existing compatible
   `render_input.json` version 2, skip annotation and analysis and hand its controlled absolute
   path to the independent visualization Skill and renderer MCP.
2. Otherwise, identify whether the analysis unit is a single genome, MAG, isolate proteome,
   pangenome, metagenomic community, mixed collection, or unknown.
3. Read [workflow-selection.md](references/workflow-selection.md), select the smallest applicable
   workflow, and ask only for information that changes the route or interpretation.
4. If the input is protein FASTA without K numbers, read
   [deepkoala-companion.md](references/deepkoala-companion.md). Always attempt the configured local
   `deepkoala-mcp` route first, and make `get_deepkoala_runner_status` the first annotation-tool
   call after discovery. Never open, submit to, or automate the DeepKOALA web service. If the local
   runtime or companion is absent or unready, report the precise local route state and ask whether
   to install, register, download, or repair only the missing deployment component. When the
   companion is registered, ready, and already authorized for the supplied local FASTA, continue
   through prepare, submit, and bounded polling without a second per-job confirmation. DeepKOALA
   execution is local-only because GenomeNet does not provide a DeepKOALA API for MCP automation.
   Never send FASTA to the core `kegg-mcp` server.
5. If the user requests pathway or MODULE graphics, read
   [visualization-handoff.md](references/visualization-handoff.md). Obtain an allowed
   `output_directory` before analysis, require `render_input.json` version 2, and hand its
   controlled absolute path to the independent visualization Skill and `kegg-render-mcp`.
   Do not implement rendering here.

## Use the MCP server

- Do not recommend annotation software when the user already supplies K numbers or usable KO
  annotation evidence.
- Prefer `analyze_ko_annotations` for the common KO-to-module/pathway workflow. When the user
  supplies a shared annotation file, pass its absolute `file_path`, source provenance, and an
  allowed new or empty `output_directory`; do not copy a private result identifier across MCP
  processes.
- When the user asks for the most detected pathway or a Top-N pathway result, pass
  `pathway_selection={"mode":"top_detected","top_n":N,"metric":"unique_selected_ko_count"}`
  to `analyze_ko_annotations`. Let the server aggregate, rank, select, and retain the full
  KO-to-pathway detail. Do not parse or rank relationship rows in the Skill.
- Use `normalize_ko_annotations`, `get_kegg_entries`, `map_ko_ids`, `analyze_modules`,
  `analyze_pathways`, or `compare_ko_sets` only for the corresponding primitive or an advanced
  staged workflow. Let the tools perform validation, normalization, and analysis exactly once.
- Use `probe_kegg_connectivity` before a network-dependent workflow when connectivity is unknown.
  Treat disabled access or a failed probe as a deployment issue, not a biological result.
- Use `get_server_status` only when configuration, access state, or KEGG eligibility is relevant.
  KEGG academic or licensed authorization is deployment configuration and is never a per-task
  confirmation.
- Follow discovered tool schemas. Do not fabricate parameters, identifiers, result sections, or
  successful retrievals. Do not request or verify workflow hashes.
- Treat companion job identifiers as opaque. The companion owns FASTA validation, execution,
  cancellation, and output bounds; the core importer remains the only normalization authority.
- A successful `prepare_deepkoala_job` response is a non-blocking execution notice. Preserve its
  model, resource date, device policy, bounds, and input summary in the workflow record, then call
  `submit_deepkoala_job` with only the opaque `job_id` and poll `get_deepkoala_job` to a terminal
  state. Do not ask for routine inference confirmation when the deployment is already ready.
- Prefer the stable output bundle for cross-process handoff. Retrieve full retained artifacts only
  when a bounded preview is insufficient, and treat result identifiers as opaque and
  session-scoped.
- When the user requests immediate privacy cleanup, call `delete_analysis_result` only after every
  required retained section has been read and any requested output bundle has committed. Never
  treat an old or cross-session result identifier as recoverable.
- Keep a successful DeepKOALA job and its controlled output until the core import and complete
  output-bundle write have succeeded. Delete it only after the renderer handoff no longer depends
  on that private output.
- For the ordinary protein-FASTA-to-pathway workflow, keep the fixed local order
  `deepkoala-mcp -> analyze_ko_annotations(pathway_selection=top_detected) -> kegg-render-mcp`.
  Skip completed stages and pass only controlled absolute artifact paths plus source provenance.

## Interpret the result

1. Read [confidence-policy.md](references/confidence-policy.md) before discussing strict, lenient,
   uncertain, or rejected evidence.
2. Read [module-interpretation.md](references/module-interpretation.md) for MODULE or pathway
   results. Keep exact module completion separate from pathway KO coverage.
3. Apply [reporting-policy.md](references/reporting-policy.md) to every user-facing summary.
4. Surface missing or unsupported content, stale-cache state, retrieval provenance, truncation,
   and analysis-unit limitations.
5. Never infer a K number from a protein sequence, gene name, product name, or unsupported
   identifier. Route to an evidence-producing annotation or supported mapping workflow instead.
