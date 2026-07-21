# KEGG Pathway and MODULE Visualization Architecture

Status: implemented post-MVP architecture; exact-release deployment validation remains gated.

External interface review dates: KEGG visualization and rights sources 2026-07-16; Codex plugin
source 2026-07-19.

## Purpose

This document defines the current local visualization architecture for KEGG-aware KO analysis. It
covers the supported FASTA-to-image workflow, component and Skill boundaries, stable handoffs,
rendering semantics, security requirements, KEGG access constraints, and release validation.

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
    -> render_input.json schema version 3
    -> kegg-render-mcp
    -> static SVG, optional PNG, and render_manifest.json
```

Existing K numbers or annotation tables enter at `kegg-mcp`. An existing compatible
`render_input.json` enters directly at `kegg-render-mcp`. Earlier stages are not repeated when a
valid stable handoff already exists.

The supported scope covers regular canonical `koNNNNN` reference-pathway overlays from one matching
KEGG PNG/KGML pair and project-owned MODULE logic diagrams from the core AST and evaluation states.
Accepted and policy-defined uncertain evidence remain separate. SVG is canonical, PNG is a bounded
derivative, and artifacts are available through local output directories and scoped MCP resources.

The following remain unsupported:

- global and overview pathway overlays that require line- and arrow-specific mapping policy;
- organism-specific pathway claims derived from KO-only input;
- interactive HTML, JavaScript, browser automation, screenshots, or a web UI;
- pathway activity, flux, phenotype, enrichment, or metabolic-model inference;
- unbounded bulk rendering; and
- redistribution of KEGG source assets or real KEGG-derived fixtures without a separate rights
  review.

## Process and package boundaries

### `deepkoala-mcp`

The annotation companion accepts one allowlisted absolute protein FASTA path and one new allowed
output directory, validates and privately stages the FASTA, and owns bounded process lifecycle and
cleanup. It defaults to the installed `202502` resources, `device=cpu`, detailed output, zero
data-loader workers, and single-domain execution. It accepts `device=cuda` subject to deployment
and runtime checks and never uses automatic device selection. A request may opt into multi-domain
execution only when the deployment has separately configured and validated local HMMER/KOfam
resources. The companion never downloads those resources, normalizes K numbers, or queries KEGG.

The stable successful output is:

```text
deepkoala_annotations.csv
deepkoala_run_report.md
```

The versioned handoff keeps the original `input_path` separate from the generated
`annotations_path` and `report_path`. Its `source` records the annotation tool and resolved model
version; private staged FASTA and raw runner paths are never returned.

The process-scoped job identifier and `deepkoala://` resources are lifecycle and bounded-transfer
aids. Stable files, not job identity, are the default cross-process handoff.

The suite installer may clone the official DeepKOALA repository and install its declared Python
requirements only after the one-time confirmation for each new installation root. Later FASTA jobs
in that installed deployment do not repeat the installation question. The repository provides no
automatic path for installing later model weights.

### Core `kegg-mcp`

The core is the sole authority for evidence import, normalization, KEGG reference retrieval, MODULE
evaluation, pathway KO coverage, target ranking, and report construction. It never starts an
annotation process, parses KGML, or renders images.

For a visualization request, the high-level service imports evidence through the source-agnostic
boundary, applies the named decision policy, selects explicit targets or up to five MODULEs and up
to five pathways by default, loads typed references, evaluates the targets, and writes a non-overwriting
bundle containing `render_input.json`.

Automatic ranking uses unique selected-KO overlap only to choose targets. MODULE ranking is not
MODULE completion or enrichment. Exact MODULE completion and project block coverage are calculated
separately after reference loading.

The core exposes transport-independent Pydantic models in `kegg_mcp.services.render_contracts`.
`RenderInput` schema version 3 contains producer and dataset identity, analysis unit, taxonomic and
source provenance, decision-policy identity, disjoint accepted and uncertain KO sets, bounded MODULE
and pathway targets, and serializable parameters, limits, ranking provenance, and calculation
versions.

Rejected, unclassified, and invalid records remain available in analysis summaries but never enter
renderer evidence. Every tuple is deterministically ordered, and total serialized size is bounded.
No workflow or artifact digest is required.

