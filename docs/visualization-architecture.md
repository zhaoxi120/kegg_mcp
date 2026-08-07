# KEGG Pathway and MODULE Visualization Architecture

This document owns the implemented renderer handoff, typed pathway-asset boundary, rendering
semantics, graphics security, and visualization-specific rights rules. The cross-component
[architecture](architecture.md) owns process boundaries, and
[release readiness](release-readiness.md) owns version and release status.

External interface review dates: KEGG visualization and rights sources 2026-07-16; inert KGML
declaration observation 2026-07-21; Codex plugin source 2026-07-23; canonical KO total-map PNG,
KGML, and line-coordinate behavior 2026-08-06; current broad PATHWAY classification metadata
2026-08-07.

## Purpose

This document defines the current local visualization architecture for KEGG-aware KO analysis. It
covers stable handoffs, rendering semantics, security requirements, and KEGG access constraints.

Annotation, analysis, and rendering run in three independent local stdio MCP processes. The core
normalizes evidence exactly once, the renderer never recomputes analysis, and each Skill orchestrates
one declared MCP without implementing inference, parsing, analysis, or rendering. Graphics remain
visualizations of annotation evidence, not stronger biological claims.

## Supported workflow

```text
Protein FASTA
    -> deepkoala-mcp
    -> deepkoala_annotations.csv plus source provenance
    -> kegg-mcp
    -> render_input.json schema version 4
    -> kegg-render-mcp
    -> static SVG, optional PNG, and render_manifest.json
```

Existing K numbers or annotation tables enter at `kegg-mcp`. An existing compatible
`render_input.json` enters directly at `kegg-render-mcp`. Earlier stages are not repeated when a
valid stable handoff already exists.

The supported scope covers regular canonical `koNNNNN` reference-pathway box overlays, explicitly
opted-in canonical KO global/overview total-map line overlays from one matching KEGG PNG/KGML pair,
and project-owned MODULE logic diagrams from the core AST and evaluation states. Accepted and
policy-defined uncertain evidence remain separate. SVG is canonical, PNG is a bounded derivative,
and artifacts are available through local output directories and scoped MCP resources.

The following remain unsupported:

- reconstruction or inference of arrow direction from total-map line overlays;
- `map` or organism-specific total-map overlays from KO-only evidence;
- organism-specific pathway claims derived from KO-only input;
- interactive HTML, JavaScript, browser automation, screenshots, or a web UI;
- pathway activity, flux, phenotype, enrichment, or metabolic-model inference;
- unbounded bulk rendering; and
- redistribution of KEGG source assets or real KEGG-derived fixtures without a separate rights
  review.

## Process and package boundaries

### `deepkoala-mcp`

The annotation companion is upstream of visualization. It owns FASTA validation, execution, and
the stable successful output:

```text
deepkoala_annotations.csv
deepkoala_run_report.md
```

The Core consumes the detailed CSV rather than a process-scoped companion job identifier. The
companion never normalizes K numbers or queries KEGG. Its
[component README](../companions/deepkoala-mcp/README.md) owns installation, model/device policy,
multi-domain readiness, tools, lifecycle, and detailed file provenance.

### Core `kegg-mcp`

The core is the sole authority for evidence import, normalization, KEGG reference retrieval, MODULE
evaluation, pathway KO coverage, target ranking, and report construction. It never starts an
annotation process, parses KGML, or renders images.

For a visualization request, the high-level service imports evidence through the source-agnostic
boundary, applies the named decision policy, selects explicit targets or up to five MODULEs and up
to five pathways by default, loads typed references, evaluates the targets, and writes a non-overwriting
bundle containing `render_input.json`.

Automatic ranking uses unique selected-KO overlap only to choose targets. It retains broad maps in
the complete ranking but excludes Global, Overview, and higher-level Overview identifiers before
automatic Top-N truncation. Explicit broad-map analysis requires
`allow_global_or_overview=True`. MODULE ranking is not MODULE completion or enrichment. Exact
MODULE completion and project block coverage are calculated separately after reference loading.

The core exposes transport-independent Pydantic models in `kegg_mcp.services.render_contracts`.
`RenderInput` schema version 4 contains producer and dataset identity, analysis unit, taxonomic and
source provenance, decision-policy identity, disjoint accepted and uncertain KO sets, bounded MODULE
and pathway targets, and serializable parameters, limits, ranking provenance, and calculation
versions.

