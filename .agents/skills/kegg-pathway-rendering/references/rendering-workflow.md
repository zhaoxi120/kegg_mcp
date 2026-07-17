# Rendering workflow

## Compatible handoff

1. Call `get_renderer_status` and require readiness plus compatible schema version 2.
2. Pass exactly one controlled absolute `render_input.json` path or bounded inline handoff to
   `render_analysis_bundle`, `render_pathway`, or `render_module`.
3. Let the renderer validate schema, targets, evidence states, paths, output limits, and assets.

Version 1 cannot be upgraded losslessly because it contains previews rather than complete renderer
evidence. Request a new bundle from the independent KO-analysis stage. Do not patch, expand, or
reinterpret renderer input inside this Skill.

## Missing earlier stages

- Protein FASTA without KO evidence belongs to `deepkoala-annotation`.
- Existing K numbers or annotation tables belong to `kegg-ko-analysis`.
- This Skill starts only from the stable render handoff produced by a completed core analysis.

Do not call either earlier MCP, describe the full three-stage workflow as one Skill, or pass a
private result identifier between MCP processes.

## Unavailable renderer and lifecycle

If the renderer is absent, unready, incompatible, or missing an allowed root, return the stable
diagnostic and suggested operator action. Do not install software, download assets, or invoke an
unrelated image tool as a fallback.

Return only renderer-provided `kegg-render://results/{render_id}` and
`kegg-render://results/{render_id}/{artifact}` resource URIs; never construct one from an ID.
Delete a retained result only via `delete_render_result`; unknown, expired, deleted, and
cross-scope identifiers remain safe not-found states.
