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

## Contribution rules

- Read `AGENTS.md` and the relevant sections of `docs/development-plan.md` before editing.
- Use English for all tracked content, issue text, pull requests, and commit messages.
- Preserve raw annotation evidence, provenance, ambiguity, and multiple KO assignments.
- Do not add live KEGG requests to the default test suite or CI.
- Use synthetic or independently authored fixtures. Do not commit KEGG payload collections, KOfam profiles, model weights, secrets, or large biological inputs.
- Do not execute DeepKOALA or another external annotation tool from this project.
- Update documentation and tests whenever a public contract or biological interpretation rule changes.

## Pull requests

Describe the affected layer, the contract or behavior being changed, the validation commands run, and any data-rights or biological-interpretation implications. A pull request should not claim completion until its documented acceptance criteria pass.
