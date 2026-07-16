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

## Protein FASTA without KO evidence

Use the ordered local stdio route `deepkoala-mcp -> kegg-mcp -> kegg-render-mcp`:

1. Route preparation, explicit acknowledgement, bounded polling, and the detailed-CSV handoff
   through `$kegg-ko-analysis` and the explicitly configured DeepKOALA companion.
2. Keep the successful DeepKOALA job until `kegg-mcp` has imported the evidence and atomically
   written the complete version 2 output bundle.
3. Pass the controlled absolute renderer-input path, never the DeepKOALA or core private result
   identifier, to the renderer.
4. Delete the DeepKOALA job only after the bundle succeeds and its private output is no longer
   required.

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
