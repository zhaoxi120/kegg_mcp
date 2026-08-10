# Core MCP Server

The core runs as a local stdio server. It never launches DeepKOALA or another annotation tool,
parses KGML, renders images, or exposes remote HTTP transport. Optional annotation and rendering
capabilities are separate local stdio processes and independently reviewed distributions:

```text
deepkoala-mcp -> detailed annotation CSV -> kegg-mcp
kegg-mcp      -> render_input.json version 6 -> kegg-render-mcp
```

See the [DeepKOALA companion README](../companions/deepkoala-mcp/README.md) and
[renderer companion README](../companions/kegg-render-mcp/README.md) for their independent
contracts.

This document owns Core tools, resources, transport schemas, public retention behavior, protocol
errors, and deployment environment variables. Transport-independent orchestration, serializers,
bundle publication, and storage internals are owned by
[Services, Result Storage, and Reporting](services-results-reporting.md).

## Start the server

After installing the project, configure an MCP client to run:

```text
uv run kegg-mcp
```

The raw Core distribution defaults to network-disabled `offline_cache`. A cache miss never enables
live access. The suite installer does not inherit this default and requires the operator to select
an access mode explicitly. Server logs and configuration failures are written to stderr; stdout is
reserved for MCP protocol messages.

Core supports CPython 3.11.x on Linux and native Apple Silicon macOS 14 or later. Native Intel
macOS and native Windows server execution are unsupported; Windows hosts use WSL2 as the Linux
route. The native Windows diagnostic reports that platform boundary without starting the server
or replacing the required POSIX security and lock backends.

Local pytest skips live requests by default. Pull-request CI explicitly runs the bounded
at-most-120-request public-academic compatibility campaign once.

Use the side-effect-free operator diagnostic before client startup:

```text
uv run kegg-mcp doctor
uv run kegg-mcp doctor --json
```

The diagnostic validates configuration but performs no KEGG request or SQLite inspection and
redacts allowed-root paths and endpoint values. `kegg-mcp serve` is an explicit equivalent to the
default stdio command.

## KEGG access configuration

| Environment variable | Meaning |
| --- | --- |
| `KEGG_MCP_ACCESS_MODE` | `public_academic`, `licensed`, or network-disabled `offline_cache` (default) |
| `KEGG_MCP_ACADEMIC_USE_CONFIRMED` | Must equal `true` before `public_academic` access is enabled |
| `KEGG_MCP_LICENSED_ENDPOINT` | Authorized HTTPS endpoint for licensed access |
| `KEGG_MCP_LICENSED_USE_CONFIRMED` | Must equal `true` before licensed access is enabled |
| `KEGG_MCP_CACHE_PATH` | Optional absolute path to the user-local KEGG cache |
| `KEGG_MCP_CACHE_MAX_ENTRIES` | Optional positive cache-row limit; default 10,000 |
| `KEGG_MCP_CACHE_MAX_PAYLOAD_BYTES` | Optional positive cache-payload limit; default 512 MiB |
| `KEGG_MCP_CACHE_MAX_DATABASE_BYTES` | Optional positive SQLite main-database limit; default 640 MiB |
| `KEGG_MCP_RATE_LIMIT_ROOT` | Optional owner-only state root shared by Core and Renderer |
| `KEGG_MCP_RESULT_STORE_PATH` | Optional absolute path to the user-local retained-result database |
| `KEGG_MCP_ALLOWED_ROOTS` | Path-separated existing directories allowed for file input and output bundles |

The public KEGG REST service is limited to academic use by academic users. Other deployments must
use an appropriately licensed endpoint. The live client defaults to two requests per second with
no burst, enforces a hard deployment-wide maximum no greater than three requests per second for
one canonical endpoint, and batches `get` requests at no more than ten entries. Core and Renderer
coordinate through the same owner-only rate-limit root.

