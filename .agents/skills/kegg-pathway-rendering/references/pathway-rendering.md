# Pathway rendering

Render a regular reference pathway only from its compatible version 3 handoff. Use
`render_pathway` for one canonical `koNNNNN` target and `render_analysis_bundle` for a bounded
selection. The renderer owns KEGG PNG/KGML retrieval, validation, mapping, scene creation, SVG,
and optional bounded PNG rasterization.

Never recompute pathway coverage from KGML. Report the supplied namespace, scope, numerator,
denominator, ratio, evidence mode, retrieval time, cache state, calculation version, and warnings.
Coverage is descriptive KO coverage, not pathway presence, completeness, activity, flux, or
phenotype.

Global and overview pathway overlays are unsupported by the current renderer. Do not approximate
them with a regular box overlay or infer organism-specific claims from KO-only evidence. Never
fetch arbitrary URLs, expose endpoint configuration, return raw KEGG assets, or copy cache
payloads.

An original request that explicitly names `ko01100` is handled outside this Skill by a separate
model-native drawing step after supported renderer targets finish. That drawing must be labelled as
a model-generated conceptual diagram, not a KEGG-derived asset or KO coverage overlay. Never use
this exception to conceal or replace a failed regular-pathway render.
