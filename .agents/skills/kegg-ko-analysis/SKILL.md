---
name: kegg-ko-analysis
description: Search bounded KEGG entries, resolve gene or organism identifiers, trace typed KEGG relations, map BRITE hierarchies, audit KO mappings, normalize existing K numbers or KO annotation tables, evaluate MODULE logic and descriptive pathway KO coverage, and compare KO sets through the local core kegg-mcp server. Use for KEGG entity lookup or when the user already has KO evidence or a DeepKOALA detailed CSV. Do not use for protein-sequence annotation, external annotator execution, model management, rendering, statistical enrichment, flux inference, or non-KEGG ontology analysis.
---

# KEGG query and KO analysis

## Select a core-only route

1. Identify whether the starting point is a search term, an external identifier, a typed KEGG
   entity, an annotation table, or supplied KO evidence. For annotation evidence, also identify the
   analysis unit when possible. If the only input is protein FASTA, follow the out-of-scope route in
   [workflow-selection.md](references/workflow-selection.md) and stop before a core call.
   Never call `deepkoala-mcp` or any annotator MCP from this Skill.
2. Read [workflow-selection.md](references/workflow-selection.md) and choose the smallest core
   workflow. Do not ask the user to restate readable input.
3. For a shared annotation file, apply the reference's path, provenance, and output-directory
   rules. Do not parse or rewrite a companion CSV in the Skill.
4. If the input is an existing compatible `render_input.json`, route it unchanged to the installed
   `kegg-pathway-rendering` Skill; do not repeat analysis.

## Continue the original request across focused Skills

Apply the canonical continuation rules in
[workflow-selection.md](references/workflow-selection.md#automatic-cross-skill-continuation).
Cross a Skill boundary only through a successful stable file handoff. This Skill never calls an
upstream or rendering MCP.

## Call only core `kegg-mcp`

- Prefer `analyze_ko_annotations` for a complete normalization-to-MODULE/pathway workflow. It
  normalizes evidence once and writes the stable bundle used by later stages.
- When no MODULE or pathway target and no explicit selection are supplied, omit
  `pathway_selection`. Let the server independently select the Top-5 MODULEs and Top-5 canonical KO
  reference pathways by unique selected-KO overlap.
- For Top-N detected pathways, supply `pathway_selection={"top_n":N}`. Let the server map,
  canonicalize `ko`/`map` views, rank, batch, and summarize.
- Use `search_kegg_entries` for bounded endpoint candidates. Never describe a candidate as the
  selected best match, invent a relevance score, or call an exact-mass hit a compound
  identification.
- Use `resolve_kegg_entities` for gene or organism crosswalks. Preserve every reported ambiguous
  candidate, require organism context for gene symbols, and treat unmapped results as mapping
  outcomes rather than biological absence. Request `include_pathway_directory=true` only when the
  user explicitly asks which organism-specific pathway references KEGG provides. Treat that
  directory as reference availability, never as pathway presence, completeness, activity, flux, or
  phenotype.
- Use `trace_kegg_relations` for one- or two-level allowlisted cross-references. Do not interpret
  returned edges as regulation, causality, mechanism, activity, or phenotype.
- Resolver and relation-trace direct responses are bounded previews. Read the returned resource URI
  only when the complete retained crosswalk, nodes, edges, or provenance are needed; do not
  reconstruct an authoritative result by manually batching and merging calls.
- Use `map_brite_hierarchy` for complete BRITE paths and descriptive unique-input counts; never
  call those counts enrichment or dominant function.
- Use `audit_annotation_mapping` to summarize evidence status, strict/lenient mapping yield,
  ambiguity, provenance, and optional assembly-quality warnings without correcting scores or
  filling missing K numbers. Set `mapping_targets` to the relationship classes required by the
  question; an empty list requests an evidence-only audit. If relationship mapping is skipped by a
  request limit, retain and report the complete evidence audit instead of treating the skipped
  mapping as missing biology.
- Use `normalize_ko_annotations`, `get_kegg_entries`, `analyze_modules`, `analyze_pathways`, or
  `compare_ko_sets` only for the corresponding narrower request.
- Call `get_server_status` when deployment state matters. If live access is enabled and
  connectivity is unknown, call `probe_kegg_connectivity` before network-dependent analysis.
  Treat access or connectivity failure as a technical deployment result, not biological absence.
- Treat public-academic or licensed authorization as deployment configuration. Never request a
  per-task qualification confirmation, endpoint, secret, workflow digest, or artifact hash.
- Follow discovered schemas and field-level errors. Do not fabricate identifiers, parameters,
  successful retrievals, resource URIs, or unsupported results.
- Prefer output-bundle files for durable KO-analysis handoff. Query and audit resources are
  same-session retained detail, not durable cross-process files. A result identifier is opaque and
  valid only in the current stdio process. Use `list_analysis_results` for bounded discovery within
  the current scope, then use a result identifier only for same-session retrieval or requested
  cleanup.
- Call `delete_analysis_result` only after all required sections and bundle files are committed and
  only when the user requests immediate cleanup.

## Interpret and report

1. Read [confidence-policy.md](references/confidence-policy.md) before discussing accepted,
   uncertain, rejected, strict, or lenient evidence.
2. Read [module-interpretation.md](references/module-interpretation.md) for MODULE or pathway
   output. Treat automatic MODULE ranking only as target selection, never as completion or
   enrichment. Keep exact completion separate from block coverage and descriptive pathway coverage.
3. Apply [reporting-policy.md](references/reporting-policy.md) to the user-facing result.
4. Surface the fields relevant to the selected route: query or input identity, ambiguity and
   mapping status, analysis unit and normalization policy when applicable, retrieval/cache
   provenance, unsupported content, warnings, and truncation.
5. Never infer a K number from a sequence, product name, or unsupported identifier, and never
   implement import, HTTP, MODULE logic, ranking, or normalization inside this Skill.
