# MODULE rendering

MODULE graphics are project-owned logic diagrams, not biochemical topology claims. Use
`render_module` for one canonical `MNNNNN` target and `render_analysis_bundle` for a bounded
selection.

Require the renderer to preserve top-level AND blocks, OR alternatives, optional components,
parentheses, MODULE references, and visible unsupported or cyclic content. Keep exact strict and
lenient completion separate from project block coverage and keep optional components outside the
required denominator.

Preserve partially evaluable, not evaluable, oversized, summary-only, or not-renderable status.
Never drop blocks, reconstruct an AST, or claim that adding a minimal missing alternative will
activate a biological process.
