# Workflow selection

Choose the route from KO evidence already supplied. Do not ask the user to restate readable input.

## Protein FASTA without K numbers

The core `kegg-mcp` server does not annotate proteins. Always discover the optional local
`deepkoala-mcp` companion first and follow [deepkoala-companion.md](deepkoala-companion.md).
If it is absent or unready, report the precise route state and ask permission for the required
installation, registration, download, allowed-root change, or repair. Stop when permission is
declined. If it is ready, automatically prepare, submit, and poll the local job without a repeated
per-job confirmation. There is no remote execution fallback: never open, submit to, or automate the
DeepKOALA web form. GenomeNet does not provide a DeepKOALA API for MCP automation. Resume core
analysis only from a controlled absolute annotation path and source provenance, not a private
result identifier or workflow hash.

Keep the successful companion job until `analyze_ko_annotations` has imported its controlled
detailed CSV and written the complete output bundle. Do not delete the retained job after only a
preview or partial analysis succeeds.

## Plain K numbers

- Preserve the user's analysis unit and context.
- Prefer `analyze_ko_annotations`. Supply explicit targets when the question names them; otherwise
  allow the high-level tool to discover canonical KO-reference pathways from accepted K numbers.
- For a Top-N request, set `pathway_selection.mode="top_detected"` and the requested bounded
  `top_n`. Do not call `map_ko_ids` and aggregate its full rows in the model context.
- Resolve a named target from supported KEGG evidence rather than guessing an identifier.
- Do not recommend or invoke annotation software when usable KO evidence is already present.

## Annotation table

1. Prefer the high-level tool with `file_path` and a new or empty `output_directory` when the user
   wants analysis. Use `normalize_ko_annotations` alone when the user wants only a reusable
   normalized table.
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

## Existing renderer handoff

If the user supplies a `render_input.json`, route its controlled absolute path to the independent
visualization Skill and renderer first. A compatible version 2 handoff skips annotation and core
analysis. Let the renderer validate the schema; never infer compatibility from a filename or
manually upgrade a version 1 preview.

## Pathway or MODULE rendering

When graphics are requested without a compatible handoff, request an allowed `output_directory`
and complete the smallest necessary analysis. Pass the canonical absolute `render_input.json`
version 2 path from the output bundle to the independent visualization Skill and
`kegg-render-mcp`. Read [visualization-handoff.md](visualization-handoff.md) before transfer. Do not
parse KGML, manipulate pixels, or generate images inside this Skill.

For protein FASTA without KO evidence, retain the local DeepKOALA job through the core bundle
write, call `analyze_ko_annotations(pathway_selection=top_detected, top_n=N)` for a Top-N request,
and then pass the version 2 handoff. Do not parse the detailed CSV, KO-to-pathway rows, ranking
artifact, or renderer input inside the Skill.

## Requests that need explanation rather than analysis

- A K number is an annotation, not experimentally validated evidence. A K number or module result
  does not prove pathway activity, flux, or phenotype. Explain this directly; run a lookup only if
  the user also asks for database context.
- Never assign or guess a K number from a gene name. Ask for an existing stable identifier with an
  organism for a supported mapping, or route protein sequence annotation to its independent Skill.
- Decline statistical enrichment, abundance analysis, nucleotide assembly, alignment, and
  non-KEGG ontology analysis as MCP capabilities. Connect them only when the user explicitly asks
  for a separate KO/KEGG step and provides appropriate inputs.
