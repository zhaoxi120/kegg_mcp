---
name: kegg-mcp
description: Route protein FASTA, K numbers, KO annotation tables, KEGG module or pathway questions, metabolic reconstruction requests, and descriptive comparisons of multiple KO sets through the local kegg-mcp server. Use when a user needs a KO-annotation workflow or cautious KO/KEGG analysis. Do not use for general gene-expression analysis, nucleotide assembly, sequence alignment, statistical enrichment, or non-KEGG ontology analysis unless the user explicitly asks to connect that work to KO/KEGG analysis.
---

# KEGG KO analysis

## Route the request

1. Inspect the supplied content before asking questions. Identify the data type and whether the
   analysis unit is a single genome, MAG, isolate proteome, pangenome, metagenomic community,
   mixed collection, or unknown.
2. Read [workflow-selection.md](references/workflow-selection.md), select the smallest applicable
   workflow, and ask only for information that changes the route or interpretation.
3. If K numbers are absent, explain external annotation choices using
   [annotation-tools.md](references/annotation-tools.md). Read
   [deepkoala.md](references/deepkoala.md) only when DeepKOALA is chosen or supplied.
4. If K numbers are already present, skip sequence-annotation guidance and use the MCP server.

## Use the MCP server

- Prefer `analyze_ko_annotations` for the common inline KO-to-module/pathway workflow.
- Use `normalize_ko_annotations`, `get_kegg_entries`, `map_ko_ids`, `analyze_modules`,
  `analyze_pathways`, or `compare_ko_sets` only when the user needs the corresponding primitive or
  an advanced staged workflow. Let the tools perform validation, normalization, and analysis.
- Use `get_server_status` only when configuration, offline state, or KEGG eligibility is relevant.
- Follow the discovered tool schemas. Do not fabricate parameters, identifiers, result sections,
  or successful retrievals.
- Retrieve full artifacts through the returned result resource when a bounded preview is
  insufficient. Treat result identifiers as opaque and session-scoped.

## Interpret the result

1. Read [confidence-policy.md](references/confidence-policy.md) before discussing strict, lenient,
   uncertain, or rejected evidence.
2. Read [module-interpretation.md](references/module-interpretation.md) for MODULE or pathway
   results. Keep exact module completion separate from pathway KO coverage.
3. Apply [reporting-policy.md](references/reporting-policy.md) to every user-facing summary.
4. Surface missing or unsupported content, stale-cache state, retrieval provenance, truncation,
   and analysis-unit limitations.
5. Never infer a K number from a protein sequence, gene name, product name, or unsupported
   identifier. Recommend an evidence-producing annotation or mapping workflow instead.
