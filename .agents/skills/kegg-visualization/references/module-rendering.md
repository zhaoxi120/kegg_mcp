# MODULE rendering

MODULE graphics are project-owned logic diagrams, not KEGG pathway maps or biochemical topology
claims. Use `render_module` for one canonical `MNNNNN` target and `render_analysis_bundle` for a
bounded selection.

Require the renderer to preserve the authoritative core structure:

- top-level spaces and plus signs are AND;
- commas are OR;
- a minus sign marks an optional component;
- parentheses preserve grouping;
- MODULE references remain distinct nodes and expand only from the resolved graph; and
- unsupported tokens plus unresolved or cyclic references remain visible with their reasons.

Keep exact strict and lenient completion separate from project block coverage. Keep optional
components outside the required denominator. Surface policy-defined uncertain support separately
when it changes a lenient result. If a target is partially evaluable, not evaluable, oversized, or
not renderable, preserve that status instead of dropping blocks or reconstructing the AST.

Minimal missing alternatives are bounded results under the evaluated definition. Never claim that
adding the shown genes will activate a biological process.
