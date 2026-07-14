# External annotation tools

Use an external annotator only when the user lacks KO assignments. The MCP server imports results;
it does not install, download, or execute these tools.

## Selection factors

Ask about only factors that affect the choice:

- complete proteins versus fragments;
- likely multi-domain proteins;
- local execution versus submission to an external service;
- dataset size, CPU and memory constraints, and acceptable runtime;
- licensing, reference-data access, privacy, and institutional policy; and
- availability of detailed, machine-readable, versioned output.

## Options

- **DeepKOALA:** local or GenomeNet neural KO assignment with full-length and fragment models.
  Consider it when its documented model scope and compute profile fit. Read
  [deepkoala.md](deepkoala.md) before giving a command.
- **KofamScan or KofamKOALA:** profile-HMM-based assignment. Confirm the current profile/database
  requirements, thresholds, license, and output fields from the official documentation.
- **BlastKOALA or GhostKOALA:** KEGG/GenomeNet similarity-search services. Confirm current input
  limits, data-submission policy, reference selection, and result format before recommending them.
- **Other validated pipelines:** accept generic KO tables when their score semantics and decision
  rules are documented. Require explicit column mapping if the format is not recognized.

Do not compare scores or thresholds across tools unless their documented semantics are compatible.
Do not convert a best hit, gene name, or product description into a KO without tool-produced
evidence. Preserve unassigned and rejected rows rather than treating them as biological absence.

Official KEGG annotation-tool descriptions were checked on 2026-07-14:
<https://www.genome.jp/kegg/annotation/>.
