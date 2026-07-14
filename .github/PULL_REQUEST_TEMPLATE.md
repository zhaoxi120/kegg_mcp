## Summary

Describe the focused change and the affected architectural layer or public contract.

## Validation

- [ ] `uv run ruff check .`
- [ ] `uv run ruff format --check .`
- [ ] `uv run pyright`
- [ ] `uv run pytest`

## Review checklist

- [ ] The change stays within the assigned issue and MVP scope.
- [ ] Tests use synthetic or independently authored data and do not call live KEGG services.
- [ ] Provenance, ambiguity, and multiple KO assignments are preserved where relevant.
- [ ] Biological claims are no stronger than the evidence.
- [ ] KEGG licensing, rate limits, and cache boundaries remain enforced where relevant.
- [ ] Public contracts and documentation were updated together.
- [ ] All tracked content is in English.
