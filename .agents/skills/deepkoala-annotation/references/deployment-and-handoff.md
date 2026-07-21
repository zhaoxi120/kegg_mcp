# Deployment and handoff

## Readiness routing

- `local_ready`: run the requested job directly.
- `multi_dependencies_unavailable`: ordinary annotation remains available, but do not pass
  `multi=true`; report the operator action needed to repair HMMER, profiles, or the supported
  upstream interface.
- missing declared MCP dependency or required tool in Codex: stop before annotation, report an
  incomplete suite deployment, request explicit permission once to install or repair the complete
  repository suite, and resume the original request in a new Codex task after discovery.
- missing companion registration in another MCP client: explain how to register the existing
  executable, then stop.
- missing checkout, interpreter, or model resources: identify the missing deployment component and
  request permission before changing it.
- incompatible runtime, state root, output root, or device policy: return the companion's stable
  diagnostic and the named operator action. Do not work around policy in the Skill.

Suite installation permission applies once to each new suite installation root. An installed
`local_ready` deployment does not repeat that question for later FASTA jobs; a separate new root is
a separate first installation.

DeepKOALA is the preferred first route for protein FASTA unless the user explicitly selected
another annotator. In that case, this Skill stops and the independent core stage can resume only
after the selected workflow supplies supported KO evidence. If the user instead declines a
requested suite action, remain stopped until a user-selected route supplies that evidence.

The companion must use an existing official checkout and existing local resources. Multi-domain
execution additionally requires deployment `allow_multi`, a direct trusted profile directory, a
direct trusted absolute `hmmsearch` executable, and `multi_ready=true`. These optional dependencies
are operator-managed and may be provisioned separately after explicit user authorization; this
Skill does not configure them. The companion must not automate the GenomeNet web form or make
network requests.

## Stable file contract

A successful job provides:

- `schema_version` and `tool_version`;
- the original allowlisted absolute protein FASTA path;
- an absolute `deepkoala_annotations.csv` path;
- an absolute `deepkoala_run_report.md` path;
- `input_format="deepkoala_detailed"`;
- source provenance without workflow digests; and
- model, installed resource date, fixed execution parameters, and timestamps.

The execution parameters include the actual boolean `multi` value. A fully empty prediction,
score, annotation marker, and coordinate tuple is an unclassified row rather than a KO assignment;
partially empty or malformed evidence is invalid.

Pass the CSV path and provenance to the independent KO-analysis stage only when both servers allow
the shared output root. If a client has no shared filesystem, read the companion's bounded resource
pages and pass the reconstructed content through a core-supported bounded inline transport. The
Skill must not parse, transform, or validate CSV rows itself.

Treat private job identifiers and resource URIs as process-scoped. Stable output-directory files,
not a private identifier, are the cross-MCP handoff.

## Automatic cross-Skill continuation

When the original user request includes downstream KEGG analysis, a successful annotation stage
continues with the installed `kegg-ko-analysis` Skill using the returned `annotations_path`,
`input_format`, and `source` values unchanged. The transition uses the stable CSV rather than the
job identifier and does not require the user to copy a path, repeat the request, or approve an
already requested analysis stage.

When the original request also includes graphics, retain that goal for the later
`kegg-pathway-rendering` stage. Do not interpret that goal here, and do not call a core or renderer
MCP from this Skill. A failed or unready annotation stage has no valid downstream handoff, so stop
with its specific route state instead of continuing. If a required downstream component is
unavailable, preserve the requested formats and target scope for resumption after the suite is
repaired and discovered in a new Codex task.
