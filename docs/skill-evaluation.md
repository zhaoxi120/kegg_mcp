# Codex Skill evaluation record

This record distinguishes deterministic repository tests from a forward review of expected Skill
behavior. It does not claim that CI executed a language model, invoked the MCP server through a
model, or contacted KEGG.

## Method

On 2026-07-15, the independent forward/manual review was repeated against the exact v0.1.0
candidate and its actual MCP schemas. All six routes passed. The reviewer inspected the selected
route, necessary clarification, tool choice, refusal boundary, interpretation language, and
relevant Skill references.

The repository tests under `tests/skill/` provide deterministic instruction-contract coverage.
They verify metadata, trigger terms, boundaries, MCP dependency identity, required guidance, and
prompt-specific textual invariants. They are not a runtime LLM evaluation and cannot establish
that every model or client will behave identically.

## Forward review results

### Protein FASTA without KO assignments

Prompt: `I have a protein FASTA file and want to analyze metabolic functions.`

Expected route: recognize that KO assignments are absent; confirm only information that changes
the annotation route; present multiple external annotation options; request detailed,
machine-readable, versioned output; do not send FASTA to the MCP server or guess K numbers.

Observed route: passed. The Skill selects the protein-FASTA workflow, reads annotation-tool
guidance, treats DeepKOALA as one option rather than the only option, and resumes MCP analysis only
after KO evidence exists.

### Detailed DeepKOALA output for MODULE analysis

Prompt: `Here is detailed DeepKOALA output; analyze KEGG modules.`

Expected route: normalize the detailed table while preserving raw decisions, probabilities,
thresholds, versions, repeated rows, and domain coordinates; evaluate strict evidence first;
never reclassify every below-threshold row as uncertain; keep exact completion separate from block
coverage.

Observed route: passed. The Skill selects the annotation-table route, uses the detailed
DeepKOALA and confidence references, delegates normalization to MCP, and applies conservative
MODULE interpretation.

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

## Limitations and release use

This forward review covers the exact v0.1.0 release candidate. It used no live KEGG request and did
not benchmark model-to-model variability, malicious prompt injection, long-context degradation,
client-specific tool selection, or external annotator compatibility. Future releases must repeat
the review against their exact candidate; any routing or interpretation regression blocks release
even if the deterministic static tests still pass.

## Unreleased companion-runner evaluation

The optional `deepkoala-mcp` companion is now implemented as a separately installed service, but
it is not part of the observed or signed core v0.1.0 behavior above and has not received an
independent release sign-off. The Skill instructions now distinguish the optional companion from
the core server and describe its discovered orchestration contract. This documentation update does
not retroactively change the six-prompt v0.1.0 forward-review result.

Before a companion release, repeat the protein-FASTA prompt with at least these client states:

1. the companion is absent: retain multiple external annotation choices and never claim that local
   execution is available;
2. the companion is discovered but status is not structurally ready: explain the redacted
   inventory failure and offer external-run guidance without installing or downloading anything;
3. the companion is structurally ready but not selected: do not prepare or submit a job, and do
   not claim that a selected model has passed executable preflight;
4. the companion is selected: call `get_deepkoala_runner_status`, then use
   `prepare_deepkoala_job` as the authoritative selected-job preflight, display the complete notice,
   and wait for explicit user acknowledgement;
5. the notice changes or expires: do not reuse acknowledgement; prepare again and display the new
   notice; and
6. the user acknowledges the exact digest: call `submit_deepkoala_job`, poll with
   `get_deepkoala_job`, and use `cancel_deepkoala_job` or `delete_deepkoala_job` only for their
   documented user-authorized lifecycle actions.

The selected-run review must verify the user-visible `device=auto` GPU warning, requested and
resolved device, installed weight source and artifact identities, no-download statement,
updated-weight URL, effective `batch_size=32`, `num_workers=2`, `topk=1`, `multi=false`, default
two CPU threads, hard one-job concurrency limit, and running or queued disposition. If the user
requires CPU-only execution, the Skill must request `device=cpu` explicitly rather than treating
the two-thread default as a device-selection guarantee.

After success, the Skill must read the scoped output and provenance resources, follow bounded
pagination, decode each range's `content_base64`, assemble bytes in order, verify the total byte
count and SHA-256 digest, and pass the verified detailed CSV plus returned source-provenance
template to core
`normalize_ko_annotations` with `input_format=deepkoala_detailed`. It must not send FASTA to core,
ask core to dereference a companion resource URI, duplicate normalization, or infer that a K number
is experimentally validated.

On 2026-07-15, a separate manual runner smoke check exercised both `full` and `frag` models with
the checkout-bundled `202502` weights, official DeepKOALA commit
`bebbe0c43f50a26488f7092f6b355aae870a4ed9`, the existing Python 3.11/PyTorch `2.9.1+cu130`
environment, `torch.cuda.is_available() == false`, `device=cpu`, two CPU threads,
`batch_size=1`, `num_workers=0`, `topk=1`, and `multi=false`. Both jobs completed without a GPU,
download, or new environment and produced schema-valid detailed output. Both terminal jobs were
deleted and the temporary state root was empty afterward. This evidence establishes local CPU
compatibility for that exact configuration only; it is not a forward Skill evaluation, accelerator
evidence, biological validation, or companion release sign-off. Static Skill text and runner smoke
evidence remain complementary rather than interchangeable.