A pathway render target carries the canonical identifier, namespace, scope, name, class, evidence
mode, evaluation status, supplied coverage numerator and denominator, detected KOs when complete
within the render limit, retrieval/cache provenance, calculation version, warnings, and
renderability state. Non-`ko` references and unsupported broad maps are explicit summary-only or
not-renderable targets.

A MODULE render target carries the root and reachable definitions, authoritative AST, reference
edges and issues, strict and lenient completion, project block coverage, complete bounded block and
optional-component states, uncertain support, unsupported content, and parser/evaluator provenance.
Oversized content is marked `not_renderable`; a truncated preview is never relabeled as complete
renderer evidence.

The output-bundle manifest records renderer schema version 3 and its MIME type independently from
the output-bundle schema. Graph, reference, result, target-count, and byte-limit validation completes
before publication. The bundle manifest is published last. Incompatible older renderer handoffs are
rejected with an action to rerun core analysis; they are not upgraded from previews.

### Typed KEGG pathway assets

The public core library provides `PathwayAssetRequest`, `PathwayAssetResult`, and the fixed asset
kinds `image`, `image2x`, and `kgml`. This interface accepts one canonical pathway identifier and no
arbitrary URL.

Asset retrieval reuses the core access gate, HTTPS transport, endpoint-scoped cache,
deployment-wide no-burst rate limiter, retry policy, response-size bounds, and retrieval provenance.
The core performs bounded PNG structural and decompression validation. For KGML it performs only
bounded byte/text and active-declaration preflight; complete XML parsing, pathway identity, and
PNG/KGML dimension compatibility belong to the renderer.

The asset interface is a library contract used by the independently packaged renderer. It is not a
core MCP tool, and the renderer does not implement a second network client.

### `kegg-render-mcp`

The renderer is an independently packaged local stdio MCP server requiring a compatible core
library. It validates exactly one version-3 handoff supplied as an allowed absolute path or bounded
inline JSON document.

Its public tools are:

- `get_renderer_status`;
- `probe_renderer_kegg_connectivity`;
- `render_analysis_bundle`;
- `render_pathway`;
- `render_module`; and
- `delete_render_result`.

Status is redacted and closed-world. A connectivity probe performs one explicit `INFO` request in a
live access mode and zero requests in `offline_cache` or `unconfigured` mode. MODULE rendering is
closed-world when its handoff is complete. Pathway rendering is open-world when it retrieves KEGG
assets. Tool annotations reflect these effects.

Every successful render returns an opaque process-scoped `render_id`, bounded artifact metadata,
warnings, and renderer-created resource URIs:

```text
kegg-render://results/{render_id}
kegg-render://results/{render_id}/{artifact}
```

The retained `render_manifest.json` records artifact identity, MIME type, byte size, dimensions,
renderer versions, core calculation provenance, target warnings, and safe source-asset provenance.
SVG resources use `image/svg+xml`; PNG resources use binary `image/png`.

Retained results have bounded count, lifetime, payload bytes, allocated storage, and cleanup work.
They belong to one renderer process. Unknown, expired, deleted, and cross-scope identifiers return
the same safe not-found result. An allowed output directory is the durable handoff and must be new
or empty; publication never overwrites an existing entry and installs the manifest last.

## Rendering semantics

### Regular pathway overlays

For a regular reference pathway, the renderer:

1. obtains the matching PNG and KGML under the configured access policy;
2. validates pathway identity, bytes, dimensions, and compatibility;
3. parses bounded KGML without DTD, entity, or network resolution;
4. identifies KO-bearing graphics from canonical `ko:KNNNNN` values;
5. overlays only the authoritative detected evidence from the core handoff;
6. adds a versioned legend, warnings, provenance, and conservative caption; and
7. emits static SVG and any requested bounded PNG derivative.

Accepted evidence has precedence when one graphic maps to both accepted and uncertain KOs.
Policy-defined uncertain evidence uses a distinct color and dashed non-color cue. Unmatched graphics
remain unchanged and are described as not detected in the supplied annotations, never biologically
absent.

The displayed coverage numerator, denominator, ratio, namespace, and evidence mode come from the
core target. KGML graphics do not replace or recompute the core denominator. Coverage is
descriptive and does not establish pathway presence, completeness, expression, activity, flux,
phenotype, or statistical significance.

