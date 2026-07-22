---
name: kegg-ko-analysis
description: Normalize existing K numbers or KO annotation tables, retrieve bounded KEGG references, evaluate MODULE logic and descriptive pathway KO coverage, and compare KO sets through the local core kegg-mcp server. Use when the user already has KO evidence, a DeepKOALA detailed CSV, or asks a KO, MODULE, pathway, metabolic reconstruction, or deterministic KO-set question. Do not use for protein-sequence annotation, external annotator execution, model management, rendering, statistical enrichment, flux inference, or non-KEGG ontology analysis.
---

# KEGG KO analysis

## Select a core-only route

1. Inspect the supplied KO list or annotation table and identify the analysis unit when possible.
   If the only input is protein FASTA without KO evidence and the user explicitly selected another
   annotator, stop this Skill and return only after that route supplies supported KO evidence.
   Otherwise prefer the installed `deepkoala-annotation` Skill as the first annotation route. If
   that Skill or `deepkoala-mcp` is unavailable, first distinguish `task_reload_required` from an
   incomplete deployment. A successful suite result with `new_task_required=true`,
   `current_task_reload_supported=false`, and `repeat_installation_required=false`, or equivalent
   inventory evidence that the enabled plugin and all three MCP registrations exist, means the
   current task has a stale tool snapshot. Preserve the original request, stop before a core call,
   direct the user to one new Codex task outside the source checkout, and do not request or perform
   another installation. Only a fresh-task failure combined with incomplete deployment inventory
   is an incomplete suite deployment for which explicit permission may be requested once to install
   or repair the complete repository suite. If the user declines that action, remain stopped until
   a user-selected route supplies supported KO evidence. After a successful suite action, resume
   the original request in a new Codex task after discovery. Never call `deepkoala-mcp` or any
   annotator MCP from this Skill.
2. Read [workflow-selection.md](references/workflow-selection.md) and choose the smallest core
   workflow. Do not ask the user to restate readable input.
3. For a shared annotation file, pass its controlled absolute path, declared format, source
   provenance, and a new or empty allowed output directory. Do not parse or rewrite a companion
   CSV in the Skill.
4. If the input is an existing compatible `render_input.json`, route it unchanged to the installed
   `kegg-pathway-rendering` Skill; do not repeat analysis.

## Continue the original request across focused Skills

1. If the current evidence was produced by the immediately preceding `deepkoala-annotation`
   stage, consume its stable CSV handoff directly. Use the returned `annotations_path`,
   `input_format`, and `source` object unchanged; do not ask the user to restate the path, repeat
   the analysis goal, or confirm continuation. Do not rerun annotation or rewrite the CSV.
2. Complete the requested core analysis and require a successfully written, compatible
   `render_input.json` before any rendering transition.
3. If the original request also asks to render, visualize, draw, export SVG or PNG, create pathway
   images, or create MODULE diagrams, automatically continue with the installed
   `kegg-pathway-rendering` Skill. Pass the unchanged `render_input.json` path plus the original
   requested formats and target scope. Do not ask the user to copy the path or send another
   prompt, and do not repeat analysis.
4. If the original request asks only for a core report, return that report and stop without
   invoking the rendering stage. A failed core analysis has no renderer handoff and must not
   continue downstream.
5. If the rendering Skill or its declared MCP dependency is unavailable, stop without repeating
   core analysis, preserve the unfinished graphics goal, and request explicit permission once to
   repair the complete suite. Resume in a new Codex task after the component is discovered.

## Call only core `kegg-mcp`

- Prefer `analyze_ko_annotations` for a complete normalization-to-MODULE/pathway workflow. It
  normalizes evidence once and writes the stable bundle used by later stages.
- When no MODULE or pathway target and no explicit selection are supplied, omit
  `pathway_selection`. Let the server independently select the Top-5 MODULEs and Top-5 canonical KO
  reference pathways by unique selected-KO overlap.
- For Top-N detected pathways, supply `pathway_selection={"top_n":N}`. Let the server map,
  canonicalize `ko`/`map` views, rank, batch, and summarize.
- Use `normalize_ko_annotations`, `get_kegg_entries`, `map_ko_ids`, `analyze_modules`,
  `analyze_pathways`, or `compare_ko_sets` only for the corresponding narrower request.
- Call `get_server_status` when deployment state matters. If live access is enabled and
  connectivity is unknown, call `probe_kegg_connectivity` before network-dependent analysis.
  Treat access or connectivity failure as a technical deployment result, not biological absence.
- Treat public-academic or licensed authorization as deployment configuration. Never request a
  per-task qualification confirmation, endpoint, secret, workflow digest, or artifact hash.
- Follow discovered schemas and field-level errors. Do not fabricate identifiers, parameters,
  successful retrievals, resource URIs, or unsupported results.
- Prefer output-bundle files for durable handoff. A result identifier is opaque and valid only in
  the current stdio process. Use `list_analysis_results` for bounded discovery within the current
  scope, then use a result identifier only for same-session retrieval or requested cleanup.
- Call `delete_analysis_result` only after all required sections and bundle files are committed and
  only when the user requests immediate cleanup.

## Interpret and report

1. Read [confidence-policy.md](references/confidence-policy.md) before discussing accepted,
   uncertain, rejected, strict, or lenient evidence.
2. Read [module-interpretation.md](references/module-interpretation.md) for MODULE or pathway
   output. Treat automatic MODULE ranking only as target selection, never as completion or
   enrichment. Keep exact completion separate from block coverage and descriptive pathway coverage.
3. Apply [reporting-policy.md](references/reporting-policy.md) to the user-facing result.
4. Surface the analysis unit, original input path, normalization policy, retrieval/cache
   provenance, unsupported content, warnings, and truncation.
5. Never infer a K number from a sequence, product name, or unsupported identifier, and never
   implement import, HTTP, MODULE logic, ranking, or normalization inside this Skill.
