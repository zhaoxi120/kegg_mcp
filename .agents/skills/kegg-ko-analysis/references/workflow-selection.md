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

## Automatic cross-Skill continuation

- When the immediately preceding `deepkoala-annotation` stage produced the evidence, consume the
  returned stable CSV path, `input_format`, and source provenance unchanged. Do not ask the user to
  copy the path, repeat the request, or confirm a KEGG-analysis stage already present in the
  original request. Do not rerun or reinterpret annotation.
- When the original request also asks for graphics, retain its formats and target scope. After the
  core writes a compatible `render_input.json`, continue with the installed
  `kegg-pathway-rendering` Skill using that path unchanged. Do not repeat analysis in the rendering
  transition.
- When graphics were not requested, stop after the core report. Continue only after a successful
  handoff; never treat an upstream failure as empty KO evidence.

## Multiple KO sets

- Use `compare_ko_sets` with compatible evidence modes and reference provenance.
- Describe results as deterministic set membership and functional-reference differences, not
  differential abundance, enrichment, or biological specificity.

## Out-of-scope starting points

- Protein FASTA without KO evidence starts with the independent `deepkoala-annotation` Skill and
  returns here automatically only when the original request includes KEGG analysis.
- A compatible `render_input.json` continues directly with the independent
  `kegg-pathway-rendering` Skill without rerunning core analysis.
- Statistical enrichment, abundance testing, nucleotide assembly, sequence alignment, and
  non-KEGG ontologies require a separate workflow.