Cache payloads and retained results are local data and must not be committed, packaged, or attached
to CI artifacts. The low-level client keeps explicit refresh semantics, while high-level MCP query
and analysis services use fresh-cache-first requests so an equivalent call can report cache hits
without repeating network requests. An explicit service caller may still request refresh. The
cached-entry resource remains a cache-only read and never falls back to the network.

`get_server_status` and `ko-analysis://cache/info` report redacted configuration state. Status
includes `file_handoff_enabled` and `allowed_root_count`, but never the configured roots. These
surfaces do not probe connectivity or enumerate cache contents. Use the explicit connectivity
tool when a live-access preflight is required. Operators can inspect bounded cache counts with
`kegg-mcp cache status --json` and remove only expired rows with
`kegg-mcp cache cleanup --expired --json`.

The MCP initialization response guides clients toward the high-level analysis tool and records
the connectivity, file-handoff, result-scope, stable-bundle, and biological interpretation
boundaries. It does not replace the explicit input and output schemas.

Every advertised input schema is self-contained: local Pydantic references are inlined so clients
can display nested object fields and enum values without resolving `$defs`.

## Tools

The server exposes eighteen Core tools:

- `analyze_ko_annotations`: one-call annotation intake and MODULE/pathway analysis. Supply either
  `ko_text` or a nested `annotations` request. If no MODULE or pathway target and no explicit
  selection are supplied, the server independently selects up to five MODULEs and up to five
  canonical KO reference pathways by unique accepted-KO overlap. MODULE ranking selects targets;
  it is not completion or enrichment, and exact completion is evaluated separately. To override
  the pathway count, supply only
  `pathway_selection.top_n` from 1 through 25. The server applies its versioned
  `selected_unique_ko_count` ranking policy before reference loading. Every supported input is
  reduced to the same compact sorted unique accepted-KO analysis view. This workflow does not
  retain normalized records or protein mappings; use `normalize_ko_annotations` or audit when
  record evidence is required.
- `normalize_ko_annotations`: normalize inline content or an allowed-root file containing plain
  K numbers, generic CSV/TSV, or a DeepKOALA detailed table, then retain the complete dataset.
- `get_kegg_entries`: retrieve selected allowlisted KEGG entries with `projection="preview"`
  (default), `projection="card"`, or `projection="references"`. Preview returns at most ten
  compact flat-file or BRITE text previews. Card projection accepts KO, MODULE, pathway, reaction,
  enzyme, compound, glycan, gene, and genome entries,
  deterministically parses their supported fields into typed cards, returns at most ten card
  previews, and retains a versioned `entry_snapshot` beside the complete parsed GET `detail`.
  Unknown flat-file field names remain recorded on the cards, while their complete content remains
  in the parsed detail; a card is a field projection, not an LLM summary. The snapshot is
  current-scope retained data for local comparison and preserves the original database-qualified
  GET request independently from returned canonical identifiers. It is not a durable KEGG archive
  or a new retrieval endpoint. References projection accepts the same nine card-supported entry
  types and deterministically extracts only PubMed identifiers explicitly listed in KEGG
  `REFERENCE` fields. It returns at most ten entry previews with at most ten PMID previews each and
  retains the complete card `entry_snapshot`; it does not retrieve, read, or summarize papers.
  All projections report total provenance-batch count and at most five provenance records;
  complete provenance remains in `detail`.
- `search_kegg_entries`: perform keyword FIND over KO, pathway, MODULE, reaction, enzyme, compound,
  glycan, drug, reaction class, genome, or organism. Formula, exact-mass, and molecular-weight
  modes are limited to compound or drug. It returns bounded endpoint candidates without an
  invented relevance score, canonical-name claim, or automatic best-match selection. The direct
  result returns at most ten candidate previews with 128-character match-text previews and compact
  retrieval counts; full rows and provenance remain in the scoped `detail` artifact. Chemical
  search results are candidates, not identifications.
