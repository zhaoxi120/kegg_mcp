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

1. Use a controlled absolute path in `file_path`. Pass a user-specified new or empty
   `output_directory` unchanged; the user-specified path wins. Otherwise, omit `output_directory`
   and let Core allocate a fresh directory beneath its configured project output root. Do not guess
   a root from the input path, create it with a shell command, or reuse a non-empty directory. Use
   `normalize_ko_annotations` alone only when the user wants a reusable normalized table.
2. Let the server infer unambiguous common columns and report the mapping. Supply an explicit
   column mapping only for ambiguous or non-standard tables.
3. Preserve raw source decisions, scores, thresholds, ranks, domains, protein names, source/model
   versions, timestamps, and the original absolute input path.
4. Run strict analysis with accepted K numbers. Add only policy-defined uncertain records to a
   requested lenient view.
5. Use stable bundle files for later MCP stages; do not pass a process-private result identifier.

## Automatic cross-Skill continuation

- When the immediately preceding `deepkoala-annotation` stage produced the evidence,
  consume its stable CSV handoff directly. Use the returned `annotations_path`, `input_format`, and
  `source` object unchanged; do not ask the user to restate the path, copy it, repeat the request,
  or confirm a KEGG-analysis stage already present in the original request.
  Do not rerun annotation or rewrite the CSV.
- When the original request also asks to render, visualize, draw, or export graphics, first require
  a successfully written, compatible `render_input.json`, then automatically continue with the installed
  `kegg-pathway-rendering` Skill. Pass the unchanged `render_input.json` path while
  preserving the requested formats and target scope. Do not ask the user to copy the path, and do
  not repeat analysis in the rendering transition.
- When the original request asks only for a core report, return it and stop. Other requests may
  continue downstream only after a successful handoff; never treat an upstream failure as empty KO
  evidence.
- When no rendering output path was specified, omit it and let the renderer allocate a fresh
  project output directory.

## Multiple KO sets

- Use `compare_ko_sets` with compatible evidence modes and reference provenance.
- Describe results as deterministic set membership and functional-reference differences, not
  differential abundance, enrichment, or biological specificity.

## Out-of-scope starting points

- If the user explicitly selected another annotator for protein FASTA without KO evidence, stop
  before a core call and resume only after that route supplies supported KO evidence. Otherwise
  prefer the installed `deepkoala-annotation` Skill as the independent first annotation route. If
  that route is unavailable in the same task that just installed a registered suite, classify
  `task_reload_required` and resume in one new Codex task outside the source checkout. The fields
  `new_task_required=true`, `current_task_reload_supported=false`, and
  `repeat_installation_required=false` identify a stale tool snapshot; do not request or perform
  another installation. Only a fresh-task failure with incomplete suite deployment inventory may
  request explicit permission once to install or repair the complete repository suite. If the user
  declines that action, remain stopped until a selected route supplies supported KO evidence.
- A compatible `render_input.json` continues directly with the independent
  `kegg-pathway-rendering` Skill without rerunning core analysis.
- Statistical enrichment, abundance testing, nucleotide assembly, sequence alignment, and
  non-KEGG ontologies require a separate workflow.
