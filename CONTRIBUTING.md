# Contributing

Thank you for contributing to KEGG MCP. The MVP is implemented; keep each subsequent change
focused on one layer or one public contract.

## Development setup

Install Python 3.11.x and [uv](https://docs.astral.sh/uv/), then create the project environment.
Version 0.2.x does not claim support for Python 3.12 or later:

```bash
uv sync
```

The local validation suite is available for focused checks, but it is not required before every
commit or before requesting review. Pull-request CI is the authoritative automated gate:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```

## Contribution rules

- Read `AGENTS.md` and the relevant sections of `docs/development-plan.md` before editing.
- Use English for all tracked content, issue text, pull requests, and commit messages.
- Preserve raw annotation evidence, provenance, ambiguity, and multiple KO assignments.
- Keep local live KEGG tests opt-in. Pull-request CI may run the reviewed serialized compatibility
  campaign described in `tests/live/README.md`; it must not upload responses or cache files.
- Use synthetic or independently authored fixtures. Do not commit KEGG payload collections, KOfam profiles, model weights, secrets, or large biological inputs.
- Do not execute DeepKOALA or another external annotation tool from the core package or core
  server. Changes to the separately installed companion must preserve its independent entry point,
  CPU-only bounds, fixed subprocess interface, and no-download policy.
- Update documentation and tests whenever a public contract or biological interpretation rule changes.

## Pull requests

Describe the affected layer, the contract or behavior being changed, any optional local checks
run, and any data-rights or biological-interpretation implications. A pull request should not
claim completion until its GitHub checks and documented acceptance criteria pass.

GitHub Actions runs the companion's independent offline suite when applicable. The equivalent
optional local commands are:

```bash
cd companions/deepkoala-mcp
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest
```