- `resolve_kegg_entities`: resolve a discriminated gene, organism, or substance request through
  typed FIND, GET, CONV, LINK, and optional organism-pathway LIST steps. Substance resolution
  accepts KEGG compound, glycan, or drug identifiers, ChEBI identifiers, and explicitly named
  PubChem SIDs; it never interprets PubChem input as a CID or claims compound identification.
  Gene symbols require organism context, and organism mismatches and all ambiguous candidates
  remain explicit. Taxonomy resolution supports `exact`, `species`, `genus`, `family`, `order`,
  `class`, and `phylum`; `candidate_materialization="auto"` fully materializes exact/species
  results but uses identity-only candidates for broader ranks unless the caller selects `"full"`.
  Organism pathway LIST retrieval remains explicit and requires full materialization. The direct
  result returns bounded input, candidate, projected-entity, taxonomy, and pathway previews plus
  compact retrieval counts; complete crosswalks and provenance remain in `detail`. Unmapped
  identifiers are mapping outcomes rather than evidence of biological absence.
- `trace_kegg_relations`: traverse one or two levels over a fixed relation allowlist with at most
  200 nodes and 500 edges. The allowlist includes the existing gene, KO, enzyme, reaction,
  compound, pathway, BRITE, genome, and taxonomy directions plus MODULE-to-KO/pathway/reaction,
  pathway-to-MODULE, glycan-to/from reaction or pathway, and drug-to-pathway directions.
  KO-to-gene and organism-specific pathway-to-gene require one canonical `organism_scope`; no
  global KO-to-all-genes expansion is exposed. The direct result returns retrieval counts and at
  most 25 node and 25 edge previews; the complete bounded graph, provenance, and edge provenance
  indexes remain retained. Typed edges are database cross-references, not evidence of regulation,
  causality, activity, flux, phenotype, or mechanism. Selected-entry reaction-class edges and
  RMODULE routes are not exposed because the live public endpoint shapes checked on 2026-07-30 did
  not support a safe selected-entry contract.
- `map_brite_hierarchy`: preserve all matched BRITE paths, multi-parent memberships, unmatched
  entities, and descriptive unique-input classification counts. The direct result returns at most
  three lightweight path and classification previews, ten unmatched-entity previews, clipped node
  names, and compact retrieval counts. Complete paths, classifications, and provenance remain
  retained; the TSV protects formula-like spreadsheet cells. It does not calculate enrichment or
  dominant function.
- `audit_annotation_mapping`: summarize source evidence, the accepted-KO view, mapping
  yields, mapping-degree distributions, retrieval provenance, and optional assembly-quality
  warnings. `mapping_targets` selects any subset of pathway, MODULE, reaction, enzyme, and BRITE;
  it defaults to all five and an empty list requests an evidence-only audit. The server preflights
  exact LINK batches. If the selected mapping would exceed the 100-request audit budget, it reports
  `skipped_request_limit` while preserving the complete local evidence audit instead of failing the
  whole call. If an in-progress target exceeds the aggregate row or response-byte limit, the
  result instead reports `incomplete_row_limit` or `incomplete_response_limit`, retains summaries
  only for previously completed targets, discards partial rows for the incomplete target, and
  preserves the complete local evidence audit. The direct result contains evidence counts,
  execution state, compact per-target yields, compact retrieval counts, and at most five warning
  previews. Complete degree distributions, KO previews, warnings, completed relationship rows, and
  provenance remain retained. It never corrects scores, fills missing K numbers, or interprets an
  unmapped KO as biological absence.
- `compare_kegg_reference_snapshots`: compare two canonical `entry_snapshot` artifacts produced by
  typed card or references projection in the current stdio scope. Both snapshots must use the
  current card schema and cover the same database-qualified requested entries. The caller selects
  any of entry fields,
  relationships, MODULE definitions, and pathway KO denominators; membership changes from returned
  to missing or vice versa remain explicit. The operation is local and makes no KEGG request. Its
  direct result reports parser, endpoint, retrieval, and release compatibility plus at most 25
  value-free change locations; the complete deterministic diff is retained. A structural
  difference is not biological gain, loss, validation, or a general KEGG release history.
