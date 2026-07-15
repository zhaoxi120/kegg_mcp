---
name: kegg-ko-analysis
description: Route K numbers, KO annotation tables, KEGG module or pathway questions, metabolic reconstruction requests, and descriptive comparisons of multiple KO sets through the local kegg-mcp server. Use when a user already has KO evidence and needs cautious KO/KEGG analysis. Do not use this Skill to execute protein annotation, DeepKOALA, pathway rendering, general gene-expression analysis, nucleotide assembly, sequence alignment, statistical enrichment, or non-KEGG ontology analysis.
---

# KEGG KO analysis

## Route the request

1. Inspect the supplied KO evidence before asking questions. Identify whether the analysis unit is
   a single genome, MAG, isolate proteome, pangenome, metagenomic community, mixed collection, or
   unknown.
2. Read [workflow-selection.md](references/workflow-selection.md), select the smallest applicable
   workflow, and ask only for information that changes the route or interpretation.
3. If the input is protein FASTA without K numbers, stop this Skill and route annotation to an
   independent annotation Skill and MCP. Do not execute or describe a DeepKOALA workflow here.
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
- Use `get_server_status` only when configuration, offline state, or KEGG eligibility is relevant.
  KEGG academic or licensed authorization is deployment configuration and is never a per-task
  confirmation.
- Follow discovered tool schemas. Do not fabricate parameters, identifiers, result sections, or
  successful retrievals. Do not request or verify workflow hashes.
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
