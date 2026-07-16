# Rights and reporting

## Deployment and data boundaries

Treat KEGG access rights as deployment configuration. The public KEGG service is for academic use
by academic users; non-academic deployments require an appropriately licensed endpoint, and a
service-provider deployment requires its own review. Do not turn this into a per-render user
confirmation or make redistribution claims for rendered derivatives.

Keep raw KEGG images, KGML, cache payloads, endpoint details, credentials, usernames, and full
cache paths out of responses, repositories, packages, releases, examples, and CI artifacts. Use
only renderer-returned bounded artifacts and validated local resource URIs.

## Result summary

For each artifact, report:

- canonical target, output format, and renderer-provided resource URI;
- accepted and policy-defined uncertain evidence semantics;
- pathway reference scope and denominator, or MODULE exact completion and block coverage as
  separate quantities;
- retrieval and cache provenance, calculation or parser versions, and stale state when returned;
- unsupported content, truncation, output bounds, and other warnings; and
- the analysis unit and conservative interpretation limits.

State that a visualization presents KO annotation evidence. It does not validate pathway activity,
complete organismal capability, metabolic flux, phenotype, or experimental function. A community-
level graphic represents pooled encoded potential and not a complete pathway in one organism.
