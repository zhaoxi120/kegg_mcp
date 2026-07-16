# Evidence visual-state policy

Use only the versioned legend and semantic states returned by the renderer. The Skill does not
choose colors, precedence, line styles, or pixel values.

- **Accepted:** at least one accepted KO annotation supports the mapped graphic or logic state.
- **Uncertain:** no accepted KO supports that state, but policy-defined uncertain evidence does.
  Accepted and uncertain evidence require distinct renderer-provided visual states and redundant
  non-color cues.
- **Not detected:** no selected accepted or uncertain evidence maps to the graphic. Leave its
  interpretation neutral; not detected is not biological absence.
- **Unsupported:** the renderer cannot map or display the content safely. Preserve its warning or
  summary-only state.

Rejected, unclassified, and invalid predictions are excluded from rendering evidence. Never color
rejected predictions and never report a rejected or unchanged graphic as absence. When a graphic
maps multiple K numbers, report the renderer's deterministic state and legend without recreating
its precedence logic.
