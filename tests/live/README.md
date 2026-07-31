# Opt-in KEGG live compatibility campaign

Pull-request CI makes 120 serialized requests: 20 each for `INFO`, organism-pathway `LIST`, `FIND`,
`GET`, `LINK`, and `CONV`.
Local pytest skips this suite unless the operator explicitly enables it.
Within each 20-request operation budget, the campaign rotates a fixed small matrix rather than
repeating one request:

- `INFO`: KO, compound, genome, BRITE, glycan, reaction class, and drug;
- organism-pathway `LIST`: `hsa` and `eco`;
- `FIND`: KO, compound, glycan, and drug keywords; compound formula, exact-mass, and
  molecular-weight modes; and drug exact-mass mode;
- `GET`: one BRITE hierarchy plus representative KO, MODULE, pathway, reaction, enzyme, compound,
  glycan, gene, genome, drug, and reaction-class flat files; supported flat-file shapes also pass
  through the deterministic typed-card parser;
- `LINK`: the established KO, BRITE, gene, compound, and taxonomy cases plus organism-scoped
  KO/pathway-to-gene; module-to-KO/reaction/pathway; pathway-to-module/glycan;
  reaction-to-glycan; glycan-to-reaction/pathway; and drug-to-pathway directions; and
- `CONV`: the established selected gene directions plus ChEBI/PubChem SID-to-compound and
  drug-to-PubChem SID directions.

Assertions cover response structure and request namespaces without freezing names, release labels,
row counts, or exact relationship sets. Injected-transport tests cover broader parameter and
failure boundaries.

The client is limited to one request per second without burst and zero retries. A transport wrapper
enforces the configured wire budget and opens its circuit after any transport or non-200 response.
Every call bypasses the temporary cache, and the workflow stops after the first failed test. The
temporary SQLite cache is deleted after the session. Do not use pytest-xdist or upload KEGG
responses, cache files, or generated artifacts.

Run the local non-live suite:

```bash
uv run --frozen pytest
```

Run the live campaign after confirming eligible access:

```bash
KEGG_MCP_RUN_LIVE_TESTS=true uv run --frozen pytest tests/live
```

The enabled default is 20 requests per operation. Set
`KEGG_MCP_LIVE_REQUESTS_PER_OPERATION` to an integer from 1 through 20 for an explicitly
authorized smaller manual run.

An authorized licensed endpoint may be used with the documented `licensed` access variables.
The campaign, FIND behavior, compact BRITE htext shape, typed-card source shapes, and selected
relation directions use official KEGG API behavior reviewed on 2026-07-30. Unsupported selected
reaction-class relations and inconsistent `rmodule` selected endpoints remain outside the
allowlist. Repeat the campaign against the exact merged release commit. The governing primary
sources were retrieved on 2026-07-30:
[KEGG API page](https://www.kegg.jp/kegg/rest/) and
[KEGG API manual](https://www.kegg.jp/kegg/rest/keggapi.html).

GitHub Actions runs the 120-request campaign for pull requests and manual workflow dispatches. A
merge to `main` does not repeat the same validation. The workflow does not upload KEGG responses
or cache files.