Rejected, unclassified, and invalid records remain available in analysis summaries but never enter
renderer evidence. Every tuple is deterministically ordered, and total serialized size is bounded.
No workflow or artifact digest is required.

A pathway render target carries the canonical identifier, namespace, scope, name, bounded
classification evidence, evidence mode, evaluation status, supplied coverage numerator and
denominator, detected KOs when complete within the render limit, retrieval/cache provenance,
calculation version, warnings, and renderability state. Classification evidence retains `CLASS`
lines when present; current broad entries that omit `CLASS` use an exact source-tagged `ENTRY`
Global/Overview subtype. An explicitly opted-in global/overview target is renderable only when it
is a canonical KO reference with an evaluated denominator and complete detected evidence.
Non-`ko` references and unevaluable or incomplete targets remain summary-only or not-renderable.

A MODULE render target carries the root and reachable definitions, authoritative AST, reference
edges and issues, strict and lenient completion, project block coverage, complete bounded block and
optional-component states, uncertain support, unsupported content, and parser/evaluator provenance.
Oversized content is marked `not_renderable`; a truncated preview is never relabeled as complete
renderer evidence.

The output-bundle manifest records renderer schema version 4 and its MIME type independently from
the output-bundle schema. Graph, reference, result, target-count, and byte-limit validation completes
before publication. The bundle manifest is published last. Only schema version 4 is accepted. A
schema-mismatched handoff is rejected with an action to rerun core analysis; it is not repaired or
reinterpreted.

### Typed KEGG pathway assets

The public core library provides `PathwayAssetRequest`, `PathwayAssetResult`, and the fixed asset
kinds `image`, `image2x`, and `kgml`. This interface accepts one canonical pathway identifier and no
arbitrary URL. Public paired rendering assets use the canonical `koNNNNN` identity. A `mapNNNNN`
value used by KEGG's internal KGML+ representation is not substituted as a public KGML asset
identity and remains a summary-only renderer target.

Asset retrieval reuses the core access gate, HTTPS transport, endpoint-scoped cache,
deployment-wide no-burst rate limiter, retry policy, response-size bounds, and retrieval provenance.
The core performs bounded PNG structural and decompression validation. For KGML it performs only
bounded byte/text and declaration-policy preflight. It accepts the single inert KEGG KGML v0.7.2
HTTPS `SYSTEM` declaration observed on 2026-07-21 only in the XML prolog and never resolves it;
complete XML parsing, pathway identity, and PNG/KGML dimension compatibility belong to the renderer.

The asset interface is a library contract used by the independently packaged renderer. It is not a
core MCP tool, and the renderer does not implement a second network client.

### `kegg-render-mcp`

The renderer is an independently packaged local stdio MCP server requiring a compatible core
library. It validates exactly one version-4 handoff supplied as an allowed absolute path or bounded
inline JSON document.

Status is redacted and closed-world. A connectivity probe performs one explicit `INFO` request in a
live access mode and zero requests in `offline_cache` or `unconfigured` mode. MODULE rendering is
closed-world when its handoff is complete. Pathway rendering is open-world when it retrieves KEGG
assets. Tool annotations reflect these effects.

Successful operations produce bounded static artifacts and a schema-version-2
`render_manifest.json` in a new or empty durable output directory. Its image records contain only a
controlled relative path, MIME type, byte size, width, and height. Opaque render IDs,
expiry timestamps, and resource URIs remain process-scoped result metadata; they are excluded from
the durable manifest and are not cross-stage authorization. The
[Renderer README](../companions/kegg-render-mcp/README.md) owns the exact tools, resource URIs,
status fields, retention limits, configuration, and output lifecycle.

`render_analysis_bundle` is one all-or-nothing operation. It validates every selected target's
capability before pathway retrieval and encodes all requested target artifacts before retaining or
publishing a result. A target, asset, encoding, output-bound, or publication failure returns no
partial `RenderResult`; the typed error identifies the failing target when target-specific work has
started. A caller may use that context to retry a smaller `target_ids` set, but must not assemble
partial results as if the original bundle succeeded.

## Rendering semantics

### Pathway overlays

For a renderable canonical KO pathway, the renderer:

