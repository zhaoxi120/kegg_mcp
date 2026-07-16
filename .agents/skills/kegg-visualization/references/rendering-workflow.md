# Rendering workflow

Choose the shortest valid route. Do not repeat a completed stage.

## Compatible renderer handoff

1. Call `get_renderer_status` and require readiness plus compatible schema version `2`.
2. Pass the supplied controlled absolute `render_input.json` path to
   `render_analysis_bundle`, `render_pathway`, or `render_module`.
3. Skip annotation and core analysis. Let the renderer validate the handoff and target identities.

Version 1 cannot be upgraded losslessly because it contains previews rather than complete renderer
evidence. Request a new analysis bundle through `$kegg-ko-analysis` and `kegg-mcp`; do not patch,
expand, or reinterpret the old JSON in the Skill.

## Existing K numbers or annotation table

Never invoke DeepKOALA when usable KO evidence already exists. Request an allowed
`output_directory`, route the evidence to `analyze_ko_annotations` through `$kegg-ko-analysis`, and
pass the completed bundle's controlled absolute version 2 path to `kegg-render-mcp`.
For a most-detected or Top-N request, include `pathway_selection.mode="top_detected"` and the
bounded `top_n`; do not call `map_ko_ids` and rank its relationship preview in the Skill.

## Protein FASTA without KO evidence

Use the ordered local stdio route `deepkoala-mcp -> kegg-mcp -> kegg-render-mcp`:

1. Route discovery and `get_deepkoala_runner_status` through `$kegg-ko-analysis`. If the local
   runtime or companion is missing or unready, request installation, registration, or repair
   permission. Never open or automate the DeepKOALA web form; GenomeNet provides no DeepKOALA API
   for MCP automation.
2. Route preparation, explicit acknowledgement, bounded polling, and the detailed-CSV handoff
   through `$kegg-ko-analysis` and the configured local DeepKOALA companion.
3. Keep the successful DeepKOALA job until `kegg-mcp` has imported the evidence and atomically
   written the complete version 2 output bundle.
4. Call `analyze_ko_annotations` with
   `pathway_selection={"mode":"top_detected","top_n":N,"metric":"unique_selected_ko_count"}`
   when the request asks for the most detected pathway or Top-N pathways. Let the core retain the
   complete ranking and relationships.
5. Pass the controlled absolute renderer-input path, never the DeepKOALA or core private result
   identifier, to the renderer.
6. Delete the DeepKOALA job only after the bundle succeeds and its private output is no longer
   required.

Do not parse the DeepKOALA CSV, KO-to-pathway rows, ranking artifact, or renderer input in the
Skill, and do not repeat a successfully completed stage.

## Renderer unavailable or incompatible

Stop without attempting local rendering in the Skill. Report whether the separately installed
`kegg-render-mcp` server is absent, unready, missing an allowed root, missing a requested format, or
does not support schema version `2`. Preserve the recoverable suggested action and do not install,
download, synthesize, or invoke an unrelated image tool as a fallback.

## Result lifecycle

Use the renderer's high-level tool for an ordinary request and a single-target tool only for a
targeted request. Return only renderer-provided `kegg-render://results/{render_id}` and
`kegg-render://results/{render_id}/{artifact}` URIs; the templates describe the returned shapes,
not instructions to construct a URI. Delete a retained result only by calling
`delete_render_result` under the agreed lifecycle; unknown, expired, deleted, and cross-scope IDs
are safe not-found states.
