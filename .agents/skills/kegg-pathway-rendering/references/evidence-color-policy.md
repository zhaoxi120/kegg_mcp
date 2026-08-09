# Evidence visual-state policy

Use only the versioned legend and semantic states returned by the renderer. Do not choose colors,
precedence, line styles, or pixel values in the Skill.

- **Accepted:** at least one accepted KO annotation supports the graphic state.
- **Not detected:** no selected accepted evidence maps to the state; this does not establish
  biological absence.
- **Unsupported:** the renderer cannot map or display the content safely.

Rejected, unclassified, and invalid predictions are excluded. Multiple accepted K numbers mapped
to one box or line do not create another visual state or line style. Unmatched graphics remain
unchanged. The source PNG's
pathway-category colors remain background context; neither unmatched state nor category color is
evidence of presence or absence.
