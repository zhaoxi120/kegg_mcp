# Codex Skill evaluation record

This record distinguishes deterministic repository tests from a forward review of expected Skill
behavior. It does not claim that CI executed a language model, invoked the MCP server through a
model, or contacted KEGG.

## Method

On 2026-07-15, the independent forward/manual review was repeated against the exact v0.2.0
candidate and its actual MCP schemas. The seven core and annotation-companion routes below passed.
On 2026-07-16, a separate nine-route forward review covered the v0.3.0 visualization candidate,
the version 2 handoff, and the independently installed renderer MCP. The reviewer inspected the
selected route, necessary clarification, tool choice, refusal boundary, interpretation language,
and relevant Skill references.

The repository tests under `tests/skill/` provide deterministic instruction-contract coverage.
They verify metadata, trigger terms, boundaries, MCP dependency identity, required guidance, and
prompt-specific textual invariants. They are not a runtime LLM evaluation and cannot establish
that every model or client will behave identically.

## Forward review results

### Protein FASTA without KO assignments

Prompt: `I have a protein FASTA file and want to analyze metabolic functions.`

Expected route: recognize that KO assignments are absent; never send FASTA to the core server or
guess K numbers; discover an explicitly configured local DeepKOALA companion or route to another
independent annotation Skill and MCP.

Observed route: passed. The Skill states its boundary, keeps annotation outside the core MCP, and
resumes only from a controlled versioned annotation file after KO evidence exists.

### Optional companion lifecycle

The companion route was reviewed in five states:

- **Absent:** the Skill routes to another independent annotation MCP and never sends FASTA to the
  core server.
- **Not ready:** the Skill reports the bounded structural status and stops without installing,
  downloading, or repairing dependencies.
- **Prepared:** the Skill presents the returned CPU execution notice without starting inference.
- **Confirmed:** only explicit user confirmation permits submission of the opaque job identifier;
  no workflow or artifact digest is requested.
- **Successful:** the Skill passes the controlled absolute detailed-CSV path and readable source
  provenance to the core importer, which remains the sole normalization authority.

Companion route check: passed. The instructions do not contain an annotator command, subprocess
implementation, model management, output parser, or duplicate decision policy.

### Detailed DeepKOALA output for MODULE analysis

Prompt: `Here is detailed DeepKOALA output; analyze KEGG modules.`

Expected route: normalize the detailed table while preserving raw decisions, probabilities,
thresholds, versions, repeated rows, and domain coordinates; evaluate strict evidence first;
never reclassify every below-threshold row as uncertain; keep exact completion separate from block
coverage.

Observed route: passed. The Skill selects the annotation-table route, delegates parsing and
normalization to the core MCP, and applies the confidence and conservative MODULE interpretation
rules without embedding a DeepKOALA execution workflow.

### One KO column and carbon-metabolism coverage

Prompt: `I have one column of K numbers; check carbon-metabolism coverage.`

Expected route: skip annotation-tool guidance; preserve the analysis unit; resolve or confirm an
explicit supported pathway target and namespace rather than guessing; run descriptive pathway KO
coverage; state the numerator, denominator, provenance, and interpretation limits.

Observed route: passed. The Skill selects the plain-KO route, uses an MCP analysis workflow only
after an explicit target is available, and does not equate coverage with pathway presence or
activity.

### Compare two KO sets

Prompt: `Compare these two KO sets.`

Expected route: preserve labels and analysis units, use `compare_ko_sets`, require compatible
policies and KEGG provenance or recompute together, and describe deterministic set differences
without enrichment, differential abundance, or biological-specificity claims.

Observed route: passed. The Skill selects the multiple-set route and applies the required
non-statistical comparison language.

### Activity claim from one K number

Prompt: `Does K00844 prove that glycolysis is active?`

Expected route: answer no; distinguish annotation/database association from expression, pathway
activity, flux, phenotype, and experimental validation; do not run a lookup unless additional
database context is requested.

Observed route: passed. The Skill routes this as an interpretation question and rejects the
overstated activity claim.

### Gene-name-to-KO request

Prompt: `Map this gene name to a KO for me.`

Expected route: refuse to guess a KO from a name; request an organism-scoped stable identifier for
a supported mapping or recommend evidence-producing sequence annotation when a protein sequence
is available.

Observed route: passed. The Skill follows the explicit no-guessing boundary and does not fabricate
an MCP result.

## Visualization extension forward review

The visualization review used the exact tracked `kegg-visualization` instructions, its five
references, the amended `kegg-ko-analysis` instructions, and the actual six-tool renderer surface.
All nine routes passed:

| Prompt class | Expected route and boundary | Result |
| --- | --- | --- |
| Protein FASTA to pathway graphic | Use `deepkoala-mcp -> kegg-mcp -> kegg-render-mcp`; retain the annotation job until the complete version 2 bundle exists. | Passed |
| Existing K numbers | Skip annotation, request an allowed core output directory, and render the resulting handoff. | Passed |
| Existing version 2 handoff | Skip annotation and analysis; let the renderer validate and render the unchanged handoff. | Passed |
| Renderer unavailable | Stop with the deployment result; do not synthesize a fallback image or install another tool. | Passed |
| Version 1 handoff | Request a new core analysis bundle because preview-only input cannot be upgraded losslessly. | Passed |
| Accepted and uncertain evidence | Preserve distinct renderer-owned visual states and redundant non-color cues. | Passed |
| Rejected prediction as a missing gene | Refuse the absence claim and exclude rejected evidence from coloring. | Passed |
| Global or overview pathway | Preserve explicit rejection or summary-only behavior; do not approximate a regular box overlay. | Passed |
| MODULE logic diagram | Preserve AND, OR, optional, grouping, and MODULE-reference semantics from the authoritative core AST. | Passed |

The review also confirmed that the Skill does not contain inference, normalization, KGML parsing,
MODULE evaluation, pathway-coverage calculation, color assignment, SVG construction, pixel
manipulation, endpoint configuration, or resource-URI construction logic.

## Limitations and release use

This record covers the exact v0.2.0 review and the v0.3.0 visualization candidate. It used no live
KEGG request, did not execute DeepKOALA, and used no real KEGG PNG or KGML payload. It did not
benchmark model-to-model variability, malicious prompt injection, long-context degradation,
client-specific tool selection, or external annotator compatibility. Future releases must repeat
the review against their exact candidate; any routing or interpretation regression blocks release
even if the deterministic static tests still pass.
