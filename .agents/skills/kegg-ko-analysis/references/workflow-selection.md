# Workflow selection

Choose the smallest Core route for a bounded KEGG query or supplied KO evidence. This Skill never
runs an annotator or renderer. Let the server validate identifiers, perform endpoint batching,
deduplicate relationships, and retain complete bounded detail. Do not replace those deterministic
steps with LLM ranking, ad hoc chunk merging, or inferred database content.

## Construct exactly one input branch

- For `analyze_ko_annotations`, provide exactly one of top-level `ko_text` or nested
  `annotations`. The `ko_text` branch owns top-level `analysis_unit` and `sample_id`. The
  `annotations` branch owns those fields inside `annotations`; omit both top-level context fields
  even when their values would match or equal a default. Never copy context into both locations.
- Inside `annotations`, provide exactly one payload selector: `file_path` for a readable controlled
  file or `text` for an unchanged bounded inline payload. Never send both. Keep `input_format`,
  `source`, `analysis_unit`, and `sample_id` in the same nested object.
- For `audit_annotation_mapping`, `analyze_modules`, `analyze_pathways`, or `compare_ko_sets`,
  each dataset `source` provides exactly one of `ko_text` or `result_id`. When reusing a
  `result_id`, the source object contains only `result_id`: omit `ko_text`, `analysis_unit`, and
  `sample_id` because the retained dataset already owns its analysis context. Inline `ko_text`
  sources may carry `analysis_unit` and `sample_id`.

## Search terms and chemical candidates

- Use `search_kegg_entries` when the user supplies a name, keyword, formula, exact mass, or
  molecular weight instead of a canonical KEGG identifier.
- Keyword search may target KO, pathway, MODULE, reaction, enzyme, compound, glycan, drug,
  reaction class, genome, or organism. Formula and mass modes apply only to compound or drug.
- Return the endpoint-ordered candidates without inventing a relevance score or selecting a best
  match. For a compound search, an exact-mass result is a compound candidate, not a compound
  identification; for a drug search it is likewise only a drug candidate, not an identification.
- The public RCLASS FIND endpoint returned a well-formed empty result for a known identifier and
  definition fragments on 2026-07-31. Report zero candidates as an upstream result; when a
  canonical RCLASS identifier is already known, prefer `get_kegg_entries` with the preview
  projection instead of promising positive keyword discovery.
- Ask for disambiguating context only when the next requested step requires one canonical entity.
  Do not silently carry one search candidate into resolution, relation tracing, or analysis.

## Typed entry cards, citations, and selected-reference persistence

- Use `get_kegg_entries` with `projection="card"` when the user wants supported fields for known
  KO, MODULE, pathway, reaction, enzyme, compound, glycan, gene, or genome identifiers. Cards are
  deterministic projections of parsed KEGG flat files, not LLM summaries. Use the default preview
  projection for BRITE, drug, reaction-class, or other requests without a card schema.
- Card and references calls retain complete GET detail and one current-scope `entry_snapshot`. Use
  `compare_kegg_reference_snapshots` only when the user supplies two successful canonical snapshot
  result IDs from the current stdio session, both snapshots cover the same requested entries, and a
  local structural comparison is requested.
- Compare only the requested entry fields, relationships, MODULE definitions, or pathway
  denominators. Report parser, endpoint, retrieval/cache, and release compatibility. A returned
  field or membership change is not biological gain, loss, correction, or validation, and the
  comparison is not a general KEGG release history.
- Use `get_kegg_entries` with `projection="references"` only when the user asks which PubMed
  identifiers KEGG explicitly lists for the same nine entry types supported by card projection.
  Report these as KEGG-listed PMID identifiers; do not retrieve papers, summarize their
  conclusions, infer a mechanism, or imply that KEGG endorses every paper claim.
- Use `write_kegg_reference_bundle` when the user needs one successful canonical entry snapshot to
  survive the current stdio session. Pass the card or references result ID while it is still valid,
  preserve an explicit entry subset when supplied, and attach a BRITE result only when requested.
  Require a user-selected allowed `output_directory`; do not infer it from a cache, result, or input
  path.
  The server writes the versioned selected-reference files and manifest. Do not reconstruct cards,
  relationships, BRITE paths, hashes, or provenance in the LLM, and do not call the bundle a KEGG
  cache export, database mirror, or release archive.

