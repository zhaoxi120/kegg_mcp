# Reporting policy

Lead with a short answer to the user's question, then separate:

1. input and normalization summary;
2. annotation evidence and evidence mode;
3. KEGG mappings;
4. exact module completion and project block coverage;
5. descriptive pathway KO coverage;
6. deterministic comparison results, when requested;
7. caveats, unsupported content, and truncation; and
8. provenance.

State unknown provenance as unknown or null; never guess it. Include the analysis unit, input
digest, importer and decision-policy versions, annotation tool/model versions when supplied, KEGG
retrieval and cache state, namespace and denominator metadata, algorithm versions, and non-default
parameters. Call out stale cache reuse explicitly.

Use bounded previews in conversation. Prefer a resource URI returned by a tool over constructing
one. The server declares these fixed resources and templates:

```text
ko-analysis://status
ko-analysis://cache/info
ko-analysis://results/{result_id}
ko-analysis://results/{result_id}/{section}
ko-analysis://results/{result_id}/{section}/{offset}/{limit}
kegg-cache://entries/{database}/{identifier}
```

Use a section or validated page resource rather than pasting large annotation or missing-KO lists.
Never expose cache paths, credentials, environment values, or raw payloads from status data.

Use evidence-calibrated language:

- say "annotated as" or "the KO set covers" rather than "validated" or "active";
- say "not detected in the supplied annotations" rather than "absent";
- describe KO-set differences as set-specific annotations, not differential function; and
- state that sequencing, assembly, gene calling, annotation, and reference coverage can affect the
  result.
