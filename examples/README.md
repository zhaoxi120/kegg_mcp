# Synthetic examples

These examples are independently authored, redistributable inputs and configuration templates.
They contain no KEGG response body, cache entry, pathway image, model weight, KOfam profile,
protein sequence, secret, or private biological data.

## Contents

- `plain-ko/ko-list.txt` demonstrates prefix normalization, duplicate reporting, and one invalid
  row without asserting biological meaning.
- `plain-ko/clean-ko-list.txt` is a minimal syntactically valid input for an analysis request.
- `config/` contains access-mode environment templates. Copy only the template matching the
  rights and network mode you are authorized to use.

The K numbers are syntax examples. They are annotations supplied for analysis, not experimental
validation, and the files do not claim that the identifiers belong to one real organism or
sample.

See `docs/installation.md` for installation, MCP client configuration, tool calls, result
retrieval, and offline behavior.
