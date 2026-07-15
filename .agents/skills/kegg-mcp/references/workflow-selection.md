# Workflow selection

Choose the route from evidence already supplied. Do not ask the user to restate readable input.

## Protein FASTA without K numbers

1. Confirm that the sequences are proteins, not nucleotide FASTA.
2. Establish the analysis unit and whether proteins are expected to be complete, fragmented, or
   multi-domain. Ask only when this cannot be inferred and would change tool or model selection.
3. Offer external annotation choices; do not present DeepKOALA as the only option.
4. If a separately installed DeepKOALA companion MCP is discovered, explicitly configured, and
   chosen, read `deepkoala.md`, inspect runner status, prepare the job without inference, show the
   complete execution notice, and submit only after the user explicitly acknowledges its exact
   digest. Otherwise provide an external command for the selected annotator.
5. Request detailed machine-readable output plus the command, software version, model or database
   version, execution date, and effective device and weight source when applicable.
6. Do not send FASTA to the MCP server named `kegg-mcp` and do not execute an annotator through the
   core server. Resume with the annotation-table route after KO evidence is available. The
   optional companion is a distinct MCP service, not a Skill implementation or a core tool.
7. For a successful companion job, read and paginate its scoped output resource, decode the
   base64-encoded ranges, verify the declared byte count and SHA-256 digest, and submit the verified
   detailed CSV plus returned source-provenance template to the core importer. Do not assume that
   the core server can read a private resource owned by another MCP server.

## Plain K numbers

- Do not recommend annotation software.
- Preserve the user's analysis unit and context.
- Prefer `analyze_ko_annotations` when module or pathway targets are known.
- Resolve or confirm a named target from supported KEGG evidence rather than guessing an
  identifier. State the pathway namespace explicitly.

## Annotation table

1. Use `normalize_ko_annotations`. Select a named importer only when the signature is unambiguous;
   otherwise require explicit column mapping for ambiguous fields.
2. Preserve raw source decisions, scores, thresholds, ranks, domain coordinates, and versions.
3. Run strict analysis first. Offer lenient analysis only when the selected named policy produced
   `uncertain` records.
4. Use `analyze_modules` or `analyze_pathways` for a staged workflow, or the high-level tool when
   its schema accepts the supplied representation.

## Multiple KO sets

- Use `compare_ko_sets` with the same evidence policy and compatible KEGG reference provenance.
- Recompute under one reference retrieval when provenance is incompatible.
- Describe shared and set-specific KOs as deterministic set differences, not differential
  abundance, enrichment, or biological specificity.

## Requests that need explanation rather than analysis

- A K number is an annotation, not experimentally validated evidence. A K number or module result
  does not prove pathway activity, flux, or phenotype. Explain this directly; run a lookup only if
  the user also asks for database context.
- Never assign or guess a K number from a gene name. Ask for an existing stable identifier with an
  organism for a supported mapping, or recommend sequence annotation when a protein sequence is
  available.
- Decline statistical enrichment, abundance analysis, nucleotide assembly, alignment, and
  non-KEGG ontology analysis as MCP capabilities. Connect them only when the user explicitly asks
  for a separate KO/KEGG step and provides appropriate inputs.
