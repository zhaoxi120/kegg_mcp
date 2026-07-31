# `kegg-mcp` Core Distribution

This document is the package description for the independently installable `kegg-mcp` Core
distribution. The repository-level [README](../README.md) describes the complete three-server
suite and its Codex installer.

`kegg-mcp` is a local, Linux-only stdio MCP server for bounded KEGG entity queries and traceable
analysis of supplied KEGG Orthology (KO) evidence. The release-supported runtime is CPython
3.11.x.

## Core capabilities

The server accepts K-number lists, generic CSV/TSV annotation tables, and previously generated
DeepKOALA detailed output. It:

- preserves source decisions, scores, thresholds, multiple assignments, and provenance;
- derives accepted-only strict and policy-defined lenient evidence views;
- retrieves bounded KEGG `INFO`, organism-pathway `LIST`, `FIND`, `GET`, `LINK`, and `CONV`
  references through a local cache;
- searches endpoint candidates, projects supported GET entries into deterministic typed cards,
  resolves gene, organism, or substance identifiers, and traces allowlisted typed relations
  without hiding ambiguity or inferring causality;
- extracts KEGG-supplied PubMed identifiers from selected flat-file references without retrieving
  or summarizing the cited papers;
- compares two same-session, same-request card snapshots locally without building a KEGG archive;
- maps BRITE hierarchy paths and audits fixed KO relationship classes descriptively;
- writes a selected card snapshot and optional BRITE mapping as a bounded local reference bundle;
- prepares supported KEGG Mapper or KEGG Syntax input files without uploading data, invoking a
  browser, or executing an external tool;
- evaluates exact MODULE completion separately from project block coverage;
- reports descriptive pathway KO coverage with an explicit reference type and denominator;
- compares KO sets deterministically; and
- produces bounded structured results, reports, output bundles, and a typed renderer handoff.

A K-number assignment is annotation evidence, not experimental validation. Search hits are
candidates and database relationships are cross-references. Pathway KO coverage does not establish
pathway presence, completeness, expression, activity, flux, or phenotype. BRITE classifications
and target rankings are descriptive, not enrichment results.

## Distribution boundary

The Python wheel contains only the Core server and library. It does not install
`deepkoala-mcp`, `kegg-render-mcp`, or any repository-scoped Skill. Installing this wheel alone
therefore does not provide protein annotation, graphics, or the complete Codex workflow.

The Core never runs an annotator, parses KGML, or renders images. Optional annotation and rendering
remain independent stdio processes connected by stable versioned files:

```text
deepkoala-mcp -> detailed annotation CSV -> kegg-mcp
kegg-mcp      -> render_input.json version 3 -> kegg-render-mcp
```

Use the repository [suite installation guide](installation.md) for the supported Codex path.
Other MCP clients may install and register the components independently by following
[manual component deployment](manual-component-deployment.md).

## Start and configure Core

After installation, configure an MCP client to launch the direct executable:

```text
/absolute/path/to/kegg-mcp
```

Use `kegg-mcp doctor` or `kegg-mcp doctor --json` for a side-effect-free, redacted configuration
check. Protocol messages are the only stdout content; diagnostics use stderr.

The raw Core distribution supports `public_academic`, `licensed`, and network-disabled
`offline_cache` access modes. Its `public_academic` default is appropriate only when both the user
and work qualify for public academic KEGG access. Non-academic deployments require an appropriately
licensed endpoint. See the [Core MCP contract](mcp-server.md) for the complete environment,
tool, resource, output-bundle, and retention contract.

Inputs and output directories remain disabled until `KEGG_MCP_ALLOWED_ROOTS` identifies controlled
absolute roots. KEGG cache payloads, result databases, annotation inputs, and output bundles must
remain local and out of version control, packages, examples, CI artifacts, and releases.

## Rights and interpretation

The MIT license covers project source only. It grants no rights to KEGG content, DeepKOALA
resources, KOfam profiles, biological inputs, or generated derivatives. Review the
[KEGG API documentation](https://www.kegg.jp/kegg/rest/) and
[KEGG legal notice](https://www.kegg.jp/kegg/legal.html) before enabling live access.
