# Synthetic examples

These examples are independently authored, redistributable inputs and configuration templates.
They contain no KEGG response body, cache entry, pathway image, model weight, KOfam profile,
protein sequence, secret, or private biological data.

## Contents

- `plain-ko/ko-list.txt` demonstrates prefix normalization, duplicate reporting, and one invalid
  row without asserting biological meaning.
- `plain-ko/clean-ko-list.txt` is a minimal syntactically valid input for an analysis request.
- `config/` contains explicit public-academic and licensed environment templates plus the strict
  placeholder-only `kegg-mcp-suite.toml` template for the unified Codex installer.
  Manual Core deployments default to network-disabled `offline_cache`; public-academic access
  always requires the explicit mode and confirmation shown in its template.

The K numbers are syntax examples. They are annotations supplied for analysis, not experimental
validation, and the files do not claim that the identifiers belong to one real organism or
sample.

See `docs/installation.md` for installation, MCP client configuration, tool calls, result
retrieval, and cache behavior.
