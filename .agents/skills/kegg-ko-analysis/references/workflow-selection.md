# Workflow selection

Choose only from KO evidence already supplied. This Skill never runs an annotator or renderer.

## Plain K numbers

- Preserve the analysis unit and context.
- Prefer `analyze_ko_annotations`; provide named targets when requested. Otherwise omit
  `pathway_selection` and let the server independently choose the Top-5 MODULEs and Top-5 canonical
  KO reference pathways by unique selected-KO overlap.
- Use server-side Top-N selection instead of reading and ranking complete relationship rows.
- Automatic selection leaves current KEGG Global, Overview, and higher-level Overview maps in the
  retained ranking but excludes them from the renderable Top-N targets. If the user explicitly
  requests `ko01100`, do not send it through the regular-pathway Core/Renderer pipeline. Preserve
  that target for a separate model-native drawing step outside this Skill, and label the result as
  a model-generated conceptual diagram rather than a KEGG-derived coverage overlay.
- Treat MODULE overlap ranking as target selection, not MODULE completion or enrichment. Evaluate
  exact completion and required-block coverage separately from the selected MODULE definitions.
- Do not recommend annotation software when usable KO evidence already exists.

## Annotation table or detailed CSV

1. Use a controlled absolute `file_path`. Pass a user-specified new or empty `output_directory`
   unchanged; otherwise omit it so Core allocates a fresh directory beneath its configured project
   output root. Use `normalize_ko_annotations` alone only when the user wants a reusable normalized
   table.
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

- Protein FASTA without KO evidence starts with a user-selected annotator when one was explicit;
  return only when it supplies supported KO evidence. Otherwise prefer the independent
  `deepkoala-annotation` Skill. If that route is unavailable in the same task that just installed a
  registered suite, classify `task_reload_required`, do not install again, and resume in one new
  Codex task outside the source checkout. Only a fresh-task failure with incomplete plugin or MCP
  inventory requests explicit permission once to install or repair the complete suite. If the user
  declines that action, remain stopped until a selected route supplies supported KO evidence.
- A compatible `render_input.json` continues directly with the independent
  `kegg-pathway-rendering` Skill without rerunning core analysis.
- Statistical enrichment, abundance testing, nucleotide assembly, sequence alignment, and
  non-KEGG ontologies require a separate workflow.
