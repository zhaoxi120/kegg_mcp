# Codex Skill evaluation record

This record distinguishes deterministic repository tests from a forward review of expected Skill
behavior. It does not claim that CI executed a language model, invoked the MCP server through a
model, or contacted KEGG.

## Method

Independent forward/manual reviews covered the three repository Skills: seven core-analysis and
annotation routes, plus focused rendering routes for the version 2 handoff. On 2026-07-18, two
fresh Codex reviews also exercised one combined FASTA-to-SVG request, once with the focused Skills
named and once through implicit selection. The reviewers inspected route selection, necessary
clarification, tool choice, refusal boundaries, interpretation language, single-MCP dependency
ownership, automatic continuation, and the stable file handoffs between Skills.

The repository tests under `tests/skill/` provide deterministic instruction-contract coverage.
They verify metadata, trigger terms, boundaries, MCP dependency identity, required guidance, and
prompt-specific textual invariants. They are not a runtime LLM evaluation and cannot establish
that every model or client will behave identically.

## Forward review results

### Protein FASTA without KO assignments

Prompt: `I have a protein FASTA file and want to analyze metabolic functions.`

Expected route: recognize that KO assignments are absent; never send FASTA to the core server or
guess K numbers; route to the independent `deepkoala-annotation` Skill and stop if its one declared
companion is unavailable.

Observed route: passed. The Skill states its boundary, keeps annotation outside the core MCP, and
returns controlled versioned files. It stops after annotation when that is the complete request;
when the original request also asks for KEGG analysis, the model continues with the independent
`kegg-ko-analysis` Skill without asking for another prompt or a manual path copy.

### Optional companion lifecycle

The `deepkoala-annotation` route was reviewed in four states:

- **Absent:** the Skill returns an actionable deployment result and never sends FASTA to the core
  server or calls another MCP.
- **Not ready:** the Skill reports the bounded structural status and stops without installing,
  downloading, or repairing dependencies.
- **Ready:** one explicit annotation request permits one `run_deepkoala_job` call; no second
  confirmation, acknowledgement field, workflow digest, or artifact digest is requested.
- **Successful:** the Skill returns the controlled absolute detailed-CSV path and readable source
  provenance for the independent core-analysis stage, which remains the sole normalization
  authority. If that stage was already requested, the model passes `annotations_path`,
  `input_format`, and `source` unchanged and continues automatically. When a shared allowed
  filesystem root is unavailable, it returns only the companion's bounded resource pages for
  adapter reconstruction.

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

## Rendering Skill forward review

The rendering review used the tracked `kegg-pathway-rendering` instructions and its focused
references. The rendering Skill depends only on `kegg-render-mcp`; analysis and annotation are
independent preceding Skills that deliver stable files. One original request may continue across
all three focused Skills, but no Skill calls more than its one declared MCP. All focused routes
passed:

| Prompt class | Expected route and boundary | Result |
| --- | --- | --- |
| Protein FASTA to pathway graphic | Route annotation, KO analysis, and rendering through three independent Skills and stable files; continue automatically after each successful handoff without creating an umbrella multi-server Skill. | Passed |
| Existing K numbers | Route first to core analysis for a version 2 handoff, then continue with the rendering Skill when graphics were part of the original request. | Passed |
| Existing version 2 handoff | Skip annotation and analysis; let the renderer validate and render the unchanged handoff. | Passed |
| Renderer unavailable | Stop with the deployment result; do not synthesize a fallback image or install another tool. | Passed |
| Version 1 handoff | Request a new core analysis bundle because preview-only input cannot be upgraded losslessly. | Passed |
| Accepted and uncertain evidence | Preserve distinct renderer-owned visual states and redundant non-color cues. | Passed |
| Rejected prediction as a missing gene | Refuse the absence claim and exclude rejected evidence from coloring. | Passed |
| Global or overview pathway | Preserve explicit rejection or summary-only behavior; do not approximate a regular box overlay. | Passed |
| MODULE logic diagram | Preserve AND, OR, optional, grouping, and MODULE-reference semantics from the authoritative core AST. | Passed |

The explicit and implicit combined-request reviews both selected the same order:
`deepkoala-annotation` -> stable detailed CSV -> `kegg-ko-analysis` -> version 2
`render_input.json` -> `kegg-pathway-rendering`. They requested no second prompt, stage-transition
confirmation, or manual path copy. They also preserved stop conditions for an unavailable or
failed upstream dependency and did not claim that any MCP call or biological analysis had actually
run.

The review also confirmed that the Skill does not contain inference, normalization, KGML parsing,
MODULE evaluation, pathway-coverage calculation, color assignment, SVG construction, pixel
manipulation, endpoint configuration, or resource-URI construction logic.

## Limitations and release use

This record covers the current core, annotation, and rendering contracts. It used no live KEGG
request, did not execute DeepKOALA, and used no real KEGG PNG or KGML payload. It did not
benchmark model-to-model variability, malicious prompt injection, long-context degradation,
client-specific tool selection, or external annotator compatibility. Releases must repeat the
review against their exact commit; any routing or interpretation regression blocks release
even if the deterministic static tests still pass.
