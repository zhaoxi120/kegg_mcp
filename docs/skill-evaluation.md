# Codex Skill Evaluation

This document defines the compact release review for the three repository Skills. Deterministic
tests validate metadata, declared MCP dependencies, required instructions, and forbidden embedded
logic. The manual review is not a runtime LLM evaluation and does not claim that CI contacted KEGG,
executed DeepKOALA, or rendered a real KEGG asset.

## Deterministic checks

The `tests/skill/` suite verifies that:

- each Skill declares exactly one matching MCP dependency;
- protein FASTA, existing KO evidence, and renderer handoffs route to the appropriate Skill;
- no Skill implements inference, normalization, MODULE evaluation, KGML parsing, or rendering;
- cross-stage continuation uses stable files rather than private process identifiers; and
- absent explicit output paths, each server allocates a fresh directory beneath its configured
  project output root, while explicit user paths win;
- the first annotation call discloses the CPU default without adding a confirmation gate, while
  CUDA requires an explicit user request and compatible status;
- DeepKOALA model routing uses explicit provenance rather than an invented length cutoff; and
- biological and data-rights language remains conservative.

These checks run in the ordinary offline test suite.

## Release review matrix

Independent forward/manual reviews should cover the following routes against the exact release
candidate:

| Scenario | Expected behavior |
| --- | --- |
| Protein FASTA without KO assignments | Use the user's explicitly selected annotator; otherwise prefer `deepkoala-annotation`. Do not send FASTA to the core server or use the GenomeNet form as an automation fallback. |
| First DeepKOALA call in a Codex task | Tell the user that CPU is the default and that GPU requires an explicit request to the LLM. Continue the already authorized CPU job without waiting for confirmation. |
| Fragmented or metagenomic protein calls | Select `frag` before the first annotation call and briefly report why; an explicit user model choice wins. |
| Complete/reference proteins or ambiguous provenance | Select `full`; do not infer completeness from a sequence-length threshold. |
| No requested output directory | Omit `output_directory` so each server allocates beneath its configured project output root; never guess a writable root from an input path. |
| Explicit GPU annotation request | Use `device=cuda` only when status both allows CUDA and reports it available; otherwise stop instead of silently substituting CPU or automatic device selection. |
| DeepKOALA detailed CSV | Use `kegg-ko-analysis` and preserve source evidence and model provenance. |
| Plain K-number column | Normalize once, then run requested MODULE/pathway analysis through the core server. |
| Two KO sets | Report deterministic set and shared-reference differences without statistical claims. |
| Activity claim from one K number | Refuse the activity inference and explain the evidence boundary. |
| Existing `render_input.json` | Use `kegg-pathway-rendering` without recomputing core analysis. |
| Combined FASTA-to-graphics request | Continue across the three focused Skills using the stable CSV and renderer handoff files. |
| Combined FASTA-to-graphics request with only Core discovered | Stop before any core analysis call and ask once for explicit permission to install or repair the complete suite; after success, continue only in a new task where all stages are discovered. |
| Explicit multi-domain annotation request | Use `multi=true` with `batch_size=1` only when companion status reports the deployment capability ready. The Skill does not accept dependency paths or manage their provisioning; separate provisioning requires explicit user authorization. |

Reviewers should record the exact commit, Codex version, explicit or implicit Skill selection,
observed route, any clarification, MCP calls, stable handoffs, final interpretation, and failures.
All focused routes must pass before release.

## Limits

The manual review complements static tests; it does not replace a real suite installation and
new-task plugin discovery smoke. It should use synthetic or user-controlled inputs and no real KEGG
PNG or KGML payload. Live KEGG compatibility remains governed separately by
`tests/live/README.md`.

Model-to-model variability, malicious prompt injection, long-context degradation, client-specific
tool selection, and external annotator compatibility require separately assigned evaluation scope.
