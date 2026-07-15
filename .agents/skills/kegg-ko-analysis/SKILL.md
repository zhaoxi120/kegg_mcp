---
name: kegg-ko-analysis
description: Route K numbers, KO annotation tables, KEGG module or pathway questions, metabolic reconstruction requests, descriptive comparisons of multiple KO sets, and optional local DeepKOALA companion handoff into cautious KO/KEGG analysis. Use when a user has KO evidence or explicitly wants an available DeepKOALA companion to produce it. Do not use this Skill to implement annotation inference, manage models or weights, launch arbitrary subprocesses, perform pathway rendering, or perform general gene-expression analysis, nucleotide assembly, sequence alignment, statistical enrichment, or non-KEGG ontology analysis.
---

# KEGG KO analysis

## Route the request

1. Inspect the supplied KO evidence before asking questions. Identify whether the analysis unit is
   a single genome, MAG, isolate proteome, pangenome, metagenomic community, mixed collection, or
   unknown.
2. Read [workflow-selection.md](references/workflow-selection.md), select the smallest applicable
   workflow, and ask only for information that changes the route or interpretation.
3. If the input is protein FASTA without K numbers, read
   [deepkoala-companion.md](references/deepkoala-companion.md). Use an explicitly configured local
   companion when it is available and ready; otherwise stop and route annotation to an independent
   annotation Skill and MCP. Never send FASTA to the core `kegg-mcp` server.
4. If the user requests pathway graphics, finish the KO analysis and hand off `render_input.json`
   to an independent rendering Skill and MCP. Do not implement rendering here.

## Use the MCP server

- Do not recommend annotation software when the user already supplies K numbers or usable KO
  annotation evidence.
- Prefer `analyze_ko_annotations` for the common KO-to-module/pathway workflow. When the user
  supplies a shared annotation file, pass its absolute `file_path`, source provenance, and an
  allowed `output_directory`; do not copy a private result identifier across MCP processes.
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
- Prefer the stable output bundle for cross-process handoff. Retrieve full retained artifacts only
  when a bounded preview is insufficient, and treat result identifiers as opaque and
  session-scoped.

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
