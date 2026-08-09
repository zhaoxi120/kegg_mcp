# Confidence policy

Keep source evidence and normalized analysis decisions separate.

A K-number assignment is computational annotation evidence, not experimental validation.

- Preserve every raw decision, score, score type, threshold rule, rank, domain coordinate, and
  source/model version. Apply only a named, versioned normalization policy.
- Use only sorted unique accepted K numbers for MODULE, pathway, ranking, comparison, and
  rendering analysis. Source-rejected, unclassified, invalid, and below-threshold records enter no
  analysis set.
- Do not treat a source-rejected assignment as evidence that the function is absent.
- Do not compare scores or thresholds between annotation tools unless their semantics are known to
  be compatible.
- Preserve multiple assignments to one sequence, including top-k and domain-level records. Derive
  a KO set as an analysis view; do not replace the record-level evidence with it.

Full normalization retains record-level evidence. The high-level analysis route always uses an
intentionally compact analysis view: state that it retains aggregate counts and sorted unique
accepted K numbers but not record evidence, protein-to-KO mappings, or duplicate/conflict
accounting. Never imply that this view is a normalized annotation table. Route requests for those
records through `normalize_ko_annotations` or the audit workflow, regardless of input size.