1. obtains the matching PNG and KGML under the configured access policy;
2. validates pathway identity, bytes, dimensions, and compatibility;
3. parses bounded KGML under the typed asset and security policy above;
4. identifies KO-bearing graphics from canonical `ko:KNNNNN` values;
5. overlays only the authoritative detected evidence from the core handoff;
6. adds a versioned legend, warnings, provenance, and conservative caption; and
7. emits static SVG and any requested bounded PNG derivative.

Regular maps use bounded KGML rectangle geometry. Explicit canonical KO global/overview maps use
bounded `graphics type="line"` `coords` polylines, while retaining bounded boxes when KGML declares
them. Every geometry coordinate must be a short ASCII non-negative integer within the matching PNG.
Polylines additionally enforce per-line points, total points, total Euclidean length, and bounded
graphic-to-KO associations. Degenerate or malformed coordinate lists are rejected.

Accepted evidence uses a solid vivid-red (`#FF0000`) overlay and has precedence when one graphic
maps to both accepted and uncertain KOs. Policy-defined uncertain evidence uses orange (`#E69F00`)
and a dashed non-color cue for both boxes and polylines. Graphics that are not selected as
visualization evidence remain unchanged and are never described as biologically absent. Original
pathway-category colors and arrows in the validated PNG remain background context rather than
evidence states.

The displayed coverage numerator, denominator, ratio, namespace, and evidence mode come from the
core target. KGML graphics do not replace or recompute the core denominator. Coverage is
descriptive and does not establish pathway presence, completeness, expression, activity, flux,
phenotype, or statistical significance.

The total-map overlay follows KGML coordinates while preserving any arrows already present in the
source PNG; it does not reconstruct arrows, direction, reaction semantics, or causality. Its broad
descriptive KO coverage does not establish pathway presence, completeness, expression, activity,
flux, phenotype, or statistical significance. A summary-only broad target is never promoted or
replaced with a model-native conceptual drawing.

### MODULE logic diagrams

MODULE graphics are project-owned logic diagrams, not KEGG pathway maps or biochemical topology
claims. They preserve the authoritative core syntax and state:

- top-level spaces and plus signs represent AND;
- commas represent OR;
- a minus sign marks an optional component;
- parentheses preserve grouping;
- MODULE references remain distinct and use the resolved graph;
- unsupported, unresolved, and cyclic content remains visible; and
- optional components stay outside the required completion denominator.

The graphic displays strict and lenient exact completion separately from project block coverage.
Uncertain support that changes lenient results remains visible. Partially evaluable, not evaluable,
summary-only, and not-renderable states include their reasons. Minimal missing alternatives are
bounded requirements under one evaluated definition, not proof that adding genes activates a
biological process.

## Security and operational contract

All three servers use local stdio transport and reserve stdout for protocol traffic. They never use
`shell=True`.

Renderer input and output enforce:

- absolute allowed-root paths with lexical traversal, unsafe ancestry, and symlink-escape rejection;
- strict UTF-8 and schema-version validation;
- bounds on source bytes, targets, identifiers, XML structure, coordinate characters and tokens,
  polyline points and total length, graphic-to-KO associations, dimensions, pixels, SVG nodes,
  cumulative artifact and manifest bytes, retained results, disk use, and cleanup;
- canonical identifiers and fixed suffixes for artifact names;
- restrictive permissions and non-overwriting atomic publication; and
- rollback limited to files created by the failed operation.

KGML parsing accepts only the exact inert KEGG KGML v0.7.2 HTTPS `SYSTEM` declaration and disables
parameter-entity, external-entity, DTD, and network resolution. Other DTD declarations and all entity
declarations are rejected. PNG input is checked for valid structure, dimensions, decompression
bounds, and compatible identity. Generated SVG has no scripts, event handlers, active links, remote
fonts, or external resources; the validated source PNG is embedded as static data.

Status, errors, manifests, and provenance redact credentials, environment values, usernames,
endpoint URLs, cache fingerprints, private state paths, and full cache paths. Errors return bounded
safe details and actionable typed classifications.

## KEGG access, cache, and rights

The public KEGG REST service is for academic use by academic users. Non-academic deployments must
use an appropriately licensed endpoint. The source-code license grants no rights to KEGG content or
rendered derivatives.

Renderer pathway access supports:

