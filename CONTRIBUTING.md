# Contributing

Thank you for contributing to KEGG MCP. The MVP is implemented; keep each subsequent change
focused on one layer or one public contract.

## Development setup

Install Python 3.11.x and [uv](https://docs.astral.sh/uv/), then create the project environment.
Version 0.1.x does not claim support for Python 3.12 or later:

```bash
uv sync
```

Run the local validation suite before requesting review:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

The optional DeepKOALA companion is an independent distribution. Validate it from its own project
directory or with an explicit project selector; do not add PyTorch or DeepKOALA to either locked
development environment:

```bash
cd companions/deepkoala-mcp
uv sync --frozen
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pyright
uv run --frozen pytest
```

Default companion tests must remain offline and must use a synthetic runner. A real DeepKOALA
compatibility check is a separately authorized manual test with existing local code and resources,
CPU-only settings, few threads, minimal input, and no automatic download.

## Contribution rules

- Read `AGENTS.md` and the relevant sections of `docs/development-plan.md` before editing.
- Use English for all tracked content, issue text, pull requests, and commit messages.
- Keep local Simplified Chinese reference documents under the ignored `*.zh-CN.md` convention.
  Do not link, stage, package, release, or upload them to GitHub; the English counterpart remains
  normative.
- Preserve raw annotation evidence, provenance, ambiguity, and multiple KO assignments.
- Do not add live KEGG requests to the default test suite or pull-request CI. Keep them in the
  dedicated, explicitly enabled, serialized live job with eligible access confirmation and
  no uploaded KEGG payloads.
- Use synthetic or independently authored fixtures. Do not commit KEGG payload collections, KOfam profiles, model weights, secrets, or large biological inputs.
- Do not execute DeepKOALA or another external annotation tool from the core `kegg-mcp` server.
  The existing candidate runner must remain a separately installed companion MCP service. The
  Skill may orchestrate that service but must not implement inference, process control, weight
  management, normalization, or cross-server resource access.
- Keep the companion's two-phase prepare, complete-notice display, exact-digest acknowledgement,
  fixed one-job concurrency, `multi=false`, no-download behavior, allowed-root enforcement, and
  source-agnostic core-import handoff intact unless an assigned contract change updates tests and
  security review at the same time.
- Update documentation and tests whenever a public contract or biological interpretation rule changes.

## Pull requests

Describe the affected layer, the contract or behavior being changed, the validation commands run, and any data-rights or biological-interpretation implications. A pull request should not claim completion until its documented acceptance criteria pass.
