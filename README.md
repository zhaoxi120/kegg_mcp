# KEGG MCP

KEGG MCP is a local stdio MCP server and repository-scoped Codex Skill for analyzing KEGG
Orthology (KO) annotations. It normalizes source evidence, retrieves bounded KEGG references,
evaluates MODULE logic, summarizes pathway KO coverage, compares KO sets deterministically, and
produces traceable local reports.

> Release status: the only published GitHub release is core `v0.1.0`. This checkout contains
> unreleased candidates and no distribution here is published to a Python package registry.

| Distribution | Source version | Release state | Compatibility |
| --- | --- | --- | --- |
| `kegg-mcp` | `0.3.0` | Unreleased candidate | Produces `RenderInput`; Python 3.11.x |
| `deepkoala-mcp` | `0.2.0` | Unreleased candidate | Controlled detailed-CSV handoff; Python 3.11.x |
| `kegg-render-mcp` | `0.1.0` | Unreleased candidate | Requires `kegg-mcp>=0.3,<0.4`; Python 3.11.x |

The release-supported platform is Linux with CPython 3.11.x.

## What it does

The core server accepts plain K-number lists and generic CSV/TSV annotation tables. It can:

- preserve source decisions, scores, thresholds, multiple assignments, and provenance;
- derive strict accepted-only and lenient accepted-plus-uncertain KO views;
- retrieve typed `INFO`, `GET`, `LINK`, and `CONV` references through a local cache and a
  process-wide no-burst rate limit;
- report exact MODULE completion separately from project-defined block coverage;
- calculate descriptive pathway KO coverage with an explicit reference denominator;
- compare KO sets and shared-reference outcomes without statistical change claims; and
- return bounded structured results, Markdown, CSV, and a renderer handoff.

A K-number assignment is annotation evidence, not experimental validation. Pathway coverage does
not establish pathway presence, activity, flux, phenotype, or completeness in one organism.

## Quick start

Install the locked development environment from an exact source checkout:

```bash
uv sync --frozen
mkdir -p /tmp/kegg-mcp-demo
KEGG_MCP_ALLOWED_ROOTS=/tmp/kegg-mcp-demo \
  .venv/bin/kegg-mcp doctor
```

Register the absolute stdio command with Codex, then restart Codex:

```bash
codex mcp add kegg-mcp \
  --env KEGG_MCP_ALLOWED_ROOTS=/tmp/kegg-mcp-demo \
  -- /absolute/path/to/kegg_mcp/.venv/bin/kegg-mcp
codex mcp list
```

The default access profile is confirmed `public_academic`. Before making live requests, confirm
that both the user and the work qualify for public academic KEGG access. A useful first request is:

> Check server status, probe connectivity once, then retrieve only KO entry `K00844`. Report its
> identifier, name, database release when available, and whether it came from network or cache.

See [Installation and operation](docs/installation.md) for licensed endpoints, environment
variables, client configuration, and result retrieval. Local tests do not make live KEGG requests
unless explicitly enabled.

## Components

The optional end-to-end FASTA-to-image workflow uses three independent local processes:

| Process | Responsibility |
| --- | --- |
| `deepkoala-mcp` | Runs an explicitly configured external DeepKOALA installation and returns a controlled detailed-CSV handoff. |
| `kegg-mcp` | Imports evidence, applies the named normalization policy, retrieves references, and performs authoritative analysis. |
| `kegg-render-mcp` | Renders the core handoff as bounded static SVG or PNG without recomputing analysis. |

The companions are separately installed distributions. The core never runs annotation software or
generates pathway images. The renderer accepts `render_input.json` schema version 2, represented in
Python as `RenderInput`, and preserves `AnalysisExecutionProvenance` version 2. Source KEGG PNG and
KGML assets remain local and are not included in tests, packages, or releases.

Companion details:

- [DeepKOALA companion](companions/deepkoala-mcp/README.md)
- [Renderer companion](companions/kegg-render-mcp/README.md)
- [Visualization architecture](docs/visualization-extension-plan.md)

## MCP surface

The `kegg-mcp` entry point starts stdio by default. It also supports `serve`, redacted
`doctor [--json]`, and operator-only `cleanup --expired [--json]` commands.

Core tools:

- `analyze_ko_annotations`, `normalize_ko_annotations`
- `get_kegg_entries`, `map_ko_ids`
- `analyze_modules`, `analyze_pathways`, `compare_ko_sets`
- `probe_kegg_connectivity`, `get_server_status`, `delete_analysis_result`

Fixed resources expose redacted server and cache status. Validated resource templates expose
scope-isolated result metadata, sections, byte ranges, and cache-only KEGG entry reads. Result IDs
belong to one stdio process; an output bundle is the durable cross-process artifact.

The repository-scoped Skills are:

- `.agents/skills/kegg-ko-analysis/` for KO evidence, MODULE/pathway analysis, and comparisons;
- `.agents/skills/kegg-visualization/` for renderer orchestration from authoritative core output.

## KEGG access and local-data boundary

The public KEGG REST service is for academic use by academic users. This project enforces at most
three requests per second and uses a safer no-burst default. Non-academic deployments must use an
appropriately licensed endpoint. The MIT license for this source code grants no rights to KEGG
content.

Cached responses, pathway assets, result databases, and generated derivatives must remain local
and out of version control, package archives, examples, and CI artifacts. Redistribution of a
KEGG-derived image requires a separate rights review. Review the
[KEGG API documentation](https://www.kegg.jp/kegg/rest/) and
[KEGG legal notice](https://www.kegg.jp/kegg/legal.html) before live use.

File inputs and output bundles are restricted to absolute paths beneath
`KEGG_MCP_ALLOWED_ROOTS`. The server rejects traversal and symlink escapes, bounds input and output
sizes, writes protocol logs away from stdout, and redacts local paths and environment values from
status responses.

## Installation and distribution boundary

The core Python wheel contains the MCP server and required notices. It does not contain either
optional companion or either repository-scoped Skill. Use a tag source archive or an exact GitHub
checkout when the Skills are required; installing the wheel alone does not make either Skill
available.

For deployment instructions, see:

- [Installation and operation](docs/installation.md)
- [MCP tools, resources, and configuration](docs/mcp-server.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release readiness](docs/release-readiness.md)

## Technical contracts

- [Import formats and decision policies](docs/import-contracts.md)
- [KEGG client, cache, and provenance](docs/kegg-client.md)
- [MODULE grammar and evaluation](docs/module-analysis.md)
- [Pathway coverage and deterministic comparison](docs/pathway-comparison-analysis.md)
- [Services, result storage, and reporting](docs/services-results-reporting.md)
- [Development plan](docs/development-plan.md)

## Development

The normal validation suite is offline by default:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

Pull-request CI separately runs one serialized, explicitly enabled live campaign with 30 requests
for each supported KEGG operation, one request per second, zero retries, and no uploaded KEGG
payloads. See [the live-test guide](tests/live/README.md).

All tracked repository files are written in English. Maintainer collaboration may use Simplified
Chinese. Repository guidance is in [AGENTS.md](AGENTS.md).

## License

The project source code is licensed under the [MIT License](LICENSE). This license does not grant
rights to KEGG data, KOfam profiles, DeepKOALA model artifacts, or other third-party content.
