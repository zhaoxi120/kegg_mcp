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
`render_input.json`, use that handoff path unchanged. Preserve the formats and bounded target scope
from the original request without asking the user to copy the path, repeat the graphics request,
or approve a rendering stage that was already requested; do not rerun or revise any upstream analysis.

Rendering is the final stage unless the user requests a new or different analysis. If the original
request did not ask for graphics, this Skill must not be invoked automatically. Return
renderer-provided artifacts and the manifest from the explicit or default output directory. The
renderer Skill does not call an annotation or core MCP; the model changes focused Skills only at
successful stable file boundaries.

## Missing earlier stages

- Prefer DeepKOALA as the first protein-FASTA route when the user did not select another
  annotator; protein FASTA without KO evidence starts with `deepkoala-annotation`.
- Existing K numbers or annotation tables start with `kegg-ko-analysis`.
- This Skill starts only from the stable render handoff produced by a completed core analysis.

If a required focused Skill or declared MCP dependency is unavailable in the same task that just
installed a registered suite, classify `task_reload_required`, preserve the original analysis goal
plus graphics formats and target scope, and resume in one new Codex task outside the source
checkout. The success fields `new_task_required=true`, `current_task_reload_supported=false`, and
`repeat_installation_required=false` identify a stale tool snapshot; do not request or perform
another installation. Only a fresh-task failure with incomplete deployment inventory may
request explicit permission once to install or repair the complete repository suite.
If the user explicitly selects another annotator, enter this Skill only after the independent core
stage produces a compatible handoff from supported KO evidence.

An original request may continue automatically across the installed focused Skills.
Do not call either earlier MCP from this Skill, describe the full three-stage workflow as one
Skill, or pass a private result identifier between MCP processes.

## Unavailable renderer and lifecycle

If the renderer is absent, unready, incompatible, or missing an allowed root, return the stable
diagnostic and suggested operator action. An unavailable renderer tool immediately after successful
installation is `task_reload_required`, not evidence that repair is needed. Stop before rendering
and use one new task; for every other unavailable route, stop before rendering and request explicit
repair permission only after a fresh-task failure and
incomplete inventory. The Skill itself does not install software, download assets, or invoke an
unrelated image tool. Preserve the requested formats and target scope.

Return only renderer-provided `kegg-render://results/{render_id}` and
`kegg-render://results/{render_id}/{artifact}` resource URIs; never construct one from an ID.
Delete a retained result only via `delete_render_result`; unknown, expired, deleted, and
cross-scope identifiers remain safe not-found states.
