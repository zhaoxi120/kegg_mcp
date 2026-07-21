# KEGG MCP

KEGG MCP is a local, Linux-only suite for KEGG Orthology (KO) analysis. It provides three
independent stdio MCP servers and three focused repository-scoped Codex Skills:

```text
protein FASTA -> deepkoala-mcp -> detailed CSV -> kegg-mcp
             -> render_input.json version 3 -> kegg-render-mcp -> SVG/PNG
```

The release-supported runtime is Linux with Python 3.11.x.

## Capabilities

The core `kegg-mcp` server accepts K-number lists, generic CSV/TSV tables, and DeepKOALA detailed
CSV evidence. It:

- preserves source decisions, scores, thresholds, multiple assignments, and provenance;
- derives accepted-only strict and policy-defined lenient evidence views;
- retrieves bounded KEGG `INFO`, `GET`, `LINK`, and `CONV` references through a local cache;
- evaluates exact MODULE completion separately from project block coverage;
- reports descriptive pathway KO coverage with an explicit namespace and denominator;
- compares KO sets deterministically; and
- produces bounded structured output, reports, and a typed renderer handoff.

A K-number assignment is annotation evidence, not experimental validation. Pathway KO coverage does
not establish pathway presence, completeness, activity, flux, or phenotype.

## Quick start for Codex

Use the repository suite installer from a reviewed release checkout. It creates three locked Python
runtimes, copies the three canonical Skills into one generated local plugin, and registers three
absolute stdio launch commands without merging their processes or state.

Prepare the strict owner-only TOML described in
[Installation and operation](docs/installation.md), then run:

```bash
/absolute/path/to/python3.11 \
  /absolute/path/to/kegg_mcp/scripts/install-suite.py \
  --config /absolute/private/kegg-mcp-deployment.toml \
  --install-root /absolute/private/kegg-mcp-install \
  --python /absolute/path/to/python3.11 \
  --uv /absolute/path/to/uv \
  --git /absolute/path/to/git \
  --codex /absolute/path/to/codex \
  --allow-deepkoala-install
```

`uv` dependency resolution is offline by default. The separate
`--allow-locked-dependency-downloads` option permits only artifacts required by the checked-in
lockfiles and declared build requirements.

For a new suite root, `--allow-deepkoala-install` confirms one clone of the official DeepKOALA
repository and installation of its upstream requirements. Later FASTA jobs do not ask again. The
bundled `202502` model and `device=cpu` are the defaults. Before the first annotation call in a
Codex task, the installed Skill tells the user that CPU will be used. A user who needs GPU execution
must explicitly ask the LLM; it selects `device=cuda` only after status reports that CUDA is allowed
and available. Multi-domain mode remains off for every request unless the operator has separately
configured local HMMER/KOfam resources and the user explicitly requests it. This repository does
not download those resources or update DeepKOALA models.

Select KEGG access explicitly. Use confirmed `public_academic` only when both the user and the work
qualify for public academic KEGG access. Non-academic deployments require an appropriately licensed
endpoint.

After installation, start a new Codex task outside this source checkout and confirm discovery of all
three Skills and MCP servers. The generated-plugin path is release-supported only after the exact
release candidate passes [release readiness](docs/release-readiness.md).

## End-to-end requests

The installed Skills are designed to continue one original request across stable output files.
For example:

> Analyze `/absolute/project/proteins.faa` with the default DeepKOALA model, report up to five
> MODULEs and up to five canonical KO pathways, and render SVG diagrams. Use separate annotation,
> analysis, and rendering output directories beneath `/absolute/project/results`.

The stages produce `deepkoala_annotations.csv`, `render_input.json`, and static renderer output.
The final response reports the resolved DeepKOALA model version. Target ranking selects what to
analyze; it is not enrichment, completion, or pathway-presence inference.

Requests that already contain KO evidence start at `kegg-mcp`. Requests that provide a compatible
`render_input.json` may start at `kegg-render-mcp`.

## Components and contracts

| Process | Responsibility | Details |
| --- | --- | --- |
| `deepkoala-mcp` | Runs one configured local DeepKOALA job and returns a controlled detailed-CSV handoff. | [Companion README](companions/deepkoala-mcp/README.md) |
| `kegg-mcp` | Owns evidence normalization, KEGG retrieval, MODULE/pathway analysis, reports, and renderer handoff. | [MCP contract](docs/mcp-server.md) |
| `kegg-render-mcp` | Renders the authoritative handoff without recomputing biological analysis. | [Renderer README](companions/kegg-render-mcp/README.md) |

The core produces `render_input.json` schema version 3 and preserves
`AnalysisExecutionProvenance` version 3 in output-bundle schema version 3. Source KEGG PNG and KGML
assets remain local.

Core tools cover normalization, high-level analysis, bounded KEGG retrieval and mapping, MODULE and
pathway analysis, KO-set comparison, connectivity/status, and scoped result listing/deletion. Full
tool and resource schemas are documented in [MCP server](docs/mcp-server.md).

Each Skill declares exactly one MCP dependency. Stable versioned files, not private process-scoped
job or result identifiers, are the durable cross-stage interface.

## KEGG and local-data boundary

The public KEGG REST service is for eligible academic users performing academic work. Core and
Renderer coordinate through one owner-only rate-limit root, use a safer no-burst default, and never
exceed three requests per second deployment-wide.

Cached responses, pathway assets, result databases, biological inputs, and generated derivatives
must remain local and out of version control, packages, examples, CI artifacts, and releases. The
MIT source license grants no rights to KEGG content, DeepKOALA weights, KOfam profiles, or other
third-party material. Redistribution of KEGG-derived images requires a separate rights review.

File input and output are restricted to each server's configured roots:
`KEGG_MCP_ALLOWED_ROOTS`, `KEGG_RENDER_MCP_ALLOWED_ROOTS`, and the DeepKOALA input/output roots.
The servers reject traversal and symlink escapes, bound sizes, keep protocol stdout clean, and
redact private configuration from status output.

Review the [KEGG API documentation](https://www.kegg.jp/kegg/rest/) and
[KEGG legal notice](https://www.kegg.jp/kegg/legal.html) before live use.

## Installation and distribution

The suite installer is the supported Codex installation path. A Python wheel contains one MCP
distribution only; it does not install either companion or any repository-scoped Skill.
Installing a wheel alone does not make repository-scoped Skills available. Other MCP clients may
install the three distributions independently and register their stdio commands manually.

- [Installation and operation](docs/installation.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release readiness](docs/release-readiness.md)

## Developer reference

- [Current architecture and development contract](docs/development-plan.md)
- [Visualization architecture](docs/visualization-extension-plan.md)
- [Import contracts](docs/import-contracts.md)
- [KEGG client and cache](docs/kegg-client.md)
- [MODULE evaluation](docs/module-analysis.md)
- [Pathway coverage and KO-set comparison](docs/pathway-comparison-analysis.md)
- [Services, results, and reporting](docs/services-results-reporting.md)

The normal offline validation profile is:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Pull-request CI additionally runs the governed serialized live KEGG compatibility campaign. See
[the live-test guide](tests/live/README.md).

## License

Project source is available under the [MIT License](LICENSE).
