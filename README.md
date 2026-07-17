# KEGG MCP

KEGG MCP provides a local stdio analysis server, two optional companion servers, and three focused
repository-scoped Codex Skills for KEGG Orthology (KO) workflows. The core normalizes source
evidence, retrieves bounded KEGG references, evaluates MODULE logic, summarizes pathway KO
coverage, compares KO sets deterministically, and produces traceable local reports.

The release-supported platform is Linux with CPython 3.11.x.

## What it does

The core server accepts plain K-number lists and generic CSV/TSV annotation tables. It can:

- preserve source decisions, scores, thresholds, multiple assignments, and provenance;
- derive strict accepted-only and lenient accepted-plus-uncertain KO views;
- retrieve typed `INFO`, `GET`, `LINK`, and `CONV` references through a local cache and a
  deployment-wide no-burst rate limit shared with the renderer;
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

MCP registration does not install the repository Skills. When Codex runs at this checkout root,
its `.agents/skills` is already on the discovery path; do not run the managed-copy installer back
into the same checkout. When the reviewed checkout is nested below or separate from the workspace
where Codex will run, copy the three version-matched Skills into that distinct workspace:

```bash
python3 scripts/install-skills.py --workspace /absolute/path/to/workspace
```

This managed copy is required when the checkout is nested below the workspace, for example under
`.mcp/kegg_mcp`. Codex detects newly installed Skills automatically; restart only if the three names
do not appear in the client's discovered Skill list or selector. See the installation guide for
tag-archive content binding, wheel version guards, and separate MCP and Skill discovery checks.

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
`doctor [--json]`, retained-result `cleanup --expired [--json]`, and KEGG-cache
`cache status [--json]` or `cache cleanup --expired [--json]` commands.

Core tools:

- `analyze_ko_annotations`, `normalize_ko_annotations`
- `get_kegg_entries`, `map_ko_ids`
- `analyze_modules`, `analyze_pathways`, `compare_ko_sets`
- `probe_kegg_connectivity`, `get_server_status`
- `list_analysis_results`, `delete_analysis_result`

Fixed resources expose redacted server and cache status. Validated resource templates expose
scope-isolated result metadata, sections, byte ranges, and cache-only KEGG entry reads. Result IDs
belong to one stdio process; an output bundle is the durable cross-process artifact.

The repository-scoped Skills are:

- `.agents/skills/deepkoala-annotation/` for controlled local protein-FASTA annotation;
- `.agents/skills/kegg-ko-analysis/` for existing KO evidence, MODULE/pathway analysis, and
  comparisons; and
- `.agents/skills/kegg-pathway-rendering/` for static renderer orchestration from an authoritative
  core handoff.

Each Skill declares exactly one MCP dependency. Stable output-directory files connect stages; no
Skill owns the complete annotation-to-rendering workflow. When one original user request spans
annotation, KEGG analysis, and rendering, Codex automatically continues across the matching focused
Skills and passes the stable files forward without asking the user to copy paths into new prompts.

For a protein FASTA-to-image workflow, make one request that names separate private annotation,
analysis, and rendering output directories. `deepkoala-annotation` produces
`deepkoala_annotations.csv` and its source provenance, `kegg-ko-analysis` consumes that stable file
and produces `render_input.json`, and `kegg-pathway-rendering` consumes the unchanged renderer
handoff when graphics were requested. A request that asks for only one stage stops after that stage.

The default handoff is the named file in each output directory. Opaque job and result identifiers
are useful only while the originating stdio process remains active.

## KEGG access and local-data boundary

The public KEGG REST service is for academic use by academic users. This project enforces at most
three requests per second across local Core and Renderer processes sharing the configured
rate-limit root, and uses a safer no-burst default. Non-academic deployments must use an
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
optional companion or any repository-scoped Skill. Obtain a version-matched tag source archive or
exact GitHub checkout and run `scripts/install-skills.py` when the Skills are required; installing
the wheel alone does not make the Skills available.

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