## Gene, organism, and substance identifiers

- Use `resolve_kegg_entities` for a KEGG gene, NCBI GeneID, NCBI Protein ID, UniProt accession,
  organism code, genome T number, NCBI Taxonomy ID, organism name, KEGG compound/glycan/drug,
  ChEBI identifier, or PubChem SID.
- A gene symbol requires explicit organism context. Preserve all reported candidates and all
  one-to-one, one-to-many, many-to-one, organism-mismatch, and unmapped outcomes; never choose one
  from biological familiarity.
- For substance resolution, use `pubchem_sid` only for a PubChem SID. Never reinterpret a CID as a
  SID, and never call a crosswalk or mass candidate a chemical identification.
- For a known KEGG compound, glycan, or drug, use `trace_kegg_relations` for a relationship-only
  question. Use substance resolution when identity validation or an external-ID crosswalk is also
  required; include the matching `kegg_compound`, `kegg_glycan`, or `kegg_drug` identity target
  before requesting its supported one-hop projections.
- Taxonomy resolution supports exact, species, genus, family, order, class, and phylum. Leave
  `candidate_materialization="auto"` unless the user explicitly needs full GENOME records: auto
  uses full materialization for exact/species and identity-only candidates for broader ranks.
  Rank expansion requires an NCBI Taxonomy ID for the requested rank; for example, a species-rank
  lookup uses the species Taxonomy ID rather than a strain Taxonomy ID. A genus or family name is
  only a name search and must not be reinterpreted as a taxonomy-rank request.
- Leave `include_pathway_directory` false unless the user explicitly asks which organism-specific
  pathway references KEGG provides. It requires full candidate materialization. A returned
  directory describes reference availability, not pathway presence, completeness, activity, flux,
  or phenotype.
- The direct response contains counts and bounded resolution and candidate previews. When complete
  crosswalks, projected entities, pathway-directory rows, or provenance are needed, follow the
  returned resource URI in the same stdio session.

## Typed relation questions

- Use `trace_kegg_relations` only when the user supplies typed KEGG seeds and asks for an
  allowlisted relationship. Keep the default one-level trace unless the question requires the
  supported second level.
- For KO-to-gene or organism-specific pathway-to-gene, require one canonical `organism_scope`.
  Use the direct scoped edge instead of emulating KO-to-gene through a generic pathway. Never
  request or emulate a global KO-to-all-genes expansion. MODULE, glycan, and drug relations remain
  limited to the advertised edge allowlist. MODULE-source edges accept reference `M` identifiers,
  not organism- or genome-prefixed module identifiers.
- Do not invent selected-entry reaction-class edges or RMODULE routes. The bounded client omits
  both after the 2026-07-30 live compatibility review found no safe selected-entry contract.
- Treat every edge as a KEGG database cross-reference. Do not infer regulation, causality,
  mechanism, activity, phenotype, centrality, shortest paths, or communities.
- The direct response contains node and edge counts plus bounded previews. Read the returned
  resource URI for complete retained nodes, edges, and provenance; do not manually batch and merge
  a replacement graph.

## BRITE hierarchy questions

- Use `map_brite_hierarchy` for hierarchy membership or classification. Preserve every returned
  path and multi-parent membership requested by the user.
- Report counts as descriptive unique supplied-entity counts, never as enrichment, abundance,
  dominance, activity, or functional importance.

## Annotation mapping audit

- Use `audit_annotation_mapping` for evidence status, duplicate or conflicting assignments,
  accepted-KO views, selected KEGG mapping yields, provenance completeness, or optional
  assembly-quality warnings.
- Supply only the relationship classes required by the question in `mapping_targets`. Use an empty
  list for an evidence-only audit with no KEGG relationship requests.
- For a large plain-KO set that needs one relationship class, select that single mapping target and
  let the audit service batch, de-duplicate, and retain the complete relationship rows. Do not split
  the set through graph traces or merge shards in the LLM.
- Evidence auditing remains complete when relationship mapping reports `skipped_request_limit`.
  It also remains complete when mapping reports `incomplete_row_limit` or
  `incomplete_response_limit`. Report completed, incomplete, and skipped targets plus the limit
  reason; do not calculate yield from discarded partial rows, discard the evidence result, infer
  biological absence, or fill missing K numbers.

