# Rendering workflow

## Compatible handoff

1. Call `get_renderer_status` and require readiness plus compatible schema version 3.
2. Pass exactly one controlled absolute `render_input.json` path or bounded inline handoff to
   `render_analysis_bundle`, `render_pathway`, or `render_module`.
3. Let the renderer validate schema, targets, evidence states, paths, output limits, and assets.

Do not patch or reinterpret an earlier schema version. Version 1 cannot be upgraded losslessly
because it contains previews rather than complete renderer evidence, and version 2 lacks the current
version 3 execution and automatic-selection contract. Request a new bundle from the independent
KO-analysis stage.

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

- Protein FASTA without KO evidence preferably starts with `deepkoala-annotation`.
- Existing K numbers or annotation tables start with `kegg-ko-analysis`.
- This Skill starts only from the stable render handoff produced by a completed core analysis.

If a required focused Skill or declared MCP dependency is unavailable in Codex, stop, preserve the
original analysis goal plus graphics formats and target scope, and request explicit permission once
to install or repair the complete repository suite. Resume in a new Codex task after discovery. If
the user explicitly selects another annotator, enter this Skill only after the independent core
stage produces a compatible handoff from supported KO evidence.

An original request may continue automatically across the installed focused Skills.
Do not call either earlier MCP from this Skill, describe the full three-stage workflow as one
Skill, or pass a private result identifier between MCP processes.

## Unavailable renderer and lifecycle

If the renderer is absent, unready, incompatible, or missing an allowed root, return the stable
diagnostic and suggested operator action. For a missing MCP registration or unavailable renderer
tool in Codex, stop before rendering and request explicit permission once to repair the complete
suite; resume in a new Codex task after discovery. The Skill itself does not install software,
download assets, or invoke an unrelated image tool. Preserve the requested formats and target scope.

Return only renderer-provided `kegg-render://results/{render_id}` and
`kegg-render://results/{render_id}/{artifact}` resource URIs; never construct one from an ID.
Delete a retained result only via `delete_render_result`; unknown, expired, deleted, and
cross-scope identifiers remain safe not-found states.
