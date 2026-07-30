# Reporting policy

Lead with a short answer to the user's question and include only sections relevant to the selected
route.

For a bounded query, report:

1. the supplied query or identifier namespace and requested scope;
2. candidate, resolution, relation, hierarchy, or audit counts and the bounded direct preview;
3. ambiguity, mismatch, unmapped, skipped, and truncation states;
4. the interpretation caveat appropriate to that query; and
5. retrieval/cache provenance and the returned resource URI when retained detail is needed.

For KO evidence analysis, report:

1. input and normalization summary;
2. annotation evidence and evidence mode;
3. KEGG mappings;
4. exact module completion and project block coverage;
5. descriptive pathway KO coverage;
6. deterministic comparison results, when requested;
7. caveats, unsupported content, and truncation; and
8. provenance.

State unknown provenance as unknown or null; never guess it. Include the analysis unit, original
input absolute path when supplied, importer and decision-policy versions, annotation tool/model
versions when supplied, KEGG retrieval and cache state, namespace and denominator metadata,
algorithm versions, and non-default parameters. Call out stale cache reuse explicitly. Do not
request or display input, dataset, response, definition, or artifact digests.

For KO normalization or analysis, prefer the concise output bundle for durable delivery:

```text
normalized_annotations.tsv
protein_ko_mapping.tsv
module_ranking.tsv
ko_module_relationships.tsv
pathway_ranking.tsv
ko_pathway_relationships.tsv
pathway_coverage.tsv
module_completion.tsv
analysis_report.md
render_input.json
bundle_manifest.json
```

The four ranking and relationship tables are present only when the corresponding automatic target
selection runs. MODULE ranking reports selected-KO overlap for target selection; it is not a
completion or enrichment result.

Treat the bundle as the durable cross-process artifact. The manifest redacts absolute source paths
by default, while retained result IDs are valid only in the current stdio session and are removed
on normal server shutdown. Use `delete_analysis_result` after required reads only when the user
requests immediate cleanup.

Query and audit tools do not turn their retained details into durable cross-process handoffs. Use
their bounded direct previews in conversation. For complete search, resolution, relation, BRITE, or
audit detail, follow the resource URI returned by the tool instead of pasting every candidate,
crosswalk, path, distribution, row, edge, or provenance batch or constructing a URI.

Use a section or validated page resource rather than pasting large annotation or missing-KO lists.
Never expose cache paths, credentials, environment values, or raw payloads from status data.

Use query-calibrated language:

- call search rows candidates, not best matches, and never invent a relevance score;
- call an exact-mass hit a compound candidate, not a compound identification;
- preserve all reported resolver candidates and describe mismatch or unmapped as mapping outcomes,
  not biological absence;
- describe an organism pathway directory as available KEGG references only;
- describe relation-trace edges as database cross-references, not regulation, causality, mechanism,
  activity, or phenotype;
- describe BRITE counts as unique supplied-entity classifications, not enrichment or dominant
  function; and
- state whether audit `mapping_targets` completed, were not requested, or were skipped by the
  request limit. A skipped mapping phase does not invalidate the complete evidence audit.

Use evidence-calibrated language:

- say "annotated as" or "the KO set covers" rather than "validated" or "active";
- say "not detected in the supplied annotations" rather than "absent";
- describe KO-set differences as set-specific annotations, not differential function; and
- state that sequencing, assembly, gene calling, annotation, and reference coverage can affect the
  result.
