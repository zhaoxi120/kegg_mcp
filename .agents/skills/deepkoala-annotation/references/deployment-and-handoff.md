# Deployment and handoff

## Readiness routing

- `local_ready`: run the requested job directly.
- missing companion registration: explain how to register the existing executable, then stop.
- missing checkout, interpreter, or model resources: identify the missing deployment component and
  request permission before changing it.
- incompatible runtime, state root, output root, or device policy: return the companion's stable
  diagnostic and the named operator action. Do not work around policy in the Skill.

The companion must use an existing official checkout and existing local resources. It must not
automate the GenomeNet web form, make network requests, or download models.

## Stable file contract

A successful job provides:

- `schema_version` and `tool_version`;
- the original allowlisted absolute protein FASTA path;
- an absolute `deepkoala_annotations.csv` path;
- an absolute `deepkoala_run_report.md` path;
- `input_format="deepkoala_detailed"`;
- source provenance without workflow digests; and
- model, installed resource date, fixed execution parameters, and timestamps.

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
with its specific route state instead of continuing or substituting another annotator.