## KEGG web-tool input handoffs

- Use `prepare_kegg_handoff` only to prepare validated local input files. Require a
  user-selected allowed `output_directory`; this workflow never guesses a destination, uploads
  data, opens a browser, executes a downstream service or package, or parses a downstream result.
- For KEGG Mapper Reconstruct, Search, Color, Join, or MWsearch and KEGG Syntax KO Composition,
  use the matching discriminated target and preserve validated caller order. For KEGG Syntax KO
  Sequence, proceed only when the caller confirms that the rows are already in genomic order and
  set `order_semantics="caller_supplied_genomic_order"`. Do not infer order from identifiers,
  annotation rows, coordinates, or biological familiarity.
- Return the data-file and manifest paths as prepared inputs. Any upload, execution, statistical
  analysis, or result interpretation is a separate workflow and requires separately authorized
  tooling.

## Plain K numbers

- Preserve the analysis unit and context.
- Prefer `analyze_ko_annotations`; provide named targets when requested. Otherwise omit
  `pathway_selection` and let the server independently choose the Top-5 MODULEs and Top-5 canonical
  KO reference pathways by unique selected-KO overlap.
- Use server-side Top-N selection instead of reading and ranking complete relationship rows.
- Automatic selection leaves current KEGG Global, Overview, and higher-level Overview maps in the
  retained ranking but excludes them before Top-N target truncation. If the user explicitly
  requests a canonical KO total map such as `ko01100`, pass that explicit pathway target with
  `allow_global_or_overview=True`. Continue to rendering only after Core emits a complete,
  renderable version 6 handoff. Do not substitute a `map` or organism reference, promote a
  summary-only result, or request a model-native conceptual fallback.
- Treat MODULE overlap ranking as target selection, not MODULE completion or enrichment. Evaluate
  exact completion and required-block coverage separately from the selected MODULE definitions.
- Do not recommend annotation software when usable KO evidence already exists.

## Annotation table or detailed CSV

1. Use a controlled absolute path in `file_path`. Pass a user-specified new or empty
   `output_directory` unchanged; the user-specified path wins. Otherwise, omit `output_directory`
   and let Core allocate a fresh directory beneath its configured project output root. Do not guess
   a root from the input path, create it with a shell command, or reuse a non-empty directory. Use
   `normalize_ko_annotations` alone only when the user wants a reusable normalized table.
2. Let the server infer unambiguous common columns and report the mapping. Supply an explicit
   column mapping only for ambiguous or non-standard tables.
3. `analyze_ko_annotations` always derives a compact analysis view and does not retain raw
   per-record evidence. Use `normalize_ko_annotations` when the request requires raw source
   decisions, scores, thresholds, ranks, domains, protein names, sequence-to-KO mappings, or
   duplicate/conflict accounting. Preserve source/model versions, timestamps, and the original
   absolute input path as provenance in either route.
4. Run every analysis with sorted unique accepted K numbers. Rejected, unclassified, and invalid
   records remain evidence outcomes and do not enter MODULE, pathway, ranking, comparison, or
   rendering results.
5. Use stable bundle files for later MCP stages; do not pass a process-private result identifier.
6. For an immediately preceding successful DeepKOALA handoff, first use its stable
   `annotations_path`. If and only if Core rejects that path with
   `ANALYSIS_CONFIGURATION_INVALID`, the typed message
   `A local handoff path is outside the configured allowed roots.`, and a `safe_details` entry of
   `field="file_path"`, return control to the installed
   `deepkoala-annotation` Skill for its bounded `annotations_resource_uri` fallback. This Skill
   does not call the companion MCP, change either server's allowed roots, copy the CSV, or retry the
   same unreadable path. The preceding Skill may return byte-identical UTF-8 content only when the
   successful job's `output_bytes` is at most the 5,000,000-byte Core inline limit. Then call Core
   with nested `annotations.text` plus the unchanged `input_format` and `source`; omit
   `annotations.file_path` and keep any analysis context only inside `annotations`. For a larger
   result, stop without reading resource pages and require repaired shared handoff roots that cover
   the returned input and output paths in Core, as enforced by the supported suite installer. The
   same message with
   `field="output_directory"` is an output-location error and must not enter this fallback.

