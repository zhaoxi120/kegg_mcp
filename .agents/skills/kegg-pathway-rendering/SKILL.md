---
name: kegg-pathway-rendering
description: Render a validated KEGG render_input.json analysis handoff as bounded static SVG or PNG pathway overlays and MODULE logic diagrams through the local kegg-render-mcp server. Use when the user supplies a compatible renderer handoff or asks to render, draw, color, visualize, or export an already completed KO analysis. Do not use for protein annotation, KO normalization, KEGG biological analysis, statistical enrichment, flux inference, arbitrary image editing, interactive HTML, or non-KEGG diagrams.
---

# KEGG pathway rendering

## Require an authoritative handoff

1. Accept a controlled `render_input.json` version 3 path or the renderer's bounded inline input
   transport. If the original request starts with only protein FASTA or KO evidence, route those
   earlier stages through the installed focused Skills and enter this Skill only after the core
   returns a compatible stable handoff; never call those MCP servers here.
2. Read [rendering-workflow.md](references/rendering-workflow.md), then call
   `get_renderer_status`. Require readiness, schema version 3, the requested static output format,
   and compatible bounds.
3. Let the renderer validate the handoff. Never parse, repair, upgrade, or recompute its evidence
   in the Skill.

## Call only `kegg-render-mcp`

- Prefer `render_analysis_bundle` for an ordinary bounded multi-target request.
- Use `render_pathway` or `render_module` only for one canonical target.
- Inspect renderer status before considering a connectivity probe. In a live access mode, use
  `probe_renderer_kegg_connectivity` only for an explicit preflight or after a classified
  connectivity failure; it makes one bounded INFO request. In `offline_cache`, the probe makes
  zero requests and confirms only that network access is disabled. It does not prove that the
  requested cache entries exist. MODULE rendering needs no KEGG request when its handoff is
  complete.
- Use `delete_render_result` only for requested cleanup. Treat result identifiers and resource
  URIs as opaque and process-scoped; return only URIs supplied by the renderer.
- Follow discovered schemas. Do not add arbitrary URLs, endpoint details, cache controls, style
  code, fonts, dimensions, or external resources.
- Treat an offline cache miss or stale-disallowed result as a technical asset-unavailable state.
  Do not request a per-call cache path, endpoint, refresh, or stale override; those choices belong
  to deployment configuration.

Read [pathway-rendering.md](references/pathway-rendering.md) for pathways and
[module-rendering.md](references/module-rendering.md) for MODULE diagrams.

## Finish an original cross-stage request

- When the immediately preceding `kegg-ko-analysis` stage returns a compatible
  `render_input.json`, use that handoff path unchanged. Carry forward the formats and target scope
  from the original request without asking the user to copy the path, restate what to draw, send
  another prompt, or confirm continuation.
- Render only the targets present in the handoff and selected by the original bounded request.
  Return the renderer-provided stable image files and manifest when an output directory was
  requested; do not rerun or revise any upstream analysis.
- This is the final stage unless the user requests a new or different analysis. If the original
  request did not ask for graphics, this Skill must not be invoked automatically.

## Report conservative graphics

1. Apply [evidence-color-policy.md](references/evidence-color-policy.md) without choosing or
   reproducing colors in the Skill.
2. Apply [rights-and-reporting.md](references/rights-and-reporting.md) before returning an artifact.
3. Surface not-renderable or summary-only targets, stale cache, provenance, warnings, truncation,
   and output bounds. Never hide a renderer failure behind a locally generated image.
4. Describe every artifact as a visualization of KO annotation evidence, not proof of pathway
   presence, activity, completeness, flux, phenotype, or experimental function.
