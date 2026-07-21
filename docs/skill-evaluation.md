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
- biological and data-rights language remains conservative.

These checks run in the ordinary offline test suite.

## Release review matrix

Independent forward/manual reviews should cover the following routes against the exact release
candidate:

| Scenario | Expected behavior |
| --- | --- |
| Protein FASTA without KO assignments | Use `deepkoala-annotation`; do not send FASTA to the core server or use the GenomeNet form as an automation fallback. |
| DeepKOALA detailed CSV | Use `kegg-ko-analysis` and preserve source evidence and model provenance. |
| Plain K-number column | Normalize once, then run requested MODULE/pathway analysis through the core server. |
| Two KO sets | Report deterministic set and shared-reference differences without statistical claims. |
| Activity claim from one K number | Refuse the activity inference and explain the evidence boundary. |
| Existing `render_input.json` | Use `kegg-pathway-rendering` without recomputing core analysis. |
| Combined FASTA-to-graphics request | Continue across the three focused Skills using the stable CSV and renderer handoff files. |

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
