# Confidence policy

Keep source evidence and normalized analysis decisions separate.

- Preserve every raw decision, score, score type, threshold rule, rank, domain coordinate, and
  source/model version. Apply only a named, versioned normalization policy.
- Use accepted K numbers only for strict analysis.
- Use accepted plus policy-defined uncertain K numbers for lenient analysis.
- Never place all below-threshold predictions into lenient analysis. Source-rejected,
  unclassified, and invalid records enter neither mode.
- Do not treat a source-rejected assignment as evidence that the function is absent.
- Do not compare scores or thresholds between annotation tools unless their semantics are known to
  be compatible.
- Preserve multiple assignments to one sequence, including top-k and domain-level records. Derive
  a KO set as an analysis view; do not replace the record-level evidence with it.

When strict and lenient results differ, identify the uncertain K numbers and source records that
caused the change. If no documented policy produced uncertain records, do not invent a lenient
result.
