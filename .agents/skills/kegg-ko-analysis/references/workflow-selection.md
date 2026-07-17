# Workflow selection

Choose only from KO evidence already supplied. This Skill never runs an annotator or renderer.

## Plain K numbers

- Preserve the analysis unit and context.
- Prefer `analyze_ko_annotations`; provide named targets when requested, otherwise use bounded
  automatic canonical pathway discovery.
- Use server-side Top-N selection instead of reading and ranking complete relationship rows.
- Do not recommend annotation software when usable KO evidence already exists.

## Annotation table or detailed CSV

1. Use a controlled absolute `file_path` and a new or empty `output_directory` for analysis. Use
   `normalize_ko_annotations` alone only when the user wants a reusable normalized table.
2. Let the server infer unambiguous common columns and report the mapping. Supply an explicit
   column mapping only for ambiguous or non-standard tables.
3. Preserve raw source decisions, scores, thresholds, ranks, domains, protein names, source/model
   versions, timestamps, and the original absolute input path.
4. Run strict analysis with accepted K numbers. Add only policy-defined uncertain records to a
   requested lenient view.
5. Use stable bundle files for later MCP stages; do not pass a process-private result identifier.

## Multiple KO sets

- Use `compare_ko_sets` with compatible evidence modes and reference provenance.
- Describe results as deterministic set membership and functional-reference differences, not
  differential abundance, enrichment, or biological specificity.

## Out-of-scope starting points

- Protein FASTA without KO evidence belongs to the independent `deepkoala-annotation` Skill.
- A compatible `render_input.json` belongs to the independent `kegg-pathway-rendering` Skill.
- Statistical enrichment, abundance testing, nucleotide assembly, sequence alignment, and
  non-KEGG ontologies require a separate workflow.