Global and overview pathways remain explicit unsupported or summary-only targets. The renderer does
not approximate their line-oriented semantics with regular box overlays.

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
- bounds on source bytes, targets, identifiers, XML structure, coordinates, dimensions, pixels,
  SVG nodes, artifact bytes, retained results, disk use, and cleanup;
- canonical identifiers and fixed suffixes for artifact names;
- restrictive permissions and non-overwriting atomic publication; and
- rollback limited to files created by the failed operation.

KGML parsing disables DTDs, entities, external resolution, and network access. PNG input is checked
for valid structure, dimensions, decompression bounds, and compatible identity. Generated SVG has
no scripts, event handlers, active links, remote fonts, or external resources; the validated source
PNG is embedded as static data.

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

`scripts/install-suite.py` is the supported Codex app and Codex CLI installation path. It consumes
the three checked-in lockfiles, creates three separate runtimes, and generates one local plugin with
version-matched copies of the three canonical Skills and three absolute MCP launch registrations.
No server starts another, and private deployment configuration does not enter the plugin cache.

`uv` dependency resolution is offline by default. The explicit locked-dependency download switch
applies only to artifacts required by the checked-in lockfiles and declared build requirements. It
does not authorize Python, uv, Codex, repository source, model updates, KOfam profiles, KEGG data, or
KEGG assets.

The generated-plugin path is Codex-specific. Other MCP clients use explicit component installation
and stdio registration.

## Testing and release status

The implemented source is covered by:

- core unit, integration, MCP contract, output-bundle, and release tests for `RenderInput` version 3
  and typed pathway assets;
- DeepKOALA companion contract, lifecycle, stdio, stable-file, and real MCP JSON-boundary tests using
  synthetic inputs and output;
- renderer schema, pathway, MODULE, KGML, PNG, SVG, filesystem, cache, retention, resource, stdio,
  and distribution tests using only synthetic assets;
- static Skill contract and route-evaluation tests; and
- deterministic suite-installer tests for strict configuration, three runtime commands, generated
  plugin content, registration validation, interrupted publication, and bounded rollback.

Pull-request CI runs the governed core compatibility campaign once: 30 requests each for `INFO`,
`GET`, `LINK`, and `CONV`, serialized at one request per second with zero retries. Renderer CI uses
only synthetic assets and performs no live KEGG request. CI does not upload KEGG payloads.

The automated synthetic composition gate is
`companions/kegg-render-mcp/tests/test_synthetic_pipeline.py`. It carries a FASTA-derived companion
handoff through core high-level analysis and into renderer output across three independent MCP
sessions without rebuilding an intermediate contract or accessing the network.

One exact-release gate cannot be established by repository tests: an installation produced from the
exact release candidate must be opened in a new Codex task and prove discovery and invocation of all
three Skills and all three MCP servers. A generated plugin tree, mocked Codex output, or successful
installer return is not that gate. Record exact commit, package versions, Python, uv, Git, Codex
version, CI result, access/rights review, and security review in release notes before calling that
revision release-supported.

Global/overview overlays, renderer live-KEGG compatibility tests, automatic installation of later
DeepKOALA models, and redistribution approval are outside the implemented release scope. They are
not substitutes for the automated composition gate or the real-Codex release gate above.

## Primary external sources

The visualization contract was reviewed on 2026-07-16 against:

- [KEGG API Manual](https://www.kegg.jp/kegg/rest/keggapi.html), including pathway `image`,
  `image2x`, and `kgml` retrieval;
- [KEGG Copyright and Disclaimer](https://www.kegg.jp/kegg/legal.html), including public academic
  use and licensing boundaries;
- [KEGG Mapper](https://www.kegg.jp/kegg/mapper/), including pathway mapping behavior;
- [KEGG Pathway Map Viewer Help](https://www.kegg.jp/kegg/document/help_pathway.html), including
  regular and global/overview presentation differences; and
- [Model Context Protocol resources](https://modelcontextprotocol.io/specification/2025-06-18/server/resources),
  including binary content and MIME metadata.

The generated local plugin contract was reviewed on 2026-07-19 against the official
[Codex plugin documentation](https://learn.chatgpt.com/docs/build-plugins).

Any external fact that changes a parser, schema, fixture, or acceptance test requires a new
retrieval date in the corresponding tracked change.
