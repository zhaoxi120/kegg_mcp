# Pathway rendering

Render a pathway only from the current version 5 handoff whose canonical `koNNNNN` target is
marked `renderable`. Use `render_pathway` for one target and `render_analysis_bundle` for a bounded
selection. The renderer owns KEGG PNG/KGML retrieval, validation, mapping, scene creation, SVG,
and optional bounded PNG rasterization.

Never recompute pathway coverage from KGML. Report the supplied namespace, scope, numerator,
denominator, ratio, retrieval time, cache state, calculation version, and warnings.
Coverage is descriptive KO coverage, not pathway presence, completeness, activity, flux, or
phenotype.

Regular maps use bounded KGML box geometry. An explicitly requested canonical KO global or
overview map, such as `ko01100`, is eligible only when Core evaluated it with
`allow_global_or_overview=True` and emitted a complete renderable version 5 target. Its evidence
overlay follows bounded KGML `line` coordinates. The overlay highlights accepted KO annotation
evidence only. Arrows already present in the validated PNG remain background context;
the overlay does not reconstruct arrow direction or establish pathway direction, activity,
completeness, or flux.

Do not convert a summary-only broad target, schema-mismatched handoff, `map` target, or
organism-specific target into a total-map overlay,
and do not create a model-native conceptual fallback. Never fetch arbitrary URLs, expose endpoint
configuration, return raw KEGG assets, or copy cache payloads.