- `write_kegg_reference_bundle`: persist one successful canonical `entry_snapshot` from card or
  references projection, an optional selected entry subset, and an optional current-scope BRITE
  mapping in one explicit allowed-root output directory. It writes `reference_snapshot.json`,
  `relationships.tsv`, optional
  `brite_paths.tsv`, and the commit-marker `reference_manifest.json`. The snapshot records the
  selected cards, request, parser/schema, sanitized retrieval batches, and optional BRITE detail.
  The manifest records the producer, selection and optional BRITE summary, sanitized retrieval
  summary, and payload size, MIME, and hash metadata without result IDs, request keys, endpoint
  values, or local paths. This local tool makes no KEGG request and exports neither the raw cache
  nor an unbounded KEGG mirror.
- `prepare_kegg_handoff`: prepare one discriminated local handoff under an explicit allowed-root
  output directory. Its seven targets prepare validated input files for KEGG Mapper Reconstruct,
  Search, Color, Join, or MWsearch, or KEGG Syntax KO Composition or caller-ordered KO Sequence.
  Each bundle contains the target-specific data file and commit-marker `handoff_manifest.json`.
  Every target is local and makes no KEGG request; the tool does not upload data, launch a browser,
  execute an external tool, or parse its result. Mapper Reconstruct accepts one unannotated data
  block only; the official `#` multi-organism and comment-block forms are unsupported. Caller text
  fields are serialized verbatim, while tab, CR, LF, NUL, other C0 controls, and DEL are rejected
  before writing. Mapper Color accepts `#RRGGBB` or an ASCII letter name matching
  `[A-Za-z]{3,20}` and preserves caller casing. These formats were checked on 2026-07-31 against
  the official [KEGG Mapper Reconstruct](https://www.kegg.jp/kegg/mapper/reconstruct.html),
  [KEGG Mapper Color](https://www.kegg.jp/kegg/mapper/color.html), and
  [KEGG Syntax user-data analysis](https://www.kegg.jp/kegg/syntax/synteny.html) pages.
- `analyze_modules`: evaluate exact MODULE completion and required-block coverage from inline or
  retained evidence.
- `analyze_pathways`: calculate descriptive unique-KO coverage after inferring and validating the
  pathway namespace; paired `mapNNNNN` input is canonicalized to the `koNNNNN` reference view,
  while organism-specific identifiers are never folded into that pair and are rejected by the
  public KO-only analysis tools.
- `compare_ko_sets`: calculate deterministic set differences for two to ten datasets, with optional
  shared-reference MODULE or pathway comparisons.
- `probe_kegg_connectivity`: make one explicit low-cost INFO request and classify DNS,
  connection, local cache/rate-limit storage, or authorization/configuration outcomes.
- `list_analysis_results`: list a bounded metadata page of active results in the current stdio
  scope without exposing any other scope.
- `delete_analysis_result`: immediately delete one retained result in the current stdio scope.
- `get_server_status`: return redacted access, capability, and result-retention information.

The advertised MCP behavior hints describe local effects as well as remote effects:

| Tool class | Read-only | Destructive | Idempotent | Open world |
| --- | --- | --- | --- | --- |
| `get_server_status`, `list_analysis_results` | Yes | No | Yes | No |
| `normalize_ko_annotations`, `compare_kegg_reference_snapshots` | No | No | No | No |
| `write_kegg_reference_bundle` | No | No | No | No |
| `prepare_kegg_handoff` | No | No | No | No |
| KEGG retrieval and analysis tools | No | No | No | Yes |
| `probe_kegg_connectivity` | No | No | No | Yes |
| `delete_analysis_result` | No | Yes | Yes | No |

Normalization and analysis create retained results and can create an output bundle. Reference and
input-handoff tools publish new local files, so they are additive rather than read-only or
idempotent. The seven handoff targets are closed-world local formatting operations. Other
KEGG-facing tools can update the local response cache, so even a connectivity probe is not
advertised as read-only. Deletion is idempotent in environmental effect although a repeated call
returns the same safe not-found class. These hints inform clients; they do not replace server-side
validation or authorization.

Minimal compound candidate search:

```json
{"database":"compound","query":"glucose","mode":"keyword","max_results":20}
```

Minimal typed card snapshot:

```json
{
  "entries": [
    {"database":"reaction","identifier":"R01786"},
    {"database":"compound","identifier":"C00031"}
  ],
  "projection": "card"
}
```

Minimal deterministic KEGG-listed PMID projection:

```json
{
  "entries": [
    {"database":"reaction","identifier":"R01786"},
    {"database":"compound","identifier":"C00031"}
  ],
  "projection": "references"
}
```

This returns only identifiers present in those KEGG flat-file `REFERENCE` fields. A returned PMID
is a KEGG-linked citation, not a paper summary, causal claim, or validation.

Minimal ChEBI-to-compound resolution:

```json
{
  "kind": "substance",
  "source_namespace": "chebi",
  "identifiers": ["CHEBI:17234"],
  "targets": ["kegg_compound", "reaction", "pathway"],
  "ambiguity_policy": "report_all"
}
```

The related PubChem namespace is `pubchem_sid`; PubChem CIDs are not accepted or reinterpreted.

Minimal taxonomy-to-organism resolution that preserves all family candidates without materializing
every GENOME record:

```json
{
  "kind": "organism",
  "source_namespace": "taxonomy",
  "identifiers": ["taxid:543"],
  "taxonomy_rank": "family",
  "candidate_materialization": "identity_only"
}
```

Set `"include_pathway_directory": true` only when the caller needs each resolved organism's
available KEGG pathway-reference directory; that option requires effective full materialization
(`"auto"` already provides it for exact/species, while broader ranks require explicit `"full"`).
Omitting it performs no organism-pathway LIST request.

Minimal one-level typed relation trace:

```json
{
  "seeds": [{"kind":"ko","identifier":"K00844"}],
  "edge_types": ["ko_to_reaction"],
  "max_depth": 1
}
```

Organism-scoped KO-to-gene trace:

```json
{
  "seeds": [{"kind":"ko","identifier":"K01810"}],
  "edge_types": ["ko_to_gene"],
  "organism_scope": "eco",
  "max_depth": 1
}
```

Minimal local reference comparison after two successful card-projection calls for the same
requested entries:

```json
{
  "left": {"result_id":"<opaque-left-result-id>"},
  "right": {"result_id":"<opaque-right-result-id>"},
  "compare": ["entry_fields", "relationships", "module_definitions", "pathway_denominators"]
}
```

Minimal durable export of one successful card result:

```json
{
  "source": {"result_id":"<opaque-card-result-id>"},
  "entries": [
    {"database":"reaction","identifier":"R01786"}
  ],
  "output_directory": "/absolute/private/results/reaction-reference"
}
```

Omit `entries` to select the complete bounded card snapshot. Add
`"brite_source":{"result_id":"<opaque-brite-result-id>"}` only when a successful
`map_brite_hierarchy` detail from the same stdio scope should be included.

Minimal caller-ordered KEGG Syntax sequence handoff:

```json
{
  "output_directory": "/absolute/private/results/syntax-sequence",
  "handoff": {
    "target": "syntax_ko_sequence",
    "order_semantics": "caller_supplied_genomic_order",
    "rows": [
      {"gene_id":"gene_001","ko_id":"K00844"},
      {"gene_id":"gene_002","ko_id":"K01810"}
    ]
  }
}
```

Core preserves that row order. The required
`order_semantics="caller_supplied_genomic_order"` field records its provenance; Core does not
infer coordinates, upload the file, run KEGG Syntax, or parse a result. The other external target
values are `mapper_reconstruct`, `mapper_search`,
`mapper_color`, `mapper_join`, `mapper_mwsearch`, and `syntax_ko_composition`; clients should use
the discovered branch schema rather than construct unvalidated upload text.

Bounded query, card, audit, and reference-comparison direct results are independently limited to
64 KiB. A projection that violates that bound fails closed and compensates the newly created
retained result; callers use a returned scoped resource to read complete bounded detail only after
the projection succeeds.

Minimal single-relationship mapping audit:

```json
{
  "source": {"ko_text":"K00844\nK01810"},
  "mapping_targets": ["pathway"]
}
```

Minimal plain-KO normalization input:

```json
{"text":"K00844\nK01810"}
```

Minimal one-call MODULE input:

```json
{
  "ko_text": "K00844\nK01810",
  "module_ids": ["M00001"],
  "analysis_unit": "isolate_proteome"
}
```

Minimal server-ranked Top-1 pathway input:

```json
{
  "annotations": {
    "file_path": "/absolute/private/handoff/deepkoala_annotations.csv",
    "input_format": "deepkoala_detailed"
  },
  "pathway_selection": {
    "top_n": 1
  },
  "output_directory": "/absolute/private/results/top-pathway"
}
```

The same request shape applies to small and large DeepKOALA files. Allowed detailed-file input is
streamed under fixed maxima of 1 GiB, 10,000,000 source rows, 20,000,000 expanded assignments,
100,000 unique accepted K numbers, 64 columns, and 16,384 characters per field. High-level analysis
retains exact aggregate counts, source/policy provenance, at most 100 diagnostics, and the compact
accepted-KO view; it does not retain annotation records, protein-to-KO mappings, or
duplicate/conflict accounting. Bounded inline, plain-KO, and generic-table inputs produce the same
analysis view under their applicable importer limits. `normalize_ko_annotations` remains the
full-record route. Existing KEGG request, relationship-row, reference-loading, ranking, report, and
output budgets still apply. If a large accepted-KO set would exceed automatic Top-N KO-to-target
mapping limits, provide bounded explicit `module_ids` or `pathways`, or split the source into
independently meaningful analysis units.

Minimal explicit pathway input:

```json
{
  "ko_text": "K00844\nK01810",
  "analysis_unit": "isolate_proteome",
  "pathways": [{"pathway_id": "ko00010"}]
}
```

The server selects sorted unique accepted K numbers, performs one logical KO-to-pathway
mapping stage, de-duplicates each pathway's K numbers, sorts by descending unique selected-KO count
and then canonical pathway ID. Automatic selection directly excludes the current KEGG Global,
Overview, and higher-level Overview KO map identifiers before Top-N truncation and fills up to the
requested count from subsequent regular references; the complete overlap ranking remains retained.
The fixed identifier set was checked against the official KEGG PATHWAY identifier classes and map
list on 2026-07-22. An explicitly requested canonical KO total map such as `ko01100` requires
`allow_global_or_overview=true`; when its denominator is evaluated and detected evidence is
complete, Core emits a renderable version 6 handoff. Its renderer overlay follows bounded KGML line
coordinates and does not infer arrow direction, pathway activity, completeness, or flux. `map` and
organism references remain summary-only. The server loads pathway LINK/GET references only for the
selected targets. Duplicate annotation records and duplicate LINK rows cannot increase the detected
node count.

Generic tables with unambiguous common headers are mapped automatically and the decision is
reported; ambiguous or non-standard tables require an explicit mapping. When `annotations` is used
in the high-level tool, biological context belongs inside that nested object. KO-only MCP inputs do
not accept organism-specific pathway references because they lack gene-level context. Cache tuning,
refresh flags, and internal limit models are deployment-owned rather than ordinary tool inputs.

File input and explicit `output_directory` paths are disabled until `KEGG_MCP_ALLOWED_ROOTS` is
configured. When roots are configured and `output_directory` is omitted, Core allocates a fresh
child beneath the last configured root; root order therefore defines the default output root. Paths
must be absolute and resolve beneath an allowed root. Traversal, missing files, symlink escapes, and
unsafe output ancestors are rejected. Once an output path enters a directory owned by the service
user that is not group- or world-writable, that private directory and every descendant directory
on the path must retain those ownership and mode constraints. Owner-owned shared ancestors may be
group-writable before that private boundary; the configured allowed root itself must establish the
private boundary. An output directory must be new or empty;
any existing entry causes `OUTPUT_ALREADY_EXISTS`, and this release exposes no overwrite operation.
`write_kegg_reference_bundle` and `prepare_kegg_handoff` always require an explicit
`output_directory`; their selected content and external target are deliberate filesystem
mutations, so they never infer a destination from retained-result or input paths.
A successful normalization bundle contains
`normalized_annotations.tsv`, `protein_ko_mapping.tsv`, and `bundle_manifest.json`; analysis adds
`unique_accepted_kos.tsv`, `pathway_coverage.tsv`, `module_completion.tsv`, `analysis_report.md`,
and `render_input.json`.
Automatic MODULE selection also adds `module_ranking.tsv` and `ko_module_relationships.tsv`;
server-ranked pathway selection adds `pathway_ranking.tsv` and `ko_pathway_relationships.tsv`.
The report records the original absolute input path when source provenance supplies it.
`render_input.json` is an immutable renderer-specific version 6 contract: it carries sorted unique
accepted K numbers, complete-within-limit pathway evidence, authoritative MODULE states, and
producer and calculation provenance. An explicitly
requested canonical KO global or overview target is renderable when
`allow_global_or_overview=true`, its denominator was evaluated, and its detected evidence is
complete; the Renderer maps that evidence onto bounded KGML line-coordinate polylines while
preserving the base image. Automatic Top-N selection continues to exclude broad maps, while `map`
and organism references remain summary-only. Bundle schema version 5 installs its manifest last as
a commit marker and represents source paths with redacted labels by default. The explicit
`manifest_path_mode="absolute"` option includes absolute paths in the manifest when required. The
bundle manifest records the renderer schema and MIME type.
`AnalysisExecutionProvenance` version 5 records exactly one applicable intake-limit contract,
applicable MODULE analysis limits, MODULE- and pathway-ranking parameters when applicable, pathway
parameters, pathway coverage limits, and report limits. Every high-level analysis bundle omits
`normalized_annotations.tsv` and `protein_ko_mapping.tsv`; obtain those files from the separate
normalization workflow.

The three analysis tools use separate concise output models. Their shared `summary` distinguishes
source `input_rows` and `skipped_rows` from expanded `assignment_count` and per-status assignment
counts. It also contains the selected unique-KO count, aggregate logical/network/cache request
counts, response bytes, warnings, and interpretation caveats. `analyze_modules` returns MODULE previews
only, `analyze_pathways` returns pathway previews only, and `analyze_ko_annotations` may return both
plus bounded `automatic_module_selection` and `automatic_pathway_selection` summaries and
output-bundle metadata. Direct responses do not expose import structures, annotation or KEGG batch
provenance, execution parameters, stage metrics, complete relationship rows, or detected-KO lists.

The high-level retained `structured` JSON is the authoritative detail artifact. It contains the
compact `KoAnalysisView` and never fabricates record evidence. It retains analyses, execution
parameters, mapping provenance, and six canonical stage-metric rows. The narrower MODULE
and pathway tools retain the same classes of provenance,
parameters, metrics, and full evaluations in their `detail` JSON. Bundle artifact metadata includes
MIME type, exact byte size, and controlled path. A K number is an annotation, MODULE exact
completion is distinct from
the project block-coverage metric, and pathway coverage does not establish pathway presence,
activity, flux, phenotype, or statistical significance.

## Resources and retention

Fixed resources:

- `ko-analysis://status`
- `ko-analysis://cache/info`

Resource templates:

- `ko-analysis://results/{result_id}`
- `ko-analysis://results/{result_id}/{section}`
- `ko-analysis://results/{result_id}/{section}/{offset}/{limit}`
- `kegg-cache://entries/{database}/{identifier}`

Result identifiers are opaque and scoped to one stdio server process. They cannot be read from
another scope, and normal stdio shutdown deletes the current scope. The default 24-hour value is
both the active-result hard TTL and the eligibility threshold for cleaning orphan rows left by an
abnormal exit; it is not a cross-process persistence promise. `get_server_status` reports
`result_scope="stdio_session"`, both TTL meanings, normal-exit cleanup, and
`durable_output="output_bundle"` explicitly. The result index lists validated section URIs.
The `output_bundle` capability label is the umbrella for KO-analysis bundles, selected-reference
bundles, and prepared external-input bundles; it does not imply that every bundle contains a
renderer handoff.
High-level analysis normally retains `structured`, `summary`, and `accepted_kos`; automatic target
selection also retains the applicable MODULE and pathway ranking and relationship artifacts.
Normalization retains `dataset`; primitive query tools retain `detail`; card and references
projections also retain `entry_snapshot`; and local reference comparison retains `reference_diff`.

Reference and external-input bundles are ordinary owner-controlled files rather than resource
templates or retained-result artifacts. `write_kegg_reference_bundle` must read its source result
while that ID is valid in the current stdio scope; after a successful write, the versioned bundle
remains usable across sessions without that ID. `prepare_kegg_handoff` returns stable file paths
and does not create a scoped retained result.

`delete_analysis_result` removes one active result only when it belongs to the current scope.
Unknown, expired, already deleted, and cross-scope identifiers all return `RESULT_NOT_FOUND`.
`list_analysis_results` returns at most 100 metadata rows per page from only the current scope.
Operators can remove TTL-expired rows without starting the stdio server by running
`kegg-mcp cleanup --expired [--json]`. This command does not remove unexpired results or KEGG cache
entries. Durable delivery uses a non-overwriting output bundle controlled by the operator.

An artifact larger than the inline resource limit returns an
`artifact_requires_pagination` notice. Range resources return base64 content, exact byte counts,
and a continuation URI. Clients concatenate decoded pages in order and validate the reconstructed
content with its declared parser or schema; workflow digests are not part of this contract.

The internal resource parser accepts only canonical identifiers, section names, and numeric ranges;
visible traversal segments, encoded separators, queries, fragments, and malformed ranges are
rejected. The MCP SDK canonicalizes some dot-segment URI aliases before the handler can inspect the
original spelling. Such an alias can only resolve to the same validated, scoped canonical resource;
it cannot cross a result or process scope.

`kegg-cache://entries/...` is cache-only and never triggers network access or creates a retained
result. It returns a bounded parsed preview rather than the raw cached payload.

Core runs synchronous service operations in bounded worker threads so HTTPS retries, analysis,
file access, and SQLite transactions do not block the MCP event loop. KEGG-client operations are
serialized because the injected client contract does not promise thread safety; local storage and
resource reads use separate bounded worker capacity so status and resource handling remain
responsive during a slow KEGG request. The MCP cancellation response can be sent immediately, but
the cancelled request task and shutdown wait for its in-flight synchronous operation to finish;
the task then propagates cancellation without a second response. This prevents detached workers
from writing after scope cleanup. Fully interruptible cancellation requires a future asynchronous
transport and service API.

## Independent renderer MCP

`kegg-render-mcp` is a separate distribution and process. It accepts the core's version 6 handoff,
renders bounded static SVG or PNG, and never normalizes evidence or recomputes analysis. Its tools,
resources, access modes, result lifecycle, and security contract are documented in the
[renderer README](../companions/kegg-render-mcp/README.md) and
[visualization architecture](visualization-architecture.md).

## Errors and testing

Repairable tool failures use a schema-conforming error envelope with `code`, `message`,
`recoverable`, `suggested_action`, and bounded safe details. Input validation includes stage,
field path, issue type, and issue count. Result-store and output-directory failures use dedicated
codes rather than `CACHE_FAILED`. Invalid or unauthorized resource URIs use MCP protocol errors.
Endpoint URLs, environment values, credentials, raw tables, and cache payloads are not included in
status or error output.

Default local tests are offline. The governed Core live campaign is defined in
[the live-test guide](../tests/live/README.md). The independent synthetic renderer pipeline and
its exact release command are defined in the
[release-readiness checklist](release-readiness.md#automated-validation).