- `public_academic` for eligible academic use;
- `licensed` with an explicitly authorized HTTPS endpoint;
- `offline_cache` for an existing safe read-only cache; and
- `unconfigured` for MODULE-only rendering.

Core and Renderer share the same endpoint-scoped, owner-only rate-limit state and never exceed three
requests per second. The default is safer and has no burst. One pathway PNG and one KGML document
are retrieved as separate bounded requests.

Offline mode opens only an existing compatible cache, makes no network request, performs no cache
mutation, and preserves public-versus-licensed namespace isolation. Stale assets are rejected by
default. Deployment-authorized stale use remains explicit in warnings and provenance.

Raw KEGG PNG, KGML, cache payloads, and real KEGG-derived fixtures remain local and are excluded from
version control, examples, CI artifacts, wheels, source distributions, and releases. Redistribution
of rendered derivatives requires a separate rights review.

## Codex Skill boundaries

The three focused Skills have one dependency each: `deepkoala-annotation` uses `deepkoala-mcp`,
`kegg-ko-analysis` uses core `kegg-mcp`, and `kegg-pathway-rendering` uses `kegg-render-mcp`.

One original user request may continue across those Skills after each successful stable-file
handoff. The model carries forward the original formats and target scope without asking the user to
copy a path or repeat an already authorized stage. A request for only one stage stops after that
stage.

The annotation Skill never normalizes the CSV. The analysis Skill never calls the annotation or
renderer MCP and never parses KGML. The rendering Skill never calls an upstream MCP, repairs the
handoff, recomputes analysis, assigns colors itself, or creates a fallback image outside the
renderer. Missing, failed, or incompatible stages stop with their specific diagnostic.

## Unified Codex deployment

`scripts/install-suite.py` creates three separate runtimes and one generated local plugin without
merging component processes or state. The [installation guide](installation.md) owns operator
configuration and lifecycle; generic clients use
[manual component deployment](manual-component-deployment.md).

## Validation ownership

The visualization implementation is covered by:

- core unit, integration, MCP contract, output-bundle, and release tests for `RenderInput` version 4
  and typed pathway assets;
- renderer schema, pathway, MODULE, KGML, PNG, SVG, filesystem, cache, retention, resource, stdio,
  and distribution tests using only synthetic assets;
- static Skill contract and route-evaluation tests; and
- the synthetic three-process composition test.

The composition test is
`companions/kegg-render-mcp/tests/test_synthetic_pipeline.py`. It carries a FASTA-derived companion
handoff through core high-level analysis and into renderer output across three independent MCP
sessions without rebuilding an intermediate contract or accessing the network.

The [release-readiness checklist](release-readiness.md) owns the exact commands, live-versus-
synthetic distinction, new-task discovery smoke, evidence record, and publication decision.

## Primary external sources

The visualization contract was reviewed on 2026-07-16, with total-map behavior reviewed again on
2026-08-06, against:

- [KEGG API Manual](https://www.kegg.jp/kegg/rest/keggapi.html), including pathway `image`,
  `image2x`, and `kgml` retrieval;
- [KEGG Copyright and Disclaimer](https://www.kegg.jp/kegg/legal.html), including public academic
  use and licensing boundaries;
- [KEGG Mapper](https://www.kegg.jp/kegg/mapper/), including pathway mapping behavior;
- [KEGG Mapper Color](https://www.kegg.jp/kegg/mapper/color.html), including line and circle color
  handling;
- [KEGG KGML manual](https://www.kegg.jp/kegg/xml/docs/), including `graphics` line coordinates;
- [KEGG XML/KGML resources](https://www.kegg.jp/kegg/xml/), including the internal KGML+
  `map` representation distinguished from the canonical public rendering-asset identity;
- [KEGG Pathway Map Viewer Help](https://www.kegg.jp/kegg/document/help_pathway.html), including
  regular pathway presentation;
- [KEGG Global Map Viewer Help](https://www.kegg.jp/kegg/document/help_global.html), including
  global/overview presentation differences; and
- [Model Context Protocol resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources),
  including binary content and MIME metadata.

The generated local plugin contract was reviewed again on 2026-07-23 against the official
[Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins).

Any external fact that changes a parser, schema, fixture, or acceptance test requires a new
retrieval date in the corresponding tracked change.
