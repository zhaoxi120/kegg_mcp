# Visualization handoff

Use this boundary when a user requests a KEGG pathway overlay or MODULE logic diagram. The
analysis Skill prepares evidence; the independent `kegg-visualization` Skill orchestrates the
functional local `kegg-render-mcp` server.

## Select the shortest route

- If a controlled absolute `render_input.json` version 2 already exists, skip annotation and core
  analysis and pass that path to the visualization workflow.
- If usable K numbers or an annotation table already exist, skip annotation. Request an allowed
  `output_directory`, call `analyze_ko_annotations`, and pass the resulting absolute renderer-input
  path.
- For protein FASTA without KO evidence, use the explicitly available DeepKOALA companion, retain
  its job through the core import and bundle write, call
  `analyze_ko_annotations(pathway_selection=top_detected, top_n=N)`, and then pass the version 2
  handoff.

Never pass a private or session-scoped `result_id` between MCP processes. Never copy the handoff to
an unapproved directory merely to satisfy an allowed-root check.
Never parse the DeepKOALA CSV, KO-to-pathway detail, ranking artifact, or renderer input in the
Skill. Do not repeat a successfully completed stage.

## Check compatibility and readiness

Before transfer, require `get_renderer_status` to report `ready=true`, support schema version `2`,
and expose the requested output format within its configured bounds. The renderer, not this Skill,
validates the file contents. Pass only the controlled absolute path returned by the core output
bundle.

If the renderer is absent or not ready, stop with an actionable deployment result. Explain that
the separately installed local stdio `kegg-render-mcp` server must be configured with compatible
allowed roots; do not attempt local rendering in the Skill. If version 1 is reported, request a new
analysis bundle because its preview cannot be upgraded losslessly. Preserve all other compatible-
schema, target, output-limit, access, and not-renderable errors without inventing a fallback.

## Preserve boundaries

Do not parse KGML, manipulate pixels, assign display colors, duplicate KO normalization, recompute
MODULE completion or pathway coverage, or reinterpret renderer targets. Return the renderer's
validated resource URIs and conservative evidence wording through the visualization Skill.
