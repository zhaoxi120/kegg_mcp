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

For Codex, the primary deployment path is the repository suite installer. Start from a reviewed
release checkout and prepare the strict owner-only deployment TOML
inside a direct owner-only parent directory as described in
[Installation and operation](docs/installation.md). Then run the installer with
absolute tool and destination paths:

```bash
chmod 700 /absolute/private
chmod 600 /absolute/private/kegg-mcp-deployment.toml
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

The suite installer keeps three independent locked Python runtimes and three independent stdio
processes. It generates one local Codex plugin containing the three version-matched Skills and
three absolute MCP launch registrations. It does not merge the distributions or let one server
start another.

Dependency resolution is offline by default. `--allow-locked-dependency-downloads` authorizes `uv`
network access only while resolving or downloading artifacts required by the three checked-in
lockfiles and their declared build requirements. For each new suite installation root, the user
confirms once before `--allow-deepkoala-install` authorizes cloning the official DeepKOALA repository
and installing its upstream requirements into a private managed environment. The same installed
deployment does not ask again for later FASTA jobs. The bundled `202502` model is the default. The
installer does not install Python, uv, Codex, HMMER, KOfam profiles, KEGG data, or later DeepKOALA
model versions.
This repository intentionally provides no model updater. If a user later requests a newer official
model, an LLM or operator may install it separately after explicit confirmation and then select that
installed model date; neither the Skill nor the serving MCP performs that update.
After a successful install, start a new Codex task so the generated plugin Skills and MCP servers
are loaded together; use a workspace outside this source checkout for an unambiguous plugin smoke
check. This generated-plugin path targets only the Codex app and Codex CLI; release support remains
gated on the exact acceptance evidence in the release-readiness checklist. Other MCP clients use
the documented manual configuration.

Select the KEGG access profile explicitly in the private deployment TOML. During first setup, ask
the user whether both the user and the work qualify for public academic KEGG access before setting
`public_academic` and `academic_use_confirmed=true`.
The raw Core server retains confirmed `public_academic` as its manual unconfigured default; the
suite does not infer that choice. A useful first request is:

> Check server status, probe connectivity once, then retrieve only KO entry `K00844`. Report its
> identifier, name, database release when available, and whether it came from network or cache.

See [Installation and operation](docs/installation.md) for licensed endpoints, environment
variables, client configuration, and result retrieval. Local tests do not make live KEGG requests
unless explicitly enabled.

## Components

The optional end-to-end FASTA-to-image workflow uses three independent local processes:

| Process | Responsibility |
| --- | --- |
| `deepkoala-mcp` | Runs the suite-managed or manually configured DeepKOALA installation and returns a controlled detailed-CSV handoff. |
| `kegg-mcp` | Imports evidence, applies the named normalization policy, retrieves references, and performs authoritative analysis. |
| `kegg-render-mcp` | Renders the core handoff as bounded static SVG or PNG without recomputing analysis. |

The companions remain separately packaged distributions with isolated runtimes even when the
suite installer provisions them in one operation. The core never runs annotation software or
generates pathway images. The renderer accepts `render_input.json` schema version 3, represented
in Python as `RenderInput`, and preserves `AnalysisExecutionProvenance` version 3 inside output
bundle schema version 3. Source KEGG PNG and KGML assets remain local and are not included in
tests, packages, or releases.

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
handoff when graphics were requested. The default model date is `202502`, `device` remains `auto`,
multi-domain mode remains disabled, and the final response states the resolved model version. When
no pathway or MODULE target and no explicit selection are supplied, the analysis independently
selects the top five MODULEs and top five canonical KO reference pathways by unique selected-KO
overlap. MODULE ranking selects targets; it is not completion or enrichment, and completion is
calculated separately. A request that asks for only one stage stops after that stage.

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
optional companion or any repository-scoped Skill. The repository-level `scripts/install-suite.py`
installer consumes all three checked-in lock files, creates independent runtimes, copies the three
canonical Skill trees into a generated local plugin, and registers that plugin through Codex. The
generated plugin is a local deployment artifact, not a Python wheel or a tracked second copy of the
Skills.

The suite installer is the only supported Codex installation path. Installing a wheel alone does
not make repository-scoped Skills available. Other MCP clients may configure the independently
installed stdio servers manually, but that component-level setup does not install Codex Skills.

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
