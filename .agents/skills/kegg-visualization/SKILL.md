---
name: kegg-visualization
description: Render, draw, color, visualize, or export KEGG pathway overlays and MODULE logic diagrams from existing KO evidence or a compatible render_input.json analysis bundle by orchestrating local kegg-mcp and kegg-render-mcp stdio servers. Use for bounded SVG or PNG KEGG evidence graphics. Do not use for protein inference, KO normalization implementation, statistical enrichment, flux or phenotype inference, arbitrary image editing, non-KEGG diagrams, or unsupported general pathway illustration.
---

# KEGG visualization

## Route before rendering

1. Read [rendering-workflow.md](references/rendering-workflow.md) and select the shortest route from
   the supplied evidence.
2. Call `get_renderer_status` before any handoff. Require readiness, schema version `2`, the
   requested output format, and compatible configured bounds. Stop with an actionable deployment
   result when the renderer is absent, unready, or incompatible.
3. When a controlled absolute `render_input.json` version 2 already exists, send it directly to
   the renderer. Skip annotation and core analysis.
4. When usable K numbers or an annotation table exist, skip annotation. Route the missing analysis
   through `$kegg-ko-analysis` and the local `kegg-mcp` server, with an allowed
   `output_directory`, then use the absolute renderer-input path from the completed bundle.
5. For protein FASTA without KO evidence, route local companion discovery, readiness, installation
   permission, and execution through `$kegg-ko-analysis`. Never use the DeepKOALA web form. Retain
   local output until the core import and bundle write succeed, request server-side Top-N pathway
   selection when applicable, then hand the version 2 path to the renderer.

Never implement inference, normalization, KGML parsing, MODULE evaluation, coverage calculation,
color assignment, SVG construction, or pixel manipulation in this Skill.

## Call the renderer

- Prefer `render_analysis_bundle` for ordinary multi-target workflows.
- Use `render_pathway` or `render_module` only when the user requests one canonical target.
- Use `probe_renderer_kegg_connectivity` only for an explicit pathway-access check or after a
  network-related renderer failure. MODULE rendering can remain closed-world when its handoff is
  complete.
- Use `delete_render_result` only when the user asks to delete a retained result or the agreed
  lifecycle requires cleanup. Treat the returned `render_id` as opaque and process-scoped.
- Follow the discovered schemas. Never add endpoint URLs, cache controls, style code, arbitrary
  fonts, or unbounded dimensions to tool input.

For pathway work, read [pathway-rendering.md](references/pathway-rendering.md). For MODULE work,
read [module-rendering.md](references/module-rendering.md).

## Return evidence-calibrated results

1. Read [evidence-color-policy.md](references/evidence-color-policy.md) before describing visual
   states or legends.
2. Read [rights-and-reporting.md](references/rights-and-reporting.md) before returning or discussing
   an artifact.
3. Surface unsupported or summary-only targets, stale cache state, provenance, warnings,
   truncation, and output bounds. Never hide a renderer error behind a locally generated image.
4. Return the renderer-provided `result_uri` or artifact `resource_uri`, such as a validated
   `kegg-render://results/{render_id}/{artifact}` link. Never construct a resource URI from an ID.
5. Describe every graphic as a visualization of KO annotation evidence. Do not claim validated
   pathway activity, completeness, flux, phenotype, or experimental function.
