# Evidence visual-state policy

Use only the versioned legend and semantic states returned by the renderer. Do not choose colors,
precedence, line styles, or pixel values in the Skill.

- **Accepted:** at least one accepted KO annotation supports the graphic state.
- **Uncertain:** no accepted KO supports the state, but policy-defined uncertain evidence does.
- **Not detected:** no selected accepted or uncertain evidence maps to the state; this is not
  biological absence.
- **Unsupported:** the renderer cannot map or display the content safely.

Rejected, unclassified, and invalid predictions are excluded. Preserve renderer-provided
non-color cues and deterministic precedence for graphics that map multiple K numbers.