### Compact high-level analysis view

- Every `analyze_ko_annotations` input produces the same sorted unique accepted-KO analysis view.
  Do not supply or look for an annotation-retention selector, and never change analysis semantics
  because a file is large or small.
- State when relevant that the view retains aggregate intake/status counts, source and
  decision-policy provenance, and bounded diagnostics. It does not retain record evidence,
  sequence/protein-to-KO mappings, raw per-record score or threshold evidence, or
  duplicate/conflict accounting. Use `normalize_ko_annotations` or the audit workflow when the
  user needs those records.
- For an allowed DeepKOALA detailed `annotations.file_path`, let Core stream the source under its
  fixed maxima: 1 GiB, 10,000,000 source rows, 20,000,000 expanded assignments, 100,000 unique
  accepted K numbers, 64 columns, 16,384 characters per field, and 100 retained diagnostics. Do
  not split and merge file chunks in the Skill to evade these limits. Other formats and bounded
  inline inputs keep their applicable importer limits but produce the same analysis view.
- Compact local intake does not raise existing KEGG request, reference-loading, relationship-row,
  response-byte, ranking, or output budgets. If the accepted set is likely to exhaust those
  budgets, provide bounded explicit `module_ids` or `pathways` when the scientific targets are
  known, or split the source into independently meaningful analysis units; do not shard one unit
  and merge rankings in the LLM.

## Automatic cross-Skill continuation

- When the immediately preceding `deepkoala-annotation` stage produced the evidence,
  consume its stable CSV handoff directly when Core can read it. Use the returned
  `annotations_path`, `input_format`, and `source` object unchanged;
  do not ask the user to restate the path, copy it, repeat the request, or confirm a KEGG-analysis
  stage already present in the original request. If the typed allowed-root error described above
  occurs, use the canonical resource fallback and then the mutually exclusive `annotations.text`
  branch only for an output no larger than 5,000,000 bytes. A larger output must remain a stable
  file and requires the shared-root deployment repair described above. Do not page it into the
  prompt. Do not rerun annotation or rewrite the CSV.
- When the original request also asks to render, visualize, draw, or export graphics, first require
  a successfully written, compatible `render_input.json`, then automatically continue with the installed
  `kegg-pathway-rendering` Skill. Pass the unchanged `render_input.json` path while
  preserving the requested formats and target scope. Do not ask the user to copy the path, and do
  not repeat analysis in the rendering transition.
- When the original request asks only for a core report, return it and stop. Other requests may
  continue downstream only after a successful handoff; never treat an upstream failure as empty KO
  evidence.
- When no rendering output path was specified, omit it and let the renderer allocate a fresh
  project output directory.

## Multiple KO sets

- Use `compare_ko_sets` with compatible decision-policy and reference provenance.
- Describe results as deterministic set membership and functional-reference differences, not
  differential abundance, enrichment, or biological specificity.

## Out-of-scope starting points

- If the user explicitly selected another annotator for protein FASTA without KO evidence, stop
  before a core call and resume only after that route supplies supported KO evidence. Otherwise
  prefer the installed `deepkoala-annotation` Skill as the independent first annotation route. If
  that route is unavailable in the same task that just installed a registered suite, classify
  `task_reload_required` and resume in one new Codex task outside the source checkout. The fields
  `new_task_required=true`, `current_task_reload_supported=false`, and
  `repeat_installation_required=false` identify a stale tool snapshot; do not request or perform
  another installation. In a fresh task, first inspect the plugin and MCP inventories when
  available. If the exact enabled suite and all three registrations remain present, classify
  `plugin_discovery_stale`, restart Codex once, and retry in one new task without reinstalling or
  adding duplicate MCP entries. Only an incomplete suite deployment inventory or concrete
  component failure may request explicit permission once to install or repair the complete
  repository suite. If the user declines that action, remain stopped until a selected route
  supplies supported KO evidence.
- A compatible `render_input.json` continues directly with the independent
  `kegg-pathway-rendering` Skill without rerunning core analysis.
- Statistical enrichment execution, abundance testing, nucleotide assembly, sequence alignment,
  and non-KEGG ontologies require a separate workflow.
