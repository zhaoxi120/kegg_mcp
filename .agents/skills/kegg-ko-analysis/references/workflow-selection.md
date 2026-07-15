# Workflow selection

Choose the route from KO evidence already supplied. Do not ask the user to restate readable input.

## Protein FASTA without K numbers

The core `kegg-mcp` server does not annotate proteins. If the optional local `deepkoala-mcp`
companion is discovered, follow [deepkoala-companion.md](deepkoala-companion.md). Otherwise route
the request to an independent annotation Skill and MCP. Resume core analysis only from a controlled
absolute annotation path and source provenance, not a private result identifier or workflow hash.

## Plain K numbers

- Preserve the user's analysis unit and context.
- Prefer `analyze_ko_annotations`. Supply explicit targets when the question names them; otherwise
  allow the high-level tool to discover canonical KO-reference pathways from accepted K numbers.
- Resolve a named target from supported KEGG evidence rather than guessing an identifier.
- Do not recommend or invoke annotation software when usable KO evidence is already present.

## Annotation table

1. Prefer the high-level tool with `file_path` and `output_directory` when the user wants analysis.
   Use `normalize_ko_annotations` alone when the user wants only a reusable normalized table.
2. Let the server auto-detect unambiguous common columns and report its decision. Supply an
   explicit mapping only when columns are ambiguous or non-standard.
3. Preserve raw source decisions, scores, thresholds, ranks, domain coordinates, protein names,
   versions, timestamps, and the original input path.
4. Run strict analysis first. Offer lenient analysis only when the named policy produced
   `uncertain` records.
5. Use output-bundle files for later stages. A private `result_id` is only a same-session
   optimization.

## Multiple KO sets

- Use `compare_ko_sets` with the same evidence policy and compatible KEGG reference provenance.
- Recompute under one reference retrieval when provenance is incompatible.
- Describe shared and set-specific KOs as deterministic set differences, not differential
  abundance, enrichment, or biological specificity.

## Pathway rendering

Complete analysis first. If graphics are requested, pass the canonical `render_input.json` from
the output bundle to an independent rendering Skill and MCP. Do not parse KGML or generate images
inside this Skill.

## Requests that need explanation rather than analysis

- A K number is an annotation, not experimentally validated evidence. A K number or module result
  does not prove pathway activity, flux, or phenotype. Explain this directly; run a lookup only if
  the user also asks for database context.
- Never assign or guess a K number from a gene name. Ask for an existing stable identifier with an
  organism for a supported mapping, or route protein sequence annotation to its independent Skill.
- Decline statistical enrichment, abundance analysis, nucleotide assembly, alignment, and
  non-KEGG ontology analysis as MCP capabilities. Connect them only when the user explicitly asks
  for a separate KO/KEGG step and provides appropriate inputs.
