# Pathway rendering

## Supported route

Render a regular reference pathway target only from its compatible version 2 handoff. Use
`render_pathway` for one canonical `koNNNNN` target and `render_analysis_bundle` for a bounded
selection. The renderer owns KEGG image and KGML retrieval, validation, mapping, scene creation,
SVG output, and optional bounded PNG rasterization.

Never recompute the supplied pathway coverage from KGML graphics. Report its reference namespace
and scope, numerator, denominator, ratio, evidence mode, retrieval time, cache state, calculation
version, and warnings. Coverage is descriptive KO coverage, not pathway presence, completeness,
activity, flux, or phenotype.

## Unsupported map types

Global and overview maps require a separately reviewed line-overlay policy. Preserve the
renderer-provided rejection or explicit summary-only result; do not approximate them with regular
KO-box overlays. Do not infer organism-specific pathway claims from KO-only input.

## Access and assets

Surface stale-cache and asset-identity warnings. If a pathway asset needs network access, let the
renderer use its configured KEGG access gate and rate limiter. Do not fetch arbitrary URLs, expose
endpoint configuration, copy cache payloads, or return raw KEGG PNG or KGML content.
