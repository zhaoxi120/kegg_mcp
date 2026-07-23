# Module and pathway interpretation

## KEGG MODULE results

- Report exact completion as the Boolean evaluation of all required logical blocks.
- Report the project's top-level block coverage separately as completed required blocks divided by
  required blocks, only when every required block is evaluable.
- Do not call block coverage an official KEGG completeness percentage.
- Optional components do not increase the required denominator. Report their presence separately.
- Preserve AND, OR, optional, parenthesized, and referenced-module semantics. A required block
  whose truth cannot be established safely because of unsupported, malformed, unresolved, cyclic,
  or unavailable content is not evaluable and retains its reason. Report `partially_evaluable`
  when another required block is evaluable and `not_evaluable` when none can be evaluated safely;
  never silently discard the affected content.
- Treat minimal missing alternatives as bounded requirements under the evaluated definition, not
  proof that adding those genes will create an active biological process.

## Pathway results

- Describe KO coverage only as detected unique reference KOs divided by the unique linked-KO
  denominator retrieved for the stated `map`, `ko`, or supported organism reference.
- State the reference type, namespace, numerator, denominator, retrieval time, and cache state.
- Do not equate coverage with pathway presence, completeness, activity, flux, phenotype, or
  statistical significance.
- Do not apply an organism-specific denominator to KO-only input. Global or overview maps require
  explicit opt-in when the tool schema requests it.

## Analysis units

Interpret a single genome, MAG, isolate proteome, pangenome, and metagenomic community differently.
For a pangenome or community, module completion and coverage describe pooled encoded potential; they
do not establish that all components co-occur in one organism or are expressed together.
