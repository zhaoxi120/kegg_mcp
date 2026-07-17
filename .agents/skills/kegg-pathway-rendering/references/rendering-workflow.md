# Rendering workflow

## Compatible handoff

1. Call `get_renderer_status` and require readiness plus compatible schema version 2.
2. Pass exactly one controlled absolute `render_input.json` path or bounded inline handoff to
   `render_analysis_bundle`, `render_pathway`, or `render_module`.
3. Let the renderer validate schema, targets, evidence states, paths, output limits, and assets.

Version 1 cannot be upgraded losslessly because it contains previews rather than complete renderer
evidence. Request a new bundle from the independent KO-analysis stage. Do not patch, expand, or
reinterpret renderer input inside this Skill.

## Pathway access modes

Read `get_renderer_status` before probing or rendering. In `public_academic` or `licensed` live
mode, an explicit connectivity probe makes one bounded INFO request. In `offline_cache`, the same
probe makes zero requests and reports the network-disabled deployment policy; it does not inspect
the cache or establish that a target's PNG and KGML entries are present. `unconfigured` supports
MODULE diagrams only.

The cache path, public-versus-licensed namespace, and stale policy are deployment settings. Never
add them to a render call. Report a typed miss, unsafe cache, invalid cached asset, or
stale-disallowed result as the renderer returned it. Do not enable network access, request a
per-call refresh or stale override, or synthesize a substitute graphic.

## Automatic cross-Skill continuation

When the immediately preceding `kegg-ko-analysis` stage produced a compatible
`render_input.json`, use its stable path unchanged. Preserve the formats and bounded target scope
from the original request. Do not ask the user to copy the path, repeat the graphics request, or
approve a rendering stage that was already requested, and do not repeat analysis.

Rendering is the final stage of that original request. Return renderer-provided artifacts and the
manifest from the requested output directory. The renderer Skill does not call an annotation or
core MCP; the model changes focused Skills only at successful stable file boundaries.

## Missing earlier stages

- Protein FASTA without KO evidence starts with `deepkoala-annotation`.
- Existing K numbers or annotation tables start with `kegg-ko-analysis`.
- This Skill starts only from the stable render handoff produced by a completed core analysis.

An original request may continue automatically across those installed focused Skills.
Do not call either earlier MCP from this Skill, describe the full three-stage workflow as one
Skill, or pass a private result identifier between MCP processes.

## Unavailable renderer and lifecycle

If the renderer is absent, unready, incompatible, or missing an allowed root, return the stable
diagnostic and suggested operator action. Do not install software, download assets, or invoke an
unrelated image tool as a fallback.

Return only renderer-provided `kegg-render://results/{render_id}` and
`kegg-render://results/{render_id}/{artifact}` resource URIs; never construct one from an ID.
Delete a retained result only via `delete_render_result`; unknown, expired, deleted, and
cross-scope identifiers remain safe not-found states.
